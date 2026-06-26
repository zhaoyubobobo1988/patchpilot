"""
tests/unit/test_pipeline_workspace_preflight.py

Tests for the workspace strategy preflight check that runs in pipeline.py
after Stage 0 (clone) and before ContextAgent.

No real git / Claude CLI / Codex CLI calls — all external dependencies mocked.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pipeline import run_pipeline


# ── shared fixtures / helpers ─────────────────────────────────────────────────

def _noop_clone(*args, **kwargs) -> None:
    """Drop-in replacement for _clone_repo that does nothing."""


class _ImmediateStopContextAgent:
    """
    Minimal ContextAgent stub that records it was reached, then raises
    a sentinel to stop the pipeline without running any real logic.
    """
    reached: list[bool] = []

    def __init__(self, ctx):
        _ImmediateStopContextAgent.reached.append(True)

    async def gather(self, task, path):
        raise _Sentinel("ContextAgent.gather reached")


class _Sentinel(Exception):
    pass


class _SilentContextAgent:
    """ContextAgent stub that silently returns an empty CodeContext."""

    def __init__(self, ctx):
        pass

    async def gather(self, task, path):
        from models.context import CodeContext
        return CodeContext(feature_task_id=task.id)


# ── Test 1: preflight ok → pipeline proceeds to ContextAgent ─────────────────

@pytest.mark.asyncio
async def test_preflight_ok_pipeline_proceeds_to_context_stage():
    """
    When validate_strategy returns ok=True, the pipeline must reach
    ContextAgent (stage "context") and not bail out as "done".
    """
    _ImmediateStopContextAgent.reached = []

    with patch("pipeline._clone_repo", side_effect=_noop_clone), \
         patch("agents.worker.workspace.WorkerWorkspaceManager.validate_strategy",
               return_value=(True, "clone strategy is available")), \
         patch("pipeline.ContextAgent", _ImmediateStopContextAgent):
        try:
            run = await run_pipeline("add login limit", "org/repo")
        except _Sentinel:
            pass  # expected: we just want to confirm ContextAgent was reached

    assert len(_ImmediateStopContextAgent.reached) == 1, \
        "ContextAgent must be reached when preflight succeeds"


# ── Test 2: preflight fail → pipeline terminates before ContextAgent ──────────

@pytest.mark.asyncio
async def test_preflight_fail_terminates_before_context():
    """
    When validate_strategy returns ok=False, run_pipeline must:
    - set run.stage = "done"
    - append the error message to run.error_log
    - never reach ContextAgent
    """
    _ImmediateStopContextAgent.reached = []
    error_msg = "worktree not available: not a git repository"

    with patch("pipeline._clone_repo", side_effect=_noop_clone), \
         patch("agents.worker.workspace.WorkerWorkspaceManager.validate_strategy",
               return_value=(False, error_msg)), \
         patch("pipeline.ContextAgent", _ImmediateStopContextAgent):
        run = await run_pipeline("add login limit", "org/repo")

    assert run.stage == "done"
    assert any(error_msg in e for e in run.error_log), \
        f"error_log should contain the preflight message; got {run.error_log}"
    assert len(_ImmediateStopContextAgent.reached) == 0, \
        "ContextAgent must NOT be reached when preflight fails"


# ── Test 3: default clone strategy does not block ─────────────────────────────

@pytest.mark.asyncio
async def test_default_clone_strategy_preflight_does_not_block():
    """
    With the default strategy="clone", validate_strategy always returns ok.
    The preflight must not block the pipeline.
    validate_strategy must be called exactly once.
    """
    _ImmediateStopContextAgent.reached = []
    call_count = []

    def _fake_validate(workspace):
        call_count.append(workspace)
        return (True, "clone strategy is available")

    with patch("pipeline._clone_repo", side_effect=_noop_clone), \
         patch("config.settings.settings.WORKER_WORKSPACE_STRATEGY", "clone"), \
         patch("agents.worker.workspace.WorkerWorkspaceManager.validate_strategy",
               side_effect=_fake_validate), \
         patch("pipeline.ContextAgent", _ImmediateStopContextAgent):
        try:
            await run_pipeline("add login limit", "org/repo")
        except _Sentinel:
            pass

    assert len(call_count) == 1, "validate_strategy must be called exactly once"
    assert len(_ImmediateStopContextAgent.reached) == 1


# ── Test 4: preflight fail → pr_url stays None, ci_passed stays None ─────────

@pytest.mark.asyncio
async def test_preflight_fail_run_has_empty_pr_and_ci():
    """
    When preflight fails, run.pr_url and run.ci_passed must keep their
    default values (None) — no PR is created, no CI is polled.
    """
    with patch("pipeline._clone_repo", side_effect=_noop_clone), \
         patch("agents.worker.workspace.WorkerWorkspaceManager.validate_strategy",
               return_value=(False, "git not found")):
        run = await run_pipeline("add login limit", "org/repo")

    assert run.pr_url is None
    assert run.ci_passed is None
    assert run.stage == "done"


# ── Test 5: validate_strategy receives the cloned workspace path ──────────────

@pytest.mark.asyncio
async def test_preflight_receives_workspace_path():
    """
    validate_strategy is called with ctx.workspace_path (the cloned repo path),
    not the raw source_workspace or empty string.
    """
    captured_path = []

    def _fake_validate(workspace):
        captured_path.append(workspace)
        return (True, "ok")

    _ImmediateStopContextAgent.reached = []

    with patch("pipeline._clone_repo", side_effect=_noop_clone), \
         patch("agents.worker.workspace.WorkerWorkspaceManager.validate_strategy",
               side_effect=_fake_validate), \
         patch("pipeline.ContextAgent", _ImmediateStopContextAgent):
        try:
            await run_pipeline("add login limit", "org/repo")
        except _Sentinel:
            pass

    assert len(captured_path) == 1
    # The workspace path must be non-empty and look like a real path
    assert len(captured_path[0]) > 0
    assert "openclaw" in captured_path[0].lower() or "/" in captured_path[0] or "\\" in captured_path[0]
