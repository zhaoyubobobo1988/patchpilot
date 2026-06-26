"""
tests/unit/test_quality_gate.py  —  PR2 quality gate (written FIRST, before impl)

Red phase: all tests here must FAIL before implementation is written.

Covers:
  1. QualityGateResult model exists with expected fields
  2. Settings has LINT_COMMAND, TYPECHECK_COMMAND, QUALITY_GATE_WARN_ONLY
  3. _run_quality_gate() runs a command and returns QualityGateResult
  4. _run_quality_gate() returns passed=False on non-zero exit
  5. pipeline skips apply_and_push when gate blocks (warn_only=False, gate fails)
  6. pipeline proceeds when gate passes
  7. pipeline proceeds when warn_only=True even if gate fails
  8. pipeline proceeds when no gate commands are configured
"""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── 1. QualityGateResult model ────────────────────────────────────────────────

def test_quality_gate_result_model_exists():
    from models.patch import QualityGateResult  # noqa: F401


def test_quality_gate_result_has_expected_fields():
    from models.patch import QualityGateResult
    r = QualityGateResult(
        level="pre_publish",
        passed=True,
        command="ruff check .",
    )
    assert r.passed is True
    assert r.command == "ruff check ."
    assert r.level == "pre_publish"
    assert r.exit_code is None
    assert r.output_summary == ""
    assert r.warn_only is False


def test_quality_gate_result_failed_with_output():
    from models.patch import QualityGateResult
    r = QualityGateResult(
        level="pre_publish",
        passed=False,
        command="ruff check .",
        exit_code=1,
        output_summary="E501 line too long",
        warn_only=True,
    )
    assert r.passed is False
    assert r.exit_code == 1
    assert r.warn_only is True


# ── 2. Settings fields ────────────────────────────────────────────────────────

def test_settings_has_lint_command():
    from config.settings import Settings
    s = Settings(LINT_COMMAND="ruff check .")
    assert s.LINT_COMMAND == "ruff check ."


def test_settings_has_typecheck_command():
    from config.settings import Settings
    s = Settings(TYPECHECK_COMMAND="mypy .")
    assert s.TYPECHECK_COMMAND == "mypy ."


def test_settings_has_quality_gate_warn_only():
    from config.settings import Settings
    s = Settings(QUALITY_GATE_WARN_ONLY=True)
    assert s.QUALITY_GATE_WARN_ONLY is True


def test_settings_defaults_are_safe():
    """Gate commands default to empty (disabled) and warn_only defaults to False."""
    from config.settings import Settings
    s = Settings()
    assert s.LINT_COMMAND == ""
    assert s.TYPECHECK_COMMAND == ""
    assert s.QUALITY_GATE_WARN_ONLY is False


# ── 3 & 4. _run_quality_gate() helper ────────────────────────────────────────

async def test_run_quality_gate_passes_on_exit_zero(tmp_path):
    from pipeline import _run_quality_gate
    from models.patch import QualityGateResult

    ok_cmd = f"{sys.executable} -c \"raise SystemExit(0)\""
    result: QualityGateResult = await _run_quality_gate(
        command=ok_cmd,
        cwd=str(tmp_path),
        level="pre_publish",
    )
    assert result.passed is True
    assert result.exit_code == 0


async def test_run_quality_gate_fails_on_nonzero_exit(tmp_path):
    from pipeline import _run_quality_gate
    from models.patch import QualityGateResult

    fail_cmd = f"{sys.executable} -c \"raise SystemExit(1)\""
    result: QualityGateResult = await _run_quality_gate(
        command=fail_cmd,
        cwd=str(tmp_path),
        level="pre_publish",
    )
    assert result.passed is False
    assert result.exit_code == 1


async def test_run_quality_gate_captures_stderr(tmp_path):
    from pipeline import _run_quality_gate

    cmd = f"{sys.executable} -c \"import sys; sys.stderr.write('oops'); raise SystemExit(2)\""
    result = await _run_quality_gate(command=cmd, cwd=str(tmp_path), level="pre_publish")
    assert result.passed is False
    assert "oops" in result.output_summary


# ── 5-8. Pipeline pre-publish gate integration ───────────────────────────────

