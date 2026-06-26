"""
tests/unit/test_task_board.py  —  PR4 TaskBoard (written FIRST, before impl)

Red phase: all tests must FAIL before implementation is written.

Covers:
  1. TaskBoard constructed from list[SubTask]; all start PENDING
  2. update() changes entry status
  3. update() sets started_at on IN_PROGRESS, ended_at on COMPLETED/FAILED
  4. update() records worker_id
  5. append_event() adds AgentEvent to the correct entry
  6. snapshot() returns correct per-status counts
  7. snapshot().total == len(subtasks)
  8. _run_workers() calls board.update() for each status transition
  9. board shows COMPLETED after successful task
  10. board shows FAILED for dep-blocked task (same as status test in PR1)
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from models.task import SubTask, TaskStatus
from models.patch import PatchResult, PatchStatus
from models.context import AgentContext


# ── helpers ──────────────────────────────────────────────────────────────────

def _subtask(tid: str) -> SubTask:
    return SubTask(id=tid, feature="feat", goal=f"goal {tid}", files=["features/f.py"])


def _ctx() -> AgentContext:
    return AgentContext(
        run_id="test-run",
        feature_task_id="feat-1",
        repository="org/repo",
        base_branch="main",
        workspace_path="/tmp/test",
    )


def _ok_patch(subtask_id: str) -> PatchResult:
    return PatchResult(
        subtask_id=subtask_id,
        worker_id="w",
        patch_content="diff --git a/features/f.py b/features/f.py\n",
        affected_files=["features/f.py"],
        status=PatchStatus.SUCCESS,
    )


def _fail_patch(subtask_id: str) -> PatchResult:
    return PatchResult(
        subtask_id=subtask_id,
        worker_id="w",
        patch_content="",
        affected_files=[],
        status=PatchStatus.FAILED,
        error_message="worker error",
    )


# ── 1. Construction ───────────────────────────────────────────────────────────

def test_task_board_exists():
    from models.board import TaskBoard  # noqa: F401


def test_task_board_constructed_from_subtasks():
    from models.board import TaskBoard
    tasks = [_subtask("t1"), _subtask("t2"), _subtask("t3")]
    board = TaskBoard(run_id="r1", subtasks=tasks)
    snap = board.snapshot()
    assert snap.total == 3
    assert snap.pending == 3
    assert snap.in_progress == 0
    assert snap.completed == 0
    assert snap.failed == 0


# ── 2 & 3. update() ──────────────────────────────────────────────────────────

def test_update_changes_status_to_in_progress():
    from models.board import TaskBoard
    board = TaskBoard(run_id="r1", subtasks=[_subtask("t1")])
    board.update("t1", TaskStatus.IN_PROGRESS)
    snap = board.snapshot()
    assert snap.in_progress == 1
    assert snap.pending == 0


def test_update_in_progress_sets_started_at():
    from models.board import TaskBoard
    before = time.time()
    board = TaskBoard(run_id="r1", subtasks=[_subtask("t1")])
    board.update("t1", TaskStatus.IN_PROGRESS)
    after = time.time()
    entry = board.snapshot().entries[0]
    assert entry.started_at is not None
    assert before <= entry.started_at <= after


def test_update_completed_sets_ended_at():
    from models.board import TaskBoard
    board = TaskBoard(run_id="r1", subtasks=[_subtask("t1")])
    board.update("t1", TaskStatus.IN_PROGRESS)
    before = time.time()
    board.update("t1", TaskStatus.COMPLETED)
    after = time.time()
    entry = board.snapshot().entries[0]
    assert entry.ended_at is not None
    assert before <= entry.ended_at <= after


def test_update_failed_sets_ended_at():
    from models.board import TaskBoard
    board = TaskBoard(run_id="r1", subtasks=[_subtask("t1")])
    board.update("t1", TaskStatus.IN_PROGRESS)
    board.update("t1", TaskStatus.FAILED)
    entry = board.snapshot().entries[0]
    assert entry.ended_at is not None


# ── 4. worker_id ─────────────────────────────────────────────────────────────

def test_update_records_worker_id():
    from models.board import TaskBoard
    board = TaskBoard(run_id="r1", subtasks=[_subtask("t1")])
    board.update("t1", TaskStatus.IN_PROGRESS, worker_id="worker-0-0")
    entry = board.snapshot().entries[0]
    assert entry.worker_id == "worker-0-0"


# ── 5. append_event() ────────────────────────────────────────────────────────

def test_append_event_adds_to_correct_entry():
    from models.board import TaskBoard
    from models.events import AgentEvent, AgentEventKind
    board = TaskBoard(run_id="r1", subtasks=[_subtask("t1"), _subtask("t2")])
    ev = AgentEvent(kind=AgentEventKind.STARTED, agent_id="t1")
    board.append_event("t1", ev)
    entries = {e.subtask_id: e for e in board.snapshot().entries}
    assert len(entries["t1"].events) == 1
    assert len(entries["t2"].events) == 0


# ── 6. snapshot() counts ─────────────────────────────────────────────────────

def test_snapshot_counts_all_statuses():
    from models.board import TaskBoard
    tasks = [_subtask(f"t{i}") for i in range(4)]
    board = TaskBoard(run_id="r1", subtasks=tasks)
    board.update("t0", TaskStatus.IN_PROGRESS)
    board.update("t1", TaskStatus.COMPLETED)
    board.update("t2", TaskStatus.FAILED)
    # t3 stays PENDING
    snap = board.snapshot()
    assert snap.pending == 1
    assert snap.in_progress == 1
    assert snap.completed == 1
    assert snap.failed == 1
    assert snap.total == 4
    assert snap.run_id == "r1"


# ── 8 & 9. _run_workers() integrates with board ──────────────────────────────

async def test_run_workers_updates_board_on_success():
    from pipeline import _run_workers
    from models.board import TaskBoard
    from models.task import FeatureTask, TaskGraph

    t1 = _subtask("t1")
    ft = FeatureTask(raw_requirement="r", feature_name="f", repository="org/repo", subtasks=[t1])
    tg = TaskGraph(feature_task=ft, parallel_groups=[["t1"]], dependencies={})
    board = TaskBoard(run_id="test-run", subtasks=[t1])

    with (
        patch("pipeline.ClaudeCodeWorker") as MockWorker,
        patch("pipeline.settings") as mock_settings,
    ):
        mock_settings.MAX_PARALLEL_WORKERS = 4
        MockWorker.return_value.execute = AsyncMock(return_value=_ok_patch("t1"))
        await _run_workers(tg, _ctx(), board=board)

    snap = board.snapshot()
    assert snap.completed == 1
    assert snap.failed == 0


async def test_run_workers_updates_board_on_failure():
    from pipeline import _run_workers
    from models.board import TaskBoard
    from models.task import FeatureTask, TaskGraph

    t1 = _subtask("t1")
    ft = FeatureTask(raw_requirement="r", feature_name="f", repository="org/repo", subtasks=[t1])
    tg = TaskGraph(feature_task=ft, parallel_groups=[["t1"]], dependencies={})
    board = TaskBoard(run_id="test-run", subtasks=[t1])

    with (
        patch("pipeline.ClaudeCodeWorker") as MockWorker,
        patch("pipeline.settings") as mock_settings,
    ):
        mock_settings.MAX_PARALLEL_WORKERS = 4
        MockWorker.return_value.execute = AsyncMock(return_value=_fail_patch("t1"))
        await _run_workers(tg, _ctx(), board=board)

    snap = board.snapshot()
    assert snap.failed == 1
    assert snap.completed == 0


async def test_run_workers_no_board_still_works():
    """board=None (default) keeps existing behaviour unchanged."""
    from pipeline import _run_workers
    from models.task import FeatureTask, TaskGraph

    t1 = _subtask("t1")
    ft = FeatureTask(raw_requirement="r", feature_name="f", repository="org/repo", subtasks=[t1])
    tg = TaskGraph(feature_task=ft, parallel_groups=[["t1"]], dependencies={})

    with (
        patch("pipeline.ClaudeCodeWorker") as MockWorker,
        patch("pipeline.settings") as mock_settings,
    ):
        mock_settings.MAX_PARALLEL_WORKERS = 4
        MockWorker.return_value.execute = AsyncMock(return_value=_ok_patch("t1"))
        patches = await _run_workers(tg, _ctx())

    assert patches[0].status == PatchStatus.SUCCESS
