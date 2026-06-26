"""
diagnostics/health_check.py — OpenClaw startup health check (Phase 9).

Read-only preflight tool that inspects configuration, registry, and recent
telemetry without starting any Agent subprocess, calling any external service,
or modifying .env / settings.

Usage:
  python -m diagnostics.health_check            # plain text report
  python -m diagnostics.health_check --json     # JSON output
  python -m diagnostics.health_check --path /path/to/exec.jsonl

Exit codes:
  0  — OK or WARNING only
  1  — one or more ERROR checks

Sensitive values (API keys, tokens) are never printed.
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class HealthCheckResult:
    """Result of a single named check."""
    name: str
    status: str        # "ok" | "warning" | "error"
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthReport:
    """Aggregated report from run_health_checks()."""
    checks: list[HealthCheckResult]
    overall_status: str   # "ok" | "warning" | "error"


# ── Individual checks ─────────────────────────────────────────────────────────

def _check_claude_code_backend() -> HealthCheckResult:
    """ANTHROPIC_BASE_URL must be set for ClaudeCodeAgent to work."""
    from config.settings import settings
    if settings.ANTHROPIC_BASE_URL:
        return HealthCheckResult(
            name="claude_code_backend",
            status="ok",
            message="ANTHROPIC_BASE_URL is configured",
            details={"anthropic_base_url_set": True},
        )
    return HealthCheckResult(
        name="claude_code_backend",
        status="warning",
        message="ANTHROPIC_BASE_URL is not set; ClaudeCodeAgent will be unavailable",
        details={"anthropic_base_url_set": False},
    )


def _check_review_agent_backend() -> HealthCheckResult:
    from config.settings import settings
    _VALID = {"claude-code", "codex"}
    backend = settings.REVIEW_AGENT_BACKEND
    if backend in _VALID:
        return HealthCheckResult(
            name="review_agent_backend",
            status="ok",
            message=f"REVIEW_AGENT_BACKEND={backend!r} is valid",
            details={"value": backend},
        )
    return HealthCheckResult(
        name="review_agent_backend",
        status="error",
        message=f"REVIEW_AGENT_BACKEND={backend!r} is not a valid value; "
                f"expected one of {sorted(_VALID)}",
        details={"value": backend, "valid_values": sorted(_VALID)},
    )


def _check_router_flags() -> list[HealthCheckResult]:
    from config.settings import settings
    results: list[HealthCheckResult] = []

    dry_run = settings.ENABLE_REVIEW_ROUTER_DRY_RUN
    active  = settings.ENABLE_REVIEW_ROUTER_ACTIVE
    stats   = settings.ENABLE_REVIEW_ROUTER_STATS

    results.append(HealthCheckResult(
        name="router_dry_run",
        status="ok",
        message=f"ENABLE_REVIEW_ROUTER_DRY_RUN={dry_run}",
        details={"value": dry_run},
    ))

    # ACTIVE without DRY_RUN means Router affects real execution with no observation trail
    if active and not dry_run:
        results.append(HealthCheckResult(
            name="router_active",
            status="warning",
            message="ENABLE_REVIEW_ROUTER_ACTIVE=True but ENABLE_REVIEW_ROUTER_DRY_RUN=False; "
                    "Router decisions affect real backend without dry-run safety net",
            details={"active": active, "dry_run": dry_run},
        ))
    else:
        results.append(HealthCheckResult(
            name="router_active",
            status="ok",
            message=f"ENABLE_REVIEW_ROUTER_ACTIVE={active}",
            details={"active": active, "dry_run": dry_run},
        ))

    results.append(HealthCheckResult(
        name="router_stats",
        status="ok",
        message=f"ENABLE_REVIEW_ROUTER_STATS={stats}",
        details={"value": stats},
    ))

    return results


def _check_codex_prerequisites() -> HealthCheckResult:
    """Warn when Codex backend is configured but missing credentials / model."""
    from config.settings import settings

    # Only check prerequisites when REVIEW_AGENT_BACKEND is explicitly "codex".
    # ENABLE_REVIEW_ROUTER_ACTIVE alone does not guarantee Codex will be selected;
    # the Router chooses based on preferred_backend and failure_categories.
    codex_used = settings.REVIEW_AGENT_BACKEND == "codex"
    if not codex_used:
        return HealthCheckResult(
            name="codex_prerequisites",
            status="ok",
            message="Codex backend is not configured (REVIEW_AGENT_BACKEND != codex); "
                    "no prerequisites required",
            details={"codex_in_use": False},
        )

    missing: list[str] = []
    if not settings.CODEX_MODEL:
        missing.append("CODEX_MODEL")
    if not settings.OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")

    if missing:
        return HealthCheckResult(
            name="codex_prerequisites",
            status="warning",
            message=f"Codex backend may be used but missing: {', '.join(missing)}",
            details={"missing_fields": missing, "codex_in_use": True},
        )
    return HealthCheckResult(
        name="codex_prerequisites",
        status="ok",
        message="Codex prerequisites are configured",
        details={"codex_in_use": True, "missing_fields": []},
    )


def _check_execution_log_path() -> HealthCheckResult:
    from config.settings import settings
    path_str = settings.EXECUTION_LOG_PATH
    if not path_str:
        return HealthCheckResult(
            name="execution_log_path",
            status="ok",
            message="EXECUTION_LOG_PATH is empty; telemetry logging is disabled",
            details={"configured": False},
        )
    # Read-only check: never create directories or files.
    log_path = Path(path_str)
    parent = log_path.parent
    if not parent.exists():
        return HealthCheckResult(
            name="execution_log_path",
            status="warning",
            message=f"Execution log parent directory does not exist: {parent}. "
                    f"record_execution() will attempt to create it at runtime.",
            details={"path": str(log_path), "parent_exists": False},
        )
    import os
    if not os.access(parent, os.W_OK):
        return HealthCheckResult(
            name="execution_log_path",
            status="error",
            message="Execution log parent directory exists but is not writable",
            details={"path": str(log_path), "parent_exists": True},
        )
    return HealthCheckResult(
        name="execution_log_path",
        status="ok",
        message="Execution log path is configured and directory is writable",
        details={"path": str(log_path), "configured": True, "parent_exists": True},
    )


def _check_agent_registry() -> HealthCheckResult:
    """Warn when expected backends are missing from the default registry."""
    try:
        from agents.registry import default_registry
        missing = [b for b in ("claude-code", "codex") if not default_registry.has(b)]
        if missing:
            return HealthCheckResult(
                name="agent_registry",
                status="warning",
                message=f"AgentRegistry is missing backend(s): {missing}",
                details={"missing": missing,
                         "registered": default_registry.names()},
            )
        return HealthCheckResult(
            name="agent_registry",
            status="ok",
            message="AgentRegistry has both 'claude-code' and 'codex'",
            details={"registered": default_registry.names()},
        )
    except Exception as exc:
        return HealthCheckResult(
            name="agent_registry",
            status="error",
            message=f"Failed to inspect AgentRegistry: {exc}",
        )


def _check_router_safety(log_path: str = "") -> HealthCheckResult:
    """Load recent stats and return the Router safety advisory as a check."""
    from config.settings import settings
    path = log_path or settings.EXECUTION_LOG_PATH
    if not path:
        return HealthCheckResult(
            name="router_safety",
            status="ok",
            message="No execution log path configured; skipping Router safety check",
            details={"skipped": True},
        )
    try:
        from telemetry.log_viewer import load_execution_records
        from telemetry.log_stats import summarize_all_runs
        from telemetry.router_safety import recommend_router_safety
        records = load_execution_records(path)
        stats = summarize_all_runs(records)
        rec = recommend_router_safety(stats)
        status = "ok" if rec.level == "ok" else (
            "warning" if rec.level == "watch" else "error"
        )
        return HealthCheckResult(
            name="router_safety",
            status=status,
            message=f"Router safety: {rec.action} — {rec.reason}",
            details={
                "level": rec.level,
                "action": rec.action,
                "router_decisions_total": rec.details.get("router_decisions_total", 0),
                "router_failure_rate": rec.details.get("router_failure_rate", 0.0),
                "active_fallback_rate": rec.details.get("active_fallback_rate", 0.0),
            },
        )
    except Exception as exc:
        return HealthCheckResult(
            name="router_safety",
            status="warning",
            message=f"Could not evaluate Router safety: {exc}",
            details={"error": str(exc)[:200]},
        )


# ── Aggregate runner ──────────────────────────────────────────────────────────

def run_health_checks(log_path: str = "") -> HealthReport:
    """
    Run all health checks and return a HealthReport.

    Read-only: no subprocess, no network, no config modification.
    """
    checks: list[HealthCheckResult] = []

    checks.append(_check_claude_code_backend())
    checks.append(_check_review_agent_backend())
    checks.extend(_check_router_flags())
    checks.append(_check_codex_prerequisites())
    checks.append(_check_execution_log_path())
    checks.append(_check_agent_registry())
    checks.append(_check_router_safety(log_path))

    statuses = {c.status for c in checks}
    if "error" in statuses:
        overall = "error"
    elif "warning" in statuses:
        overall = "warning"
    else:
        overall = "ok"

    return HealthReport(checks=checks, overall_status=overall)


# ── Shared redaction helper ───────────────────────────────────────────────────

def _redact_value(value: Any) -> Any:
    """
    Recursively redact sensitive patterns from string scalars.
    Lists and dicts are walked; other types are returned unchanged.
    Never raises.
    """
    try:
        from telemetry.log_viewer import redact_sensitive_text as _r
    except Exception:
        def _r(t: str) -> str:
            return t

    if isinstance(value, str):
        return _r(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


# ── 9B: Human-readable formatter ─────────────────────────────────────────────

def format_health_check_report(report: HealthReport) -> str:
    """
    Render a HealthReport as human-readable text.

    Sensitive values are never emitted — all string details go through
    telemetry.log_viewer.redact_sensitive_text before output.
    """
    _ICONS = {"ok": "[OK]", "warning": "[WARN]", "error": "[ERR]"}

    lines = [
        "OpenClaw Health Check",
        f"Overall: {report.overall_status.upper()}",
        "",
    ]

    for c in report.checks:
        icon = _ICONS.get(c.status, "?")
        lines.append(f"  {icon} [{c.status.upper():<7}] {c.name}")
        lines.append(f"           {_redact_value(c.message)}")
        for k, v in c.details.items():
            rv = _redact_value(v)
            if isinstance(rv, (int, float, bool, str, list, type(None))):
                lines.append(f"           {k}: {rv}")

    # Summary counts
    ok_n  = sum(1 for c in report.checks if c.status == "ok")
    wa_n  = sum(1 for c in report.checks if c.status == "warning")
    er_n  = sum(1 for c in report.checks if c.status == "error")
    lines += [
        "",
        f"Checks: {ok_n} ok, {wa_n} warning, {er_n} error",
    ]
    return "\n".join(lines)


# ── 9C: CLI ───────────────────────────────────────────────────────────────────

def _cli_main() -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m diagnostics.health_check",
        description="OpenClaw startup health check (read-only)",
    )
    parser.add_argument("--path", default="",
                        help="Path to execution JSONL log (overrides settings).")
    parser.add_argument("--json", action="store_true",
                        help="Output results as JSON instead of plain text.")
    args = parser.parse_args()

    log_path = args.path
    if not log_path:
        try:
            from config.settings import settings
            log_path = settings.EXECUTION_LOG_PATH
        except Exception:
            pass

    report = run_health_checks(log_path=log_path)

    if args.json:
        data = {
            "overall_status": report.overall_status,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    "message": _redact_value(c.message),
                    "details": _redact_value(c.details),
                }
                for c in report.checks
            ],
        }
        print(_json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(format_health_check_report(report))

    return 1 if report.overall_status == "error" else 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli_main())
