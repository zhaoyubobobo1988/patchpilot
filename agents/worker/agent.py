from __future__ import annotations

import re
from pathlib import Path

from agents.base import AgentTask
from agents.claude_code import ClaudeCodeAgent
from agents.worker.workspace import WorkerWorkspaceManager
from config.llm import llm_complete
from config.logging import get_logger
from config.settings import settings
from libs.permissions import PermissionChecker
from models.patch import PatchResult, PatchStatus
from models.task import SubTask
from models.context import AgentContext
from .prompts import WORKER_SYSTEM_PROMPT, build_worker_prompt

_claude_agent = ClaudeCodeAgent()

logger = get_logger(__name__)

# Prompt 后缀：指示 Claude Code 完成任务后输出 git diff
_GIT_DIFF_SUFFIX = """

---
After completing the above task:
1. Run `git diff HEAD` in the terminal
2. Print the COMPLETE output of that command as your final response
3. Do NOT print anything else — only the raw unified diff output
"""


class ClaudeCodeWorker:
    def __init__(
        self,
        worker_id: str,
        ctx: AgentContext,
        workspace_manager: WorkerWorkspaceManager | None = None,
    ) -> None:
        self.worker_id = worker_id
        self.ctx = ctx
        # Allow injection for testing; default to the standard clone-based manager.
        self.workspace_manager = workspace_manager or WorkerWorkspaceManager()

    async def execute(self, subtask: SubTask) -> PatchResult:
        logger.info(f"[{self.worker_id}] executing subtask {subtask.id}: {subtask.goal}")
        try:
            if settings.ANTHROPIC_BASE_URL:
                diff = await self._run_claude_code(subtask)
            else:
                diff = await self._run_llm(subtask)

            if not self._validate_diff(diff, subtask):
                raise ValueError("Generated diff modifies files outside feature scope")
            return PatchResult(
                subtask_id=subtask.id,
                worker_id=self.worker_id,
                patch_content=diff,
                affected_files=self._extract_affected_files(diff),
                status=PatchStatus.SUCCESS,
            )
        except Exception as exc:
            logger.error(f"[{self.worker_id}] subtask {subtask.id} failed: {exc}")
            return PatchResult(
                subtask_id=subtask.id,
                worker_id=self.worker_id,
                patch_content="",
                affected_files=[],
                status=PatchStatus.FAILED,
                error_message=str(exc),
            )

    # ── subprocess 模式（Claude Code + GLM）────────────────────────────────────

    async def _run_claude_code(self, subtask: SubTask) -> str:
        """
        Prepare isolated workspace → run ClaudeCodeAgent → extract patch via git diff.
        The claude CLI edits files directly; stdout is unreliable, so the patch
        is taken from `git diff --cached` after the subprocess completes.
        """
        worker_workspace = await self.workspace_manager.prepare(
            self.ctx.run_id,
            self.worker_id,
            self.ctx.workspace_path,
        )
        prompt = build_worker_prompt(
            feature=subtask.feature,
            goal=subtask.goal,
            files=subtask.files,
            constraints=subtask.constraints,
        ) + _GIT_DIFF_SUFFIX

        task = AgentTask(
            task_id=f"{self.ctx.run_id}-{self.worker_id}-{subtask.id}",
            role="worker",
            prompt=prompt,
            workspace=Path(worker_workspace),
            timeout_seconds=settings.CLAUDE_CODE_TIMEOUT if settings.CLAUDE_CODE_TIMEOUT > 0 else 300,
            output_format="text",   # Worker: we rely on git diff, not stdout
        )
        result = await _claude_agent.run(task)

        if not result.success:
            logger.error(
                f"[{self.worker_id}] ClaudeCodeAgent failed  "
                f"task_id={result.metadata.get('task_id')}  "
                f"elapsed={result.metadata.get('elapsed_seconds')}s  "
                f"error={result.error!r}"
            )
            raise RuntimeError(result.error or f"claude CLI failed (exit {result.exit_code})")

        # Primary: git diff captures actual file edits made by the claude subprocess
        git_diff = self.workspace_manager.git_diff(worker_workspace)
        if git_diff:
            return git_diff
        # Fallback: attempt to parse a diff from whatever stdout contained
        return self._extract_diff(result.output)

    # ── litellm 模式（直接 API 调用）──────────────────────────────────────────

    async def _run_llm(self, subtask: SubTask) -> str:
        prompt = build_worker_prompt(
            feature=subtask.feature,
            goal=subtask.goal,
            files=subtask.files,
            constraints=subtask.constraints,
        )
        raw = await llm_complete(
            model=self.ctx.model,
            max_tokens=self.ctx.max_tokens,
            messages=[
                {"role": "system", "content": WORKER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        return self._extract_diff(raw)

    # ── 工具方法 ──────────────────────────────────────────────────────────────

    def _extract_diff(self, response: str) -> str:
        match = re.search(r"```diff\s*\n(.*?)```", response, re.DOTALL)
        if match:
            return match.group(1).strip()
        if response.strip().startswith("diff --git"):
            return response.strip()
        raise ValueError(f"No valid unified diff found in response: {response[:200]}")

    def _validate_diff(self, diff: str, subtask: SubTask) -> bool:
        # PR10: delegate to centralized PermissionChecker
        # Only pass subtask.files as allowed_files when it's a non-empty list
        # (backward compat — MagicMock or empty list treated as no restriction)
        raw_files = getattr(subtask, "files", None)
        allowed = raw_files if (isinstance(raw_files, list) and raw_files) else None
        is_valid, violations = PermissionChecker.validate_diff(diff, allowed_files=allowed)
        if violations:
            logger.warning(
                f"[{self.worker_id}] diff touches out-of-scope file(s): "
                f"{', '.join(violations)}"
            )
        return is_valid

    def _extract_affected_files(self, diff: str) -> list[str]:
        # PR10: delegate to centralized PermissionChecker (includes normalization)
        return PermissionChecker.extract_affected_files(diff)
