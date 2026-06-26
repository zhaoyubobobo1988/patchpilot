"""
telemetry/log_stats.py — cross-run statistics and failure classification.

Builds on log_viewer primitives (load_execution_records, summarize_run)
to answer questions across multiple pipeline runs:
  - How many runs succeeded / failed / are unknown?
  - Which stages fail most often?
  - Which agents fail most often?
  - What are the most common error categories?
  - What did the last N runs look like?

No network calls, no database, no modification of any pipeline state.
All functions are pure: they read lists of dicts and return dicts/strings.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from telemetry.log_viewer import (
    filter_records_by_run,
    load_execution_records,
    summarize_run,
)

# ── I. Group by run ───────────────────────────────────────────────────────────

def group_records_by_run(
    records: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """
    Partition *records* by run_id and sort each partition by timestamp.

    Records without a run_id (or with an empty run_id) are silently ignored
    because they cannot be attributed to a specific pipeline execution and
    would pollute per-run statistics.

    Returns a dict keyed by run_id; values are timestamp-sorted record lists.
    The input list is not modified.
    """
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        rid = r.get("run_id", "")
        if not rid:
            continue
        groups[rid].append(r)

    # Sort each group by timestamp (missing ts sorts last, matching filter_records_by_run)
    def _ts(r: dict) -> tuple:
        ts = r.get("timestamp", "")
        return ("" if ts else "~", ts)

    return {rid: sorted(recs, key=_ts) for rid, recs in groups.items()}


# ── II. Error classification ──────────────────────────────────────────────────

_CATEGORIES: list[tuple[str, list[str]]] = [
    ("timeout",           ["timeout", "timed out", "超时"]),
    # codex_config before auth so "codex exec failed: 403" matches codex_config first
    ("codex_config",      ["codex_model", "review_agent_backend=codex", "codex exec",
                           "codex is not configured"]),
    ("auth",              ["auth", "unauthorized", "permission denied",
                           "api key", "401", "403"]),
    ("integration_test",  ["integration tests failed", "tests failed",
                           "pytest", "assertionerror"]),
    ("empty_patch",       ["empty merged diff", "no patches", "all worker patches"]),
    ("aggregation_failed",["aggregation failed"]),
    ("review_blocked",    ["review blocked", "blocked after"]),
    ("ci_failed",         ["ci failed", "failed checks"]),
]


def classify_error(error: str) -> str:
    """
    Map an error string to a stable category name.

    Matching is case-insensitive keyword search; first match wins.
    Empty string or no match → "unknown".
    The raw error text is never surfaced in the return value.
    """
    if not error or not isinstance(error, str):
        return "unknown"
    lower = error.lower()
    for category, keywords in _CATEGORIES:
        if any(kw in lower for kw in keywords):
            return category
    return "unknown"


# ── II-b. Router decision aggregation ────────────────────────────────────────

def _empty_router_stats() -> dict[str, Any]:
    """Return a fresh router stats dict with independent nested dicts each call."""
    return {
        "router_decisions_total": 0,
        "router_decisions_failed": 0,
        "router_active_count": 0,
        "router_dry_run_count": 0,
        "router_fallback_count": 0,
        "router_selected_backend_counts": {},
        "router_applied_backend_counts": {},
        "router_preferred_to_applied": {},
        "router_errors_by_category": {},
    }


def _aggregate_router_decisions(records: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Compute Router-related statistics from all router_decision events in *records*.

    Never emits raw error text — error strings are classified via classify_error().
    Non-string backend values are ignored.
    """
    router_recs = [r for r in records if r.get("event") == "router_decision"]
    if not router_recs:
        return _empty_router_stats()

    total = len(router_recs)
    failed = sum(1 for r in router_recs if r.get("success") is False)

    active_count = 0
    dry_run_count = 0
    fallback_count = 0
    selected_counts: Counter = Counter()
    applied_counts: Counter = Counter()
    preferred_to_applied: Counter = Counter()
    errors_by_category: Counter = Counter()

    for r in router_recs:
        meta = r.get("metadata") or {}
        active = meta.get("active")
        dry_run = meta.get("dry_run")

        if active is True:
            active_count += 1
        elif active is False and dry_run is True:
            dry_run_count += 1

        if meta.get("fallback_used") is True:
            fallback_count += 1

        selected = meta.get("selected_backend")
        if isinstance(selected, str):
            selected_counts[selected] += 1

        applied = meta.get("applied_backend")
        if isinstance(applied, str):
            applied_counts[applied] += 1

        preferred = meta.get("preferred_backend")
        if isinstance(preferred, str) and isinstance(applied, str):
            preferred_to_applied[f"{preferred}->{applied}"] += 1

        err = r.get("error")
        if err and isinstance(err, str):
            errors_by_category[classify_error(err)] += 1

    return {
        "router_decisions_total": total,
        "router_decisions_failed": failed,
        "router_active_count": active_count,
        "router_dry_run_count": dry_run_count,
        "router_fallback_count": fallback_count,
        "router_selected_backend_counts": dict(selected_counts.most_common()),
        "router_applied_backend_counts": dict(applied_counts.most_common()),
        "router_preferred_to_applied": dict(preferred_to_applied.most_common()),
        "router_errors_by_category": dict(errors_by_category.most_common()),
    }


