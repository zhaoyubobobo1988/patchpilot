"""
tests/unit/test_review_router_dry_run.py

Tests for Phase 8B: ReviewAgent dry-run Router integration.
Verifies that:
  - Router is only called when ENABLE_REVIEW_ROUTER_DRY_RUN=True
  - Router recommendation is recorded in last_route_decision
  - True execution backend is NEVER changed by Router recommendation
  - Router exceptions do NOT fail the review
No real Claude CLI / Codex CLI / network calls.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
        run_id="run-8b",
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


def _ok_agent_result():
    from agents.base import AgentResult
    return AgentResult(
        success=True,
        output='{"approved": true, "summary": "ok", "comments": []}',
        exit_code=0,
        metadata={"agent": "claude-code", "role": "reviewer",
                  "task_id": "run-8b", "elapsed_seconds": 1.0},
    )


def _make_test_registry(mock_result=None) -> AgentRegistry:
    registry = AgentRegistry()
    agent = MagicMock()
    agent.run = AsyncMock(return_value=mock_result or _ok_agent_result())
    registry.register("claude-code", agent)
    registry.register("codex", agent)
    return registry


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Default: ENABLE_REVIEW_ROUTER_DRY_RUN=False
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_router_not_called_when_dry_run_disabled(ctx, task, merged):
    """When ENABLE_REVIEW_ROUTER_DRY_RUN=False, AgentRouter.route must never be called"""
    registry = _make_test_registry()
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", False), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route") as mock_route:
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    mock_route.assert_not_called()


@pytest.mark.asyncio
async def test_last_route_decision_is_none_when_disabled(ctx, task, merged):
    """last_route_decision must remain None when dry-run is disabled"""
    registry = _make_test_registry()
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", False), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry):
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    assert agent.last_route_decision is None


def test_default_enable_review_router_dry_run_is_false():
    """ENABLE_REVIEW_ROUTER_DRY_RUN must default to False"""
    from config.settings import Settings
    fields = getattr(Settings, "model_fields", None) or Settings.__fields__
    default = getattr(fields["ENABLE_REVIEW_ROUTER_DRY_RUN"], "default", None)
    assert default is False


# ═══════════════════════════════════════════════════════════════════════════════
# 2. ENABLE_REVIEW_ROUTER_DRY_RUN=True: Router is called
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_router_called_when_dry_run_enabled(ctx, task, merged):
    """When enabled, AgentRouter.route must be called once"""
    registry = _make_test_registry()
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route",
               return_value=AgentRouteDecision(
                   role="reviewer", selected_backend="claude-code",
                   reason="preferred backend is claude-code", dry_run=True,
               )) as mock_route:
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    mock_route.assert_called_once()


@pytest.mark.asyncio
async def test_router_request_role_is_reviewer(ctx, task, merged):
    """Router request.role must always be 'reviewer'"""
    registry = _make_test_registry()
    captured = []

    def capture_route(request):
        captured.append(request)
        return AgentRouteDecision(
            role="reviewer", selected_backend="claude-code",
            reason="test", dry_run=True,
        )

    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route", side_effect=capture_route):
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    assert len(captured) == 1
    assert captured[0].role == "reviewer"


@pytest.mark.asyncio
async def test_router_request_preferred_backend_matches_setting(ctx, task, merged):
    """Router request.preferred_backend must equal settings.REVIEW_AGENT_BACKEND"""
    registry = _make_test_registry()
    captured = []

    def capture_route(request):
        captured.append(request)
        return AgentRouteDecision(
            role="reviewer", selected_backend="codex",
            reason="test", dry_run=True,
        )

    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "codex"), \
         patch("config.settings.settings.CODEX_TIMEOUT", 30), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route", side_effect=capture_route):
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    assert captured[0].preferred_backend == "codex"


@pytest.mark.asyncio
async def test_last_route_decision_saved_after_router_call(ctx, task, merged):
    """last_route_decision must be set to the AgentRouteDecision after a successful call"""
    registry = _make_test_registry()
    expected = AgentRouteDecision(
        role="reviewer", selected_backend="claude-code",
        reason="preferred backend is claude-code", dry_run=True,
    )
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route", return_value=expected):
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    assert agent.last_route_decision is expected
    assert agent.last_route_decision.dry_run is True


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Router recommendation does NOT change real execution
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_real_backend_unaffected_by_router_recommendation(ctx, task, merged):
    """
    Scenario: REVIEW_AGENT_BACKEND=codex, Router recommends claude-code.
    The real execution must still use CodexAgent (from registry), not ClaudeCodeAgent.
    """
    # Track which registry agents are called
    codex_agent = MagicMock()
    codex_agent.run = AsyncMock(return_value=_ok_agent_result())
    claude_agent = MagicMock()
    claude_agent.run = AsyncMock(return_value=_ok_agent_result())

    registry = AgentRegistry()
    registry.register("codex", codex_agent)
    registry.register("claude-code", claude_agent)

    # Router will recommend claude-code…
    router_decision = AgentRouteDecision(
        role="reviewer", selected_backend="claude-code",
        reason="codex has recent auth failures; recommend claude-code",
        dry_run=True, fallback_used=True,
    )

    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "codex"),   \
         patch("config.settings.settings.CODEX_TIMEOUT", 30), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route", return_value=router_decision):
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    # …but CodexAgent (not ClaudeCodeAgent) must have been called
    codex_agent.run.assert_called()
    claude_agent.run.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Router exception is non-fatal
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_router_exception_does_not_fail_review(ctx, task, merged):
    """If AgentRouter.route raises, ReviewAgent must still complete successfully"""
    registry = _make_test_registry()
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route",
               side_effect=RuntimeError("router exploded")):
        agent = ReviewAgent(ctx)
        result = await agent.review(merged, task)   # must not raise

    assert result is not None
    assert result.approved is True


@pytest.mark.asyncio
async def test_last_route_decision_is_none_after_router_exception(ctx, task, merged):
    """Router exception → last_route_decision stays None"""
    registry = _make_test_registry()
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route",
               side_effect=ValueError("bad request")):
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    assert agent.last_route_decision is None


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Existing review behavior unchanged
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_review_result_still_correct_when_dry_run_enabled(ctx, task, merged):
    """Enabling dry-run must not change ReviewResult outcome"""
    registry = _make_test_registry()
    router_decision = AgentRouteDecision(
        role="reviewer", selected_backend="claude-code",
        reason="test", dry_run=True,
    )
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route", return_value=router_decision):
        agent = ReviewAgent(ctx)
        result = await agent.review(merged, task)

    assert result.approved is True
    assert result.summary == "ok"


@pytest.mark.asyncio
async def test_protected_dir_check_still_blocks_when_router_enabled(ctx, task):
    """Protected-dir pre-check runs BEFORE Router; Router must NOT be called when blocked"""
    core_diff = MergedPatch(
        feature_task_id="ft-001",
        merged_diff="diff --git a/core/auth.py b/core/auth.py\n+x\n",
        source_patch_ids=["s1"],
        status=PatchStatus.SUCCESS,
    )
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True), \
         patch("agents.router.AgentRouter.route") as mock_route:
        agent = ReviewAgent(ctx)
        result = await agent.review(core_diff, task)

    assert result.approved is False
    # Protected-dir check returns early BEFORE Router is called
    mock_route.assert_not_called()


@pytest.mark.asyncio
async def test_last_route_decision_cleared_when_dry_run_disabled_after_enabled(
    ctx, task, merged
):
    """
    Same ReviewAgent instance: run once with dry-run enabled, then again disabled.
    After the second review, last_route_decision must be None — not the stale old value.
    """
    registry = _make_test_registry()
    decision = AgentRouteDecision(
        role="reviewer", selected_backend="claude-code",
        reason="test", dry_run=True,
    )

    # First call: enabled → last_route_decision gets set
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route", return_value=decision):
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    assert agent.last_route_decision is not None  # set by first call

    # Second call: disabled → last_route_decision must be cleared
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", False), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry):
        await agent.review(merged, task)

    assert agent.last_route_decision is None  # cleared, not stale
