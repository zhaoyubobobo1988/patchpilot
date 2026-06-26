"""
telemetry/router_safety.py — Router safety analysis and rollback recommendations.

Reads stats produced by telemetry/log_stats.summarize_all_runs() and returns
a structured advisory recommendation.  It never modifies .env, settings, or
any live configuration — callers decide whether to act on the advice.

Outputs never contain raw error text, prompts, diffs, API keys, or tokens.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Data structure ────────────────────────────────────────────────────────────

@dataclass
class RouterSafetyRecommendation:
    """
    Advisory recommendation produced by recommend_router_safety().

    level   : "ok" | "watch" | "rollback"
    action  : "keep-active" | "keep-dry-run" | "switch-to-dry-run" |
               "disable-router" | "insufficient-data"
    reason  : Short human-readable explanation.
    details : Numeric/boolean signals used by the evaluation — never raw errors.
    """
    level: str
    action: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


# ── Core recommendation ───────────────────────────────────────────────────────

def recommend_router_safety(stats: dict[str, Any]) -> RouterSafetyRecommendation:
    """
    Evaluate router stats and return a safety recommendation.

    Rules (evaluated in order; first matching rule wins):

      1. No data              → insufficient-data / watch
      2. High failure rate    → disable-router / rollback
      3. High active fallback → switch-to-dry-run / watch
      4. Codex errors post-apply → switch-to-dry-run / rollback
      5. Stable dry-run       → keep-dry-run / ok
      6. Stable active        → keep-active / ok
      7. Default              → keep-dry-run / watch

    All divisions guard against zero denominators.
    Missing stats fields default to 0 or {}.
    """
    total   = int(stats.get("router_decisions_total", 0))
    failed  = int(stats.get("router_decisions_failed", 0))
    active  = int(stats.get("router_active_count", 0))
    dry_run = int(stats.get("router_dry_run_count", 0))
    fallback = int(stats.get("router_fallback_count", 0))

    applied_counts: dict = stats.get("router_applied_backend_counts") or {}
    codex_applied  = int(applied_counts.get("codex", 0))

    error_counts: dict = stats.get("router_errors_by_category") or {}
    codex_errors = (int(error_counts.get("auth", 0)) +
                    int(error_counts.get("codex_config", 0)))

    failure_rate       = failed / total if total else 0.0
    active_fallback_rate = fallback / active if active else 0.0

    details = {
        "router_decisions_total": total,
        "router_decisions_failed": failed,
        "router_failure_rate": round(failure_rate, 4),
        "router_active_count": active,
        "router_dry_run_count": dry_run,
        "router_fallback_count": fallback,
        "active_fallback_rate": round(active_fallback_rate, 4),
        "codex_applied_count": codex_applied,
        "codex_error_count": codex_errors,
    }

    # Rule 1 — no data yet
    if total == 0:
        return RouterSafetyRecommendation(
            level="watch",
            action="insufficient-data",
            reason="no router decisions recorded yet",
            details=details,
        )

    # Rule 2 — router self-failure rate too high
    if total >= 5 and failure_rate >= 0.3:
        return RouterSafetyRecommendation(
            level="rollback",
            action="disable-router",
            reason="router decision failure rate is too high",
            details=details,
        )

    # Rule 3 — active mode has too many fallbacks
    if active >= 5 and active_fallback_rate >= 0.5:
        return RouterSafetyRecommendation(
            level="watch",
            action="switch-to-dry-run",
            reason="active router fallback rate is high",
            details=details,
        )

    # Rule 4 — codex errors detected after codex was applied
    if codex_applied >= 3 and codex_errors >= 2:
        return RouterSafetyRecommendation(
            level="rollback",
            action="switch-to-dry-run",
            reason="codex-related router errors detected after codex application",
            details=details,
        )

    # Rule 5 — stable active with enough data and low fallback rate.
    # Evaluated before stable dry-run: when both conditions are met (mixed
    # rollout where dry-run ran first and active also accumulated enough clean
    # decisions), active stable takes priority.
    if active >= 10 and failed == 0 and active_fallback_rate < 0.2:
        return RouterSafetyRecommendation(
            level="ok",
            action="keep-active",
            reason="active router decisions look stable",
            details=details,
        )

    # Rule 6 — stable dry-run with enough data (and not enough active to trigger R5)
    if dry_run >= 10 and failed == 0 and active < 10:
        return RouterSafetyRecommendation(
            level="ok",
            action="keep-dry-run",
            reason="dry-run router decisions are stable",
            details=details,
        )

    # Rule 7 — default watch
    return RouterSafetyRecommendation(
        level="watch",
        action="keep-dry-run",
        reason="not enough evidence to change router mode",
        details=details,
    )


# ── Human-readable output ─────────────────────────────────────────────────────

def format_router_safety(rec: RouterSafetyRecommendation) -> str:
    """
    Render a RouterSafetyRecommendation as human-readable text.

    Never emits raw error text, prompt, diff, API key, or token.
    String values in reason and details are redacted before output.
    Only simple scalar types (int, float, bool, str, None) are rendered;
    nested dicts/lists are silently skipped.
    """
    try:
        from telemetry.log_viewer import redact_sensitive_text as _redact
    except Exception:
        def _redact(t: str) -> str:  # type: ignore[misc]
            return t

    lines = [
        "Router Safety Recommendation",
        f"Level: {rec.level}",
        f"Action: {rec.action}",
        f"Reason: {_redact(rec.reason)}",
    ]
    if rec.details:
        lines.append("")
        lines.append("Details:")
        for key, value in rec.details.items():
            if isinstance(value, bool):
                lines.append(f"  - {key}: {value}")
            elif isinstance(value, (int, float)):
                lines.append(f"  - {key}: {value}")
            elif isinstance(value, str):
                lines.append(f"  - {key}: {_redact(value)}")
            elif value is None:
                lines.append(f"  - {key}: None")
            # nested dicts/lists silently skipped
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli_main() -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m telemetry.router_safety",
        description="Router safety recommendation from OpenClaw execution JSONL",
    )
    parser.add_argument("--path", default="",
                        help="JSONL log file. Defaults to settings.EXECUTION_LOG_PATH.")
    parser.add_argument("--recent-limit", type=int, default=10,
                        help="Number of recent runs to analyse (default: 10).")
    args = parser.parse_args()

    path = args.path
    if not path:
        try:
            from config.settings import settings
            path = settings.EXECUTION_LOG_PATH
        except Exception:
            pass

    if not path:
        print("EXECUTION_LOG_PATH is not configured.\n"
              "Set it in .env or pass --path <file>.", file=sys.stderr)
        return 1

    from telemetry.log_viewer import load_execution_records
    from telemetry.log_stats import summarize_all_runs

    records = load_execution_records(path)
    stats = summarize_all_runs(records, recent_limit=args.recent_limit)
    rec = recommend_router_safety(stats)
    print(format_router_safety(rec))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli_main())