# ── III. Per-run status ───────────────────────────────────────────────────────

def summarize_run_status(records: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Produce a lightweight status summary for a single run's records.

    Fields:
      run_id, success, final_stage, event_count,
      failed_agent_count, failed_agents (list, deduplicated in order),
      error_categories (list, deduplicated in order),
      first_timestamp, last_timestamp
    """
    if not records:
        return {
            "run_id": "",
            "success": None,
            "final_stage": None,
            "event_count": 0,
            "failed_agent_count": 0,
            "failed_agents": [],
            "error_categories": [],
            "first_timestamp": None,
            "last_timestamp": None,
            # Router decision fields (Phase 8F)
            "router_decisions_count": 0,
            "router_failed_decisions_count": 0,
            "router_last_selected_backend": None,
            "router_last_applied_backend": None,
            "router_last_fallback_used": None,
        }

    # Re-use log_viewer summary for success / final_stage / errors
    base = summarize_run(records)

    # Failed agents (preserving first-seen order, deduplicated)
    seen_agents: set[str] = set()
    failed_agents: list[str] = []
    for r in records:
        if r.get("event") == "agent_result" and r.get("success") is False:
            agent = r.get("agent", "unknown")
            if agent not in seen_agents:
                seen_agents.add(agent)
                failed_agents.append(agent)

    # Error categories (preserving first-seen order, deduplicated)
    seen_cats: set[str] = set()
    error_categories: list[str] = []
    for r in records:
        err = r.get("error")
        if err and isinstance(err, str):
            cat = classify_error(err)
            if cat not in seen_cats:
                seen_cats.add(cat)
                error_categories.append(cat)

    # Sort timestamps independently so first/last are correct even if records
    # were not pre-sorted by the caller.
    timestamps = sorted(r.get("timestamp") for r in records if r.get("timestamp"))

    # Router decision stats for this run (Phase 8F)
    router_recs = sorted(
        [r for r in records if r.get("event") == "router_decision"],
        key=lambda r: r.get("timestamp") or "",
    )
    router_last_meta = (router_recs[-1].get("metadata") or {}) if router_recs else {}

    return {
        "run_id": base["run_id"],
        "success": base["success"],
        "final_stage": base["final_stage"],
        "event_count": base["total_events"],
        "failed_agent_count": base["failed_agent_results_count"],
        "failed_agents": failed_agents,
        "error_categories": error_categories,
        "first_timestamp": timestamps[0] if timestamps else None,
        "last_timestamp": timestamps[-1] if timestamps else None,
        # Router decision fields
        "router_decisions_count": len(router_recs),
        "router_failed_decisions_count": sum(
            1 for r in router_recs if r.get("success") is False
        ),
        "router_last_selected_backend": router_last_meta.get("selected_backend"),
        "router_last_applied_backend": router_last_meta.get("applied_backend"),
        "router_last_fallback_used": router_last_meta.get("fallback_used"),
    }


# ── IV. Cross-run aggregate ───────────────────────────────────────────────────

def summarize_all_runs(
    records: list[dict[str, Any]],
    recent_limit: int = 10,
) -> dict[str, Any]:
    """
    Aggregate statistics across all runs found in *records*.

    A run is "unknown" when it has no pipeline_completed event or
    when pipeline_completed.success is None.

    recent_runs is sorted by last_timestamp descending (most recent first).
    """
    groups = group_records_by_run(records)

    if not groups:
        return {
            "total_runs": 0,
            "success_runs": 0,
            "failed_runs": 0,
            "unknown_runs": 0,
            "failure_rate": 0.0,
            "failures_by_stage": {},
            "failures_by_agent": {},
            "runs_with_error_category": {},
            "recent_runs": [],
            **_empty_router_stats(),
        }

    per_run: list[dict[str, Any]] = [
        summarize_run_status(recs) for recs in groups.values()
    ]

    success_runs = sum(1 for r in per_run if r["success"] is True)
    failed_runs  = sum(1 for r in per_run if r["success"] is False)
    unknown_runs = sum(1 for r in per_run if r["success"] is None)
    total_runs   = len(per_run)
    failure_rate = failed_runs / total_runs if total_runs else 0.0

    # failures_by_stage — only from failed runs
    failures_by_stage: Counter = Counter()
    for r in per_run:
        if r["success"] is False and r["final_stage"]:
            failures_by_stage[r["final_stage"]] += 1

    # failures_by_agent — from agent_result events that failed
    failures_by_agent: Counter = Counter()
    for rec in records:
        if rec.get("event") == "agent_result" and rec.get("success") is False:
            agent = rec.get("agent", "unknown")
            failures_by_agent[agent] += 1

    # runs_with_error_category — counts how many distinct runs had each category.
    # Each run contributes at most 1 per category (error_categories is deduplicated
    # per-run).  This answers "in how many runs did timeout occur?" not "how many
    # total timeout events were there?".
    runs_with_error_category: Counter = Counter()
    for r in per_run:
        for cat in r["error_categories"]:
            runs_with_error_category[cat] += 1

    # recent_runs — sorted by last_timestamp desc
    recent = sorted(
        per_run,
        key=lambda r: r.get("last_timestamp") or "",
        reverse=True,
    )[:recent_limit]

    recent_runs = [
        {
            "run_id": r["run_id"],
            "success": r["success"],
            "final_stage": r["final_stage"],
            "error_categories": r["error_categories"],
            "last_timestamp": r["last_timestamp"],
        }
        for r in recent
    ]

    return {
        "total_runs": total_runs,
        "success_runs": success_runs,
        "failed_runs": failed_runs,
        "unknown_runs": unknown_runs,
        "failure_rate": round(failure_rate, 4),
        "failures_by_stage": dict(failures_by_stage.most_common()),
        "failures_by_agent": dict(failures_by_agent.most_common()),
        "runs_with_error_category": dict(runs_with_error_category.most_common()),
        "recent_runs": recent_runs,
        # Router decision aggregate (Phase 8F) — computed from the full flat records list
        # so every router_decision event is counted, regardless of run grouping.
        **_aggregate_router_decisions(records),
    }


# ── V. Human-readable output ──────────────────────────────────────────────────

def format_stats_summary(stats: dict[str, Any]) -> str:
    """
    Render a cross-run statistics summary as human-readable text.

    Never emits raw error text, prompt, API keys, or tokens.
    Only categories and counts are shown.
    """
    if not stats or stats.get("total_runs", 0) == 0:
        return "No execution records found."

    lines: list[str] = ["Execution Log Stats"]
    total  = stats["total_runs"]
    ok     = stats["success_runs"]
    failed = stats["failed_runs"]
    unk    = stats["unknown_runs"]
    rate   = stats["failure_rate"] * 100

    lines += [
        f"Total runs: {total}",
        f"Success: {ok}",
        f"Failed: {failed}",
        f"Unknown: {unk}",
        f"Failure rate: {rate:.1f}%",
    ]

    def _section(title: str, data: dict) -> None:
        if data:
            lines.append("")
            lines.append(f"{title}:")
            for key, count in data.items():
                lines.append(f"  - {key}: {count}")

    _section("Failures by stage", stats.get("failures_by_stage", {}))
    _section("Failures by agent", stats.get("failures_by_agent", {}))
    _section("Runs with error category", stats.get("runs_with_error_category", {}))

    # Router decision section — only shown when there are Router events
    router_total = stats.get("router_decisions_total", 0)
    if router_total > 0:
        lines.append("")
        lines.append("Router decisions:")
        lines.append(f"  - Total: {router_total}")
        lines.append(f"  - Failed: {stats.get('router_decisions_failed', 0)}")
        lines.append(f"  - Active: {stats.get('router_active_count', 0)}")
        lines.append(f"  - Dry-run: {stats.get('router_dry_run_count', 0)}")
        lines.append(f"  - Fallback: {stats.get('router_fallback_count', 0)}")
        _section("Router selected backends",
                 stats.get("router_selected_backend_counts", {}))
        _section("Router applied backends",
                 stats.get("router_applied_backend_counts", {}))
        pref_to_applied = stats.get("router_preferred_to_applied", {})
        if pref_to_applied:
            lines.append("")
            lines.append("Router preferred → applied:")
            for mapping, count in pref_to_applied.items():
                lines.append(f"  - {mapping}: {count}")

    recent = stats.get("recent_runs", [])
    if recent:
        lines.append("")
        lines.append("Recent runs:")
        for r in recent:
            status = "success" if r["success"] is True else (
                "failed" if r["success"] is False else "unknown"
            )
            cats = ",".join(r["error_categories"]) or "none"
            stage = r["final_stage"] or "?"
            lines.append(f"  - {r['run_id']} {status} stage={stage} errors={cats}")

    return "\n".join(lines)


# ── VI. CLI ───────────────────────────────────────────────────────────────────

def _cli_main() -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m telemetry.log_stats",
        description="Cross-run statistics from OpenClaw JSONL execution log",
    )
    parser.add_argument("--path", default="",
                        help="Path to JSONL log file. Defaults to settings.EXECUTION_LOG_PATH.")
    parser.add_argument("--recent-limit", type=int, default=10,
                        help="Number of recent runs to show (default: 10).")
    args = parser.parse_args()

    path = args.path
    if not path:
        try:
            from config.settings import settings
            path = settings.EXECUTION_LOG_PATH
        except Exception:
            pass

    if not path:
        print("EXECUTION_LOG_PATH is not configured.\nSet it in .env or pass --path <file>.",
              file=sys.stderr)
        return 1

    records = load_execution_records(path)
    stats = summarize_all_runs(records, recent_limit=args.recent_limit)
    print(format_stats_summary(stats))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli_main())
