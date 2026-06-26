"""
Backward-compatible shim: delegates to agents.claude_code.ClaudeCodeAgent.

Kept so that existing import paths (orchestrator, review_agent) continue to
work without change.  New code should import ClaudeCodeAgent directly.
"""
from __future__ import annotations

from pathlib import Path

from agents.base import AgentTask
from agents.claude_code import ClaudeCodeAgent
from config.logging import get_logger
from config.settings import settings

logger = get_logger(__name__)

_agent = ClaudeCodeAgent()


async def claude_text_exec(prompt: str, cwd: str, timeout: int | None = None) -> str:
    """
    Run claude CLI in json-output mode and return the agent's final text.
    Raises RuntimeError if the subprocess fails or produces no output.
    """
    task = AgentTask(
        task_id="shim",
        role="planner",           # generic; overridden by actual callers
        prompt=prompt,
        workspace=Path(cwd),
        timeout_seconds=timeout or (settings.CLAUDE_CODE_TIMEOUT or 300),
        output_format="json",
    )
    result = await _agent.run(task)
    if not result.success:
        raise RuntimeError(result.error or "claude agent subprocess failed")
    if not result.output:
        raise ValueError("claude agent subprocess produced empty output")
    return result.output
