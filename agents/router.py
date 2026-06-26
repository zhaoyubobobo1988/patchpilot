"""
agents/router.py — Agent Router: produces backend recommendations for ReviewAgent.

This module produces DRY-RUN backend recommendations based on a request
containing the preferred backend and recent failure categories.

Design constraints:
  - Router itself does NOT execute any Agent.
  - All decisions have dry_run=True in the router's own data model.
  - ReviewAgent._resolve_backend_name() uses the recommendation when
    ENABLE_REVIEW_ROUTER_ACTIVE=True (Phase 8C+).
  - pipeline.py is NOT modified.
  - No Claude CLI / Codex CLI calls.
  - No network / database access.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config.logging import get_logger

logger = get_logger(__name__)

_VALID_REVIEWER_BACKENDS = frozenset({"claude-code", "codex"})

# Failure categories that indicate the codex backend is unreliable
_CODEX_RISKY_CATEGORIES = frozenset({"codex_config", "auth"})


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class AgentRouteRequest:
    """
    Input to AgentRouter.route().

    Fields
    ------
    role              : Agent role being routed ("reviewer", "planner", …).
    task_id           : Pipeline task identifier (for logging only).
    run_id            : Pipeline run identifier (for logging only).
    preferred_backend : Caller's requested backend (e.g. from settings).
    failure_categories: Recent error categories from telemetry stats.
    metadata          : Reserved for future use; not used in routing decisions.
    """
    role: str
    task_id: str = ""
    run_id: str = ""
    preferred_backend: str = ""
    failure_categories: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentRouteDecision:
    """
    Output from AgentRouter.route().

    Fields
    ------
    role             : The role this decision applies to.
    selected_backend : The recommended backend.
    reason           : Short human-readable explanation (for logs/debug).
    dry_run          : Always True in the Router's data model.
                       Whether the recommendation is applied depends on
                       ReviewAgent's ENABLE_REVIEW_ROUTER_ACTIVE setting.
    fallback_used    : True when Router overrode preferred_backend due to risk.
    metadata         : Carries through request metadata + any routing notes.
    """
    role: str
    selected_backend: str
    reason: str
    dry_run: bool = True
    fallback_used: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Router ────────────────────────────────────────────────────────────────────

class AgentRouter:
    """
    Stateless router that maps an AgentRouteRequest to an AgentRouteDecision.

    Rules (evaluated in order):
      1. Non-reviewer role          → echo preferred_backend (or claude-code)
      2. reviewer + no preference   → claude-code (default)
      3. reviewer + claude-code     → claude-code (confirmed)
      4. reviewer + codex + risky   → claude-code (fallback; risky categories present)
      5. reviewer + codex + safe    → codex (preferred)
      6. reviewer + invalid backend → claude-code (fallback; invalid value)

    All decisions have dry_run=True — they are recommendations only.
    """

    def route(self, request: AgentRouteRequest) -> AgentRouteDecision:
        decision = self._decide(request)
        logger.info(
            f"[AgentRouter] role={request.role} "
            f"preferred={request.preferred_backend!r} "
            f"recommended={decision.selected_backend!r} "
            f"dry_run={decision.dry_run} "
            f"fallback_used={decision.fallback_used} "
            f"reason={decision.reason!r} "
            f"task_id={request.task_id} run_id={request.run_id}"
        )
        return decision

    def _decide(self, req: AgentRouteRequest) -> AgentRouteDecision:
        # Rule 1 — non-reviewer roles are passed through unchanged
        if req.role != "reviewer":
            backend = req.preferred_backend or "claude-code"
            return AgentRouteDecision(
                role=req.role,
                selected_backend=backend,
                reason="router currently only handles reviewer role",
                dry_run=True,
                fallback_used=False,
                metadata=dict(req.metadata),
            )

        preferred = req.preferred_backend

        # Rule 2 — no preference → default
        if not preferred:
            return AgentRouteDecision(
                role="reviewer",
                selected_backend="claude-code",
                reason="default reviewer backend",
                dry_run=True,
                metadata=dict(req.metadata),
            )

        # Rule 3 — claude-code requested
        if preferred == "claude-code":
            return AgentRouteDecision(
                role="reviewer",
                selected_backend="claude-code",
                reason="preferred backend is claude-code",
                dry_run=True,
                metadata=dict(req.metadata),
            )

        # Rule 4 & 5 — codex requested
        if preferred == "codex":
            risky = _CODEX_RISKY_CATEGORIES.intersection(req.failure_categories)
            if risky:
                return AgentRouteDecision(
                    role="reviewer",
                    selected_backend="claude-code",
                    reason=(
                        f"codex backend has recent {'/'.join(sorted(risky))} "
                        f"failures; recommend claude-code"
                    ),
                    dry_run=True,
                    fallback_used=True,
                    metadata=dict(req.metadata),
                )
            return AgentRouteDecision(
                role="reviewer",
                selected_backend="codex",
                reason="preferred backend is codex",
                dry_run=True,
                fallback_used=False,
                metadata=dict(req.metadata),
            )

        # Rule 6 — invalid backend
        return AgentRouteDecision(
            role="reviewer",
            selected_backend="claude-code",
            reason=f"invalid reviewer backend {preferred!r}; recommend claude-code",
            dry_run=True,
            fallback_used=True,
            metadata=dict(req.metadata),
        )


# ── Helper: build request from telemetry stats ────────────────────────────────

def build_reviewer_route_request_from_stats(
    task_id: str,
    run_id: str,
    preferred_backend: str,
    stats: dict[str, Any],
) -> AgentRouteRequest:
    """
    Build an AgentRouteRequest for the reviewer role using telemetry statistics.

    Collects error_categories from stats["recent_runs"], deduplicates them
    (preserving first-seen order), and returns a request ready for AgentRouter.

    Safe to call when *stats* is missing fields — returns empty failure_categories.
    Does not read files or call external services.
    """
    seen: set[str] = set()
    failure_categories: list[str] = []
    for run in stats.get("recent_runs", []):
        for cat in run.get("error_categories", []):
            if cat and cat not in seen:
                seen.add(cat)
                failure_categories.append(cat)

    return AgentRouteRequest(
        role="reviewer",
        task_id=task_id,
        run_id=run_id,
        preferred_backend=preferred_backend,
        failure_categories=failure_categories,
    )
