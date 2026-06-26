from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path

import httpx

from config.logging import get_logger
from config.settings import settings
from models.context import AgentContext
from models.github import CICheckResult, CIStatus, PRRequest, PRResult
from models.patch import MergedPatch
from models.task import FeatureTask

logger = get_logger(__name__)


class MergeConflictError(RuntimeError):
    """Raised when Worker patches conflict on the same hunk and cannot be auto-resolved."""

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
        """Apply Worker patches to *main_workspace* via git apply.

        Each Worker workspace's staged diff is extracted and applied with
        ``git apply --3way`` so that non-overlapping edits to the same file
        merge cleanly.  Overlapping hunks that cannot be resolved automatically
        raise MergeConflictError instead of silently overwriting one worker's
        changes with another's.
        """
        parent = main_workspace.parent
        run_id = main_workspace.name
        applied = 0
        conflict_details: list[str] = []

        for ws_dir in sorted(parent.iterdir()):
            if not ws_dir.is_dir() or not ws_dir.name.startswith(run_id + "-"):
                continue

            # Stage any edits the Worker left uncommitted
            subprocess.run(
                ["git", "add", "-A"],
                cwd=ws_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            diff_proc = subprocess.run(
                ["git", "diff", "--cached"],
                cwd=ws_dir,
                capture_output=True,  # raw bytes — avoid any CRLF translation
            )
            patch_bytes = diff_proc.stdout
            if not patch_bytes.strip():
                logger.info(f"Worker {ws_dir.name}: no staged changes, skipping")
                continue
            # Ensure patch ends with a newline — git apply rejects truncated patches
            if not patch_bytes.endswith(b"\n"):
                patch_bytes += b"\n"

            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=".patch", delete=False
            ) as f:
                f.write(patch_bytes)
                patch_path = Path(f.name)

            try:
                # Attempt a clean apply first (no context fuzz needed)
                check = subprocess.run(
                    ["git", "apply", "--check", str(patch_path)],
                    cwd=main_workspace,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if check.returncode == 0:
                    subprocess.run(
                        ["git", "apply", str(patch_path)],
                        cwd=main_workspace,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        check=True,
                    )
                    applied += 1
                    logger.info(f"Applied patch from {ws_dir.name} (clean)")
                else:
                    # Context shifted — try 3-way merge
                    apply3 = subprocess.run(
                        ["git", "apply", "--3way", str(patch_path)],
                        cwd=main_workspace,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    if apply3.returncode == 0:
                        applied += 1
                        logger.info(f"Applied patch from {ws_dir.name} (3-way merge)")
                    else:
                        detail = (apply3.stderr or apply3.stdout or "").strip()[:300]
                        conflict_details.append(f"{ws_dir.name}: {detail}")
                        logger.error(
                            f"Merge conflict in {ws_dir.name}: {detail}"
                        )
            finally:
                patch_path.unlink(missing_ok=True)

        if conflict_details:
            raise MergeConflictError(
                f"{len(conflict_details)} worker patch(es) could not be applied: "
                + " | ".join(conflict_details)
            )

        logger.info(f"Applied patches from {applied} worker workspace(s)")

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
