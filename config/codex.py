"""
Backward-compatible shim: delegates to agents.codex.CodexAgent.

The real subprocess logic now lives in CodexAgent so there is only one
implementation of the `codex exec` call.  This module is kept only so that
any legacy import of `codex_exec` continues to work without change.
"""
from __future__ import annotations

from pathlib import Path

from agents.base import AgentTask
from agents.codex import CodexAgent
from config.logging import get_logger
from config.settings import settings

logger = get_logger(__name__)

_agent = CodexAgent()


async def codex_exec(prompt: str, cwd: str | None = None) -> str:
    """
    Run codex exec and return the agent's final text.
    Raises RuntimeError if the subprocess fails or produces no output.
    """
    task = AgentTask(
        task_id="shim",
        role="reviewer",
        prompt=prompt,
        workspace=Path(cwd or "."),
        timeout_seconds=settings.CODEX_TIMEOUT or 180,
        output_format="json",
    )
    result = await _agent.run(task)
    if not result.success:
        raise RuntimeError(result.error or "codex exec failed")
    if not result.output:
        raise ValueError("codex exec produced empty output")
    return result.output
