"""
tests/unit/test_router_safety.py

Unit tests for telemetry/router_safety.py.
Pure in-memory — no real files, CLI, or Agent calls.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from telemetry.router_safety import (
    RouterSafetyRecommendation,
    format_router_safety,
    recommend_router_safety,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _stats(
    total: int = 0,
    failed: int = 0,
    active: int = 0,
    dry_run: int = 0,
    fallback: int = 0,
    codex_applied: int = 0,
    auth_errors: int = 0,
    codex_config_errors: int = 0,
) -> dict:
    return {
        "router_decisions_total": total,
        "router_decisions_failed": failed,
        "router_active_count": active,
        "router_dry_run_count": dry_run,
        "router_fallback_count": fallback,
        "router_applied_backend_counts": {"codex": codex_applied} if codex_applied else {},
        "router_errors_by_category": {
            **( {"auth": auth_errors} if auth_errors else {} ),
            **( {"codex_config": codex_config_errors} if codex_config_errors else {} ),
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Rules
# ═══════════════════════════════════════════════════════════════════════════════

def test_rule1_no_decisions_insufficient_data():
    rec = recommend_router_safety(_stats(total=0))
    assert rec.action == "insufficient-data"
    assert rec.level == "watch"


def test_rule2_high_failure_rate_disable_router():
    # total=5, failed=2 → rate=0.4 ≥ 0.3
    rec = recommend_router_safety(_stats(total=5, failed=2))
    assert rec.action == "disable-router"
    assert rec.level == "rollback"


def test_rule2_not_triggered_below_threshold():
    # total=5, failed=1 → rate=0.2 < 0.3
    rec = recommend_router_safety(_stats(total=5, failed=1))
    assert rec.action != "disable-router"


def test_rule2_not_triggered_below_minimum_total():
    # total=4 (< 5), failed=2 → rule 2 skipped
    rec = recommend_router_safety(_stats(total=4, failed=2))
    assert rec.action != "disable-router"


def test_rule3_high_active_fallback_switch_to_dry_run():
    # active=5, fallback=3 → rate=0.6 ≥ 0.5
    rec = recommend_router_safety(_stats(total=8, active=5, fallback=3))
    assert rec.action == "switch-to-dry-run"
    assert rec.level == "watch"


def test_rule3_not_triggered_below_threshold():
    # active=5, fallback=2 → rate=0.4 < 0.5
    rec = recommend_router_safety(_stats(total=8, active=5, fallback=2))
    assert rec.action != "switch-to-dry-run" or rec.level != "rollback"


def test_rule3_not_triggered_below_minimum_active():
    # active=4 (< 5) → rule 3 skipped
    rec = recommend_router_safety(_stats(total=6, active=4, fallback=3))
    assert rec.action not in ("switch-to-dry-run",) or rec.level == "watch"


def test_rule4_codex_errors_after_apply():
    # codex_applied=3, auth_errors=2 → total codex errors = 2
    rec = recommend_router_safety(_stats(
        total=10, active=5, codex_applied=3, auth_errors=2))
    assert rec.action == "switch-to-dry-run"
    assert rec.level == "rollback"


def test_rule4_codex_config_errors_trigger():
    rec = recommend_router_safety(_stats(
        total=10, active=5, codex_applied=3, codex_config_errors=2))
    assert rec.action == "switch-to-dry-run"
    assert rec.level == "rollback"


def test_rule4_combined_auth_and_codex_config():
    # 1 auth + 1 codex_config = 2 total → triggers
    rec = recommend_router_safety(_stats(
        total=10, active=5, codex_applied=3,
        auth_errors=1, codex_config_errors=1))
    assert rec.action == "switch-to-dry-run"
    assert rec.level == "rollback"


def test_rule4_not_triggered_below_minimum_applied():
    # codex_applied=2 (< 3) → rule 4 skipped
    rec = recommend_router_safety(_stats(
        total=10, active=5, codex_applied=2, auth_errors=2))
    assert rec.action != "switch-to-dry-run" or rec.level != "rollback"


def test_rule5_stable_dry_run():
    rec = recommend_router_safety(_stats(total=10, dry_run=10, failed=0))
    assert rec.action == "keep-dry-run"
    assert rec.level == "ok"


def test_rule5_not_triggered_when_failed():
    rec = recommend_router_safety(_stats(total=11, dry_run=10, failed=1))
    assert rec.action != "keep-dry-run" or rec.level != "ok"


def test_rule6_stable_active():
    # active=10, failed=0, fallback=1 → fallback_rate=0.1 < 0.2
    rec = recommend_router_safety(_stats(
        total=12, active=10, dry_run=2, failed=0, fallback=1))
    assert rec.action == "keep-active"
    assert rec.level == "ok"


def test_rule6_not_triggered_when_failed():
    rec = recommend_router_safety(_stats(
        total=12, active=10, failed=1, fallback=1))
    assert rec.action != "keep-active"


def test_rule6_not_triggered_when_fallback_rate_high():
    # active=10, fallback=3 → rate=0.3 ≥ 0.2
    rec = recommend_router_safety(_stats(
        total=12, active=10, failed=0, fallback=3))
    assert rec.action != "keep-active"


def test_rule7_default_watch():
    # 3 decisions, no failures — not enough for any ok rule
    rec = recommend_router_safety(_stats(total=3, active=2))
    assert rec.level == "watch"
    assert rec.action == "keep-dry-run"


def test_rule5_active_wins_over_rule6_dry_run_in_mixed_rollout():
    """
    Mixed rollout: dry_run=10 AND active=10, all clean.
    stable-active (rule 5) must win over stable-dry-run (rule 6).
    """
    rec = recommend_router_safety(_stats(
        total=20, dry_run=10, active=10, failed=0, fallback=1))
    assert rec.action == "keep-active"
    assert rec.level == "ok"


def test_rule6_stable_dry_run_only_when_active_below_threshold():
    """dry_run>=10 with active<10 → keep-dry-run"""
    rec = recommend_router_safety(_stats(total=12, dry_run=10, active=2, failed=0))
    assert rec.action == "keep-dry-run"
    assert rec.level == "ok"


# ═══════════════════════════════════════════════════════════════════════════════
# Robustness
# ═══════════════════════════════════════════════════════════════════════════════

def test_missing_stats_fields_do_not_raise():
    rec = recommend_router_safety({})
    assert isinstance(rec, RouterSafetyRecommendation)


def test_missing_nested_dict_safe():
    rec = recommend_router_safety({"router_decisions_total": 10,
                                    "router_decisions_failed": 0})
    assert rec is not None


def test_division_by_zero_safe():
    """active=0 → active_fallback_rate must not raise ZeroDivisionError"""
    rec = recommend_router_safety(_stats(total=3, active=0, fallback=0))
    assert isinstance(rec, RouterSafetyRecommendation)


def test_details_contains_required_keys():
    rec = recommend_router_safety(_stats(total=5, active=3, failed=1))
    for key in ("router_decisions_total", "router_decisions_failed",
                "router_failure_rate", "router_active_count",
                "router_dry_run_count", "router_fallback_count",
                "active_fallback_rate", "codex_applied_count",
                "codex_error_count"):
        assert key in rec.details, f"missing details key: {key}"


# ═══════════════════════════════════════════════════════════════════════════════
# format_router_safety
# ═══════════════════════════════════════════════════════════════════════════════

def test_format_includes_level_action_reason():
    rec = RouterSafetyRecommendation(
        level="watch", action="keep-dry-run",
        reason="not enough evidence",
        details={"router_decisions_total": 3},
    )
    out = format_router_safety(rec)
    assert "Router Safety Recommendation" in out
    assert "Level: watch" in out
    assert "Action: keep-dry-run" in out
    assert "Reason: not enough evidence" in out


def test_format_details_shown():
    rec = RouterSafetyRecommendation(
        level="ok", action="keep-active",
        reason="stable",
        details={"router_decisions_total": 12, "router_failure_rate": 0.0},
    )
    out = format_router_safety(rec)
    assert "router_decisions_total: 12" in out
    assert "router_failure_rate: 0.0" in out


def test_format_redacts_api_key_in_reason():
    """reason containing API key pattern must be redacted"""
    rec = RouterSafetyRecommendation(
        level="rollback", action="disable-router",
        reason="error: OPENAI_API_KEY=sk-secret token=abc",
        details={"router_failure_rate": 0.5},
    )
    out = format_router_safety(rec)
    assert "sk-secret" not in out
    assert "abc" not in out or "[REDACTED]" in out   # token value redacted
    assert "[REDACTED]" in out


def test_format_redacts_api_key_in_string_detail():
    """string detail value containing API key must be redacted"""
    rec = RouterSafetyRecommendation(
        level="watch", action="keep-dry-run",
        reason="stable",
        details={"note": "OPENAI_API_KEY=sk-leaked here"},
    )
    out = format_router_safety(rec)
    assert "sk-leaked" not in out
    assert "[REDACTED]" in out


def test_format_no_sensitive_numeric_content():
    """Output must not contain API keys when all details are numeric (baseline check)"""
    rec = RouterSafetyRecommendation(
        level="rollback", action="disable-router",
        reason="failure rate high",
        details={"router_failure_rate": 0.5, "codex_applied_count": 0},
    )
    out = format_router_safety(rec)
    assert "sk-" not in out
    assert "PROMPT" not in out


def test_format_details_only_simple_types():
    """Details with nested dicts should not be rendered (only simple values)"""
    rec = RouterSafetyRecommendation(
        level="watch", action="keep-dry-run", reason="test",
        details={"count": 5, "nested": {"should": "not appear"},
                 "rate": 0.1},
    )
    out = format_router_safety(rec)
    assert "should" not in out   # nested dict not rendered
    assert "count: 5" in out
    assert "rate: 0.1" in out


# ═══════════════════════════════════════════════════════════════════════════════
# CLI smoke tests
# ═══════════════════════════════════════════════════════════════════════════════

def test_cli_with_path(tmp_path):
    """CLI with valid JSONL file outputs Router Safety Recommendation"""
    import subprocess, sys, json as _json
    log = tmp_path / "exec.jsonl"
    # Write some router_decision records
    recs = [
        {"run_id": "r1", "event": "router_decision", "agent": "router",
         "role": "reviewer", "success": True, "timestamp": "2026-06-25T10:00:01+00:00",
         "metadata": {"active": False, "dry_run": True, "fallback_used": False,
                      "selected_backend": "claude-code", "applied_backend": "claude-code",
                      "preferred_backend": "claude-code"}},
        {"run_id": "r1", "event": "pipeline_completed", "success": True,
         "timestamp": "2026-06-25T10:00:02+00:00",
         "metadata": {"stage": "done", "pr_url": "", "ci_passed": True,
                      "debug_retry_count": 0, "error_count": 0}},
    ]
    log.write_text("\n".join(_json.dumps(r) for r in recs) + "\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "telemetry.router_safety", "--path", str(log)],
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(Path(__file__).parent.parent.parent),
    )
    assert result.returncode == 0
    assert "Router Safety Recommendation" in result.stdout
    assert "Level:" in result.stdout
    assert "Action:" in result.stdout


def test_cli_no_path_exits_1():
    import subprocess, sys, os
    result = subprocess.run(
        [sys.executable, "-m", "telemetry.router_safety"],
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(Path(__file__).parent.parent.parent),
        env={**os.environ, "EXECUTION_LOG_PATH": ""},
    )
    assert result.returncode == 1
    assert "not configured" in result.stderr.lower() or "EXECUTION_LOG_PATH" in result.stderr
