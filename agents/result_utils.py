"""
Tiny helpers for consistent AgentResult handling across Planner / Reviewer.

Keeps the success-check + structured-log pattern in one place instead of
duplicated in each caller.
"""
from __future__ import annotations

from agents.base import AgentResult
from config.logging import get_logger

logger = get_logger(__name__)


def extract_agent_output(result: AgentResult, caller: str) -> str | None:
    """
    Return result.output when the agent succeeded, None otherwise.

    On success: logs an info line with task_id and elapsed_seconds from metadata.
    On failure: logs a warning with exit_code and error — so callers can fall
                through to their litellm fallback without duplicating that logic.
    """
    meta = result.metadata
    agent_name = meta.get("agent", "agent")
    if result.success and result.output:
        logger.info(
            f"[{caller}] {agent_name} ok  "
            f"task_id={meta.get('task_id')}  "
            f"elapsed={meta.get('elapsed_seconds')}s"
        )
        return result.output

    logger.warning(
        f"[{caller}] {agent_name} failed  "
        f"task_id={meta.get('task_id')}  "
        f"exit={result.exit_code}  "
        f"error={result.error!r}"
    )
    return None
