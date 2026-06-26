"""
tests/unit/test_stage_executor.py  —  PR6 StageExecutor (written FIRST, before impl)

Red phase: all tests must FAIL before implementation is written.

Covers:
  1. StageResult model: done=False default, error=None default
  2. StageResult with done=True and error carries message
  3. PipelineState holds run, ctx, task; has optional intermediate fields
  4. StageExecutor Protocol: any object with name:str + async execute() qualifies
  5. CloneStage.name == "clone"
  6. CloneStage.execute() calls _clone_repo and returns StageResult(done=False)
  7. CloneStage.execute() returns StageResult(done=True, error=...) on exception
  8. ContextStage.name == "context"
  9. ContextStage.execute() calls context_agent.gather() and stores code_context
  10. OrchestrateStage.name == "orchestrate"
  11. OrchestrateStage.execute() calls orchestrator.decompose() and stores task_graph
  12. run_pipeline() still passes through stage "clone" (regression)
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models.context import AgentContext, PipelineRun
from models.task import FeatureTask


# ── helpers ──────────────────────────────────────────────────────────────────

def _feature_task() -> FeatureTask:
    return FeatureTask(
        raw_requirement="add login",
        feature_name="add-login",
        repository="org/repo",
    )


def _ctx(tmp_path: Path) -> AgentContext:
    return AgentContext(
        run_id="r1",
        feature_task_id="ft-1",
        repository="org/repo",
        base_branch="main",
        workspace_path=str(tmp_path),
    )


def _run() -> PipelineRun:
    return PipelineRun(run_id="r1", feature_task_id="ft-1")


# ── 1 & 2. StageResult ───────────────────────────────────────────────────────

def test_stage_result_defaults():
    from pipeline_stages import StageResult
    r = StageResult()
    assert r.done is False
    assert r.error is None


def test_stage_result_done_with_error():
    from pipeline_stages import StageResult
    r = StageResult(done=True, error="clone failed")
    assert r.done is True
    assert r.error == "clone failed"


# ── 3. PipelineState ─────────────────────────────────────────────────────────

def test_pipeline_state_holds_run_ctx_task(tmp_path):
    from pipeline_stages import PipelineState
    run = _run()
    ctx = _ctx(tmp_path)
    task = _feature_task()
    state = PipelineState(run=run, ctx=ctx, task=task)
    assert state.run is run
    assert state.ctx is ctx
    assert state.task is task


def test_pipeline_state_optional_fields_default_none(tmp_path):
    from pipeline_stages import PipelineState
    state = PipelineState(run=_run(), ctx=_ctx(tmp_path), task=_feature_task())
    assert state.code_context is None
    assert state.task_graph is None
    assert state.merged is None


# ── 4. StageExecutor Protocol ─────────────────────────────────────────────────

def test_stage_executor_protocol_structural(tmp_path):
    """Any object with name:str + async execute() satisfies the Protocol."""
    from pipeline_stages import StageExecutor, StageResult, PipelineState
    import asyncio

    class MyStage:
        name = "my-stage"
        async def execute(self, state: PipelineState) -> StageResult:
            return StageResult()

    stage = MyStage()
    assert isinstance(stage, StageExecutor)


# ── 5 & 6 & 7. CloneStage ────────────────────────────────────────────────────

def test_clone_stage_name():
    from pipeline_stages import CloneStage
    assert CloneStage().name == "clone"


async def test_clone_stage_execute_success(tmp_path):
    from pipeline_stages import CloneStage, PipelineState, StageResult

    state = PipelineState(run=_run(), ctx=_ctx(tmp_path), task=_feature_task())

    with patch("pipeline_stages._clone_repo") as mock_clone:
        result = await CloneStage().execute(state)

    mock_clone.assert_called_once_with(state.ctx.workspace_path, state.task.repository)
    assert result.done is False
    assert result.error is None


async def test_clone_stage_execute_failure_returns_done(tmp_path):
    from pipeline_stages import CloneStage, PipelineState

    state = PipelineState(run=_run(), ctx=_ctx(tmp_path), task=_feature_task())

    with patch("pipeline_stages._clone_repo", side_effect=RuntimeError("git clone failed")):
        result = await CloneStage().execute(state)

    assert result.done is True
    assert result.error is not None
    assert "git clone failed" in result.error


# ── 8 & 9. ContextStage ──────────────────────────────────────────────────────

def test_context_stage_name():
    from pipeline_stages import ContextStage
    assert ContextStage().name == "context"


async def test_context_stage_stores_code_context(tmp_path):
    from pipeline_stages import ContextStage, PipelineState

    state = PipelineState(run=_run(), ctx=_ctx(tmp_path), task=_feature_task())
    fake_ctx = MagicMock(relevant_files=[], existing_patterns=[])

    with patch("pipeline_stages.ContextAgent") as MockAgent:
        MockAgent.return_value.gather = AsyncMock(return_value=fake_ctx)
        result = await ContextStage().execute(state)

    assert result.done is False
    assert state.code_context is fake_ctx


# ── 10 & 11. OrchestrateStage ────────────────────────────────────────────────

def test_orchestrate_stage_name():
    from pipeline_stages import OrchestrateStage
    assert OrchestrateStage().name == "orchestrate"


async def test_orchestrate_stage_stores_task_graph(tmp_path):
    from pipeline_stages import OrchestrateStage, PipelineState

    state = PipelineState(run=_run(), ctx=_ctx(tmp_path), task=_feature_task())
    fake_graph = MagicMock(parallel_groups=[], dependencies={},
                           feature_task=MagicMock(subtasks=[]))

    with patch("pipeline_stages.OrchestratorAgent") as MockOrch:
        MockOrch.return_value.decompose = AsyncMock(return_value=fake_graph)
        result = await OrchestrateStage().execute(state)

    assert result.done is False
    assert state.task_graph is fake_graph


# ── 12. regression: run_pipeline still works ─────────────────────────────────

async def test_run_pipeline_regression(tmp_path):
    """Existing pipeline integration still passes after PR6 refactor."""
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
        patch("pipeline.save_run_state"),
        patch("pipeline.settings") as mock_settings,
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

        result = await run_pipeline(raw_requirement="test req", repository="org/repo")

    assert result.run_id is not None
    assert result.stage == "done"
