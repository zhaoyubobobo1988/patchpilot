from __future__ import annotations

import time
from typing import TYPE_CHECKING

from pydantic import BaseModel

from models.task import TaskStatus

if TYPE_CHECKING:
    from models.events import AgentEvent
    from models.task import SubTask


class TaskEntry(BaseModel):
    subtask_id: str
    status: TaskStatus = TaskStatus.PENDING
    worker_id: str | None = None
    events: list = []          # list[AgentEvent] — untyped to avoid circular import
    started_at: float | None = None
    ended_at: float | None = None

    model_config = {"arbitrary_types_allowed": True}


class BoardSnapshot(BaseModel):
    run_id: str
    total: int
    pending: int
    in_progress: int
    completed: int
    failed: int
    entries: list[TaskEntry]


class TaskBoard:
    """Centralized, mutable status board for all subtasks in one pipeline run."""

    def __init__(self, run_id: str, subtasks: list[SubTask]) -> None:
        self._run_id = run_id
        self._entries: dict[str, TaskEntry] = {
            st.id: TaskEntry(subtask_id=st.id, status=st.status)
            for st in subtasks
        }

    def update(
        self,
        subtask_id: str,
        status: TaskStatus,
        worker_id: str | None = None,
    ) -> None:
        entry = self._entries[subtask_id]
        entry.status = status
        if worker_id is not None:
            entry.worker_id = worker_id
        if status == TaskStatus.IN_PROGRESS:
            entry.started_at = time.time()
        elif status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            entry.ended_at = time.time()

    def append_event(self, subtask_id: str, event: AgentEvent) -> None:
        self._entries[subtask_id].events.append(event)

    def snapshot(self) -> BoardSnapshot:
        entries = list(self._entries.values())
        return BoardSnapshot(
            run_id=self._run_id,
            total=len(entries),
            pending=sum(1 for e in entries if e.status == TaskStatus.PENDING),
            in_progress=sum(1 for e in entries if e.status == TaskStatus.IN_PROGRESS),
            completed=sum(1 for e in entries if e.status == TaskStatus.COMPLETED),
            failed=sum(1 for e in entries if e.status == TaskStatus.FAILED),
            entries=entries,
        )
