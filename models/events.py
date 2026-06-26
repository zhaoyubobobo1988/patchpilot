from __future__ import annotations

import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AgentEventKind(str, Enum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    TOOL_CALL = "tool_call"


class AgentEvent(BaseModel):
    kind: AgentEventKind
    agent_id: str
    timestamp: float = Field(default_factory=time.time)
    payload: dict[str, Any] = {}
