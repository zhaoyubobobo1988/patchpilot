"""
tests/unit/test_review_router_active.py

Tests for Phase 8C: ReviewAgent ACTIVE Router mode.
Verifies the four mode combinations:
  ACTIVE=False, DRY_RUN=False → no Router, use REVIEW_AGENT_BACKEND
  ACTIVE=False, DRY_RUN=True  → Router for observation, use REVIEW_AGENT_BACKEND
  ACTIVE=True,  DRY_RUN=False → Router decides, use decision.selected_backend
  ACTIVE=True,  DRY_RUN=True  → same as ACTIVE=True,DRY_RUN=False

No real Claude CLI / Codex CLI / network calls — all agents mocked.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.base import AgentResult
from agents.registry import AgentRegistry
from agents.review_agent.agent import ReviewAgent
from agents.router import AgentRouteDecision
from models.context import AgentContext
from models.patch import MergedPatch, PatchStatus
from models.task import FeatureTask


# ── shared helpers ────────────────────────────────────────────────────────────

@pytest.fixture
def ctx():
    return AgentContext(
        run_id="run-8c",
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
                  "task_id": "run-8c", "elapsed_seconds": 1.0},
    )


def _named_agent(name: str) -> MagicMock:
    """Create a named mock agent so we can assert which one was called."""
    agent = MagicMock()
    agent.run = AsyncMock(return_value=_ok_result(name))
    return agent


def _registry_with(*pairs) -> AgentRegistry:
    """Build a registry from (name, mock_agent) tuples."""
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


# ── 0. Default: ACTIVE=False ──────────────────────────────────────────────────

def test_default_enable_review_router_active_is_false():
    from config.settings import Settings
    fields = getattr(Settings, "model_fields", None) or Settings.__fields__
    default = getattr(fields["ENABLE_REVIEW_ROUTER_ACTIVE"], "default", None)
    assert default is False


# ── 1. ACTIVE=False, DRY_RUN=False → no Router ───────────────────────────────

@pytest.mark.asyncio
async def test_both_false_no_router_called(ctx, task, merged):
    codex = _named_agent("codex")
    claude = _named_agent("claude-code")
    registry = _registry_with(("codex", codex), ("claude-code", claude))

    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", False), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", False), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route") as mock_route:
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    mock_route.assert_not_called()
    assert agent.last_route_decision is None


@pytest.mark.asyncio
async def test_both_false_uses_review_agent_backend(ctx, task, merged):
    codex = _named_agent("codex")
    claude = _named_agent("claude-code")
    registry = _registry_with(("codex", codex), ("claude-code", claude))

    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", False), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", False), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "codex"), \
         patch("config.settings.settings.CODEX_TIMEOUT", 30), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route") as mock_route:
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    mock_route.assert_not_called()
    codex.run.assert_called()
    claude.run.assert_not_called()


# ── 2. ACTIVE=False, DRY_RUN=True → Router for observation only ──────────────

@pytest.mark.asyncio
async def test_dry_run_only_calls_router_but_backend_unchanged(ctx, task, merged):
    """DRY_RUN=True: Router is called but decision.selected_backend is NOT used"""
    codex = _named_agent("codex")
    claude = _named_agent("claude-code")
    registry = _registry_with(("codex", codex), ("claude-code", claude))

    # Router recommends claude-code but BACKEND is codex
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", False), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "codex"), \
         patch("config.settings.settings.CODEX_TIMEOUT", 30), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route",
               return_value=_decision("claude-code")) as mock_route:
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    mock_route.assert_called_once()
    # Router recommended claude-code, but real execution used codex
    codex.run.assert_called()
    claude.run.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_only_saves_last_route_decision(ctx, task, merged):
    registry = _registry_with(("claude-code", _named_agent("claude-code")))
    decision = _decision("claude-code")

    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", False), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route", return_value=decision):
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    assert agent.last_route_decision is decision


# ── 3. ACTIVE=True → Router decides real backend ──────────────────────────────

@pytest.mark.asyncio
async def test_active_uses_router_selected_backend(ctx, task, merged):
    """ACTIVE=True: Router recommendation → real backend"""
    codex = _named_agent("codex")
    claude = _named_agent("claude-code")
    registry = _registry_with(("codex", codex), ("claude-code", claude))

    # BACKEND=codex, Router→claude-code, ACTIVE=True → use claude-code
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", True), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", False), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "codex"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route",
               return_value=_decision("claude-code", fallback=True)):
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    claude.run.assert_called()
    codex.run.assert_not_called()


@pytest.mark.asyncio
async def test_active_backend_codex_preferred_to_codex(ctx, task, merged):
    """BACKEND=claude-code, Router→codex, ACTIVE=True → real codex"""
    codex = _named_agent("codex")
    claude = _named_agent("claude-code")
    registry = _registry_with(("codex", codex), ("claude-code", claude))

    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", True), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", False), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.CODEX_TIMEOUT", 30), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route",
               return_value=_decision("codex")):
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    codex.run.assert_called()
    claude.run.assert_not_called()


@pytest.mark.asyncio
async def test_active_saves_last_route_decision(ctx, task, merged):
    registry = _registry_with(("claude-code", _named_agent("claude-code")))
    decision = _decision("claude-code")

    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", True), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", False), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route", return_value=decision):
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    assert agent.last_route_decision is decision


@pytest.mark.asyncio
async def test_active_and_dry_run_both_true_uses_decision(ctx, task, merged):
    """ACTIVE=True and DRY_RUN=True: ACTIVE wins, uses decision.selected_backend"""
    codex = _named_agent("codex")
    claude = _named_agent("claude-code")
    registry = _registry_with(("codex", codex), ("claude-code", claude))

    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", True), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "codex"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route",
               return_value=_decision("claude-code")):
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    claude.run.assert_called()
    codex.run.assert_not_called()


# ── 4. Fallback on invalid / exception ───────────────────────────────────────

@pytest.mark.asyncio
async def test_active_invalid_router_backend_falls_back(ctx, task, merged):
    """Router returns invalid backend → fallback to REVIEW_AGENT_BACKEND"""
    codex = _named_agent("codex")
    claude = _named_agent("claude-code")
    registry = _registry_with(("codex", codex), ("claude-code", claude))

    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", True), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", False), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "codex"), \
         patch("config.settings.settings.CODEX_TIMEOUT", 30), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route",
               return_value=_decision("completely-invalid-backend")):
        agent = ReviewAgent(ctx)
        result = await agent.review(merged, task)

    assert result is not None
    # fallback to BACKEND=codex
    codex.run.assert_called()
    claude.run.assert_not_called()


@pytest.mark.asyncio
async def test_active_router_exception_falls_back(ctx, task, merged):
    """Router raises → fallback to REVIEW_AGENT_BACKEND, review does not fail"""
    codex = _named_agent("codex")
    claude = _named_agent("claude-code")
    registry = _registry_with(("codex", codex), ("claude-code", claude))

    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", True), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", False), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "codex"), \
         patch("config.settings.settings.CODEX_TIMEOUT", 30), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route",
               side_effect=RuntimeError("router crashed")):
        agent = ReviewAgent(ctx)
        result = await agent.review(merged, task)

    assert result.approved is True
    assert agent.last_route_decision is None
    codex.run.assert_called()   # BACKEND fallback
    claude.run.assert_not_called()


# ── 5. Codex selected but not in registry ────────────────────────────────────

@pytest.mark.asyncio
async def test_active_codex_not_in_registry_falls_to_litellm(ctx, task, merged):
    """Router selects codex, but registry has no codex → litellm fallback"""
    registry = AgentRegistry()   # empty — no codex registered
    mock_llm = MagicMock()
    mock_llm.choices[0].message.content = (
        '{"approved": true, "summary": "litellm ok", "comments": []}'
    )

    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", True), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", False), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route",
               return_value=_decision("codex")), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_llm)):
        agent = ReviewAgent(ctx)
        result = await agent.review(merged, task)

    assert result.approved is True   # litellm saved it
    assert result.summary == "litellm ok"


# ── 6. No pipeline.py modification ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_last_route_decision_cleared_on_protected_dir_block(ctx, task):
    """
    If the same ReviewAgent instance had a Router decision on a previous call,
    and the next call is blocked by the protected-dir precheck,
    last_route_decision must be None — not the stale value from the previous call.
    """
    registry = _registry_with(("claude-code", _named_agent("claude-code")))
    decision = _decision("claude-code")
    normal_diff = MergedPatch(
        feature_task_id="ft-001",
        merged_diff="diff --git a/features/auth/x.py b/features/auth/x.py\n+code\n",
        source_patch_ids=["s1"],
        status=PatchStatus.SUCCESS,
    )
    core_diff = MergedPatch(
        feature_task_id="ft-001",
        merged_diff="diff --git a/core/auth.py b/core/auth.py\n+x\n",
        source_patch_ids=["s1"],
        status=PatchStatus.SUCCESS,
    )

    agent = ReviewAgent(ctx)

    # First call: Router runs, decision is saved
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", False), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route", return_value=decision):
        await agent.review(normal_diff, task)

    assert agent.last_route_decision is not None  # set by first call

    # Second call: protected-dir blocks before Router → must clear decision
    with patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", False), \
         patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.router.AgentRouter.route") as mock_route:
        result = await agent.review(core_diff, task)

    assert result.approved is False          # blocked by precheck
    mock_route.assert_not_called()           # Router never reached
    assert agent.last_route_decision is None  # NOT the stale value


def test_review_agent_does_not_import_pipeline():
    import agents.review_agent.agent as mod
    import_lines = [
        l for l in open(mod.__file__, encoding="utf-8").read().splitlines()
        if l.strip().startswith(("import ", "from "))
    ]
    assert not any("pipeline" in l for l in import_lines)
