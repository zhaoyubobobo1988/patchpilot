from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from .task import SubTask


class CIStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"


class PRRequest(BaseModel):
    repository: str                  # "org/repo"
    title: str
    body: str
    head_branch: str
    base_branch: str = "main"
    draft: bool = True


class PRResult(BaseModel):
    pr_number: int
    pr_url: str
    head_branch: str
    state: str = "open"


class CICheckResult(BaseModel):
    pr_number: int
    status: CIStatus
    failed_checks: list[str] = []
    logs_url: str | None = None
    raw_log: str | None = None


class DebugContext(BaseModel):
    original_patch: str
    ci_log: str
    failed_checks: list[str]
    retry_attempt: int               # 第几次重试（1-5）
    subtask_id: str
    subtask_goal: str
    subtask_files: list[str]
