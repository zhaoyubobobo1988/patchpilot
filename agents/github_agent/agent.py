from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import httpx

from config.logging import get_logger
from config.settings import settings
from models.context import AgentContext
from models.github import CICheckResult, CIStatus, PRRequest, PRResult
from models.patch import MergedPatch
from models.task import FeatureTask

logger = get_logger(__name__)

_GH_API = "https://api.github.com"


class GitHubAgent:
    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    # ── 公开接口 ──────────────────────────────────────────────────────────────

    async def apply_and_push(self, merged_patch: MergedPatch, task: FeatureTask) -> PRRequest:
        branch = f"openclaw/{task.feature_name}/{self.ctx.run_id[:6]}"
        workspace = Path(self.ctx.workspace_path)

        logger.info(f"Applying patch to branch '{branch}' in {workspace}")

        if not merged_patch.merged_diff.strip():
            raise ValueError("Merged patch is empty — all worker patches failed")

        self._git(workspace, ["checkout", "-b", branch])

        if settings.ANTHROPIC_BASE_URL:
            # subprocess 模式：Worker 已直接在各自 workspace 编辑文件；
            # 将每个 Worker workspace 的变更文件复制回主 workspace 再 commit
            self._collect_worker_changes(workspace, task)
        else:
            # litellm 模式：使用 merged diff 文本
            patch_file = workspace / f"{self.ctx.run_id}.patch"
            patch_file.write_text(merged_patch.merged_diff, encoding="utf-8")
            try:
                self._git(workspace, ["apply", "--check", str(patch_file)])
                self._git(workspace, ["apply", str(patch_file)])
            finally:
                patch_file.unlink(missing_ok=True)

        self._git(workspace, ["add", "-A"])
        # 检查是否有实际变更
        status = self._git(workspace, ["status", "--porcelain"])
        if not status.strip():
            raise ValueError("No changes to commit after applying patches")
        self._git(workspace, ["commit", "-m",
            f"feat({task.feature_name}): {task.raw_requirement[:72]}"])
        remote_url = (
            f"https://oauth2:{settings.GITHUB_TOKEN}"
            f"@github.com/{task.repository}.git"
        )
        self._git(workspace, ["push", remote_url, branch])

        return PRRequest(
            repository=task.repository,
            title=f"[OpenClaw] feat({task.feature_name}): {task.raw_requirement[:60]}",
            body=self._build_pr_body(task, merged_patch),
            head_branch=branch,
            base_branch=task.base_branch,
            draft=True,
        )

    async def create_pr(self, pr_request: PRRequest) -> PRResult:
        owner, repo = pr_request.repository.split("/", 1)
        logger.info(f"Creating PR: {owner}/{repo}  {pr_request.head_branch} → {pr_request.base_branch}")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_GH_API}/repos/{owner}/{repo}/pulls",
                headers=self._headers(),
                json={
                    "title": pr_request.title,
                    "body": pr_request.body,
                    "head": pr_request.head_branch,
                    "base": pr_request.base_branch,
                    "draft": pr_request.draft,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        logger.info(f"PR created: {data['html_url']}")
        return PRResult(
            pr_number=data["number"],
            pr_url=data["html_url"],
            head_branch=data["head"]["ref"],
            state=data.get("state", "open"),
        )

    async def poll_ci(
        self, pr_result: PRResult, timeout_seconds: int | None = None
    ) -> CICheckResult:
        owner, repo = self.ctx.repository.split("/", 1)
        timeout = timeout_seconds or settings.CI_POLL_TIMEOUT_SECONDS
        interval = settings.CI_POLL_INTERVAL_SECONDS
        elapsed = 0

        logger.info(f"Polling CI for PR #{pr_result.pr_number} (timeout={timeout}s)")

        while elapsed < timeout:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{_GH_API}/repos/{owner}/{repo}/commits/{pr_result.head_branch}/check-runs",
                    headers=self._headers(),
                )
                data = resp.json()

            total = data.get("total_count", 0)
            if total == 0:
                # 仓库没有配置 CI，视为成功
                logger.info(f"PR #{pr_result.pr_number}: no CI checks configured, treating as success")
                return CICheckResult(
                    pr_number=pr_result.pr_number,
                    status=CIStatus.SUCCESS,
                )

            runs = data.get("check_runs", [])
            all_done = all(r["status"] == "completed" for r in runs)
            if all_done:
                failed = [
                    r["name"] for r in runs
                    if r.get("conclusion") not in ("success", "skipped", "neutral")
                ]
                status = CIStatus.FAILURE if failed else CIStatus.SUCCESS
                return CICheckResult(
                    pr_number=pr_result.pr_number,
                    status=status,
                    failed_checks=failed,
                )

            await asyncio.sleep(interval)
            elapsed += interval

        logger.warning(f"CI poll timed out after {timeout}s for PR #{pr_result.pr_number}")
        return CICheckResult(
            pr_number=pr_result.pr_number,
            status=CIStatus.FAILURE,
            failed_checks=["timeout"],
        )

    async def close_pr(self, pr_number: int) -> None:
        owner, repo = self.ctx.repository.split("/", 1)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                f"{_GH_API}/repos/{owner}/{repo}/pulls/{pr_number}",
                headers=self._headers(),
                json={"state": "closed"},
            )
            resp.raise_for_status()
        logger.info(f"Closed PR #{pr_number}")

    # ── 内部工具 ──────────────────────────────────────────────────────────────

    def _collect_worker_changes(self, main_workspace: Path, task: FeatureTask) -> None:
        """将各 Worker 独立 workspace 的变更文件复制回主 workspace。"""
        import shutil
        parent = main_workspace.parent
        copied = 0
        for ws_dir in parent.iterdir():
            # Worker workspace 命名规则：{run_id}-{worker_id}
            name = ws_dir.name
            run_id = main_workspace.name
            if not name.startswith(run_id + "-") or not ws_dir.is_dir():
                continue
            # 枚举该 Worker workspace 中有变更的文件
            result = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=ws_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            for rel_path in result.stdout.strip().splitlines():
                src = ws_dir / rel_path
                dst = main_workspace / rel_path
                if src.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    copied += 1
        logger.info(f"Collected {copied} changed files from worker workspaces")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _git(self, cwd: Path, args: list[str]) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return result.stdout.strip()

    def _build_pr_body(self, task: FeatureTask, merged_patch: MergedPatch) -> str:
        subtask_lines = "\n".join(f"- [{st.id}] {st.goal}" for st in task.subtasks)
        return (
            f"## OpenClaw Auto-generated PR\n\n"
            f"**Requirement:** {task.raw_requirement}\n\n"
            f"**Subtasks ({len(task.subtasks)}):**\n{subtask_lines}\n\n"
            f"**Patches merged:** {len(merged_patch.source_patch_ids)}, "
            f"conflicts resolved: {merged_patch.conflicts_resolved}\n\n"
            f"_Generated by OpenClaw run `{self.ctx.run_id}`_"
        )
