"""
telemetry/log_viewer.py — read-only viewer for JSONL execution logs.

Provides four pure functions that can be composed freely:
  load_execution_records  — read JSONL, tolerates bad lines
  filter_records_by_run   — filter + sort by run_id
  summarize_run           — produce a structured dict summary
  format_run_summary      — render summary + timeline as human-readable text

CLI usage:
  python -m telemetry.log_viewer [--path FILE] [--run-id RUN_ID]

No network calls, no database, no modification of any pipeline state.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from config.logging import get_logger

logger = get_logger(__name__)

_MAX_ERRORS = 10
_MAX_ERROR_LEN = 300
_SENSITIVE_KEYS = frozenset({
    "prompt", "api_key", "token", "secret", "password",
    "auth_token", "anthropic_auth_token", "openai_api_key",
})

# Patterns that might appear inside error strings written by external tools.
# Each tuple is (compiled_regex, replacement).
_REDACT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # KEY=value  (e.g. OPENAI_API_KEY=sk-xxx, ANTHROPIC_AUTH_TOKEN=xxx)
    (re.compile(r"([A-Z_]{4,}(?:KEY|TOKEN|SECRET|PASSWORD|AUTH)[A-Z_]*)=\S+",
                re.IGNORECASE), r"\1=[REDACTED]"),
    # Bearer <token>
    (re.compile(r"(Bearer\s+)\S+", re.IGNORECASE), r"\1[REDACTED]"),
    # sk-<alphanum>  (OpenAI-style keys)
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), "[REDACTED]"),
    # ghp_ / ghs_ / gho_  (GitHub tokens)
    (re.compile(r"\bgh[ps]_[A-Za-z0-9]{8,}"), "[REDACTED]"),
    # token=<value>  (lowercase variants in error messages)
    (re.compile(r"\btoken=\S+", re.IGNORECASE), "token=[REDACTED]"),
]


def redact_sensitive_text(text: str) -> str:
    """
    Remove common credential patterns from *text*.

    Public API — safe to import from other modules.

    Covers:
    - ENV_VAR_KEY=value  (API keys, tokens, secrets, passwords)
    - Bearer <token>
    - sk-<...>  (OpenAI-style secret keys)
    - ghp_/ghs_<...>  (GitHub personal/server tokens)
    - token=<value>  (in query strings or log messages)
    """
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# Internal alias kept for any internal callers that used the private name.
_redact_sensitive_text = redact_sensitive_text


# ── I. Load ──────────────────────────────────────────────────────────────────

def load_execution_records(path: str) -> list[dict[str, Any]]:
    """
    Read a JSONL execution log and return all records as dicts.

    - Empty path   → []
    - Missing file → []
    - Empty lines  → skipped silently
    - Bad JSON     → skipped with warning, other lines unaffected
    """
    if not path:
        return []

    log_path = Path(path)
    if not log_path.exists():
        return []

    records: list[dict[str, Any]] = []
    try:
        for lineno, raw in enumerate(
            log_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        ):
            line = raw.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                logger.warning(f"[log_viewer] line {lineno}: JSON parse error — {exc}")
    except OSError as exc:
        logger.warning(f"[log_viewer] cannot read {path}: {exc}")

    return records


# ── II. Filter ────────────────────────────────────────────────────────────────

def filter_records_by_run(
    records: list[dict[str, Any]],
    run_id: str,
) -> list[dict[str, Any]]:
    """
    Return records matching *run_id*, sorted by timestamp ascending.
    Empty *run_id* returns all records sorted by timestamp.
    Records without 'timestamp' sort after records that have one.
    """
    if run_id:
        records = [r for r in records if r.get("run_id") == run_id]

    def _sort_key(r: dict) -> tuple:
        ts = r.get("timestamp", "")
        return ("" if ts else "~", ts)  # missing ts → sorts last

    return sorted(records, key=_sort_key)


# ── III. Summarise ────────────────────────────────────────────────────────────

def summarize_run(records: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Produce a structured summary dict from a list of execution records.

    Fields:
      run_id, total_events, success, final_stage,
      agent_results_count, failed_agent_results_count,
      integration_results_count, pipeline_completed_present,
      errors (list[str], max 10, max 300 chars each)
    """
    if not records:
        return {
            "run_id": "",
            "total_events": 0,
            "success": None,
            "final_stage": None,
            "agent_results_count": 0,
            "failed_agent_results_count": 0,
            "integration_results_count": 0,
            "pipeline_completed_present": False,
            "errors": [],
        }

    run_id = records[0].get("run_id", "")
    agent_results = [r for r in records if r.get("event") == "agent_result"]
    integration_results = [r for r in records if r.get("event") == "integration_result"]
    completed = [r for r in records if r.get("event") == "pipeline_completed"]

    success: bool | None = None
    final_stage: str | None = None
    if completed:
        last = completed[-1]
        success = last.get("success")
        final_stage = (last.get("metadata") or {}).get("stage")

    # Collect errors from all events — redact credentials, truncate length
    errors: list[str] = []
    for r in records:
        err = r.get("error")
        if err and isinstance(err, str):
            snippet = _redact_sensitive_text(err)[:_MAX_ERROR_LEN]
            if len(errors) < _MAX_ERRORS:
                errors.append(snippet)

    # Router decision fields (Phase 8F) — for single-run summary
    router_recs = sorted(
        [r for r in records if r.get("event") == "router_decision"],
        key=lambda r: r.get("timestamp") or "",
    )
    router_last_meta = (router_recs[-1].get("metadata") or {}) if router_recs else {}

    return {
        "run_id": run_id,
        "total_events": len(records),
        "success": success,
        "final_stage": final_stage,
        "agent_results_count": len(agent_results),
        "failed_agent_results_count": sum(
            1 for r in agent_results if r.get("success") is False
        ),
        "integration_results_count": len(integration_results),
        "pipeline_completed_present": bool(completed),
        "errors": errors,
        # Router
        "router_decisions": len(router_recs),
        "router_failed_decisions": sum(
            1 for r in router_recs if r.get("success") is False
        ),
        "router_last_selected_backend": router_last_meta.get("selected_backend"),
        "router_last_applied_backend": router_last_meta.get("applied_backend"),
        "router_last_fallback_used": router_last_meta.get("fallback_used"),
    }


