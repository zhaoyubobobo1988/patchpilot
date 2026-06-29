"""
tests/unit/test_spans_integration.py — PR9 end-to-end observability tests

Covers:
  1. Nested spans — parent span_name appears as parent_span_id in child
  2. PipelineMetrics full lifecycle through SupervisorLoop
"""
from __future__ import annotations

from pathlib import Path

import pytest

from models.context import AgentContext, PipelineRun
from models.task import FeatureTask


def _make_state(tmp_path: Path):
    from pipeline_stages import PipelineState
    run = PipelineRun(run_id="r1", feature_task_id="ft-1")
    ctx = AgentContext(
        run_id="r1", feature_task_id="ft-1",
        repository="org/repo", base_branch="main",
        workspace_path=str(tmp_path),
    )
    task = FeatureTask(
        raw_requirement="add login", feature_name="add-login",
        repository="org/repo",
    )
    return PipelineState(run=run, ctx=ctx, task=task)


# ── 1. Nested span parent-child ────────────────────────────────────────────

async def test_nested_spans_correct_parent_child_ids():
    """End-to-end: parent span wraps child, IDs match."""
    from telemetry.spans import Span

    records: list = []

    class CaptureExporter:
        def export(self, record):
            records.append(record)

    async with Span("outer", exporter=CaptureExporter()) as outer:
        async with Span("inner", parent=outer, exporter=CaptureExporter()):
            pass
        async with Span("inner2", parent=outer, exporter=CaptureExporter()):
            pass

    assert len(records) == 3
    outer_rec = next(r for r in records if r.name == "outer")
    inner_rec = next(r for r in records if r.name == "inner")
    inner2_rec = next(r for r in records if r.name == "inner2")

    assert inner_rec.parent_span_id == outer_rec.span_id
    assert inner2_rec.parent_span_id == outer_rec.span_id
    # Outer span elapsed >= sum of children (wall clock accounting)
    assert outer_rec.elapsed_seconds >= inner_rec.elapsed_seconds


# ── 2. PipelineMetrics via SupervisorLoop ───────────────────────────────────

async def test_supervisor_loop_records_metrics(tmp_path):
    """SupervisorLoop with PipelineMetrics populates stage_metrics."""
    from pipeline_stages import StageResult
    from telemetry.metrics import PipelineMetrics
    from supervisor import SupervisorLoop

    class OkStage:
        name = "clone"

        async def execute(self, state):
            return StageResult()

    metrics = PipelineMetrics(run_id="r1")
    state = _make_state(tmp_path)
    loop = SupervisorLoop(stages=[OkStage()], metrics=metrics)
    await loop.run(state)

    assert "clone" in metrics.stage_metrics
    sm = metrics.stage_metrics["clone"]
    assert sm.attempts == 1
    assert sm.successes == 1
    assert sm.total_elapsed_seconds >= 0  # synthetic stage may be instantaneous


async def test_supervisor_loop_records_retry_metrics(tmp_path):
    """Metrics tracks retries: 1 failure + 1 success = 2 attempts."""
    from pipeline_stages import StageResult
    from models.decision import DecisionKind, OrchestratorDecision
    from models.errors import FailureCategory, ClassifiedError
    from telemetry.metrics import PipelineMetrics
    from supervisor import SupervisorLoop

    ce = ClassifiedError(
        category=FailureCategory.TRANSIENT, source="clone", message="timeout"
    )

    class FlakyStage:
        name = "clone"
        def __init__(self):
            self._calls = 0

        async def execute(self, state):
            self._calls += 1
            if self._calls == 1:
                return StageResult(done=True, error="timeout", classified_error=ce)
            return StageResult()

    metrics = PipelineMetrics(run_id="r1")
    state = _make_state(tmp_path)
    loop = SupervisorLoop(stages=[FlakyStage()], metrics=metrics)
    await loop.run(state)

    sm = metrics.stage_metrics["clone"]
    assert sm.attempts == 2
    assert sm.successes == 1
    assert sm.failures == 1
    assert sm.last_error_category == "transient"


async def test_pipeline_metrics_full_lifecycle(tmp_path):
    """Metrics can be marked complete and serialized."""
    from telemetry.metrics import PipelineMetrics

    metrics = PipelineMetrics(run_id="r1")
    metrics.record_stage_attempt("clone", 1.0, success=True)
    metrics.record_stage_attempt("context", 2.0, success=True)
    metrics.record_stage_attempt("orchestrate", 0.5, success=True)
    metrics.mark_pipeline_complete("success")

    d = metrics.model_dump()
    assert d["pipeline_status"] == "success"
    assert len(d["stage_metrics"]) == 3
    assert d["end_time"] != ""
    assert d["total_stages"] == 3
    assert d["successful_stages"] == 3
    assert d["failed_stages"] == 0
