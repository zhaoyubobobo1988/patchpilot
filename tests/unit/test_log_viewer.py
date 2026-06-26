"""
tests/unit/test_log_viewer.py

Unit tests for telemetry/log_viewer.py.
Pure in-memory / tmp-file tests — no network, no CLI, no real Agent calls.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from telemetry.log_viewer import (
    filter_records_by_run,
    format_run_summary,
    load_execution_records,
    summarize_run,
    view_run,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def _rec(event: str, run_id: str = "run1", **kw) -> dict:
    base = {
        "run_id": run_id,
        "task_id": "t1",
        "role": "worker",
        "agent": "claude-code",
        "event": event,
        "success": True,
        "timestamp": "2026-06-25T10:00:00+00:00",
    }
    base.update(kw)
    return base


AGENT_REC = _rec("agent_result", success=True)
FAILED_AGENT_REC = _rec("agent_result", success=False, error="timeout")
INTEGRATION_REC = _rec("integration_result", agent="integrator", role="integrator", success=True)
COMPLETED_REC = _rec(
    "pipeline_completed",
    agent="",
    role="",
    success=True,
    metadata={"stage": "done", "pr_url": "", "ci_passed": True,
               "debug_retry_count": 0, "error_count": 0},
)
COMPLETED_FAILED_REC = _rec(
    "pipeline_completed",
    agent="",
    role="",
    success=False,
    metadata={"stage": "done", "pr_url": "", "ci_passed": None,
               "debug_retry_count": 0, "error_count": 1},
)


# ═══════════════════════════════════════════════════════════════════════════════
# I. load_execution_records
# ═══════════════════════════════════════════════════════════════════════════════

def test_load_empty_path_returns_empty_list():
    assert load_execution_records("") == []


def test_load_missing_file_returns_empty_list(tmp_path):
    assert load_execution_records(str(tmp_path / "nope.jsonl")) == []


def test_load_normal_jsonl(tmp_path):
    p = tmp_path / "exec.jsonl"
    _write_jsonl(p, [AGENT_REC, INTEGRATION_REC])
    records = load_execution_records(str(p))
    assert len(records) == 2
    assert records[0]["event"] == "agent_result"
    assert records[1]["event"] == "integration_result"


def test_load_skips_empty_lines(tmp_path):
    p = tmp_path / "exec.jsonl"
    p.write_text(
        json.dumps(AGENT_REC) + "\n\n   \n" + json.dumps(INTEGRATION_REC) + "\n",
        encoding="utf-8",
    )
    records = load_execution_records(str(p))
    assert len(records) == 2


def test_load_skips_bad_json_lines_without_raising(tmp_path):
    p = tmp_path / "exec.jsonl"
    p.write_text(
        json.dumps(AGENT_REC) + "\n"
        "NOT_VALID_JSON }{{\n"
        + json.dumps(INTEGRATION_REC) + "\n",
        encoding="utf-8",
    )
    records = load_execution_records(str(p))
    assert len(records) == 2   # bad line skipped, others kept


def test_load_preserves_all_fields(tmp_path):
    p = tmp_path / "exec.jsonl"
    rec = {"run_id": "r1", "event": "agent_result", "extra_field": "value", "nested": {"k": 1}}
    _write_jsonl(p, [rec])
    result = load_execution_records(str(p))
    assert result[0]["extra_field"] == "value"
    assert result[0]["nested"] == {"k": 1}


def test_load_handles_utf8_content(tmp_path):
    """Non-ASCII content (e.g. Chinese characters) loads without error"""
    p = tmp_path / "exec.jsonl"
    rec = {**AGENT_REC, "error": "超时错误：300秒"}
    _write_jsonl(p, [rec])
    records = load_execution_records(str(p))
    assert "超时" in records[0]["error"]


# ═══════════════════════════════════════════════════════════════════════════════
# II. filter_records_by_run
# ═══════════════════════════════════════════════════════════════════════════════

def test_filter_empty_run_id_returns_all():
    records = [_rec("agent_result", run_id="a"), _rec("agent_result", run_id="b")]
    result = filter_records_by_run(records, "")
    assert len(result) == 2


def test_filter_by_run_id():
    recs = [
        _rec("agent_result", run_id="run1"),
        _rec("agent_result", run_id="run2"),
        _rec("integration_result", run_id="run1"),
    ]
    result = filter_records_by_run(recs, "run1")
    assert len(result) == 2
    assert all(r["run_id"] == "run1" for r in result)


def test_filter_unknown_run_id_returns_empty():
    recs = [_rec("agent_result", run_id="run1")]
    assert filter_records_by_run(recs, "nonexistent") == []


def test_filter_sorts_by_timestamp_ascending():
    recs = [
        _rec("agent_result", timestamp="2026-06-25T10:00:02+00:00"),
        _rec("agent_result", timestamp="2026-06-25T10:00:01+00:00"),
        _rec("agent_result", timestamp="2026-06-25T10:00:03+00:00"),
    ]
    result = filter_records_by_run(recs, "")
    ts = [r["timestamp"] for r in result]
    assert ts == sorted(ts)


def test_filter_records_without_timestamp_sort_last():
    recs = [
        _rec("agent_result", timestamp="2026-06-25T10:00:00+00:00"),
        {**_rec("agent_result"), "timestamp": None},
    ]
    # pop timestamp from second record to simulate missing field
    recs[1].pop("timestamp", None)
    result = filter_records_by_run(recs, "")
    assert result[0].get("timestamp") is not None
    assert result[-1].get("timestamp") is None


# ═══════════════════════════════════════════════════════════════════════════════
# III. summarize_run
# ═══════════════════════════════════════════════════════════════════════════════

def test_summarize_empty_records():
    s = summarize_run([])
    assert s["run_id"] == ""
    assert s["total_events"] == 0
    assert s["success"] is None
    assert s["pipeline_completed_present"] is False


def test_summarize_success_from_pipeline_completed():
    recs = [AGENT_REC, INTEGRATION_REC, COMPLETED_REC]
    s = summarize_run(recs)
    assert s["success"] is True
    assert s["pipeline_completed_present"] is True


def test_summarize_failure_from_pipeline_completed():
    recs = [FAILED_AGENT_REC, COMPLETED_FAILED_REC]
    s = summarize_run(recs)
    assert s["success"] is False


def test_summarize_counts_agent_results():
    recs = [AGENT_REC, AGENT_REC, FAILED_AGENT_REC]
    s = summarize_run(recs)
    assert s["agent_results_count"] == 3
    assert s["failed_agent_results_count"] == 1


def test_summarize_counts_integration_results():
    recs = [INTEGRATION_REC, INTEGRATION_REC, COMPLETED_REC]
    s = summarize_run(recs)
    assert s["integration_results_count"] == 2


def test_summarize_errors_max_10():
    recs = [_rec("agent_result", success=False, error=f"err{i}") for i in range(20)]
    s = summarize_run(recs)
    assert len(s["errors"]) <= 10


def test_summarize_error_truncated_to_300_chars():
    long_err = "x" * 500
    recs = [_rec("agent_result", success=False, error=long_err)]
    s = summarize_run(recs)
    assert len(s["errors"][0]) <= 300


def test_summarize_final_stage_from_metadata():
    recs = [COMPLETED_REC]
    s = summarize_run(recs)
    assert s["final_stage"] == "done"


def test_summarize_no_completed_event():
    recs = [AGENT_REC, INTEGRATION_REC]
    s = summarize_run(recs)
    assert s["pipeline_completed_present"] is False
    assert s["success"] is None


def test_summarize_errors_redact_api_key_patterns():
    """error text containing credentials must be redacted in summary errors list"""
    sensitive_err = "OPENAI_API_KEY=sk-secret token=abc ghp_tokenabc123"
    recs = [_rec("agent_result", success=False, error=sensitive_err)]
    s = summarize_run(recs)
    assert len(s["errors"]) == 1
    err_out = s["errors"][0]
    assert "sk-secret" not in err_out
    assert "ghp_tokenabc123" not in err_out
    # key name preserved, value redacted
    assert "OPENAI_API_KEY" in err_out
    assert "[REDACTED]" in err_out


# ═══════════════════════════════════════════════════════════════════════════════
# IV. format_run_summary
# ═══════════════════════════════════════════════════════════════════════════════

def test_format_empty_records_gives_friendly_message():
    out = format_run_summary(summarize_run([]), [])
    assert "No execution records found" in out


def test_format_shows_run_id():
    recs = [AGENT_REC]
    out = format_run_summary(summarize_run(recs), recs)
    assert "run1" in out


def test_format_shows_status():
    recs = [COMPLETED_REC]
    out = format_run_summary(summarize_run(recs), recs)
    assert "success" in out.lower()


def test_format_shows_failed_status():
    recs = [COMPLETED_FAILED_REC]
    out = format_run_summary(summarize_run(recs), recs)
    assert "failed" in out.lower()


def test_format_shows_timeline():
    recs = filter_records_by_run([AGENT_REC, INTEGRATION_REC, COMPLETED_REC], "")
    out = format_run_summary(summarize_run(recs), recs)
    assert "Timeline" in out
    assert "agent_result" in out
    assert "integration_result" in out
    assert "pipeline_completed" in out


def test_format_shows_errors():
    recs = [_rec("agent_result", success=False, error="Something went wrong")]
    out = format_run_summary(summarize_run(recs), recs)
    assert "Errors" in out
    assert "Something went wrong" in out


def test_format_warns_when_no_pipeline_completed():
    recs = [AGENT_REC]   # no pipeline_completed
    out = format_run_summary(summarize_run(recs), recs)
    assert "pipeline_completed not found" in out or "interrupted" in out


def test_format_does_not_contain_sensitive_keys():
    """Output must not reveal prompt, api_key, token, or secret"""
    sensitive_rec = {
        **AGENT_REC,
        "prompt": "SECRET PROMPT TEXT",
        "api_key": "sk-very-secret",
        "token": "ghp_secret_token",
    }
    recs = [sensitive_rec]
    out = format_run_summary(summarize_run(recs), recs)
    assert "SECRET PROMPT TEXT" not in out
    assert "sk-very-secret" not in out
    assert "ghp_secret_token" not in out


def test_format_timeline_excludes_prompt_field(tmp_path):
    """format_run_summary timeline rows must not dump full record fields"""
    recs = [{**AGENT_REC, "prompt": "DO NOT SHOW THIS"}]
    out = format_run_summary(summarize_run(recs), recs)
    assert "DO NOT SHOW THIS" not in out


# ═══════════════════════════════════════════════════════════════════════════════
# V. view_run (end-to-end convenience)
# ═══════════════════════════════════════════════════════════════════════════════

def test_view_run_empty_path():
    assert "No execution records found" in view_run("")


def test_view_run_missing_file(tmp_path):
    out = view_run(str(tmp_path / "missing.jsonl"))
    assert "No execution records found" in out


def test_view_run_full_path(tmp_path):
    p = tmp_path / "exec.jsonl"
    _write_jsonl(p, [AGENT_REC, INTEGRATION_REC, COMPLETED_REC])
    out = view_run(str(p))
    assert "run1" in out
    assert "success" in out.lower()


def test_view_run_always_sorts_by_timestamp(tmp_path):
    """view_run() must sort by timestamp even when run_id is empty (P3 fix)"""
    p = tmp_path / "exec.jsonl"
    recs = [
        _rec("agent_result",     timestamp="2026-06-25T10:00:03+00:00"),
        _rec("pipeline_completed", timestamp="2026-06-25T10:00:10+00:00",
             metadata={"stage": "done", "pr_url": "", "ci_passed": True,
                       "debug_retry_count": 0, "error_count": 0}),
        _rec("integration_result", agent="integrator", role="integrator",
             timestamp="2026-06-25T10:00:01+00:00"),
    ]
    _write_jsonl(p, recs)
    out = view_run(str(p))   # no run_id filter
    lines = [l for l in out.splitlines() if l.startswith("  ")]
    timestamps = [l.split()[0].strip() for l in lines if l.strip()]
    assert timestamps == sorted(timestamps), "Timeline must be in timestamp order"


def test_view_run_filters_by_run_id(tmp_path):
    p = tmp_path / "exec.jsonl"
    recs = [
        _rec("agent_result", run_id="run1"),
        _rec("agent_result", run_id="run2"),
        {**COMPLETED_REC, "run_id": "run1"},
    ]
    _write_jsonl(p, recs)
    out = view_run(str(p), run_id="run1")
    # Should contain run1 events only
    assert "run1" in out


# ═══════════════════════════════════════════════════════════════════════════════
# VI. CLI smoke test
# ═══════════════════════════════════════════════════════════════════════════════

def test_cli_with_path(tmp_path):
    """CLI entry point produces non-empty text output for a valid log file"""
    import subprocess, sys
    p = tmp_path / "exec.jsonl"
    _write_jsonl(p, [AGENT_REC, COMPLETED_REC])

    result = subprocess.run(
        [sys.executable, "-m", "telemetry.log_viewer", "--path", str(p)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(Path(__file__).parent.parent.parent),  # project root
    )
    assert result.returncode == 0
    assert "run1" in result.stdout
    assert "Timeline" in result.stdout


def test_cli_no_path_returns_error():
    """CLI with no path and empty EXECUTION_LOG_PATH exits with code 1"""
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "-m", "telemetry.log_viewer"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(Path(__file__).parent.parent.parent),
        env={
            **__import__("os").environ,
            "EXECUTION_LOG_PATH": "",
        },
    )
    assert result.returncode == 1
    assert "not configured" in result.stderr.lower() or "EXECUTION_LOG_PATH" in result.stderr


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 8F: Router fields in summarize_run / format_run_summary
# ═══════════════════════════════════════════════════════════════════════════════

def _router_rec_viewer(run_id: str = "run1",
                       selected: str = "claude-code",
                       applied: str = "claude-code",
                       fallback: bool = False,
                       success: bool = True,
                       ts: str = "2026-06-25T10:00:05+00:00") -> dict:
    return {
        "run_id": run_id,
        "event": "router_decision",
        "agent": "router",
        "role": "reviewer",
        "success": success,
        "timestamp": ts,
        "metadata": {
            "selected_backend": selected,
            "applied_backend": applied,
            "preferred_backend": "codex",
            "fallback_used": fallback,
            "active": True,
            "dry_run": False,
        },
    }


def test_summarize_run_router_decisions_count():
    recs = [AGENT_REC, _router_rec_viewer(), COMPLETED_REC]
    s = summarize_run(recs)
    assert s["router_decisions"] == 1
    assert s["router_failed_decisions"] == 0


def test_summarize_run_router_fields_zero_when_no_events():
    recs = [AGENT_REC, COMPLETED_REC]
    s = summarize_run(recs)
    assert s["router_decisions"] == 0
    assert s["router_last_selected_backend"] is None
    assert s["router_last_applied_backend"] is None


def test_summarize_run_router_last_fields():
    recs = [
        _router_rec_viewer(selected="codex", applied="codex",
                           ts="2026-06-25T10:00:01+00:00"),
        _router_rec_viewer(selected="claude-code", applied="claude-code",
                           fallback=True, ts="2026-06-25T10:00:05+00:00"),
        COMPLETED_REC,
    ]
    s = summarize_run(recs)
    assert s["router_last_selected_backend"] == "claude-code"
    assert s["router_last_applied_backend"] == "claude-code"
    assert s["router_last_fallback_used"] is True


def test_format_run_summary_shows_router_decisions():
    recs = [AGENT_REC, _router_rec_viewer(), COMPLETED_REC]
    s = summarize_run(recs)
    out = format_run_summary(s, recs)
    assert "Router decisions:" in out
    assert "1 total" in out


def test_format_run_summary_no_router_line_when_zero():
    recs = [AGENT_REC, COMPLETED_REC]
    s = summarize_run(recs)
    out = format_run_summary(s, recs)
    assert "Router decisions:" not in out


def test_format_run_summary_router_no_sensitive_content():
    """Router line must not expose reason/error/API keys"""
    recs = [
        _router_rec_viewer(),
        COMPLETED_REC,
    ]
    s = summarize_run(recs)
    out = format_run_summary(s, recs)
    assert "sk-" not in out
    assert "PROMPT" not in out
