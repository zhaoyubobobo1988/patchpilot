"""
tests/unit/test_metrics.py — PR9 PipelineMetrics (written FIRST, before impl)

Red phase: all tests must FAIL before implementation is written.

Covers:
  1. PipelineMetrics defaults
  2. record_stage_attempt creates StageMetric entry on first call
  3. record_stage_attempt accumulates attempts / elapsed
  4. record_stage_attempt tracks success vs failure counts
  5. mark_pipeline_complete sets end_time and status
"""
from __future__ import annotations

import pytest


# ── 1. Defaults ─────────────────────────────────────────────────────────────

def test_pipeline_metrics_defaults():
    from telemetry.metrics import PipelineMetrics
    m = PipelineMetrics(run_id="r1")
    assert m.run_id == "r1"
    assert m.pipeline_status == "running"
    assert m.stage_metrics == {}
    assert m.total_stages == 0
    assert m.total_retries == 0


# ── 2. First attempt creates entry ──────────────────────────────────────────

def test_record_stage_attempt_creates_entry():
    from telemetry.metrics import PipelineMetrics
    m = PipelineMetrics(run_id="r1")
    m.record_stage_attempt("clone", elapsed_seconds=1.5, success=True)

    assert "clone" in m.stage_metrics
    sm = m.stage_metrics["clone"]
    assert sm.stage_name == "clone"
    assert sm.attempts == 1
    assert sm.successes == 1
    assert sm.failures == 0
    assert sm.total_elapsed_seconds == 1.5


# ── 3. Accumulation ─────────────────────────────────────────────────────────

def test_record_stage_attempt_accumulates():
    from telemetry.metrics import PipelineMetrics
    m = PipelineMetrics(run_id="r1")
    m.record_stage_attempt("clone", elapsed_seconds=1.0, success=False)
    m.record_stage_attempt("clone", elapsed_seconds=2.0, success=True)
    m.record_stage_attempt("clone", elapsed_seconds=0.5, success=True)

    sm = m.stage_metrics["clone"]
    assert sm.attempts == 3
    assert sm.successes == 2
    assert sm.failures == 1
    assert sm.total_elapsed_seconds == 3.5


# ── 4. Success vs failure tracking ──────────────────────────────────────────

def test_record_stage_attempt_tracks_success_and_failure():
    from telemetry.metrics import PipelineMetrics
    m = PipelineMetrics(run_id="r1")
    m.record_stage_attempt("a", elapsed_seconds=1.0, success=True)
    m.record_stage_attempt("b", elapsed_seconds=2.0, success=False,
                           error="timeout", error_category="transient")

    assert m.stage_metrics["a"].successes == 1
    assert m.stage_metrics["a"].failures == 0
    assert m.stage_metrics["b"].successes == 0
    assert m.stage_metrics["b"].failures == 1
    assert m.stage_metrics["b"].last_error == "timeout"
    assert m.stage_metrics["b"].last_error_category == "transient"


# ── 5. mark_pipeline_complete ───────────────────────────────────────────────

def test_mark_pipeline_complete():
    from telemetry.metrics import PipelineMetrics
    m = PipelineMetrics(run_id="r1")
    m.record_stage_attempt("clone", 1.0, success=True)

    m.mark_pipeline_complete("success")
    assert m.pipeline_status == "success"
    assert m.end_time != ""

    # Marking again overwrites
    m.mark_pipeline_complete("failed")
    assert m.pipeline_status == "failed"
