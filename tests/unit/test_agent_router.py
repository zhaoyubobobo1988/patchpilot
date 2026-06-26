"""
tests/unit/test_agent_router.py

Unit tests for agents/router.py (Phase 8A).
No real CLI calls, no network, no pipeline modification.
"""
from __future__ import annotations

import pytest

from agents.router import (
    AgentRouteDecision,
    AgentRouteRequest,
    AgentRouter,
    build_reviewer_route_request_from_stats,
)

router = AgentRouter()


# ── helpers ───────────────────────────────────────────────────────────────────

def _req(role: str = "reviewer", preferred: str = "", cats: list[str] | None = None,
         **kw) -> AgentRouteRequest:
    return AgentRouteRequest(
        role=role,
        task_id="t1",
        run_id="r1",
        preferred_backend=preferred,
        failure_categories=cats or [],
        **kw,
    )


def _route(role: str = "reviewer", preferred: str = "",
           cats: list[str] | None = None) -> AgentRouteDecision:
    return router.route(_req(role=role, preferred=preferred, cats=cats))


# ═══════════════════════════════════════════════════════════════════════════════
# dry_run invariant
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("role,preferred,cats", [
    ("reviewer", "", []),
    ("reviewer", "claude-code", []),
    ("reviewer", "codex", []),
    ("reviewer", "codex", ["codex_config"]),
    ("reviewer", "codex", ["auth"]),
    ("reviewer", "invalid-backend", []),
    ("planner", "claude-code", []),
    ("worker", "", []),
])
def test_dry_run_is_always_true(role, preferred, cats):
    """All decisions must have dry_run=True in Phase 8A"""
    d = _route(role=role, preferred=preferred, cats=cats)
    assert d.dry_run is True


# ═══════════════════════════════════════════════════════════════════════════════
# Rule 1 — non-reviewer roles
# ═══════════════════════════════════════════════════════════════════════════════

def test_non_reviewer_planner_returns_preferred():
    d = _route(role="planner", preferred="claude-code")
    assert d.selected_backend == "claude-code"
    assert d.fallback_used is False


def test_non_reviewer_worker_empty_preferred_defaults_claude_code():
    d = _route(role="worker", preferred="")
    assert d.selected_backend == "claude-code"


def test_non_reviewer_reason_mentions_reviewer_only():
    d = _route(role="planner", preferred="claude-code")
    assert "reviewer" in d.reason.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# Rule 2 — reviewer + no preference
# ═══════════════════════════════════════════════════════════════════════════════

def test_reviewer_empty_preferred_returns_claude_code():
    d = _route(role="reviewer", preferred="")
    assert d.selected_backend == "claude-code"
    assert d.fallback_used is False


def test_reviewer_empty_preferred_reason_says_default():
    d = _route(role="reviewer", preferred="")
    assert "default" in d.reason.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# Rule 3 — reviewer + claude-code
# ═══════════════════════════════════════════════════════════════════════════════

def test_reviewer_claude_code_returns_claude_code():
    d = _route(role="reviewer", preferred="claude-code")
    assert d.selected_backend == "claude-code"
    assert d.fallback_used is False


# ═══════════════════════════════════════════════════════════════════════════════
# Rule 4 — reviewer + codex + risky categories
# ═══════════════════════════════════════════════════════════════════════════════

def test_reviewer_codex_with_codex_config_falls_back():
    d = _route(role="reviewer", preferred="codex", cats=["codex_config"])
    assert d.selected_backend == "claude-code"
    assert d.fallback_used is True


def test_reviewer_codex_with_auth_falls_back():
    d = _route(role="reviewer", preferred="codex", cats=["auth"])
    assert d.selected_backend == "claude-code"
    assert d.fallback_used is True


def test_reviewer_codex_with_both_risky_falls_back():
    d = _route(role="reviewer", preferred="codex", cats=["codex_config", "auth"])
    assert d.selected_backend == "claude-code"
    assert d.fallback_used is True
    # reason should mention what triggered the fallback
    assert "codex" in d.reason.lower()


def test_reviewer_codex_with_non_risky_category_does_not_fall_back():
    """timeout or review_blocked should not trigger codex fallback"""
    d = _route(role="reviewer", preferred="codex", cats=["timeout", "review_blocked"])
    assert d.selected_backend == "codex"
    assert d.fallback_used is False


# ═══════════════════════════════════════════════════════════════════════════════
# Rule 5 — reviewer + codex + no risky categories
# ═══════════════════════════════════════════════════════════════════════════════

def test_reviewer_codex_no_failures_returns_codex():
    d = _route(role="reviewer", preferred="codex", cats=[])
    assert d.selected_backend == "codex"
    assert d.fallback_used is False


# ═══════════════════════════════════════════════════════════════════════════════
# Rule 6 — reviewer + invalid backend
# ═══════════════════════════════════════════════════════════════════════════════

def test_reviewer_invalid_backend_returns_claude_code():
    d = _route(role="reviewer", preferred="opencode")
    assert d.selected_backend == "claude-code"
    assert d.fallback_used is True