def _make_pipeline_mocks(lint_cmd="", typecheck_cmd="", warn_only=False):
    """Return a patch context that replaces all side-effecting pipeline calls."""
    from models.github import CICheckResult, CIStatus, PRRequest, PRResult
    from models.patch import MergedPatch, PatchStatus

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
    ci_ok = CICheckResult(pr_number=1, status=CIStatus.SUCCESS)

    return merged, pr_result, ci_ok


async def _run_pipeline_with_gate(lint_cmd, typecheck_cmd, warn_only):
    """Run run_pipeline with all expensive stages mocked, only gate is real."""
    from pipeline import run_pipeline
    from models.task import TaskGraph, FeatureTask, SubTask
    from models.patch import PatchSet, PatchResult, PatchStatus, MergedPatch
    from models.context import AgentContext

    merged, pr_result, ci_ok = _make_pipeline_mocks(lint_cmd, typecheck_cmd, warn_only)

    task_graph = MagicMock()
    task_graph.parallel_groups = []
    task_graph.dependencies = {}
    task_graph.feature_task = MagicMock()
    task_graph.feature_task.subtasks = []

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
    ):
        mock_settings.MAX_PARALLEL_WORKERS = 4
        mock_settings.MAX_DEBUG_RETRIES = 0
        mock_settings.CI_POLL_INTERVAL_SECONDS = 1
        mock_settings.CI_POLL_TIMEOUT_SECONDS = 5
        mock_settings.LLM_MODEL = "test-model"
        mock_settings.WORKSPACE_BASE_PATH = "/tmp/test"
        mock_settings.ANTHROPIC_BASE_URL = ""
        mock_settings.EXECUTION_LOG_PATH = ""
        mock_settings.LINT_COMMAND = lint_cmd
        mock_settings.TYPECHECK_COMMAND = typecheck_cmd
        mock_settings.QUALITY_GATE_WARN_ONLY = warn_only
        mock_settings.INTEGRATION_TEST_COMMAND = ""
        mock_settings.INTEGRATION_TEST_TIMEOUT = 30

        MockWM.return_value.validate_strategy.return_value = (True, "ok")
        MockCtx.return_value.gather = AsyncMock(return_value=MagicMock(
            relevant_files=[], existing_patterns=[]))
        MockOrch.return_value.decompose = AsyncMock(return_value=task_graph)
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

        import sys as _sys
        ok_cmd = f"{_sys.executable} -c \"raise SystemExit(0)\""
        fail_cmd = f"{_sys.executable} -c \"raise SystemExit(1)\""

        # Replace configured lint/typecheck with real pass/fail commands
        if lint_cmd:
            mock_settings.LINT_COMMAND = ok_cmd if lint_cmd == "PASS" else fail_cmd
        if typecheck_cmd:
            mock_settings.TYPECHECK_COMMAND = ok_cmd if typecheck_cmd == "PASS" else fail_cmd

        result = await run_pipeline(
            raw_requirement="test req",
            repository="org/repo",
        )
        apply_called = MockGH.return_value.apply_and_push.called
        return result, apply_called


async def test_pipeline_blocks_when_lint_fails_and_not_warn_only():
    result, apply_called = await _run_pipeline_with_gate(
        lint_cmd="FAIL", typecheck_cmd="", warn_only=False
    )
    assert not apply_called, "apply_and_push should NOT be called when gate blocks"
    assert result.stage != "done" or result.pr_url is None or result.pr_url == ""


async def test_pipeline_proceeds_when_lint_passes():
    result, apply_called = await _run_pipeline_with_gate(
        lint_cmd="PASS", typecheck_cmd="", warn_only=False
    )
    assert apply_called, "apply_and_push SHOULD be called when gate passes"


async def test_pipeline_proceeds_when_warn_only_and_lint_fails():
    result, apply_called = await _run_pipeline_with_gate(
        lint_cmd="FAIL", typecheck_cmd="", warn_only=True
    )
    assert apply_called, "apply_and_push SHOULD be called in warn-only mode"


async def test_pipeline_proceeds_with_no_gate_commands():
    result, apply_called = await _run_pipeline_with_gate(
        lint_cmd="", typecheck_cmd="", warn_only=False
    )
    assert apply_called, "apply_and_push SHOULD be called when no gate is configured"
