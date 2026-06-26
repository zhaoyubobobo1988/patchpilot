"""
tests/unit/test_run_workers.py

Unit tests for pipeline._run_workers():
  - MAX_PARALLEL_WORKERS semaphore limits concurrency
  - Tasks with unsatisfied dependencies are skipped (status=FAILED)
  - SubTask.status transitions: PENDING -> IN_PROGRESS -> COMPLETED/FAILED
  - Tasks whose deps succeeded run normally
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from models.context import AgentContext
from models.patch import PatchResult, PatchStatus
from models.task import SubTask, TaskGraph, FeatureTask, TaskStatus


# ── helpers ───────────────────────────────────────────────────────────────────

def _ctx() -> AgentContext:
    return AgentContext(
        run_id="test-run",
        feature_task_id="feat-1",
        repository="org/repo",
        base_branch="main",
        workspace_path="/tmp/test",
    )


def _subtask(tid: str) -> SubTask:
    return SubTask(id=tid, feature="feat", goal=f"goal {tid}", files=["features/f.py"])


def _ok_patch(subtask_id: str, worker_id: str = "w") -> PatchResult:
    return PatchResult(
        subtask_id=subtask_id,
        worker_id=worker_id,
        patch_content="diff --git a/features/f.py b/features/f.py\n",
        affected_files=["features/f.py"],
        status=PatchStatus.SUCCESS,
    )


def _fail_patch(subtask_id: str, worker_id: str = "w") -> PatchResult:
    return PatchResult(
        subtask_id=subtask_id,
        worker_id=worker_id,
        patch_content="",
        affected_files=[],
        status=PatchStatus.FAILED,
        error_message="worker error",
    )


def _task_graph(
    subtasks: list[SubTask],
    groups: list[list[str]],
    deps: dict[str, list[str]] | None = None,
) -> TaskGraph:
    ft = FeatureTask(
        raw_requirement="test req",
        feature_name="test-feat",
        repository="org/repo",
        subtasks=subtasks,
    )
    return TaskGraph(
        feature_task=ft,
        parallel_groups=groups,
        dependencies=deps or {},
    )


# ── tests ─────────────────────────────────────────────────────────────────────

async def test_semaphore_limits_concurrency():
    """No more than MAX_PARALLEL_WORKERS tasks run simultaneously."""
    from pipeline import _run_workers

    concurrency_peak = 0
    active = 0

    async def fake_execute(subtask):
        nonlocal concurrency_peak, active
        active += 1
        concurrency_peak = max(concurrency_peak, active)
        await asyncio.sleep(0)          # yield so other coroutines can start
        active -= 1
        return _ok_patch(subtask.id)

    tasks = [_subtask(f"t{i}") for i in range(6)]
    tg = _task_graph(tasks, [[t.id for t in tasks]])

    with (
        patch("pipeline.ClaudeCodeWorker") as MockWorker,
        patch("pipeline.settings") as mock_settings,
    ):
        mock_settings.MAX_PARALLEL_WORKERS = 2
        instance = MockWorker.return_value
        instance.execute = fake_execute

        await _run_workers(tg, _ctx())

    assert concurrency_peak <= 2, (
        f"Expected ≤2 concurrent workers, got peak={concurrency_peak}"
    )


async def test_dep_unsatisfied_because_dependency_failed():
    """A task whose dependency failed is skipped with status=FAILED."""
    from pipeline import _run_workers

    t1 = _subtask("t1")
    t2 = _subtask("t2")   # depends on t1
    tg = _task_graph(
        [t1, t2],
        groups=[["t1"], ["t2"]],
        deps={"t2": ["t1"]},
    )

    with (
        patch("pipeline.ClaudeCodeWorker") as MockWorker,
        patch("pipeline.settings") as mock_settings,
    ):
        mock_settings.MAX_PARALLEL_WORKERS = 4
        # t1 fails
        mock_execute = AsyncMock(return_value=_fail_patch("t1"))
        MockWorker.return_value.execute = mock_execute

        patches = await _run_workers(tg, _ctx())

    assert len(patches) == 2
    t1_result = next(p for p in patches if p.subtask_id == "t1")
    t2_result = next(p for p in patches if p.subtask_id == "t2")

    assert t1_result.status == PatchStatus.FAILED
    assert t2_result.status == PatchStatus.FAILED
    assert "Dependencies not satisfied" in (t2_result.error_message or "")

    assert t1.status == TaskStatus.FAILED
    assert t2.status == TaskStatus.FAILED


async def test_dep_satisfied_task_runs_normally():
    """A task whose dependency succeeded is executed normally."""
    from pipeline import _run_workers

    t1 = _subtask("t1")
    t2 = _subtask("t2")
    tg = _task_graph(
        [t1, t2],
        groups=[["t1"], ["t2"]],
        deps={"t2": ["t1"]},
    )

    with (
        patch("pipeline.ClaudeCodeWorker") as MockWorker,
        patch("pipeline.settings") as mock_settings,
    ):
        mock_settings.MAX_PARALLEL_WORKERS = 4
        mock_execute = AsyncMock(side_effect=[
            _ok_patch("t1"),
            _ok_patch("t2"),
        ])
        MockWorker.return_value.execute = mock_execute

        patches = await _run_workers(tg, _ctx())

    assert all(p.status == PatchStatus.SUCCESS for p in patches)
    assert t1.status == TaskStatus.COMPLETED
    assert t2.status == TaskStatus.COMPLETED


async def test_status_transitions_in_progress_then_completed():
    """SubTask.status moves PENDING -> IN_PROGRESS -> COMPLETED on success."""
    from pipeline import _run_workers

    t1 = _subtask("t1")
    status_during_execute = []

    async def fake_execute(subtask):
        status_during_execute.append(subtask.status)
        return _ok_patch(subtask.id)

    tg = _task_graph([t1], [["t1"]])

    with (
        patch("pipeline.ClaudeCodeWorker") as MockWorker,
        patch("pipeline.settings") as mock_settings,
    ):
        mock_settings.MAX_PARALLEL_WORKERS = 4
        MockWorker.return_value.execute = fake_execute

        await _run_workers(tg, _ctx())

    assert status_during_execute == [TaskStatus.IN_PROGRESS]
    assert t1.status == TaskStatus.COMPLETED


async def test_no_deps_all_tasks_run():
    """Tasks with no declared dependencies all run regardless of order."""
    from pipeline import _run_workers

    tasks = [_subtask(f"t{i}") for i in range(3)]
    tg = _task_graph(tasks, [[t.id for t in tasks]], deps={})

    with (
        patch("pipeline.ClaudeCodeWorker") as MockWorker,
        patch("pipeline.settings") as mock_settings,
    ):
        mock_settings.MAX_PARALLEL_WORKERS = 4
        MockWorker.return_value.execute = AsyncMock(
            side_effect=[_ok_patch(t.id) for t in tasks]
        )

        patches = await _run_workers(tg, _ctx())

    assert len(patches) == 3
    assert all(p.status == PatchStatus.SUCCESS for p in patches)
