"""
tests/unit/test_review_router_stats.py

Tests for Phase 8D: ReviewAgent stats injection into AgentRouter.
Verifies that historical failure_categories from JSONL are wired into
AgentRouteRequest only when explicitly enabled.
No real CLI / network / JSONL file is used — telemetry functions are mocked.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from agents.base import AgentResult
from agents.registry import AgentRegistry
from agents.review_agent.agent import ReviewAgent
from agents.router import AgentRouteDecision
from models.context import AgentContext
from models.patch import MergedPatch, PatchStatus
from models.task import FeatureTask


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def ctx():
    return AgentContext(
        run_id="run-8d",
        feature_task_id="ft-001",
        repository="org/repo",
        base_branch="main",
        workspace_path="/tmp/ws",
        model="openai/glm-4-flash",
    )


@pytest.fixture
def task():
    return FeatureTask(
        raw_requirement="add rate limiting",
        feature_name="rate-limit",
        repository="org/repo",
    )


@pytest.fixture
def merged():
    return MergedPatch(
        feature_task_id="ft-001",
        merged_diff="diff --git a/features/auth/x.py b/features/auth/x.py\n+code\n",
        source_patch_ids=["s1"],
        status=PatchStatus.SUCCESS,
    )


def _ok_result(agent: str = "claude-code") -> AgentResult:
    return AgentResult(
        success=True,
        output='{"approved": true, "summary": "ok", "comments": []}',
        exit_code=0,
        metadata={"agent": agent, "role": "reviewer",
                  "task_id": "run-8d", "elapsed_seconds": 1.0},
    )


def _named_agent(name: str) -> MagicMock:
    m = MagicMock()
    m.run = AsyncMock(return_value=_ok_result(name))
    return m


def _registry_with(*pairs) -> AgentRegistry:
    r = AgentRegistry()
    for name, agent in pairs:
        r.register(name, agent)
    return r


def _decision(backend: str, fallback: bool = False) -> AgentRouteDecision:
    return AgentRouteDecision(
        role="reviewer",
        selected_backend=backend,
        reason="test",
        dry_run=True,
        fallback_used=fallback,
    )


# Typical stats returned by summarize_all_runs
def _fake_stats(cats_per_run: list[list[str]]) -> dict:
    return {
        "recent_runs": [
            {"run_id": f"r{i}", "error_categories": cats}
            for i, cats in enumerate(cats_per_run)
        ]
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 0. Default values
# ═══════════════════════════════════════════════════════════════════════════════

def test_default_enable_review_router_stats_is_false():
    from config.settings import Settings
    fields = getattr(Settings, "model_fields", None) or Settings.__fields__
    assert getattr(fields["ENABLE_REVIEW_ROUTER_STATS"], "default", None) is False


def test_default_review_router_stats_recent_limit():
    from config.settings import Settings
    fields = getattr(Settings, "model_fields", None) or Settings.__fields__
    assert getattr(fields["REVIEW_ROUTER_STATS_RECENT_LIMIT"], "default", None) == 10


# ═══════════════════════════════════════════════════════════════════════════════
# 1. _load_router_failure_categories: unit tests
# ═══════════════════════════════════════════════════════════════════════════════

def _make_agent(ctx) -> ReviewAgent:
    return ReviewAgent(ctx)


def test_load_categories_returns_empty_when_stats_disabled(ctx):
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_STATS", False), \
         patch("config.settings.settings.EXECUTION_LOG_PATH", "/some/path"):
        agent = _make_agent(ctx)
        cats = agent._load_router_failure_categories()
    assert cats == []


def test_load_categories_returns_empty_when_log_path_empty(ctx):
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_STATS", True), \
         patch("config.settings.settings.EXECUTION_LOG_PATH", ""):
        agent = _make_agent(ctx)
        cats = agent._load_router_failure_categories()
    assert cats == []


def test_load_categories_returns_categories_from_stats(ctx):
    stats = _fake_stats([["timeout", "auth"], ["codex_config"]])
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_STATS", True), \
         patch("config.settings.settings.EXECUTION_LOG_PATH", "/exec.jsonl"), \
         patch("config.settings.settings.REVIEW_ROUTER_STATS_RECENT_LIMIT", 10), \
         patch("telemetry.log_viewer.load_execution_records", return_value=[]), \
         patch("telemetry.log_stats.summarize_all_runs", return_value=stats):
        agent = _make_agent(ctx)
        cats = agent._load_router_failure_categories()
    assert "timeout" in cats
    assert "auth" in cats
    assert "codex_config" in cats


def test_load_categories_deduplicates(ctx):
    stats = _fake_stats([["timeout"], ["timeout", "auth"]])
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_STATS", True), \
         patch("config.settings.settings.EXECUTION_LOG_PATH", "/exec.jsonl"), \
         patch("config.settings.settings.REVIEW_ROUTER_STATS_RECENT_LIMIT", 10), \
         patch("telemetry.log_viewer.load_execution_records", return_value=[]), \
         patch("telemetry.log_stats.summarize_all_runs", return_value=stats):
        agent = _make_agent(ctx)
        cats = agent._load_router_failure_categories()
    assert cats.count("timeout") == 1


def test_load_categories_preserves_first_seen_order(ctx):
    stats = _fake_stats([["auth", "timeout"], ["codex_config"]])
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_STATS", True), \
         patch("config.settings.settings.EXECUTION_LOG_PATH", "/exec.jsonl"), \
         patch("config.settings.settings.REVIEW_ROUTER_STATS_RECENT_LIMIT", 10), \
         patch("telemetry.log_viewer.load_execution_records", return_value=[]), \
         patch("telemetry.log_stats.summarize_all_runs", return_value=stats):
        agent = _make_agent(ctx)
        cats = agent._load_router_failure_categories()
    assert cats[0] == "auth"
    assert cats[1] == "timeout"
    assert cats[2] == "codex_config"


def test_load_categories_safe_on_exception(ctx):
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_STATS", True), \
         patch("config.settings.settings.EXECUTION_LOG_PATH", "/exec.jsonl"), \
         patch("telemetry.log_viewer.load_execution_records",
               side_effect=OSError("file locked")):
        agent = _make_agent(ctx)
        cats = agent._load_router_failure_categories()
    assert cats == []


def test_load_categories_safe_when_log_missing(ctx):
    """load_execution_records returns [] for missing files — no exception"""
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_STATS", True), \
         patch("config.settings.settings.EXECUTION_LOG_PATH", "/no/such/file.jsonl"), \
         patch("config.settings.settings.REVIEW_ROUTER_STATS_RECENT_LIMIT", 10), \
         patch("telemetry.log_viewer.load_execution_records", return_value=[]), \
         patch("telemetry.log_stats.summarize_all_runs",
               return_value=_fake_stats([])):
        agent = _make_agent(ctx)
        cats = agent._load_router_failure_categories()
    assert cats == []


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Stats NOT loaded when Router is not active
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_stats_not_loaded_when_both_flags_off(ctx, task, merged):
    """ACTIVE=False, DRY_RUN=False: JSONL is never read even if STATS=True"""
    registry = _registry_with(("claude-code", _named_agent("claude-code")))
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", False), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", False), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_STATS", True), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("telemetry.log_viewer.load_execution_records") as mock_load:
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    mock_load.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Stats loaded when Router is active (DRY_RUN or ACTIVE)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_stats_loaded_when_dry_run_enabled(ctx, task, merged):
    """ACTIVE=False, DRY_RUN=True, STATS=True: JSONL is read"""
    registry = _registry_with(("claude-code", _named_agent("claude-code")))
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", False), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_STATS", True), \
         patch("config.settings.settings.EXECUTION_LOG_PATH", "/exec.jsonl"), \
         patch("config.settings.settings.REVIEW_ROUTER_STATS_RECENT_LIMIT", 5), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route", return_value=_decision("claude-code")), \
         patch("telemetry.log_viewer.load_execution_records", return_value=[]) as mock_load, \
         patch("telemetry.log_stats.summarize_all_runs",
               return_value=_fake_stats([])):
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    mock_load.assert_called_once()


@pytest.mark.asyncio
async def test_stats_loaded_when_active_enabled(ctx, task, merged):
    """ACTIVE=True, STATS=True: JSONL is read"""
    registry = _registry_with(("claude-code", _named_agent("claude-code")))
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", True), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", False), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_STATS", True), \
         patch("config.settings.settings.EXECUTION_LOG_PATH", "/exec.jsonl"), \
         patch("config.settings.settings.REVIEW_ROUTER_STATS_RECENT_LIMIT", 5), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route", return_value=_decision("claude-code")), \
         patch("telemetry.log_viewer.load_execution_records", return_value=[]) as mock_load, \
         patch("telemetry.log_stats.summarize_all_runs",
               return_value=_fake_stats([])):
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    mock_load.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# 4. failure_categories fed to Router
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_failure_categories_injected_into_router_request(ctx, task, merged):
    """categories from stats appear in AgentRouteRequest.failure_categories"""
    registry = _registry_with(("codex", _named_agent("codex")),
                               ("claude-code", _named_agent("claude-code")))
    captured = []

    def capture_route(request):
        captured.append(request)
        return _decision("claude-code", fallback=True)

    stats = _fake_stats([["codex_config", "auth"]])

    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", False), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_STATS", True), \
         patch("config.settings.settings.EXECUTION_LOG_PATH", "/exec.jsonl"), \
         patch("config.settings.settings.REVIEW_ROUTER_STATS_RECENT_LIMIT", 10), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "codex"), \
         patch("config.settings.settings.CODEX_TIMEOUT", 30), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route", side_effect=capture_route), \
         patch("telemetry.log_viewer.load_execution_records", return_value=[]), \
         patch("telemetry.log_stats.summarize_all_runs", return_value=stats):
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    assert len(captured) == 1
    assert "codex_config" in captured[0].failure_categories
    assert "auth" in captured[0].failure_categories


# ═══════════════════════════════════════════════════════════════════════════════
# 5. End-to-end: stats → Router → real backend
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_active_stats_codex_config_triggers_claude_code(ctx, task, merged):
    """
    PREFERRED=codex, stats show codex_config failure, ACTIVE=True:
    Router recommends claude-code → real backend is claude-code.
    """
    codex = _named_agent("codex")
    claude = _named_agent("claude-code")
    registry = _registry_with(("codex", codex), ("claude-code", claude))
    stats = _fake_stats([["codex_config"]])

    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", True), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", False), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_STATS", True), \
         patch("config.settings.settings.EXECUTION_LOG_PATH", "/exec.jsonl"), \
         patch("config.settings.settings.REVIEW_ROUTER_STATS_RECENT_LIMIT", 10), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "codex"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("telemetry.log_viewer.load_execution_records", return_value=[]), \
         patch("telemetry.log_stats.summarize_all_runs", return_value=stats):
        agent = ReviewAgent(ctx)
        result = await agent.review(merged, task)

    assert result.approved is True
    claude.run.assert_called()   # Router recommended claude-code and ACTIVE used it
    codex.run.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_stats_auth_records_decision_but_uses_codex(ctx, task, merged):
    """
    PREFERRED=codex, stats show auth failure, DRY_RUN=True, ACTIVE=False:
    Router recommends claude-code, last_route_decision set,
    but real execution still uses codex.
    """
    codex = _named_agent("codex")
    claude = _named_agent("claude-code")
    registry = _registry_with(("codex", codex), ("claude-code", claude))
    stats = _fake_stats([["auth"]])

    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", False), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_STATS", True), \
         patch("config.settings.settings.EXECUTION_LOG_PATH", "/exec.jsonl"), \
         patch("config.settings.settings.REVIEW_ROUTER_STATS_RECENT_LIMIT", 10), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "codex"), \
         patch("config.settings.settings.CODEX_TIMEOUT", 30), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("telemetry.log_viewer.load_execution_records", return_value=[]), \
         patch("telemetry.log_stats.summarize_all_runs", return_value=stats):
        agent = ReviewAgent(ctx)
        result = await agent.review(merged, task)

    assert agent.last_route_decision is not None
    assert agent.last_route_decision.selected_backend == "claude-code"
    codex.run.assert_called()   # DRY_RUN=True → real backend unchanged
    claude.run.assert_not_called()


@pytest.mark.asyncio
async def test_review_survives_stats_exception(ctx, task, merged):
    """stats read throws → review still completes with REVIEW_AGENT_BACKEND"""
    claude = _named_agent("claude-code")
    registry = _registry_with(("claude-code", claude))

    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", True), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", False), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_STATS", True), \
         patch("config.settings.settings.EXECUTION_LOG_PATH", "/exec.jsonl"), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("telemetry.log_viewer.load_execution_records",
               side_effect=RuntimeError("disk error")):
        agent = ReviewAgent(ctx)
        result = await agent.review(merged, task)

    # stats error → failure_categories=[] → Router runs normally
    assert result.approved is True
    claude.run.assert_called()