# ── IV. Format ────────────────────────────────────────────────────────────────

def format_run_summary(
    summary: dict[str, Any],
    records: list[dict[str, Any]],
) -> str:
    """
    Render a human-readable multi-line summary string.

    Never emits prompt text, API keys, tokens, or raw stdout/stderr.
    """
    if not records:
        return "No execution records found."

    lines: list[str] = []
    run_id = summary.get("run_id") or "?"
    success = summary.get("success")
    status = "success" if success is True else ("failed" if success is False else "unknown")

    lines.append(f"Run: {run_id}")
    lines.append(f"Status: {status}")
    if summary.get("final_stage"):
        lines.append(f"Final stage: {summary['final_stage']}")
    lines.append(f"Events: {summary['total_events']}")
    lines.append(
        f"Agent results: {summary['agent_results_count']} total, "
        f"{summary['failed_agent_results_count']} failed"
    )
    lines.append(f"Integration results: {summary['integration_results_count']}")
    # Router decisions line (Phase 8F) — only when present
    router_dec = summary.get("router_decisions", 0)
    if router_dec:
        router_fail = summary.get("router_failed_decisions", 0)
        lines.append(f"Router decisions: {router_dec} total, {router_fail} failed")
        last_sel = summary.get("router_last_selected_backend")
        last_app = summary.get("router_last_applied_backend")
        last_fb = summary.get("router_last_fallback_used")
        if last_sel is not None or last_app is not None:
            lines.append(
                f"  Last: selected={last_sel} applied={last_app} fallback={last_fb}"
            )
    if not summary.get("pipeline_completed_present"):
        lines.append("⚠  pipeline_completed not found — run may have been interrupted")

    # Timeline
    lines.append("")
    lines.append("Timeline:")
    for r in records:
        ts = (r.get("timestamp") or "")[:19]    # trim to seconds
        event = r.get("event", "?")
        agent = r.get("agent", "")
        role = r.get("role", "")
        ok = r.get("success")
        result = "success" if ok is True else ("failed" if ok is False else "")
        parts = [f"  {ts}", event]
        if agent:
            parts.append(agent)
        if role:
            parts.append(role)
        if result:
            parts.append(result)
        lines.append(" ".join(p for p in parts if p))

    # Errors
    if summary.get("errors"):
        lines.append("")
        lines.append("Errors:")
        for err in summary["errors"]:
            lines.append(f"  - {err}")

    return "\n".join(lines)


# ── V. Convenience ────────────────────────────────────────────────────────────

def view_run(path: str, run_id: str = "") -> str:
    """
    One-shot helper: load → filter → summarise → format.
    Returns the formatted string (or a friendly message if nothing found).
    """
    records = load_execution_records(path)
    # Always sort by timestamp via filter_records_by_run, even when run_id is empty.
    # filter_records_by_run("") returns all records sorted by timestamp.
    records = filter_records_by_run(records, run_id)
    summary = summarize_run(records)
    return format_run_summary(summary, records)


# ── VI. CLI ───────────────────────────────────────────────────────────────────

def _cli_main() -> int:
    """Entry point for `python -m telemetry.log_viewer [--path FILE] [--run-id ID]`."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m telemetry.log_viewer",
        description="View OpenClaw execution log (JSONL)",
    )
    parser.add_argument(
        "--path", default="",
        help="Path to JSONL log file. Defaults to settings.EXECUTION_LOG_PATH.",
    )
    parser.add_argument(
        "--run-id", default="",
        help="Filter to a specific pipeline run_id.",
    )
    args = parser.parse_args()

    path = args.path
    if not path:
        try:
            from config.settings import settings
            path = settings.EXECUTION_LOG_PATH
        except Exception:
            pass

    if not path:
        print(
            "EXECUTION_LOG_PATH is not configured.\n"
            "Set it in .env or pass --path <file>.",
            file=sys.stderr,
        )
        return 1

    print(view_run(path, run_id=getattr(args, "run_id", "")))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli_main())
