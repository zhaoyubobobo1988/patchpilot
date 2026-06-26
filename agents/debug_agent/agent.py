from __future__ import annotations

from config.llm import llm_complete
from config.logging import get_logger
from models.context import AgentContext
from models.github import DebugContext
from models.patch import PatchResult, PatchStatus

logger = get_logger(__name__)

_DEBUG_SYSTEM_PROMPT = """You are the Debug Agent in the OpenClaw system. A CI run has failed after applying a patch. Your job is to analyze the CI failure and produce a corrected unified diff patch.

Output ONLY a unified diff patch that fixes the CI failure. No explanations, no prose."""


class DebugAgent:
    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    async def fix(self, debug_ctx: DebugContext) -> PatchResult:
        logger.info(
            f"Debug attempt #{debug_ctx.retry_attempt} for subtask {debug_ctx.subtask_id}"
        )
        prompt = self._build_prompt(debug_ctx)
        try:
            fixed_patch = (await llm_complete(
                model=self.ctx.model,
                max_tokens=self.ctx.max_tokens,
                messages=[
                    {"role": "system", "content": _DEBUG_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )).strip()
            return PatchResult(
                subtask_id=debug_ctx.subtask_id,
                worker_id=f"debug-agent-attempt-{debug_ctx.retry_attempt}",
                patch_content=fixed_patch,
                affected_files=debug_ctx.subtask_files,
                status=PatchStatus.SUCCESS,
                retry_count=debug_ctx.retry_attempt,
            )
        except Exception as exc:
            logger.error(f"Debug agent failed: {exc}")
            return PatchResult(
                subtask_id=debug_ctx.subtask_id,
                worker_id=f"debug-agent-attempt-{debug_ctx.retry_attempt}",
                patch_content="",
                affected_files=[],
                status=PatchStatus.FAILED,
                error_message=str(exc),
                retry_count=debug_ctx.retry_attempt,
            )

    def _build_prompt(self, debug_ctx: DebugContext) -> str:
        return (
            f"## Original Patch (attempt #{debug_ctx.retry_attempt})\n\n"
            f"```diff\n{debug_ctx.original_patch}\n```\n\n"
            f"## CI Failure Log\n\n```\n{debug_ctx.ci_log}\n```\n\n"
            f"## Failed Checks\n{', '.join(debug_ctx.failed_checks)}\n\n"
            f"## Original Goal\n{debug_ctx.subtask_goal}\n\n"
            f"Produce a corrected unified diff that fixes the CI failures."
        )
