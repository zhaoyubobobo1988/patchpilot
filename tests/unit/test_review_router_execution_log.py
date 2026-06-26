"""
tests/unit/test_review_router_execution_log.py

Tests for Phase 8E: router_decision JSONL event written by ReviewAgent.
No real CLI / network. telemetry.execution_log.record_execution is patched
throughout so no actual file I/O occurs (unless explicitly tested).
"""
from __future__ import annotations

import contextlib
import json
import tempfile
from pathlib import Path
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
        run_id="run-8e",
        feature_task_id="ft-001",
        repository="org/repo",
        base_branch="main",
        workspace_path="/tmp/ws",
        model="openai/glm-4-flash",
    )


@pytest.fixture
def task():
    return FeatureTask(
        raw_requirement="add feature",
        feature_name="feature",
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
                  "task_id": "run-8e", "elapsed_seconds": 1.0},
    )


def _registry(*names: str) -> AgentRegistry:
    r = AgentRegistry()
    for name in names:
        m = MagicMock()
        m.run = AsyncMock(return_value=_ok_result(name))
        r.register(name, m)
    return r


def _decision(backend: str, fallback: bool = False,
              reason: str = "test reason") -> AgentRouteDecision:
    return AgentRouteDecision(
        role="reviewer",
        selected_backend=backend,
        reason=reason,
        dry_run=True,
        fallback_used=fallback,
    )