def test_reviewer_invalid_backend_reason_mentions_invalid():
    d = _route(role="reviewer", preferred="aider")
    assert "invalid" in d.reason.lower() or "aider" in d.reason


# ═══════════════════════════════════════════════════════════════════════════════
# reason is always non-empty
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("role,preferred,cats", [
    ("reviewer", "", []),
    ("reviewer", "claude-code", []),
    ("reviewer", "codex", []),
    ("reviewer", "codex", ["codex_config"]),
    ("reviewer", "bad-value", []),
    ("planner", "claude-code", []),
])
def test_reason_is_always_non_empty(role, preferred, cats):
    d = _route(role=role, preferred=preferred, cats=cats)
    assert d.reason and isinstance(d.reason, str)


# ═══════════════════════════════════════════════════════════════════════════════
# metadata pass-through
# ═══════════════════════════════════════════════════════════════════════════════

def test_metadata_passes_through():
    req = _req(preferred="claude-code", metadata={"source": "test"})
    d = router.route(req)
    assert d.metadata.get("source") == "test"


def test_metadata_does_not_affect_routing_decision():
    """metadata is inert; same routing regardless of its content"""
    d1 = router.route(_req(preferred="codex", cats=[]))
    d2 = router.route(_req(preferred="codex", cats=[], metadata={"x": 99}))
    assert d1.selected_backend == d2.selected_backend
    assert d1.fallback_used == d2.fallback_used


# ═══════════════════════════════════════════════════════════════════════════════
# build_reviewer_route_request_from_stats
# ═══════════════════════════════════════════════════════════════════════════════

def test_build_request_collects_categories_from_recent_runs():
    stats = {
        "recent_runs": [
            {"run_id": "r1", "error_categories": ["timeout", "auth"]},
            {"run_id": "r2", "error_categories": ["timeout", "review_blocked"]},
        ]
    }
    req = build_reviewer_route_request_from_stats("t1", "r2", "codex", stats)
    assert "timeout" in req.failure_categories
    assert "auth" in req.failure_categories
    assert "review_blocked" in req.failure_categories


def test_build_request_deduplicates_categories():
    stats = {
        "recent_runs": [
            {"error_categories": ["timeout"]},
            {"error_categories": ["timeout", "auth"]},
        ]
    }
    req = build_reviewer_route_request_from_stats("t1", "r1", "codex", stats)
    assert req.failure_categories.count("timeout") == 1


def test_build_request_preserves_first_seen_order():
    stats = {
        "recent_runs": [
            {"error_categories": ["auth", "timeout"]},
            {"error_categories": ["review_blocked"]},
        ]
    }
    req = build_reviewer_route_request_from_stats("t1", "r1", "codex", stats)
    assert req.failure_categories[0] == "auth"
    assert req.failure_categories[1] == "timeout"
    assert req.failure_categories[2] == "review_blocked"


def test_build_request_safe_with_missing_stats_fields():
    """Missing or empty stats must not raise"""
    req = build_reviewer_route_request_from_stats("t1", "r1", "codex", {})
    assert req.failure_categories == []
    assert req.role == "reviewer"


def test_build_request_safe_with_empty_recent_runs():
    req = build_reviewer_route_request_from_stats("t1", "r1", "codex",
                                                   {"recent_runs": []})
    assert req.failure_categories == []


def test_build_request_sets_role_to_reviewer():
    req = build_reviewer_route_request_from_stats("t1", "r1", "claude-code", {})
    assert req.role == "reviewer"
    assert req.preferred_backend == "claude-code"


def test_build_request_then_route_integration():
    """Full flow: stats → request → router → decision"""
    stats = {
        "recent_runs": [
            {"error_categories": ["codex_config"]},
        ]
    }
    req = build_reviewer_route_request_from_stats("t1", "r1", "codex", stats)
    decision = router.route(req)
    assert decision.selected_backend == "claude-code"
    assert decision.fallback_used is True
    assert decision.dry_run is True


# ═══════════════════════════════════════════════════════════════════════════════
# confirm no pipeline / ReviewAgent side effects
# ═══════════════════════════════════════════════════════════════════════════════

def test_router_does_not_import_pipeline():
    """AgentRouter must not import pipeline or ReviewAgent"""
    import agents.router as mod
    # Check import statements only (word may appear in comments/docstrings)
    import_lines = [
        l for l in open(mod.__file__, encoding="utf-8").read().splitlines()
        if l.strip().startswith(("import ", "from "))
    ]
    joined = "\n".join(import_lines)
    assert "pipeline" not in joined
    assert "ReviewAgent" not in joined


def test_router_does_not_call_subprocess():
    """AgentRouter.route() must be a pure Python function (no subprocess)"""
    import subprocess
    from unittest.mock import patch
    with patch("subprocess.run") as mock_run, \
         patch("subprocess.Popen") as mock_popen:
        _route(role="reviewer", preferred="codex", cats=[])
    mock_run.assert_not_called()
    mock_popen.assert_not_called()
