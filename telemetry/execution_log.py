"""
telemetry/execution_log.py — lightweight append-only execution record writer.

Writes one JSON line per event to settings.EXECUTION_LOG_PATH.
Empty path → no-op (default, safe for all existing tests).
Write failures → warning only, never raises.

Fields never written:
  - prompt text
  - API keys / tokens
  - full stdout / stderr (truncated to _MAX_ERROR_LEN)
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from config.logging import get_logger
from config.settings import settings

if TYPE_CHECKING:
    from agents.base import AgentResult, AgentTask

logger = get_logger(__name__)

_MAX_ERROR_LEN = 1000


class ExecutionRecord(BaseModel):
    """One structured event written as a JSONL line."""

    run_id: str
    task_id: str = ""
    role: str = ""
    agent: str = ""
    event: str                              # e.g. "agent_result", "integration_result"
    success: bool | None = None
    elapsed_seconds: float | None = None
    exit_code: int | None = None
    error: str | None = None               # truncated to _MAX_ERROR_LEN
    metadata: dict[str, Any] = {}
    timestamp: str = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )


def record_execution(record: ExecutionRecord) -> None:
    """
    Append *record* as a JSON line to settings.EXECUTION_LOG_PATH.
    Pure no-op when EXECUTION_LOG_PATH is empty (the default).
    Never raises — failures are logged as warnings.
    """
    path = settings.EXECUTION_LOG_PATH
    if not path:
        return

    try:
        log_path = Path(path)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        data = record.model_dump()
        # Enforce error truncation even if caller passed a long string
        if data.get("error") and len(data["error"]) > _MAX_ERROR_LEN:
            data["error"] = data["error"][:_MAX_ERROR_LEN]

        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False) + "\n")

    except Exception as exc:
        logger.warning(f"[telemetry] failed to write execution log: {exc}")


# ── convenience wrappers ──────────────────────────────────────────────────────

def record_agent_result(
    result: "AgentResult",
    task: "AgentTask",
    agent_name: str,
) -> None:
    """
    Record an AgentResult from ClaudeCodeAgent or CodexAgent.
    Skips prompt; takes elapsed_seconds from result.metadata.
    """
    meta = {
        k: v
        for k, v in result.metadata.items()
        if isinstance(v, (str, int, float, bool, type(None)))
    }
    record_execution(ExecutionRecord(
        run_id=task.task_id,
        task_id=task.task_id,
        role=task.role,
        agent=agent_name,
        event="agent_result",
        success=result.success,
        elapsed_seconds=result.metadata.get("elapsed_seconds"),
        exit_code=result.exit_code if result.exit_code != 0 else None,
        error=(result.error or "")[:_MAX_ERROR_LEN] or None,
        metadata=meta,
    ))