def _make_stack(
    active: bool = False,
    dry_run: bool = True,
    backend: str = "claude-code",
    reg=None,
    decision: AgentRouteDecision | None = None,
    router_exc: Exception | None = None,
    record_side_effect=None,
    log_path: str = "",
    stats_enabled: bool = False,
) -> contextlib.ExitStack:
    """Build a contextlib.ExitStack with common patches."""
    stack = contextlib.ExitStack()
    stack.enter_context(patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", active))
    stack.enter_context(patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", dry_run))
    stack.enter_context(patch("config.settings.settings.ENABLE_REVIEW_ROUTER_STATS", stats_enabled))
    stack.enter_context(patch("config.settings.settings.REVIEW_AGENT_BACKEND", backend))
    stack.enter_context(patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"))
    stack.enter_context(patch("config.settings.settings.CLAUDE_CODE_TIMEOUT", 30))
    stack.enter_context(patch("config.settings.settings.CODEX_TIMEOUT", 30))
    stack.enter_context(patch("config.settings.settings.EXECUTION_LOG_PATH", log_path))
    stack.enter_context(patch("agents.review_agent.agent.default_registry",
                              reg or _registry("claude-code", "codex")))
    if router_exc is not None:
        stack.enter_context(
            patch("agents.router.AgentRouter.route", side_effect=router_exc))
    elif decision is not None:
        stack.enter_context(
            patch("agents.router.AgentRouter.route", return_value=decision))
    if record_side_effect is not None:
        stack.enter_context(
            patch("telemetry.execution_log.record_execution",
                  side_effect=record_side_effect))
    return stack


def _router_events(mock_rec) -> list:
    return [c.args[0] for c in mock_rec.call_args_list
            if c.args[0].event == "router_decision"]


# ═══════════════════════════════════════════════════════════════════════════════
# 1 & 2. Router NOT called → no router_decision event
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_no_event_when_both_flags_off(ctx, task, merged):
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", False))
        stack.enter_context(patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", False))
        stack.enter_context(patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"))
        stack.enter_context(patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"))
        stack.enter_context(patch("agents.review_agent.agent.default_registry", _registry("claude-code")))
        mock_rec = stack.enter_context(patch("telemetry.execution_log.record_execution"))
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    assert len(_router_events(mock_rec)) == 0


@pytest.mark.asyncio
async def test_no_event_when_protected_dir_blocks(ctx, task):
    core_diff = MergedPatch(
        feature_task_id="ft-001",
        merged_diff="diff --git a/core/auth.py b/core/auth.py\n+x\n",
        source_patch_ids=["s1"],
        status=PatchStatus.SUCCESS,
    )
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", False))
        stack.enter_context(patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True))
        stack.enter_context(patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"))
        stack.enter_context(patch("agents.review_agent.agent.default_registry", _registry("claude-code")))
        mock_rec = stack.enter_context(patch("telemetry.execution_log.record_execution"))
        agent = ReviewAgent(ctx)
        result = await agent.review(core_diff, task)

    assert result.approved is False
    assert len(_router_events(mock_rec)) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 3 & 4. Event written when Router is called
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_event_written_on_dry_run(ctx, task, merged):
    with _make_stack(dry_run=True, decision=_decision("claude-code")) as stack:
        mock_rec = stack.enter_context(patch("telemetry.execution_log.record_execution"))
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    evs = _router_events(mock_rec)
    assert len(evs) == 1
    assert evs[0].success is True


@pytest.mark.asyncio
async def test_event_written_on_active(ctx, task, merged):
    with _make_stack(active=True, dry_run=False, decision=_decision("claude-code")) as stack:
        mock_rec = stack.enter_context(patch("telemetry.execution_log.record_execution"))
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    evs = _router_events(mock_rec)
    assert len(evs) == 1
    assert evs[0].success is True


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Metadata fields present
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_event_metadata_contains_required_fields(ctx, task, merged):
    with _make_stack(active=True, dry_run=False, backend="codex",
                     decision=_decision("claude-code", fallback=True,
                                        reason="codex has recent failures")) as stack:
        mock_rec = stack.enter_context(patch("telemetry.execution_log.record_execution"))
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    rec = _router_events(mock_rec)[0]
    for key in ("preferred_backend", "selected_backend", "applied_backend",
                "active", "dry_run", "stats_enabled",
                "fallback_used", "reason", "failure_categories"):
        assert key in rec.metadata, f"missing metadata key: {key}"


# ═══════════════════════════════════════════════════════════════════════════════
# 6 & 7. applied_backend correctness
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_applied_backend_is_preferred_when_active_false(ctx, task, merged):
    with _make_stack(active=False, dry_run=True, backend="codex",
                     decision=_decision("claude-code")) as stack:
        mock_rec = stack.enter_context(patch("telemetry.execution_log.record_execution"))
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    rec = _router_events(mock_rec)[0]
    assert rec.metadata["preferred_backend"] == "codex"
    assert rec.metadata["applied_backend"] == "codex"


@pytest.mark.asyncio
async def test_applied_backend_is_decision_when_active_true(ctx, task, merged):
    with _make_stack(active=True, dry_run=False, backend="codex",
                     decision=_decision("claude-code", fallback=True)) as stack:
        mock_rec = stack.enter_context(patch("telemetry.execution_log.record_execution"))
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    rec = _router_events(mock_rec)[0]
    assert rec.metadata["applied_backend"] == "claude-code"


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Invalid selected_backend → applied == preferred
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_applied_backend_is_preferred_on_invalid_selection(ctx, task, merged):
    with _make_stack(active=True, dry_run=False, backend="claude-code",
                     decision=_decision("total-garbage")) as stack:
        mock_rec = stack.enter_context(patch("telemetry.execution_log.record_execution"))
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    rec = _router_events(mock_rec)[0]
    assert rec.metadata["selected_backend"] == "total-garbage"
    assert rec.metadata["applied_backend"] == "claude-code"


# ═══════════════════════════════════════════════════════════════════════════════
# 9 & 10. Router exception path
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_event_success_false_on_router_exception(ctx, task, merged):
    with _make_stack(dry_run=True,
                     router_exc=RuntimeError("router exploded")) as stack:
        mock_rec = stack.enter_context(patch("telemetry.execution_log.record_execution"))
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    rec = _router_events(mock_rec)[0]
    assert rec.success is False
    assert rec.error is not None
    assert "router exploded" in rec.error


@pytest.mark.asyncio
async def test_review_succeeds_after_router_exception(ctx, task, merged):
    with _make_stack(active=True, dry_run=False, backend="claude-code",
                     router_exc=ValueError("bad")) as stack:
        stack.enter_context(patch("telemetry.execution_log.record_execution"))
        agent = ReviewAgent(ctx)
        result = await agent.review(merged, task)

    assert result.approved is True


# ═══════════════════════════════════════════════════════════════════════════════
# 11. failure_categories in metadata
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_failure_categories_in_metadata(ctx, task, merged):
    cats = ["timeout", "auth"]
    with _make_stack(active=True, dry_run=False, backend="codex",
                     decision=_decision("claude-code")) as stack:
        stack.enter_context(
            patch("agents.review_agent.agent.ReviewAgent._load_router_failure_categories",
                  return_value=cats))
        mock_rec = stack.enter_context(patch("telemetry.execution_log.record_execution"))
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    rec = _router_events(mock_rec)[0]
    assert rec.metadata["failure_categories"] == ["timeout", "auth"]


@pytest.mark.asyncio
async def test_non_string_failure_categories_filtered(ctx, task, merged):
    cats_with_garbage: list = ["auth", 42, None, "timeout"]  # type: ignore
    with _make_stack(dry_run=True, decision=_decision("claude-code")) as stack:
        stack.enter_context(
            patch("agents.review_agent.agent.ReviewAgent._load_router_failure_categories",
                  return_value=cats_with_garbage))
        mock_rec = stack.enter_context(patch("telemetry.execution_log.record_execution"))
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    rec = _router_events(mock_rec)[0]
    assert rec.metadata["failure_categories"] == ["auth", "timeout"]


# ═══════════════════════════════════════════════════════════════════════════════
# 12. reason truncation
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_reason_truncated_to_300_chars(ctx, task, merged):
    dec = _decision("claude-code", reason="x" * 500)
    with _make_stack(dry_run=True, decision=dec) as stack:
        mock_rec = stack.enter_context(patch("telemetry.execution_log.record_execution"))
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    rec = _router_events(mock_rec)[0]
    assert len(rec.metadata["reason"]) <= 300


# ═══════════════════════════════════════════════════════════════════════════════
# 13. No sensitive content
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_event_does_not_contain_sensitive_content(ctx, task, merged):
    with _make_stack(dry_run=True, decision=_decision("claude-code")) as stack:
        mock_rec = stack.enter_context(patch("telemetry.execution_log.record_execution"))
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    rec = _router_events(mock_rec)[0]
    rec_str = json.dumps(rec.metadata)
    assert "diff --git" not in rec_str
    assert "PROMPT" not in rec_str
    assert "sk-" not in rec_str


# ═══════════════════════════════════════════════════════════════════════════════
# 14. EXECUTION_LOG_PATH empty → no file
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_no_file_created_when_log_path_empty(ctx, task, merged, tmp_path):
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", False))
        stack.enter_context(patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True))
        stack.enter_context(patch("config.settings.settings.ENABLE_REVIEW_ROUTER_STATS", False))
        stack.enter_context(patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"))
        stack.enter_context(patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"))
        stack.enter_context(patch("config.settings.settings.EXECUTION_LOG_PATH", ""))
        stack.enter_context(patch("agents.review_agent.agent.default_registry", _registry("claude-code")))
        stack.enter_context(patch("agents.router.AgentRouter.route", return_value=_decision("claude-code")))
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    assert list(tmp_path.iterdir()) == []


# ═══════════════════════════════════════════════════════════════════════════════
# 15. Write failure doesn't affect review
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_record_failure_does_not_affect_review(ctx, task, merged):
    with _make_stack(dry_run=True, decision=_decision("claude-code"),
                     record_side_effect=IOError("disk full")) as stack:
        agent = ReviewAgent(ctx)
        result = await agent.review(merged, task)

    assert result.approved is True


# ═══════════════════════════════════════════════════════════════════════════════
# 16. Real JSONL file written
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sensitive_error_text_is_redacted(ctx, task, merged):
    """Router exception containing API key → JSONL error must not contain raw key"""
    exc_msg = "OPENAI_API_KEY=sk-secret token=abc connection refused"
    with _make_stack(dry_run=True, router_exc=RuntimeError(exc_msg)) as stack:
        mock_rec = stack.enter_context(patch("telemetry.execution_log.record_execution"))
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    rec = _router_events(mock_rec)[0]
    assert rec.success is False
    assert "sk-secret" not in (rec.error or "")
    assert "abc" not in (rec.error or "")  # token value redacted
    assert "[REDACTED]" in (rec.error or "")


@pytest.mark.asyncio
async def test_exception_branch_failure_categories_is_empty(ctx, task, merged):
    """Router exception → failure_categories in metadata must be [] (P3 fix)"""
    loaded_cats = ["timeout", "auth"]   # stats were loaded before Router crashed
    with _make_stack(dry_run=True, router_exc=RuntimeError("oops")) as stack:
        stack.enter_context(
            patch("agents.review_agent.agent.ReviewAgent._load_router_failure_categories",
                  return_value=loaded_cats))
        mock_rec = stack.enter_context(patch("telemetry.execution_log.record_execution"))
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    rec = _router_events(mock_rec)[0]
    assert rec.success is False
    # Exception events must show [] — the categories were loaded but Router never used them
    assert rec.metadata["failure_categories"] == []


@pytest.mark.asyncio
async def test_router_decision_written_to_jsonl_file(ctx, task, merged, tmp_path):
    log = str(tmp_path / "exec.jsonl")
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("config.settings.settings.ENABLE_REVIEW_ROUTER_ACTIVE", False))
        stack.enter_context(patch("config.settings.settings.ENABLE_REVIEW_ROUTER_DRY_RUN", True))
        stack.enter_context(patch("config.settings.settings.ENABLE_REVIEW_ROUTER_STATS", False))
        stack.enter_context(patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"))
        stack.enter_context(patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"))
        stack.enter_context(patch("config.settings.settings.EXECUTION_LOG_PATH", log))
        stack.enter_context(patch("agents.review_agent.agent.default_registry", _registry("claude-code")))
        stack.enter_context(patch("agents.router.AgentRouter.route",
                                  return_value=_decision("claude-code")))
        agent = ReviewAgent(ctx)
        await agent.review(merged, task)

    lines = [json.loads(l) for l in Path(log).read_text(encoding="utf-8").splitlines()
             if l.strip()]
    router_lines = [l for l in lines if l.get("event") == "router_decision"]
    assert len(router_lines) == 1
    assert router_lines[0]["agent"] == "router"
    assert router_lines[0]["success"] is True
    assert "applied_backend" in router_lines[0]["metadata"]
