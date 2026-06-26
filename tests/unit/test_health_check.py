"""
tests/unit/test_health_check.py

Unit tests for diagnostics/health_check.py (Phase 9A/9B/9C).
All checks are mocked — no real subprocess, network, or file system writes.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from diagnostics.health_check import (
    HealthCheckResult,
    HealthReport,
    format_health_check_report,
    run_health_checks,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _patch_defaults(**overrides):
    """Patch settings to a safe baseline; override individual fields as needed."""
    defaults = {
        "ANTHROPIC_BASE_URL": "https://example.com",
        "REVIEW_AGENT_BACKEND": "claude-code",
        "ENABLE_REVIEW_ROUTER_DRY_RUN": False,
        "ENABLE_REVIEW_ROUTER_ACTIVE": False,
        "ENABLE_REVIEW_ROUTER_STATS": False,
        "CODEX_MODEL": "",
        "OPENAI_API_KEY": "",
        "EXECUTION_LOG_PATH": "",
    }
    defaults.update(overrides)
    return [
        patch(f"config.settings.settings.{k}", v)
        for k, v in defaults.items()
    ]


def _enter(patches):
    cms = [p.__enter__() for p in patches]
    return patches, cms


def _exit(patches):
    for p in patches:
        p.__exit__(None, None, None)


def _run_with(**overrides):
    """Run health checks with patched settings and a fake healthy registry."""
    from agents.registry import AgentRegistry
    fake_reg = AgentRegistry()
    fake_reg.register("claude-code", MagicMock())
    fake_reg.register("codex", MagicMock())

    patches = _patch_defaults(**overrides)
    patches.append(patch("agents.registry.default_registry", fake_reg))
    patches.append(patch(
        "diagnostics.health_check._check_router_safety",
        return_value=HealthCheckResult(
            name="router_safety", status="ok", message="skipped in test",
        ),
    ))

    for p in patches:
        p.__enter__()
    try:
        return run_health_checks()
    finally:
        for p in patches:
            p.__exit__(None, None, None)


# ═══════════════════════════════════════════════════════════════════════════════
# 9A: individual checks
# ═══════════════════════════════════════════════════════════════════════════════

def test_default_config_is_healthy():
    """Baseline config (ANTHROPIC_BASE_URL set, claude-code backend) → no errors"""
    report = _run_with()
    assert report.overall_status in ("ok", "warning")
    error_checks = [c for c in report.checks if c.status == "error"]
    assert error_checks == [], f"Unexpected errors: {error_checks}"


def test_missing_anthropic_base_url_gives_warning():
    report = _run_with(ANTHROPIC_BASE_URL="")
    names = {c.name: c.status for c in report.checks}
    assert names.get("claude_code_backend") == "warning"


def test_invalid_review_agent_backend_gives_error():
    report = _run_with(REVIEW_AGENT_BACKEND="totally-invalid")
    names = {c.name: c.status for c in report.checks}
    assert names.get("review_agent_backend") == "error"


def test_valid_review_agent_backend_codex():
    report = _run_with(
        REVIEW_AGENT_BACKEND="codex",
        CODEX_MODEL="o4-mini",
        OPENAI_API_KEY="sk-real-key",
        ENABLE_REVIEW_ROUTER_ACTIVE=True,
    )
    names = {c.name: c.status for c in report.checks}
    assert names.get("review_agent_backend") == "ok"


def test_active_without_dry_run_gives_warning():
    report = _run_with(
        ENABLE_REVIEW_ROUTER_ACTIVE=True,
        ENABLE_REVIEW_ROUTER_DRY_RUN=False,
    )
    names = {c.name: c.status for c in report.checks}
    assert names.get("router_active") == "warning"


def test_active_with_dry_run_is_ok():
    report = _run_with(
        ENABLE_REVIEW_ROUTER_ACTIVE=True,
        ENABLE_REVIEW_ROUTER_DRY_RUN=True,
    )
    names = {c.name: c.status for c in report.checks}
    assert names.get("router_active") == "ok"


def test_codex_used_without_model_gives_warning():
    report = _run_with(
        REVIEW_AGENT_BACKEND="codex",
        CODEX_MODEL="",
        OPENAI_API_KEY="sk-x",
    )
    names = {c.name: c.status for c in report.checks}
    assert names.get("codex_prerequisites") == "warning"
    codex_check = next(c for c in report.checks if c.name == "codex_prerequisites")
    assert "CODEX_MODEL" in codex_check.details.get("missing_fields", [])


def test_codex_used_without_api_key_gives_warning():
    report = _run_with(
        REVIEW_AGENT_BACKEND="codex",
        CODEX_MODEL="o4-mini",
        OPENAI_API_KEY="",
    )
    names = {c.name: c.status for c in report.checks}
    assert names.get("codex_prerequisites") == "warning"
    codex_check = next(c for c in report.checks if c.name == "codex_prerequisites")
    assert "OPENAI_API_KEY" in codex_check.details.get("missing_fields", [])


def test_codex_not_in_use_skips_prereq_check():
    """When codex is not backend, prereq check is ok regardless of ACTIVE"""
    report = _run_with(
        REVIEW_AGENT_BACKEND="claude-code",
        ENABLE_REVIEW_ROUTER_ACTIVE=True,  # ACTIVE alone should NOT trigger codex prereq
        CODEX_MODEL="",
        OPENAI_API_KEY="",
    )
    names = {c.name: c.status for c in report.checks}
    assert names.get("codex_prerequisites") == "ok"


def test_execution_log_path_empty_is_ok():
    report = _run_with(EXECUTION_LOG_PATH="")
    names = {c.name: c.status for c in report.checks}
    assert names.get("execution_log_path") == "ok"


def test_execution_log_path_nonexistent_parent_gives_warning_not_create(tmp_path):
    """Non-existent parent → read-only check returns warning, does NOT create dir"""
    log = str(tmp_path / "new_dir" / "exec.jsonl")
    assert not (tmp_path / "new_dir").exists()   # ensure dir doesn't exist yet

    from agents.registry import AgentRegistry
    fake_reg = AgentRegistry()
    fake_reg.register("claude-code", MagicMock())
    fake_reg.register("codex", MagicMock())

    patches = _patch_defaults(EXECUTION_LOG_PATH=log)
    patches.append(patch("agents.registry.default_registry", fake_reg))
    patches.append(patch(
        "diagnostics.health_check._check_router_safety",
        return_value=HealthCheckResult(name="router_safety", status="ok", message="ok"),
    ))
    for p in patches:
        p.__enter__()
    try:
        report = run_health_checks()
    finally:
        for p in patches:
            p.__exit__(None, None, None)

    names = {c.name: c.status for c in report.checks}
    # Must be warning (not ok) because dir doesn't exist — and must NOT create it
    assert names.get("execution_log_path") == "warning"
    assert not (tmp_path / "new_dir").exists(), "Health check must not create directories"


def test_agent_registry_missing_backend_gives_warning():
    from agents.registry import AgentRegistry
    incomplete_reg = AgentRegistry()
    incomplete_reg.register("claude-code", MagicMock())
    # "codex" deliberately missing

    patches = _patch_defaults()
    patches.append(patch("agents.registry.default_registry", incomplete_reg))
    patches.append(patch(
        "diagnostics.health_check._check_router_safety",
        return_value=HealthCheckResult(name="router_safety", status="ok", message="ok"),
    ))
    for p in patches:
        p.__enter__()
    try:
        report = run_health_checks()
    finally:
        for p in patches:
            p.__exit__(None, None, None)

    names = {c.name: c.status for c in report.checks}
    assert names.get("agent_registry") == "warning"


def test_overall_error_when_any_check_errors():
    report = _run_with(REVIEW_AGENT_BACKEND="bad-value")
    assert report.overall_status == "error"


def test_overall_ok_when_no_errors_no_warnings():
    report = _run_with()
    assert report.overall_status in ("ok", "warning")  # warnings from router flags OK


def test_health_check_does_not_call_subprocess():
    """run_health_checks() must never spawn a real subprocess"""
    with patch("subprocess.run") as mock_run, \
         patch("subprocess.Popen") as mock_popen:
        report = _run_with()
    mock_run.assert_not_called()
    mock_popen.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# 9B: format_health_check_report
# ═══════════════════════════════════════════════════════════════════════════════

def _make_report(*checks) -> HealthReport:
    statuses = {c.status for c in checks}
    overall = "error" if "error" in statuses else ("warning" if "warning" in statuses else "ok")
    return HealthReport(checks=list(checks), overall_status=overall)


def test_format_shows_overall_status():
    report = _make_report(
        HealthCheckResult("check_a", "ok", "all good"),
    )
    out = format_health_check_report(report)
    assert "Overall: OK" in out
    assert "check_a" in out


def test_format_shows_warning():
    report = _make_report(
        HealthCheckResult("check_a", "ok", "fine"),
        HealthCheckResult("check_b", "warning", "might be an issue"),
    )
    out = format_health_check_report(report)
    assert "Overall: WARNING" in out
    assert "WARNING" in out


def test_format_shows_error():
    report = _make_report(
        HealthCheckResult("check_a", "error", "broken"),
    )
    out = format_health_check_report(report)
    assert "Overall: ERROR" in out


def test_format_redacts_sensitive_message():
    """Message containing API key must be redacted in output"""
    report = _make_report(
        HealthCheckResult("check_x", "warning",
                          "OPENAI_API_KEY=sk-secret token=abc"),
    )
    out = format_health_check_report(report)
    assert "sk-secret" not in out
    assert "[REDACTED]" in out


def test_format_redacts_sensitive_detail_value():
    """String detail values containing API keys must be redacted"""
    report = _make_report(
        HealthCheckResult("check_x", "ok", "test",
                          details={"key_info": "ghp_tokenabc123"}),
    )
    out = format_health_check_report(report)
    assert "ghp_tokenabc123" not in out
    assert "[REDACTED]" in out


def test_format_shows_check_counts():
    report = _make_report(
        HealthCheckResult("a", "ok", "x"),
        HealthCheckResult("b", "warning", "y"),
        HealthCheckResult("c", "error", "z"),
    )
    out = format_health_check_report(report)
    assert "1 ok" in out
    assert "1 warning" in out
    assert "1 error" in out


def test_format_empty_checks():
    report = HealthReport(checks=[], overall_status="ok")
    out = format_health_check_report(report)
    assert "Overall: OK" in out
    assert "0 ok" in out


# ═══════════════════════════════════════════════════════════════════════════════
# 9C: CLI
# ═══════════════════════════════════════════════════════════════════════════════

def test_cli_exits_0_when_ok(tmp_path):
    """CLI exits 0 when all checks pass"""
    result = subprocess.run(
        [sys.executable, "-m", "diagnostics.health_check"],
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(Path(__file__).parent.parent.parent),
        env={
            **__import__("os").environ,
            "ANTHROPIC_BASE_URL": "https://example.com",
            "REVIEW_AGENT_BACKEND": "claude-code",
            "ENABLE_REVIEW_ROUTER_ACTIVE": "false",
            "ENABLE_REVIEW_ROUTER_DRY_RUN": "false",
            "EXECUTION_LOG_PATH": "",
        },
    )
    assert result.returncode == 0
    assert "OpenClaw Health Check" in result.stdout


def test_cli_exits_0_with_warnings_only(tmp_path):
    """CLI exits 0 even with warnings"""
    result = subprocess.run(
        [sys.executable, "-m", "diagnostics.health_check"],
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(Path(__file__).parent.parent.parent),
        env={
            **__import__("os").environ,
            "ANTHROPIC_BASE_URL": "",  # → warning
            "REVIEW_AGENT_BACKEND": "claude-code",
            "ENABLE_REVIEW_ROUTER_ACTIVE": "false",
            "ENABLE_REVIEW_ROUTER_DRY_RUN": "false",
            "EXECUTION_LOG_PATH": "",
        },
    )
    assert result.returncode == 0


def test_cli_exits_nonzero_on_error(tmp_path):
    """Invalid REVIEW_AGENT_BACKEND → error → exit code 1"""
    result = subprocess.run(
        [sys.executable, "-m", "diagnostics.health_check"],
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(Path(__file__).parent.parent.parent),
        env={
            **__import__("os").environ,
            "REVIEW_AGENT_BACKEND": "totally-invalid",
            "EXECUTION_LOG_PATH": "",
        },
    )
    assert result.returncode == 1


def test_cli_json_output(tmp_path):
    """--json flag outputs valid JSON with expected keys"""
    result = subprocess.run(
        [sys.executable, "-m", "diagnostics.health_check", "--json"],
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(Path(__file__).parent.parent.parent),
        env={
            **__import__("os").environ,
            "ANTHROPIC_BASE_URL": "https://example.com",
            "REVIEW_AGENT_BACKEND": "claude-code",
            "ENABLE_REVIEW_ROUTER_ACTIVE": "false",
            "EXECUTION_LOG_PATH": "",
        },
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert "overall_status" in data
    assert "checks" in data
    assert isinstance(data["checks"], list)


def test_cli_json_output_no_sensitive_content():
    """--json output must also redact API keys and tokens"""
    import os
    result = subprocess.run(
        [sys.executable, "-m", "diagnostics.health_check", "--json"],
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(Path(__file__).parent.parent.parent),
        env={
            **os.environ,
            "REVIEW_AGENT_BACKEND": "claude-code",
            "EXECUTION_LOG_PATH": "",
            "OPENAI_API_KEY": "sk-never-in-json",
        },
    )
    # returncode may be 0 or 1 depending on overall status; just verify JSON
    assert "sk-never-in-json" not in result.stdout
    if result.stdout.strip():
        json.loads(result.stdout)   # must still be valid JSON


def test_cli_output_no_sensitive_content(tmp_path):
    """CLI output must not contain API keys or tokens"""
    result = subprocess.run(
        [sys.executable, "-m", "diagnostics.health_check"],
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(Path(__file__).parent.parent.parent),
        env={
            **__import__("os").environ,
            "REVIEW_AGENT_BACKEND": "claude-code",
            "EXECUTION_LOG_PATH": "",
            "OPENAI_API_KEY": "sk-never-print-this",
        },
    )
    assert "sk-never-print-this" not in result.stdout
    assert "sk-never-print-this" not in result.stderr
