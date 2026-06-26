"""
tests/unit/test_run_state.py  —  PR5 RunState persistence (written FIRST, before impl)

Red phase: all tests must FAIL before implementation is written.

Covers:
  1. save_run_state writes a JSON file at {base}/.openclaw/run_{run_id}.json
  2. load_run_state reads it back as identical PipelineRun
  3. load_run_state returns None for unknown run_id
  4. save_run_state is idempotent — overwriting reflects updated stage
  5. save_run_state creates parent dirs if they don't exist
  6. pipeline.run_pipeline() calls save_run_state() at least once
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models.context import PipelineRun


# ── helpers ──────────────────────────────────────────────────────────────────

def _run(run_id: str = "abc123", stage: str = "init") -> PipelineRun:
    return PipelineRun(run_id=run_id, feature_task_id="ft-1", stage=stage)


def _state_path(base: Path, run_id: str) -> Path:
    return base / ".openclaw" / f"run_{run_id}.json"


# ── 1. save writes file ───────────────────────────────────────────────────────

def test_save_creates_file(tmp_path):
    from persistence.run_state import save_run_state
    run = _run()
    save_run_state(run, str(tmp_path))
    assert _state_path(tmp_path, run.run_id).exists()


def test_save_writes_valid_json(tmp_path):
    from persistence.run_state import save_run_state
    run = _run(stage="worker")
    save_run_state(run, str(tmp_path))
    raw = _state_path(tmp_path, run.run_id).read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["run_id"] == run.run_id
    assert data["stage"] == "worker"


# ── 2. load round-trips ───────────────────────────────────────────────────────

def test_load_returns_equivalent_run(tmp_path):
    from persistence.run_state import save_run_state, load_run_state
    run = _run(stage="review-1")
    save_run_state(run, str(tmp_path))
    loaded = load_run_state(run.run_id, str(tmp_path))
    assert loaded is not None
    assert loaded.run_id == run.run_id
    assert loaded.stage == "review-1"
    assert loaded.feature_task_id == run.feature_task_id


# ── 3. load returns None for missing ─────────────────────────────────────────

def test_load_returns_none_for_missing(tmp_path):
    from persistence.run_state import load_run_state
    result = load_run_state("nonexistent-id", str(tmp_path))
    assert result is None


# ── 4. overwrite reflects updated stage ──────────────────────────────────────

def test_save_overwrites_with_new_stage(tmp_path):
    from persistence.run_state import save_run_state, load_run_state
    run = _run(stage="clone")
    save_run_state(run, str(tmp_path))

    run.stage = "aggregate"
    save_run_state(run, str(tmp_path))

    loaded = load_run_state(run.run_id, str(tmp_path))
    assert loaded is not None
    assert loaded.stage == "aggregate"


# ── 5. parent dirs auto-created ──────────────────────────────────────────────

def test_save_creates_parent_dirs(tmp_path):
    from persistence.run_state import save_run_state
    nested = tmp_path / "deep" / "nested"
    run = _run()
    save_run_state(run, str(nested))
    assert _state_path(nested, run.run_id).exists()


# ── 6. pipeline calls save_run_state ─────────────────────────────────────────

async def test_pipeline_saves_run_state(tmp_path):
    """run_pipeline() must call save_run_state at least once per major stage."""
    from pipeline import run_pipeline
    from models.patch import MergedPatch, PatchStatus
    from models.github import PRResult

    merged = MergedPatch(
        feature_task_id="t1",
        merged_diff="diff --git a/features/f.py b/features/f.py\n",
        source_patch_ids=["s1"],
        status=PatchStatus.SUCCESS,
    )
    pr_result = PRResult(
        pr_number=1,
        pr_url="https://github.com/org/repo/pull/1",
        head_branch="openclaw/feat/abc",
    )

    with (
        patch("pipeline._clone_repo"),
        patch("pipeline.WorkerWorkspaceManager") as MockWM,
        patch("pipeline.ContextAgent") as MockCtx,
        patch("pipeline.OrchestratorAgent") as MockOrch,
        patch("pipeline.TestAgent") as MockTest,
        patch("pipeline._run_workers", new=AsyncMock(return_value=[])),
        patch("pipeline.AggregatorAgent") as MockAgg,
        patch("pipeline.IntegratorAgent") as MockIntg,
        patch("pipeline.ReviewAgent") as MockRev,
        patch("pipeline.GitHubAgent") as MockGH,
        patch("pipeline.DebugAgent"),
        patch("pipeline.record_execution"),
        patch("pipeline.settings") as mock_settings,
        patch("pipeline.save_run_state") as mock_save,
    ):
        mock_settings.MAX_PARALLEL_WORKERS = 4
        mock_settings.MAX_DEBUG_RETRIES = 0
        mock_settings.CI_POLL_INTERVAL_SECONDS = 1
        mock_settings.CI_POLL_TIMEOUT_SECONDS = 5
        mock_settings.LLM_MODEL = "test-model"
        mock_settings.WORKSPACE_BASE_PATH = str(tmp_path)
        mock_settings.ANTHROPIC_BASE_URL = ""
        mock_settings.EXECUTION_LOG_PATH = ""
        mock_settings.LINT_COMMAND = ""
        mock_settings.TYPECHECK_COMMAND = ""
        mock_settings.QUALITY_GATE_WARN_ONLY = False
        mock_settings.INTEGRATION_TEST_COMMAND = ""
        mock_settings.INTEGRATION_TEST_TIMEOUT = 30

        MockWM.return_value.validate_strategy.return_value = (True, "ok")
        MockCtx.return_value.gather = AsyncMock(return_value=MagicMock(
            relevant_files=[], existing_patterns=[]))
        MockOrch.return_value.decompose = AsyncMock(return_value=MagicMock(
            parallel_groups=[], dependencies={},
            feature_task=MagicMock(subtasks=[])))
        MockTest.return_value.generate = AsyncMock(return_value=MagicMock(test_code=""))
        MockAgg.return_value.merge = AsyncMock(return_value=merged)
        MockIntg.return_value.integrate = AsyncMock(return_value=merged)
        MockIntg.return_value.last_result = MagicMock(
            line_count=1, source_patch_count=1, conflicts_resolved=0,
            tests_passed=None)
        MockRev.return_value.review = AsyncMock(return_value=MagicMock(
            approved=True, summary="ok", comments=[]))
        MockGH.return_value.apply_and_push = AsyncMock(return_value=MagicMock())
        MockGH.return_value.create_pr = AsyncMock(return_value=pr_result)
        MockGH.return_value.poll_ci = AsyncMock(return_value=MagicMock(
            status=MagicMock(value="success"), __eq__=lambda s, o: True))

        await run_pipeline(raw_requirement="test req", repository="org/repo")

    assert mock_save.call_count >= 3, (
        f"Expected save_run_state called ≥3 times (once per major stage), "
        f"got {mock_save.call_count}"
    )
