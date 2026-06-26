"""
tests/unit/test_supervisor_loop.py  —  PR7 SupervisorLoop (written FIRST, before impl)

Red phase: all tests must FAIL before implementation is written.

Covers:
  1.  OrchestratorDecision defaults: CONTINUE, reason="", max_retries=1
  2.  OrchestratorDecision RETRY stores reason and max_retries
  3.  OrchestratorDecision ABORT stores reason
  4.  _default_decide: CONTINUE on StageResult(done=False)
  5.  _default_decide: ABORT on StageResult(done=True, error="x")
  6.  SupervisorLoop: empty stage list returns state unchanged
  7.  SupervisorLoop: all stages executed in order (happy path)
  8.  SupervisorLoop: aborts when decide returns ABORT (remaining stages skipped)
  9.  SupervisorLoop: retries a stage until success (1 failure then pass)
  10. SupervisorLoop: aborts after max_retries exhausted
  11. SupervisorLoop: state mutations from one stage are visible to the next
  12. SupervisorLoop: custom decide overrides default (CONTINUE even on done=True)
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.context import AgentContext, PipelineRun
from models.task import FeatureTask


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_state(tmp_path: Path):
    from pipeline_stages import PipelineState
    run = PipelineRun(run_id="r1", feature_task_id="ft-1")
    ctx = AgentContext(
        run_id="r1",
        feature_task_id="ft-1",
        repository="org/repo",
        base_branch="main",
        workspace_path=str(tmp_path),
    )
    task = FeatureTask(
        raw_requirement="add login",
        feature_name="add-login",
        repository="org/repo",
    )
    return PipelineState(run=run, ctx=ctx, task=task)


class _FakeStage:
    """Fake StageExecutor that returns results from a pre-set queue."""

    def __init__(self, name: str, results):
        self.name = name
        self._results = iter(results)
        self.call_count = 0

    async def execute(self, state):
        self.call_count += 1
        return next(self._results)


# ── 1–3. OrchestratorDecision model ──────────────────────────────────────────

def test_orchestrator_decision_defaults():
    from models.decision import DecisionKind, OrchestratorDecision
    d = OrchestratorDecision()
    assert d.kind == DecisionKind.CONTINUE
    assert d.reason == ""
    assert d.max_retries == 1


def test_orchestrator_decision_retry_stores_fields():
    from models.decision import DecisionKind, OrchestratorDecision
    d = OrchestratorDecision(kind=DecisionKind.RETRY, reason="timeout", max_retries=3)
    assert d.kind == DecisionKind.RETRY
    assert d.reason == "timeout"
    assert d.max_retries == 3


def test_orchestrator_decision_abort_stores_reason():
    from models.decision import DecisionKind, OrchestratorDecision
    d = OrchestratorDecision(kind=DecisionKind.ABORT, reason="fatal error")
    assert d.kind == DecisionKind.ABORT
    assert d.reason == "fatal error"


# ── 4–5. _default_decide ─────────────────────────────────────────────────────

def test_default_decide_continue_on_success():
    from pipeline_stages import StageResult
    from supervisor import _default_decide
    from models.decision import DecisionKind
    result = StageResult(done=False)
    decision = _default_decide(result, "clone", 0)
    assert decision.kind == DecisionKind.CONTINUE


def test_default_decide_abort_on_done():
    from pipeline_stages import StageResult
    from supervisor import _default_decide
    from models.decision import DecisionKind
    result = StageResult(done=True, error="git failed")
    decision = _default_decide(result, "clone", 0)
    assert decision.kind == DecisionKind.ABORT
    assert "clone" in decision.reason or "git failed" in decision.reason


# ── 6. empty stages ───────────────────────────────────────────────────────────

async def test_supervisor_loop_empty_stages(tmp_path):
    from supervisor import SupervisorLoop
    state = _make_state(tmp_path)
    loop = SupervisorLoop(stages=[])
    returned = await loop.run(state)
    assert returned is state


# ── 7. happy path — all stages run in order ───────────────────────────────────

async def test_supervisor_loop_runs_all_stages_in_order(tmp_path):
    from pipeline_stages import StageResult
    from supervisor import SupervisorLoop

    order: list[str] = []

    class TrackingStage:
        def __init__(self, name):
            self.name = name

        async def execute(self, state):
            order.append(self.name)
            return StageResult()

    stages = [TrackingStage("a"), TrackingStage("b"), TrackingStage("c")]
    state = _make_state(tmp_path)
    await SupervisorLoop(stages=stages).run(state)
    assert order == ["a", "b", "c"]


# ── 8. abort skips remaining stages ──────────────────────────────────────────

async def test_supervisor_loop_aborts_on_abort_decision(tmp_path):
    from pipeline_stages import StageResult
    from supervisor import SupervisorLoop

    stage_a = _FakeStage("a", [StageResult(done=True, error="fail")])  # triggers abort
    stage_b = _FakeStage("b", [StageResult()])

    state = _make_state(tmp_path)
    await SupervisorLoop(stages=[stage_a, stage_b]).run(state)

    assert stage_a.call_count == 1
    assert stage_b.call_count == 0   # never reached


# ── 9. retry — stage fails once, succeeds on second attempt ──────────────────

async def test_supervisor_loop_retries_until_success(tmp_path):
    from pipeline_stages import StageResult
    from models.decision import DecisionKind, OrchestratorDecision
    from supervisor import SupervisorLoop

    stage = _FakeStage("a", [
        StageResult(done=True, error="transient"),  # attempt 0: fail
        StageResult(),                               # attempt 1: success
    ])

    def _decide(result, name, attempt):
        if result.done and attempt < 1:
            return OrchestratorDecision(kind=DecisionKind.RETRY, max_retries=1)
        if result.done:
            return OrchestratorDecision(kind=DecisionKind.ABORT)
        return OrchestratorDecision(kind=DecisionKind.CONTINUE)

    state = _make_state(tmp_path)
    await SupervisorLoop(stages=[stage], decide=_decide).run(state)
    assert stage.call_count == 2


# ── 10. abort after retries exhausted ────────────────────────────────────────

async def test_supervisor_loop_aborts_after_max_retries(tmp_path):
    from pipeline_stages import StageResult
    from models.decision import DecisionKind, OrchestratorDecision
    from supervisor import SupervisorLoop

    stage_a = _FakeStage("a", [
        StageResult(done=True, error="err"),
        StageResult(done=True, error="err"),
        StageResult(done=True, error="err"),
    ])
    stage_b = _FakeStage("b", [StageResult()])

    def _decide(result, name, attempt):
        if result.done:
            return OrchestratorDecision(kind=DecisionKind.RETRY, max_retries=1)
        return OrchestratorDecision(kind=DecisionKind.CONTINUE)

    state = _make_state(tmp_path)
    await SupervisorLoop(stages=[stage_a, stage_b], decide=_decide).run(state)

    assert stage_a.call_count == 2   # attempt 0 + 1 retry
    assert stage_b.call_count == 0   # never reached


# ── 11. state mutations persist across stages ─────────────────────────────────

async def test_supervisor_loop_state_mutations_persist(tmp_path):
    from pipeline_stages import StageResult
    from supervisor import SupervisorLoop

    class MutatorStage:
        name = "mutator"
        async def execute(self, state):
            state.extra["key"] = "set-by-stage-1"
            return StageResult()

    class ReaderStage:
        name = "reader"
        async def execute(self, state):
            assert state.extra.get("key") == "set-by-stage-1"
            return StageResult()

    state = _make_state(tmp_path)
    await SupervisorLoop(stages=[MutatorStage(), ReaderStage()]).run(state)
    assert state.extra["key"] == "set-by-stage-1"


# ── 12. custom decide overrides default ──────────────────────────────────────

async def test_supervisor_loop_custom_decide_overrides_default(tmp_path):
    from pipeline_stages import StageResult
    from models.decision import DecisionKind, OrchestratorDecision
    from supervisor import SupervisorLoop

    # Stage signals done=True, but custom decide says CONTINUE anyway
    stage_a = _FakeStage("a", [StageResult(done=True, error="non-fatal")])
    stage_b = _FakeStage("b", [StageResult()])

    def _always_continue(result, name, attempt):
        return OrchestratorDecision(kind=DecisionKind.CONTINUE)

    state = _make_state(tmp_path)
    await SupervisorLoop(stages=[stage_a, stage_b], decide=_always_continue).run(state)

    assert stage_a.call_count == 1
    assert stage_b.call_count == 1   # reached because we CONTINUE'd
