"""
tests/unit/test_error_classification.py — PR8 Failure Classification (written FIRST)

Red phase: all tests must FAIL before implementation is written.

Covers:
  I.   FailureCategory enum values
  II.  RecoveryAction enum values
  III. ClassifiedError model defaults and construction
  IV.  classify_failure() — exception-type mapping
  V.   classify_failure() — message-pattern matching
  VI.  classify_failure() — stage-context hints
  VII. classify_failure() — edge cases
  VIII.StageResult with classified_error
  IX.  OrchestratorDecision with classified_error
  X.   PipelineRun with classified_errors
  XI.  _default_decide with classified_error (category-driven)
  XII. _default_decide backward compat (no classified_error)
  XIII.Integration: stage exception → classify → decision
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def _make_state(tmp_path: Path):
    from pipeline_stages import PipelineState
    return PipelineState(run=_run(), ctx=_ctx(tmp_path), task=_feature_task())


class _FakeStage:
    """Fake StageExecutor that returns results from a pre-set queue."""

    def __init__(self, name: str, results):
        self.name = name
        self._results = iter(results)
        self.call_count = 0

    async def execute(self, state):
        self.call_count += 1
        return next(self._results)


# ── I. FailureCategory enum ──────────────────────────────────────────────────

def test_failure_category_values():
    from models.errors import FailureCategory
    assert FailureCategory.TRANSIENT.value == "transient"
    assert FailureCategory.PERMANENT.value == "permanent"
    assert FailureCategory.CONFIG.value == "config"
    assert FailureCategory.EXTERNAL.value == "external"
    assert FailureCategory.RESOURCE.value == "resource"
    assert FailureCategory.UNKNOWN.value == "unknown"


def test_failure_category_is_str_enum():
    from models.errors import FailureCategory
    assert isinstance(FailureCategory.TRANSIENT, str)


# ── II. RecoveryAction enum ──────────────────────────────────────────────────

def test_recovery_action_values():
    from models.errors import RecoveryAction
    assert RecoveryAction.RETRY.value == "retry"
    assert RecoveryAction.SKIP.value == "skip"
    assert RecoveryAction.ABORT.value == "abort"
    assert RecoveryAction.FALLBACK.value == "fallback"


# ── III. ClassifiedError model ───────────────────────────────────────────────

def test_classified_error_minimal():
    from models.errors import FailureCategory, ClassifiedError
    e = ClassifiedError(
        category=FailureCategory.TRANSIENT,
        source="clone",
        message="timeout",
    )
    assert e.category == FailureCategory.TRANSIENT
    assert e.source == "clone"
    assert e.message == "timeout"
    assert e.exception_type == ""
    assert e.recovery_hint == ""
    assert e.raw_error is None


def test_classified_error_full():
    from models.errors import FailureCategory, ClassifiedError
    e = ClassifiedError(
        category=FailureCategory.CONFIG,
        source="context",
        message="API key missing",
        exception_type="ValueError",
        recovery_hint="Set ANTHROPIC_API_KEY",
        raw_error="ValueError: API key missing at line 42",
    )
    assert e.exception_type == "ValueError"
    assert e.recovery_hint == "Set ANTHROPIC_API_KEY"
    assert e.raw_error == "ValueError: API key missing at line 42"


def test_classified_error_is_serializable():
    from models.errors import FailureCategory, ClassifiedError
    e = ClassifiedError(category=FailureCategory.UNKNOWN, source="test", message="x")
    d = e.model_dump()
    assert d["category"] == "unknown"
    assert d["source"] == "test"
    assert d["message"] == "x"


# ── IV. classify_failure() — exception-type mapping ──────────────────────────

@pytest.mark.parametrize("exc, expected_category, hint_contains", [
    (asyncio.TimeoutError(),             "transient",  "timed"),
    (TimeoutError(),                     "transient",  "timed"),
    (ConnectionRefusedError(),           "transient",  "Network"),
    (ConnectionError("refused"),         "transient",  "Network"),
    (SyntaxError("bad syntax"),          "permanent",  "syntax"),
    (json.JSONDecodeError("msg", "doc", 0), "permanent", "JSON"),
    (ValueError("bad value"),            "permanent",  ""),
    (PermissionError("denied"),          "config",     "permission"),
    (FileNotFoundError("no file"),       "config",     "not found"),
    (MemoryError("oom"),                 "resource",   "memory"),
])
def test_classify_failure_by_exception_type(exc, expected_category, hint_contains):
    from models.errors import classify_failure
    result = classify_failure(exc, message=str(exc), stage="test")
    assert result.category.value == expected_category
    if hint_contains:
        assert hint_contains.lower() in result.recovery_hint.lower()
    assert result.exception_type == type(exc).__qualname__


def test_classify_failure_merge_conflict_error():
    """MergeConflictError(RuntimeError) → PERMANENT (matched by class name)."""
    from agents.github_agent.agent import MergeConflictError
    from models.errors import classify_failure
    exc = MergeConflictError("conflict on features/auth.py")
    result = classify_failure(exc, message=str(exc), stage="integrate")
    assert result.category.value == "permanent"


def test_classify_failure_oserror_disk_full_is_resource():
    """OSError with errno 28 (ENOSPC) → RESOURCE."""
    from models.errors import classify_failure
    exc = OSError(28, "No space left on device")
    result = classify_failure(exc, message=str(exc), stage="clone")
    assert result.category.value == "resource"


def test_classify_failure_runtime_error_with_git_message_is_external():
    """RuntimeError with git-related message → EXTERNAL."""
    from models.errors import classify_failure
    exc = RuntimeError("git clone failed: remote repository not found")
    result = classify_failure(exc, message=str(exc), stage="clone")
    assert result.category.value == "external"


# ── V. classify_failure() — message-pattern matching ─────────────────────────

@pytest.mark.parametrize("message, expected_category", [
    ("claude CLI timed out after 300s",    "transient"),
    ("rate limit exceeded",                "transient"),
    ("HTTP 429 Too Many Requests",         "transient"),
    ("connection refused",                 "transient"),
    ("503 Service Unavailable",            "transient"),
    ("temporarily unavailable",            "transient"),
    ("deadline exceeded",                  "transient"),
    ("invalid syntax in generated code",   "permanent"),
    ("JSON decode error",                  "permanent"),
    ("not a valid response",               "permanent"),
    ("unexpected token in output",         "permanent"),
    ("unauthorized: bad API key",          "config"),
    ("permission denied on workspace",     "config"),
    ("missing config: model not set",      "config"),
    ("disk full on device",                "resource"),
    ("out of memory",                      "resource"),
    ("cannot allocate memory",             "resource"),
    ("git clone failed: remote hung up",   "external"),
    ("CI check failed: build error",       "external"),
    ("something completely unexpected",    "unknown"),
    ("",                                   "unknown"),
])
def test_classify_failure_by_message(message, expected_category):
    from models.errors import classify_failure
    result = classify_failure(message=message, stage="test")
    assert result.category.value == expected_category


def test_classify_failure_message_case_insensitive():
    from models.errors import classify_failure
    assert classify_failure(message="TIMED OUT", stage="x").category.value == "transient"
    assert classify_failure(message="Auth Failed 401", stage="x").category.value == "config"


# ── VI. classify_failure() — stage-context hints ─────────────────────────────

def test_classify_failure_clone_stage_runtimeerror_is_external():
    from models.errors import classify_failure
    exc = RuntimeError("operation failed")
    result = classify_failure(exc, message=str(exc), stage="clone")
    assert result.category.value == "external"


def test_classify_failure_context_stage_runtimeerror_is_config():
    from models.errors import classify_failure
    exc = RuntimeError("model not found")
    result = classify_failure(exc, message=str(exc), stage="context")
    assert result.category.value == "config"


def test_classify_failure_orchestrate_stage_runtimeerror_is_permanent():
    from models.errors import classify_failure
    exc = RuntimeError("decomposition failed")
    result = classify_failure(exc, message=str(exc), stage="orchestrate")
    assert result.category.value == "permanent"


def test_classify_failure_unknown_stage_runtimeerror_is_unknown():
    from models.errors import classify_failure
    exc = RuntimeError("something weird happened")
    result = classify_failure(exc, message=str(exc), stage="unknown-stage")
    # RuntimeError not in any explicit mapping → falls through to message
    # "something weird happened" matches no pattern → UNKNOWN
    assert result.category.value == "unknown"


# ── VII. classify_failure() — edge cases ─────────────────────────────────────

def test_classify_failure_no_exception_no_message():
    from models.errors import classify_failure
    result = classify_failure()
    assert result.category.value == "unknown"
    assert result.source == ""


def test_classify_failure_none_message():
    from models.errors import classify_failure
    result = classify_failure(message="", stage="x")
    assert result.category.value == "unknown"


def test_classify_failure_exception_type_always_set():
    from models.errors import classify_failure
    exc = ValueError("bad")
    result = classify_failure(exc, message=str(exc))
    assert result.exception_type == "ValueError"


def test_classify_failure_exception_priority_over_message():
    """Exception type takes precedence over message patterns."""
    from models.errors import classify_failure
    # TimeoutError → TRANSIENT even if message contains "syntax error"
    exc = TimeoutError("syntax check timed out")
    result = classify_failure(exc, message=str(exc), stage="test")
    assert result.category.value == "transient"


def test_classify_failure_message_pattern_priority_over_stage_hint():
    """Message matching takes precedence over stage-based hints."""
    from models.errors import classify_failure
    # "rate limit" in message → TRANSIENT, even though stage="context" would suggest CONFIG
    exc = RuntimeError("rate limit exceeded")
    result = classify_failure(exc, message=str(exc), stage="context")
    assert result.category.value == "transient"


# ── VIII. StageResult with classified_error ──────────────────────────────────

def test_stage_result_has_classified_error_default_none():
    from pipeline_stages import StageResult
    r = StageResult()
    assert r.classified_error is None


def test_stage_result_with_classified_error():
    from pipeline_stages import StageResult
    from models.errors import FailureCategory, ClassifiedError
    ce = ClassifiedError(
        category=FailureCategory.EXTERNAL,
        source="clone",
        message="git clone failed",
    )
    r = StageResult(done=True, error="git failed", classified_error=ce)
    assert r.classified_error is ce
    assert r.classified_error.category == FailureCategory.EXTERNAL


# ── IX. OrchestratorDecision with classified_error ───────────────────────────

def test_orchestrator_decision_classified_error_default_none():
    from models.decision import OrchestratorDecision
    d = OrchestratorDecision()
    assert d.classified_error is None


def test_orchestrator_decision_with_classified_error():
    from models.decision import DecisionKind, OrchestratorDecision
    from models.errors import FailureCategory, ClassifiedError
    ce = ClassifiedError(
        category=FailureCategory.TRANSIENT,
        source="clone",
        message="timeout",
    )
    d = OrchestratorDecision(kind=DecisionKind.RETRY, reason="retry", classified_error=ce)
    assert d.classified_error is ce
    assert d.classified_error.category == FailureCategory.TRANSIENT


# ── X. PipelineRun with classified_errors ────────────────────────────────────

def test_pipeline_run_classified_errors_default_empty():
    run = PipelineRun(run_id="r1", feature_task_id="ft-1")
    assert run.classified_errors == []


def test_pipeline_run_can_append_classified_error():
    from models.errors import FailureCategory, ClassifiedError
    run = PipelineRun(run_id="r1", feature_task_id="ft-1")
    ce = ClassifiedError(
        category=FailureCategory.CONFIG,
        source="context",
        message="API key missing",
    )
    run.classified_errors.append(ce)
    assert len(run.classified_errors) == 1
    assert run.classified_errors[0].category == FailureCategory.CONFIG


# ── XI. _default_decide with classified_error (category-driven) ──────────────

def test_default_decide_transient_returns_retry():
    from pipeline_stages import StageResult
    from models.decision import DecisionKind
    from models.errors import FailureCategory, ClassifiedError
    from supervisor import _default_decide

    ce = ClassifiedError(
        category=FailureCategory.TRANSIENT,
        source="clone",
        message="timeout",
    )
    result = StageResult(done=True, error="timeout", classified_error=ce)
    decision = _default_decide(result, "clone", 0)
    assert decision.kind == DecisionKind.RETRY
    assert decision.max_retries == 3
    assert decision.classified_error is ce


def test_default_decide_permanent_returns_abort():
    from pipeline_stages import StageResult
    from models.decision import DecisionKind
    from models.errors import FailureCategory, ClassifiedError
    from supervisor import _default_decide

    ce = ClassifiedError(
        category=FailureCategory.PERMANENT,
        source="orchestrate",
        message="invalid JSON",
    )
    result = StageResult(done=True, error="invalid JSON", classified_error=ce)
    decision = _default_decide(result, "orchestrate", 0)
    assert decision.kind == DecisionKind.ABORT
    assert "permanent" in decision.reason.lower()
    assert decision.classified_error is ce


def test_default_decide_config_returns_abort():
    from pipeline_stages import StageResult
    from models.decision import DecisionKind
    from models.errors import FailureCategory, ClassifiedError
    from supervisor import _default_decide

    ce = ClassifiedError(
        category=FailureCategory.CONFIG,
        source="context",
        message="API key missing",
    )
    result = StageResult(done=True, error="API key missing", classified_error=ce)
    decision = _default_decide(result, "context", 0)
    assert decision.kind == DecisionKind.ABORT
    assert decision.classified_error is ce


def test_default_decide_external_returns_abort():
    from pipeline_stages import StageResult
    from models.decision import DecisionKind
    from models.errors import FailureCategory, ClassifiedError
    from supervisor import _default_decide

    ce = ClassifiedError(
        category=FailureCategory.EXTERNAL,
        source="clone",
        message="git clone failed",
    )
    result = StageResult(done=True, error="clone failed", classified_error=ce)
    decision = _default_decide(result, "clone", 0)
    assert decision.kind == DecisionKind.ABORT


def test_default_decide_resource_returns_abort():
    from pipeline_stages import StageResult
    from models.decision import DecisionKind
    from models.errors import FailureCategory, ClassifiedError
    from supervisor import _default_decide

    ce = ClassifiedError(
        category=FailureCategory.RESOURCE,
        source="worker",
        message="out of memory",
    )
    result = StageResult(done=True, error="oom", classified_error=ce)
    decision = _default_decide(result, "worker", 0)
    assert decision.kind == DecisionKind.ABORT


# ── XII. _default_decide backward compat (no classified_error) ────────────────

def test_default_decide_no_classified_error_still_aborts_on_done():
    """Old StageResult without classified_error → ABORT (backward compat)."""
    from pipeline_stages import StageResult
    from models.decision import DecisionKind
    from supervisor import _default_decide

    result = StageResult(done=True, error="git failed")
    decision = _default_decide(result, "clone", 0)
    assert decision.kind == DecisionKind.ABORT


def test_default_decide_no_classified_error_continue_on_success():
    """Success result unchanged."""
    from pipeline_stages import StageResult
    from models.decision import DecisionKind
    from supervisor import _default_decide

    result = StageResult(done=False)
    decision = _default_decide(result, "clone", 0)
    assert decision.kind == DecisionKind.CONTINUE


# ── XIII. Integration: stage exception → classify → decision ─────────────────

async def test_clone_stage_populates_classified_error_on_failure(tmp_path):
    """CloneStage exception → StageResult.classified_error is set."""
    from pipeline_stages import CloneStage, PipelineState
    from models.errors import FailureCategory

    state = _make_state(tmp_path)

    with patch("pipeline_stages._clone_repo", side_effect=RuntimeError("git clone failed")):
        result = await CloneStage().execute(state)

    assert result.done is True
    assert result.classified_error is not None
    assert result.classified_error.category == FailureCategory.EXTERNAL
    assert result.classified_error.source == "clone"
    assert len(state.run.classified_errors) == 1


async def test_supervisor_retries_transient_and_aborts_permanent(tmp_path):
    """Integration: TRANSIENT → retry, PERMANENT → abort with SupervisorLoop."""
    from pipeline_stages import StageResult
    from models.errors import FailureCategory, ClassifiedError
    from supervisor import SupervisorLoop

    # Stage that fails with TRANSIENT on attempt 0, succeeds on attempt 1
    transient_ce = ClassifiedError(
        category=FailureCategory.TRANSIENT, source="a", message="timeout"
    )
    stage_a = _FakeStage("a", [
        StageResult(done=True, error="timeout", classified_error=transient_ce),
        StageResult(),  # success on retry
    ])

    state = _make_state(tmp_path)
    loop = SupervisorLoop(stages=[stage_a])
    await loop.run(state)
    assert stage_a.call_count == 2  # retried once and succeeded

    # Stage that fails with PERMANENT → should abort immediately
    permanent_ce = ClassifiedError(
        category=FailureCategory.PERMANENT, source="b", message="syntax error"
    )
    stage_b = _FakeStage("b", [
        StageResult(done=True, error="syntax error", classified_error=permanent_ce),
        StageResult(),
    ])

    state2 = _make_state(tmp_path)
    loop2 = SupervisorLoop(stages=[stage_b])
    await loop2.run(state2)
    assert stage_b.call_count == 1  # aborted on first failure, no retry
