"""
tests/unit/test_log_stats.py

Unit tests for telemetry/log_stats.py.
Pure in-memory / tmp-file tests — no network, no CLI, no real Agent calls.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from telemetry.log_stats import (
    classify_error,
    format_stats_summary,
    group_records_by_run,
    summarize_all_runs,
    summarize_run_status,
)


# ── fixtures / helpers ────────────────────────────────────────────────────────

def _r(event: str, run_id: str = "run1", ts: str = "2026-06-25T10:00:00+00:00",
       **kw) -> dict:
    base = {
        "run_id": run_id,
        "task_id": "t1",
        "role": "worker",
        "agent": "claude-code",
        "event": event,
        "success": True,
        "timestamp": ts,
    }
    base.update(kw)
    return base


def _completed(run_id: str = "run1", success: bool = True,
               stage: str = "done", ts: str = "2026-06-25T10:01:00+00:00") -> dict:
    return _r("pipeline_completed", run_id=run_id, success=success, ts=ts,
               agent="", role="",
               metadata={"stage": stage, "pr_url": "", "ci_passed": success,
                         "debug_retry_count": 0, "error_count": 0})


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# I. group_records_by_run
# ═══════════════════════════════════════════════════════════════════════════════

def test_group_by_run_basic():
    recs = [
        _r("agent_result", run_id="a"),
        _r("agent_result", run_id="b"),
        _r("integration_result", run_id="a"),
    ]
    groups = group_records_by_run(recs)
    assert set(groups.keys()) == {"a", "b"}
    assert len(groups["a"]) == 2
    assert len(groups["b"]) == 1


def test_group_ignores_missing_run_id():
    """Records without run_id must be silently ignored"""
    recs = [
        _r("agent_result", run_id="x"),
        {"event": "agent_result", "success": True},        # no run_id key
        {"event": "agent_result", "run_id": "", "success": True},  # empty
    ]
    groups = group_records_by_run(recs)
    assert list(groups.keys()) == ["x"]
    assert len(groups["x"]) == 1


def test_group_run_sorted_by_timestamp():
    recs = [
        _r("agent_result", run_id="r", ts="2026-06-25T10:00:03+00:00"),
        _r("pipeline_completed", run_id="r", ts="2026-06-25T10:00:01+00:00"),
        _r("integration_result", run_id="r", ts="2026-06-25T10:00:02+00:00"),
    ]
    groups = group_records_by_run(recs)
    ts = [e["timestamp"] for e in groups["r"]]
    assert ts == sorted(ts)


def test_group_does_not_mutate_input():
    recs = [_r("agent_result", run_id="r")]
    original_ids = [id(r) for r in recs]
    group_records_by_run(recs)
    assert [id(r) for r in recs] == original_ids


# ═══════════════════════════════════════════════════════════════════════════════
# II. classify_error
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("error,expected", [
    ("claude CLI timed out after 300s", "timeout"),
    ("process timed out waiting", "timeout"),
    ("操作超时，请重试", "timeout"),
    ("HTTP 401 Unauthorized", "auth"),
    ("permission denied accessing resource", "auth"),
    ("CODEX_MODEL is not configured", "codex_config"),
    ("REVIEW_AGENT_BACKEND=codex not working", "codex_config"),
    ("codex exec failed: 403", "codex_config"),
    ("integration tests failed: 3 errors", "integration_test"),
    ("pytest tests failed (exit=1)", "integration_test"),
    ("AssertionError: value mismatch", "integration_test"),
    ("empty merged diff for task", "empty_patch"),
    ("all worker patches may have failed", "empty_patch"),
    ("aggregation failed for task", "aggregation_failed"),
    ("review blocked after 2 retries", "review_blocked"),
    ("Review blocked: missing tests", "review_blocked"),
    ("ci failed after retries", "ci_failed"),
    ("failed checks: lint", "ci_failed"),
    ("some totally unknown error XYZ", "unknown"),
    ("", "unknown"),
])
def test_classify_error(error, expected):
    assert classify_error(error) == expected


def test_classify_error_case_insensitive():
    assert classify_error("TIMED OUT") == "timeout"
    assert classify_error("Auth Failed") == "auth"


def test_classify_error_empty_string():
    assert classify_error("") == "unknown"


def test_classify_error_none_type_returns_unknown():
    assert classify_error(None) == "unknown"   # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════════
# III. summarize_run_status
# ═══════════════════════════════════════════════════════════════════════════════

def test_summarize_run_status_empty():
    s = summarize_run_status([])
    assert s["run_id"] == ""
    assert s["success"] is None
    assert s["failed_agents"] == []


def test_summarize_run_status_success():
    recs = [_r("agent_result"), _completed()]
    s = summarize_run_status(recs)
    assert s["success"] is True
    assert s["final_stage"] == "done"
    assert s["event_count"] == 2


def test_summarize_run_status_failed_agents():
    recs = [
        _r("agent_result", success=False, agent="claude-code",
           error="timeout"),
        _r("agent_result", success=False, agent="codex",
           error="auth"),
        _r("agent_result", success=True, agent="claude-code"),
        _completed(),
    ]
    s = summarize_run_status(recs)
    assert "claude-code" in s["failed_agents"]
    assert "codex" in s["failed_agents"]
    assert s["failed_agent_count"] == 2


def test_summarize_run_status_failed_agents_deduplicated():
    """Same agent failing twice → listed once"""
    recs = [
        _r("agent_result", success=False, agent="claude-code", error="timeout"),
        _r("agent_result", success=False, agent="claude-code", error="timeout"),
        _completed(),
    ]
    s = summarize_run_status(recs)
    assert s["failed_agents"] == ["claude-code"]


def test_summarize_run_status_error_categories_deduplicated():
    """Same error category from two events → listed once"""
    recs = [
        _r("agent_result", success=False, error="timed out"),
        _r("integration_result", success=False, error="claude CLI timed out"),
        _completed(success=False),
    ]
    s = summarize_run_status(recs)
    cats = s["error_categories"]
    assert cats.count("timeout") == 1


def test_summarize_run_status_timestamps():
    recs = [
        _r("agent_result", ts="2026-06-25T10:00:01+00:00"),
        _completed(ts="2026-06-25T10:00:05+00:00"),
    ]
    s = summarize_run_status(recs)
    assert s["first_timestamp"] == "2026-06-25T10:00:01+00:00"
    assert s["last_timestamp"] == "2026-06-25T10:00:05+00:00"


# ═══════════════════════════════════════════════════════════════════════════════
# IV. summarize_all_runs
# ═══════════════════════════════════════════════════════════════════════════════

def _make_runs(*tuples) -> list[dict]:
    """Build records for multiple runs from (run_id, success, error_str) tuples."""
    recs = []
    for run_id, success, err in tuples:
        if err:
            recs.append(_r("agent_result", run_id=run_id, success=False,
                           agent="claude-code", error=err))
        recs.append(_completed(run_id=run_id, success=success))
    return recs


def test_summarize_all_runs_counts():
    recs = _make_runs(
        ("r1", True, ""),
        ("r2", True, ""),
        ("r3", False, "timed out"),
    )
    s = summarize_all_runs(recs)
    assert s["total_runs"] == 3
    assert s["success_runs"] == 2
    assert s["failed_runs"] == 1
    assert s["unknown_runs"] == 0


def test_summarize_all_runs_empty():
    s = summarize_all_runs([])
    assert s["total_runs"] == 0
    assert s["failure_rate"] == 0.0
    assert s["recent_runs"] == []


def test_summarize_all_runs_zero_total_failure_rate():
    s = summarize_all_runs([])
    assert s["failure_rate"] == 0.0


def test_summarize_all_runs_failure_rate():
    recs = _make_runs(
        ("r1", True, ""),
        ("r2", False, "timeout"),
    )
    s = summarize_all_runs(recs)
    assert s["failure_rate"] == 0.5


def test_summarize_all_runs_unknown_run():
    """Run with no pipeline_completed → unknown"""
    recs = [_r("agent_result", run_id="rx")]   # no pipeline_completed
    s = summarize_all_runs(recs)
    assert s["unknown_runs"] == 1
    assert s["success_runs"] == 0
    assert s["failed_runs"] == 0


def test_summarize_all_runs_failures_by_stage():
    recs = _make_runs(
        ("r1", False, ""),
        ("r2", False, ""),
        ("r3", True, ""),
    )
    # both failed runs have final_stage="done"
    s = summarize_all_runs(recs)
    assert s["failures_by_stage"].get("done", 0) == 2


def test_summarize_all_runs_failures_by_agent():
    recs = [
        _r("agent_result", run_id="r1", success=False, agent="claude-code",
           error="timeout"),
        _r("agent_result", run_id="r1", success=False, agent="claude-code",
           error="timeout"),
        _r("agent_result", run_id="r2", success=False, agent="codex",
           error="auth"),
        _completed(run_id="r1", success=False),
        _completed(run_id="r2", success=False),
    ]
    s = summarize_all_runs(recs)
    assert s["failures_by_agent"]["claude-code"] == 2
    assert s["failures_by_agent"]["codex"] == 1


def test_summarize_all_runs_runs_with_error_category():
    recs = _make_runs(
        ("r1", False, "timed out"),
        ("r2", False, "timed out"),
        ("r3", False, "review blocked"),
    )
    s = summarize_all_runs(recs)
    assert s["runs_with_error_category"]["timeout"] == 2
    assert s["runs_with_error_category"]["review_blocked"] == 1


def test_summarize_all_runs_recent_runs_sorted_desc():
    recs = [
        _r("agent_result", run_id="r1", ts="2026-06-25T10:00:00+00:00"),
        _completed(run_id="r1", ts="2026-06-25T10:01:00+00:00"),
        _r("agent_result", run_id="r2", ts="2026-06-25T11:00:00+00:00"),
        _completed(run_id="r2", ts="2026-06-25T11:01:00+00:00"),
        _r("agent_result", run_id="r3", ts="2026-06-25T09:00:00+00:00"),
        _completed(run_id="r3", ts="2026-06-25T09:01:00+00:00"),
    ]
    s = summarize_all_runs(recs)
    recent_ids = [r["run_id"] for r in s["recent_runs"]]
    assert recent_ids[0] == "r2"   # most recent last_timestamp
    assert recent_ids[-1] == "r3"  # oldest


def test_summarize_all_runs_recent_limit():
    recs = []
    for i in range(15):
        rid = f"r{i:02d}"
        recs.extend([
            _r("agent_result", run_id=rid, ts=f"2026-06-25T{i:02d}:00:00+00:00"),
            _completed(run_id=rid, ts=f"2026-06-25T{i:02d}:01:00+00:00"),
        ])
    s = summarize_all_runs(recs, recent_limit=5)
    assert len(s["recent_runs"]) == 5


# ═══════════════════════════════════════════════════════════════════════════════
# V. format_stats_summary
# ═══════════════════════════════════════════════════════════════════════════════

def test_format_stats_summary_empty():
    out = format_stats_summary(summarize_all_runs([]))
    assert "No execution records found" in out


def test_format_stats_summary_shows_total():
    recs = _make_runs(("r1", True, ""), ("r2", False, "timeout"))
    out = format_stats_summary(summarize_all_runs(recs))
    assert "Total runs: 2" in out
    assert "Success: 1" in out
    assert "Failed: 1" in out


def test_format_stats_summary_shows_failure_rate():
    recs = _make_runs(("r1", True, ""), ("r2", False, "timeout"))
    out = format_stats_summary(summarize_all_runs(recs))
    assert "50.0%" in out


def test_format_stats_summary_shows_categories():
    recs = _make_runs(("r1", False, "timed out"), ("r2", False, "review blocked"))
    out = format_stats_summary(summarize_all_runs(recs))
    assert "timeout" in out
    assert "review_blocked" in out


def test_format_stats_summary_shows_recent_runs():
    recs = _make_runs(("r1", True, ""))
    out = format_stats_summary(summarize_all_runs(recs))
    assert "Recent runs" in out
    assert "r1" in out


def test_format_stats_summary_no_raw_error_text():
    """Output must not contain raw error messages — only categories and counts"""
    recs = [
        _r("agent_result", run_id="r1", success=False,
           error="OPENAI_API_KEY=sk-secret timed out"),
        _completed(run_id="r1", success=False),
    ]
    out = format_stats_summary(summarize_all_runs(recs))
    assert "sk-secret" not in out
    assert "OPENAI_API_KEY" not in out
    # category name is allowed (it's safe)
    assert "timeout" in out


def test_format_stats_summary_no_prompt():
    """prompt field must never appear in stats output"""
    recs = [
        {**_r("agent_result", run_id="r1"), "prompt": "DO NOT SHOW"},
        _completed(run_id="r1"),
    ]
    out = format_stats_summary(summarize_all_runs(recs))
    assert "DO NOT SHOW" not in out


# ═══════════════════════════════════════════════════════════════════════════════
# VI. CLI smoke test
# ═══════════════════════════════════════════════════════════════════════════════

def test_cli_with_path(tmp_path):
    import subprocess, sys
    p = tmp_path / "exec.jsonl"
    recs = _make_runs(("r1", True, ""), ("r2", False, "timeout"))
    p.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "telemetry.log_stats", "--path", str(p)],
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(Path(__file__).parent.parent.parent),
    )
    assert result.returncode == 0
    assert "Total runs:" in result.stdout
    assert "2" in result.stdout


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 8F: Router decision statistics
# ═══════════════════════════════════════════════════════════════════════════════

def _router_rec(run_id: str = "r1", success: bool = True,
                active: bool = False, dry_run: bool = True,
                fallback: bool = False, selected: str = "claude-code",
                applied: str = "claude-code", preferred: str = "codex",
                error: str | None = None, ts: str = "2026-06-25T10:00:00+00:00") -> dict:
    return {
        "run_id": run_id,
        "event": "router_decision",
        "agent": "router",
        "role": "reviewer",
        "success": success,
        "error": error,
        "timestamp": ts,
        "metadata": {
            "active": active,
            "dry_run": dry_run,
            "fallback_used": fallback,
            "selected_backend": selected,
            "applied_backend": applied,
            "preferred_backend": preferred,
            "stats_enabled": False,
            "failure_categories": [],
            "reason": "test",
        },
    }


# ── summarize_all_runs: router fields always present ─────────────────────────

def test_router_fields_present_when_no_router_events():
    """All router stats fields exist even when no router_decision events exist"""
    recs = _make_runs(("r1", True, ""))
    s = summarize_all_runs(recs)
    for key in ("router_decisions_total", "router_decisions_failed",
                "router_active_count", "router_dry_run_count",
                "router_fallback_count", "router_selected_backend_counts",
                "router_applied_backend_counts", "router_preferred_to_applied",
                "router_errors_by_category"):
        assert key in s, f"missing router key: {key}"
    assert s["router_decisions_total"] == 0


def test_router_decisions_total():
    recs = [_router_rec("r1"), _router_rec("r1"), _completed("r1")]
    s = summarize_all_runs(recs)
    assert s["router_decisions_total"] == 2


def test_router_decisions_failed():
    recs = [_router_rec("r1", success=True), _router_rec("r1", success=False),
            _completed("r1")]
    s = summarize_all_runs(recs)
    assert s["router_decisions_failed"] == 1


def test_router_active_count():
    recs = [_router_rec("r1", active=True, dry_run=False),
            _router_rec("r1", active=False, dry_run=True),
            _completed("r1")]
    s = summarize_all_runs(recs)
    assert s["router_active_count"] == 1
    assert s["router_dry_run_count"] == 1


def test_router_fallback_count():
    recs = [_router_rec("r1", fallback=True), _router_rec("r1", fallback=False),
            _completed("r1")]
    s = summarize_all_runs(recs)
    assert s["router_fallback_count"] == 1


def test_router_selected_backend_counts():
    recs = [_router_rec("r1", selected="claude-code"),
            _router_rec("r1", selected="claude-code"),
            _router_rec("r1", selected="codex"),
            _completed("r1")]
    s = summarize_all_runs(recs)
    assert s["router_selected_backend_counts"]["claude-code"] == 2
    assert s["router_selected_backend_counts"]["codex"] == 1


def test_router_applied_backend_counts():
    recs = [_router_rec("r1", applied="claude-code"),
            _router_rec("r1", applied="codex"),
            _completed("r1")]
    s = summarize_all_runs(recs)
    assert s["router_applied_backend_counts"]["claude-code"] == 1
    assert s["router_applied_backend_counts"]["codex"] == 1


def test_router_preferred_to_applied():
    recs = [_router_rec("r1", preferred="codex", applied="claude-code"),
            _router_rec("r1", preferred="codex", applied="claude-code"),
            _router_rec("r1", preferred="claude-code", applied="codex"),
            _completed("r1")]
    s = summarize_all_runs(recs)
    assert s["router_preferred_to_applied"]["codex->claude-code"] == 2
    assert s["router_preferred_to_applied"]["claude-code->codex"] == 1


def test_router_non_string_backend_ignored():
    """Non-string backend values must not appear in counts"""
    rec = _router_rec("r1")
    rec["metadata"]["selected_backend"] = 42    # non-string
    rec["metadata"]["applied_backend"] = None   # non-string
    recs = [rec, _completed("r1")]
    s = summarize_all_runs(recs)
    assert 42 not in s["router_selected_backend_counts"]
    assert None not in s["router_applied_backend_counts"]


def test_router_error_classified_not_raw():
    """Router decision error → only category stored, not raw text"""
    recs = [_router_rec("r1", success=False, error="connection timed out"),
            _completed("r1")]
    s = summarize_all_runs(recs)
    assert "timeout" in s["router_errors_by_category"]
    # Raw error text must NOT appear in the stats dict
    assert "connection timed out" not in str(s)


# ── summarize_run_status: per-run router fields ───────────────────────────────

def test_summarize_run_status_router_decisions_count():
    recs = [_router_rec("r1"), _router_rec("r1"), _completed("r1")]
    s = summarize_run_status(recs)
    assert s["router_decisions_count"] == 2


def test_summarize_run_status_router_last_fields():
    recs = [
        _router_rec("r1", selected="codex", applied="claude-code", fallback=True,
                    ts="2026-06-25T10:00:01+00:00"),
        _router_rec("r1", selected="claude-code", applied="claude-code", fallback=False,
                    ts="2026-06-25T10:00:02+00:00"),
        _completed("r1"),
    ]
    s = summarize_run_status(recs)
    # "last" must be the record with the later timestamp
    assert s["router_last_selected_backend"] == "claude-code"
    assert s["router_last_applied_backend"] == "claude-code"
    assert s["router_last_fallback_used"] is False


def test_summarize_run_status_no_router_events():
    recs = [_r("agent_result", run_id="r1"), _completed("r1")]
    s = summarize_run_status(recs)
    assert s["router_decisions_count"] == 0
    assert s["router_last_selected_backend"] is None


# ── format_stats_summary: Router section ─────────────────────────────────────

def test_format_stats_summary_router_section_present():
    recs = [_router_rec("r1"), _completed("r1")]
    out = format_stats_summary(summarize_all_runs(recs))
    assert "Router decisions" in out
    assert "Total:" in out


def test_format_stats_summary_no_router_section_when_zero():
    """When no router_decisions, the Router section is omitted"""
    recs = _make_runs(("r1", True, ""))
    out = format_stats_summary(summarize_all_runs(recs))
    assert "Router decisions:" not in out


def test_format_stats_summary_router_no_raw_error():
    """Router error text must not appear in output"""
    recs = [_router_rec("r1", success=False, error="sk-secret timed out"),
            _completed("r1")]
    out = format_stats_summary(summarize_all_runs(recs))
    assert "sk-secret" not in out
    assert "timed out" not in out


# ── P3 fix: empty router stats isolation ─────────────────────────────────────

def test_empty_router_stats_nested_dicts_are_independent():
    """
    Two calls to summarize_all_runs with no router events must return
    independent dicts — mutating one must not affect the other.
    """
    recs = _make_runs(("r1", True, ""))
    s1 = summarize_all_runs(recs)
    s2 = summarize_all_runs(recs)

    # Mutate a nested dict in s1
    s1["router_selected_backend_counts"]["injected"] = 99

    # s2 must be unaffected
    assert "injected" not in s2["router_selected_backend_counts"]
    assert s2["router_selected_backend_counts"] == {}
