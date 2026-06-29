from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from models.errors import ClassifiedError


class DecisionKind(str, Enum):
    CONTINUE = "continue"
    RETRY = "retry"
    ABORT = "abort"


class OrchestratorDecision(BaseModel):
    kind: DecisionKind = DecisionKind.CONTINUE
    reason: str = ""
    max_retries: int = 1
    classified_error: ClassifiedError | None = None
