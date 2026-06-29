"""
tests/unit/test_integrator.py

Unit tests for IntegratorAgent.
No LLM / Claude CLI / Codex CLI calls — IntegratorAgent is deterministic Python.
Integration test subprocess (asyncio.create_subprocess_shell) is mocked.
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.integrator.agent import IntegratorAgent
from models.context import AgentContext
from models.patch import MergedPatch, PatchStatus
from models.task import FeatureTask


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def ctx():
    return AgentContext(
        run_id="test-run",
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


def _ok_patch(diff: str = "", source_ids: list[str] | None = None) -> MergedPatch:
    return MergedPatch(
        feature_task_id="ft-001",
        merged_diff=diff or "diff --git a/features/auth/x.py b/features/auth/x.py\n+code\n",
        source_patch_ids=source_ids or ["s1", "s2"],
        conflicts_resolved=0,
        status=PatchStatus.SUCCESS,
    )


def _failed_patch() -> MergedPatch:
    return MergedPatch(
        feature_task_id="ft-001",
        merged_diff="",
        source_patch_ids=[],
        status=PatchStatus.FAILED,
        error_details="No successful patches to merge",
    )


# ── normal case ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_integrate_returns_same_merged_patch_type(ctx, task):
    """integrate() must return a MergedPatch (same type as input)"""
    agent = IntegratorAgent(ctx)
    result = await agent.integrate(_ok_patch(), task)
    assert isinstance(result, MergedPatch)


@pytest.mark.asyncio
async def test_integrate_does_not_modify_diff(ctx, task):
    """Integrator must NOT change merged_diff content"""
    original_diff = "diff --git a/features/auth/x.py b/features/auth/x.py\n+import x\n"
    patch = _ok_patch(diff=original_diff)
    agent = IntegratorAgent(ctx)
    result = await agent.integrate(patch, task)
    assert result.merged_diff == original_diff


@pytest.mark.asyncio
async def test_integrate_preserves_all_fields(ctx, task):
    """source_patch_ids, conflicts_resolved, status are unchanged"""
    patch = MergedPatch(
        feature_task_id="ft-001",
        merged_diff="diff --git a/features/x.py b/features/x.py\n+x\n",
        source_patch_ids=["a", "b", "c"],
        conflicts_resolved=2,
        status=PatchStatus.SUCCESS,
    )
    agent = IntegratorAgent(ctx)
    result = await agent.integrate(patch, task)
    assert result.source_patch_ids == ["a", "b", "c"]
    assert result.conflicts_resolved == 2
    assert result.status == PatchStatus.SUCCESS


# ── empty diff ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_integrate_raises_on_empty_diff(ctx, task):
    """Empty merged_diff with SUCCESS status must raise ValueError"""
    patch = MergedPatch(
        feature_task_id="ft-001",
        merged_diff="",
        source_patch_ids=["s1"],
        status=PatchStatus.SUCCESS,
    )
    agent = IntegratorAgent(ctx)
    with pytest.raises(ValueError, match="empty"):
        await agent.integrate(patch, task)


@pytest.mark.asyncio
async def test_integrate_raises_on_whitespace_only_diff(ctx, task):
    """Whitespace-only diff is treated as empty"""
    patch = MergedPatch(
        feature_task_id="ft-001",
        merged_diff="   \n\n  ",
        source_patch_ids=["s1"],
        status=PatchStatus.SUCCESS,
    )
    agent = IntegratorAgent(ctx)
    with pytest.raises(ValueError):
        await agent.integrate(patch, task)


# ── FAILED patch → ValueError (Phase 6B) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_integrate_raises_on_failed_patch(ctx, task):
    """FAILED MergedPatch must raise ValueError (not return silently)"""
    agent = IntegratorAgent(ctx)
    with pytest.raises(ValueError):
        await agent.integrate(_failed_patch(), task)


@pytest.mark.asyncio
async def test_integrate_failed_error_message_contains_details(ctx, task):
    """ValueError message must include error_details and task id"""
    patch = MergedPatch(
        feature_task_id="ft-001",
        merged_diff="",
        source_patch_ids=[],
        status=PatchStatus.FAILED,
        error_details="No successful patches to merge",
    )
    agent = IntegratorAgent(ctx)
    with pytest.raises(ValueError) as exc_info:
        await agent.integrate(patch, task)
    msg = str(exc_info.value)
    assert "No successful patches to merge" in msg   # error_details surfaced
    assert task.id in msg                             # task context present


# ── protected directories / PR10 权限边界 ─────────────────────────────────────

@pytest.mark.asyncio
async def test_integrate_blocks_protected_dir_diff(ctx, task):
    """PR10: core/ 修改不应被 Integrator 放行 — 防御纵深，直接阻断"""
    core_diff = (
        "diff --git a/core/auth.py b/core/auth.py\n"
        "--- a/core/auth.py\n"
        "+++ b/core/auth.py\n"
        "+code\n"
    )
    patch = _ok_patch(diff=core_diff)
    agent = IntegratorAgent(ctx)
    # PR10: Integrator now blocks protected paths (defense-in-depth)
    with pytest.raises(ValueError) as exc_info:
        await agent.integrate(patch, task)
    msg = str(exc_info.value)
    assert "core/auth.py" in msg or "protected" in msg.lower()


@pytest.mark.asyncio
async def test_integrate_blocks_ci_path(ctx, task):
    """PR10: CI/CD 路径 .github/workflows/ci.yml 应被 Integrator 阻断"""
    ci_diff = (
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
        "--- a/.github/workflows/ci.yml\n"
        "+++ b/.github/workflows/ci.yml\n"
        "+code\n"
    )
    patch = _ok_patch(diff=ci_diff)
    agent = IntegratorAgent(ctx)
    with pytest.raises(ValueError):
        await agent.integrate(patch, task)


@pytest.mark.asyncio
async def test_integrate_blocks_traversal(ctx, task):
    """PR10: 路径穿越应被 Integrator 阻断"""
    traversal_diff = (
        "diff --git a/features/../core/auth.py b/features/../core/auth.py\n"
        "--- a/features/../core/auth.py\n"
        "+++ b/features/../core/auth.py\n"
        "+code\n"
    )
    patch = _ok_patch(diff=traversal_diff)
    agent = IntegratorAgent(ctx)
    with pytest.raises(ValueError):
        await agent.integrate(patch, task)


@pytest.mark.asyncio
async def test_integrate_features_only_diff_passes_cleanly(ctx, task):
    """Normal features/ diff passes without warnings"""
    agent = IntegratorAgent(ctx)
    result = await agent.integrate(_ok_patch(), task)
    assert result.status == PatchStatus.SUCCESS


# ── pipeline position test ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pipeline_calls_integrator_between_aggregator_and_reviewer():
    """
    Verify pipeline calls IntegratorAgent.integrate AFTER AggregatorAgent.merge
    and BEFORE ReviewAgent.review.

    Uses sentinel exceptions to detect call order without running real agents.
    """
    from unittest.mock import AsyncMock, patch, MagicMock

    call_order: list[str] = []

    class _SentinelAggregator:
        def __init__(self, ctx): pass
        async def merge(self, patch_set):
            call_order.append("aggregator")
            return MergedPatch(
                feature_task_id="ft-test",
                merged_diff="diff --git a/features/x.py b/features/x.py\n+x\n",
                source_patch_ids=["s1"],
                status=PatchStatus.SUCCESS,
            )

    class _SentinelIntegrator:
        def __init__(self, ctx): pass
        async def integrate(self, merged, task):
            call_order.append("integrator")
            return merged   # pass-through

    class _SentinelReviewer:
        def __init__(self, ctx): pass
        async def review(self, merged, task):
            call_order.append("reviewer")
            raise _PipelineStop("reviewer reached")

    class _PipelineStop(Exception):
        pass

    with patch("pipeline._clone_repo"), \
         patch("agents.worker.workspace.WorkerWorkspaceManager.validate_strategy",
               return_value=(True, "ok")), \
         patch("pipeline.ContextAgent") as MockCtx, \
         patch("pipeline.OrchestratorAgent") as MockOrch, \
         patch("pipeline.TestAgent") as MockTest, \
         patch("pipeline.AggregatorAgent", _SentinelAggregator), \
         patch("pipeline.IntegratorAgent", _SentinelIntegrator), \
         patch("pipeline.ReviewAgent", _SentinelReviewer):

        from models.context import CodeContext
        from models.task import TaskGraph, FeatureTask as FT, SubTask

        MockCtx.return_value.gather = AsyncMock(
            return_value=CodeContext(feature_task_id="ft-test")
        )
        ft = FT(raw_requirement="r", feature_name="f", repository="org/repo")
        ft.subtasks = []
        tg = TaskGraph(feature_task=ft, parallel_groups=[], dependencies={})
        MockOrch.return_value.decompose = AsyncMock(return_value=tg)
        MockTest.return_value.generate = AsyncMock(return_value=MagicMock(test_code=""))

        from pipeline import run_pipeline
        try:
            await run_pipeline("test", "org/repo")
        except _PipelineStop:
            pass

    assert call_order == ["aggregator", "integrator", "reviewer"], \
        f"Expected aggregator → integrator → reviewer, got {call_order}"


@pytest.mark.asyncio
async def test_pipeline_calls_integrator_again_on_review_retry():
    """
    After a review retry (Worker re-runs + Aggregator re-merges),
    IntegratorAgent.integrate must be called again before Reviewer.
    """
    from unittest.mock import AsyncMock, patch, MagicMock

    integrate_call_count = [0]

    class _CountingIntegrator:
        def __init__(self, ctx): pass
        async def integrate(self, merged, task):
            integrate_call_count[0] += 1
            return merged

    class _BlockThenApproveReviewer:
        """Blocks first review attempt, approves second."""
        _calls = 0
        def __init__(self, ctx): pass
        async def review(self, merged, task):
            _BlockThenApproveReviewer._calls += 1
            from models.patch import ReviewResult
            if _BlockThenApproveReviewer._calls == 1:
                return MagicMock(approved=False, summary="blocked", comments=[])
            raise _Done("approved on retry")

    class _Done(Exception):
        pass

    with patch("pipeline._clone_repo"), \
         patch("agents.worker.workspace.WorkerWorkspaceManager.validate_strategy",
               return_value=(True, "ok")), \
         patch("pipeline.ContextAgent") as MockCtx, \
         patch("pipeline.OrchestratorAgent") as MockOrch, \
         patch("pipeline.TestAgent") as MockTest, \
         patch("pipeline.AggregatorAgent") as MockAgg, \
         patch("pipeline.IntegratorAgent", _CountingIntegrator), \
         patch("pipeline.ReviewAgent", _BlockThenApproveReviewer):

        from models.context import CodeContext
        from models.task import TaskGraph, FeatureTask as FT

        MockCtx.return_value.gather = AsyncMock(
            return_value=CodeContext(feature_task_id="ft-test")
        )
        ft = FT(raw_requirement="r", feature_name="f", repository="org/repo")
        ft.subtasks = []
        tg = TaskGraph(feature_task=ft, parallel_groups=[], dependencies={})
        MockOrch.return_value.decompose = AsyncMock(return_value=tg)
        MockTest.return_value.generate = AsyncMock(return_value=MagicMock(test_code=""))

        ok_merged = MergedPatch(
            feature_task_id="ft-test",
            merged_diff="diff --git a/features/x.py b/features/x.py\n+x\n",
            source_patch_ids=["s1"],
            status=PatchStatus.SUCCESS,
        )
        MockAgg.return_value.merge = AsyncMock(return_value=ok_merged)
        _BlockThenApproveReviewer._calls = 0

        from pipeline import run_pipeline
        try:
            await run_pipeline("test", "org/repo")
        except _Done:
            pass

    # Integrator must be called at least twice:
    # once after initial merge, once after retry merge
    assert integrate_call_count[0] >= 2, \
        f"Expected ≥2 Integrator calls (initial + retry), got {integrate_call_count[0]}"


# ── Phase 6B: pipeline early termination ─────────────────────────────────────

def _make_shared_pipeline_mocks(MockCtx, MockOrch, MockTest):
    """Shared setup for pipeline integration tests."""
    from unittest.mock import AsyncMock, MagicMock
    from models.context import CodeContext
    from models.task import TaskGraph, FeatureTask as FT
    MockCtx.return_value.gather = AsyncMock(
        return_value=CodeContext(feature_task_id="ft-test")
    )
    ft = FT(raw_requirement="r", feature_name="f", repository="org/repo")
    ft.subtasks = []
    tg = TaskGraph(feature_task=ft, parallel_groups=[], dependencies={})
    MockOrch.return_value.decompose = AsyncMock(return_value=tg)
    MockTest.return_value.generate = AsyncMock(return_value=MagicMock(test_code=""))
    return tg


@pytest.mark.asyncio
async def test_pipeline_terminates_early_when_initial_integrate_fails():
    """
    When integrate() raises ValueError on the initial merge,
    pipeline sets run.stage='done', logs the error, and never reaches Reviewer.
    """
    from unittest.mock import AsyncMock, patch, MagicMock

    reviewer_called = []

    class _FailingIntegrator:
        def __init__(self, ctx): pass
        async def integrate(self, merged, task):
            raise ValueError("aggregation failed: no successful patches")

    class _SpyReviewer:
        def __init__(self, ctx): pass
        async def review(self, merged, task):
            reviewer_called.append(True)
            return MagicMock(approved=True, summary="ok", comments=[])

    failed_merged = MergedPatch(
        feature_task_id="ft-test",
        merged_diff="",
        source_patch_ids=[],
        status=PatchStatus.FAILED,
        error_details="no patches",
    )

    with patch("pipeline._clone_repo"), \
         patch("agents.worker.workspace.WorkerWorkspaceManager.validate_strategy",
               return_value=(True, "ok")), \
         patch("pipeline.ContextAgent") as MockCtx, \
         patch("pipeline.OrchestratorAgent") as MockOrch, \
         patch("pipeline.TestAgent") as MockTest, \
         patch("pipeline.AggregatorAgent") as MockAgg, \
         patch("pipeline.IntegratorAgent", _FailingIntegrator), \
         patch("pipeline.ReviewAgent", _SpyReviewer), \
         patch("pipeline.GitHubAgent") as MockGH:

        _make_shared_pipeline_mocks(MockCtx, MockOrch, MockTest)
        MockAgg.return_value.merge = AsyncMock(return_value=failed_merged)

        from pipeline import run_pipeline
        run = await run_pipeline("test", "org/repo")

    assert run.stage == "done"
    assert len(run.error_log) > 0
    assert any("aggregation failed" in e for e in run.error_log)
    assert len(reviewer_called) == 0, "Reviewer must NOT be called when integrate fails"
    MockGH.assert_not_called()       # GitHubAgent must NOT be called


@pytest.mark.asyncio
async def test_pipeline_error_log_contains_integrate_message():
    """error_log entry contains the ValueError message from integrate()"""
    from unittest.mock import AsyncMock, patch, MagicMock

    class _FailingIntegrator:
        def __init__(self, ctx): pass
        async def integrate(self, merged, task):
            raise ValueError("specific integration error details here")

    with patch("pipeline._clone_repo"), \
         patch("agents.worker.workspace.WorkerWorkspaceManager.validate_strategy",
               return_value=(True, "ok")), \
         patch("pipeline.ContextAgent") as MockCtx, \
         patch("pipeline.OrchestratorAgent") as MockOrch, \
         patch("pipeline.TestAgent") as MockTest, \
         patch("pipeline.AggregatorAgent") as MockAgg, \
         patch("pipeline.IntegratorAgent", _FailingIntegrator), \
         patch("pipeline.ReviewAgent"):

        ok_merged = MergedPatch(
            feature_task_id="ft-test",
            merged_diff="",
            source_patch_ids=[],
            status=PatchStatus.SUCCESS,
        )
        _make_shared_pipeline_mocks(MockCtx, MockOrch, MockTest)
        MockAgg.return_value.merge = AsyncMock(return_value=ok_merged)

        from pipeline import run_pipeline
        run = await run_pipeline("test", "org/repo")

    assert any("specific integration error details here" in e for e in run.error_log)


@pytest.mark.asyncio
async def test_pipeline_terminates_on_review_retry_integrate_failure():
    """
    When review is blocked once, and the retry's integrate() also fails,
    pipeline must terminate (run.stage='done') and not enter another review.
    """
    from unittest.mock import AsyncMock, patch, MagicMock

    review_call_count = [0]
    integrate_call_count = [0]

    ok_merged = MergedPatch(
        feature_task_id="ft-test",
        merged_diff="diff --git a/features/x.py b/features/x.py\n+x\n",
        source_patch_ids=["s1"],
        status=PatchStatus.SUCCESS,
    )
    failed_merged = MergedPatch(
        feature_task_id="ft-test",
        merged_diff="",
        source_patch_ids=[],
        status=PatchStatus.FAILED,
        error_details="retry workers all failed",
    )

    class _FailOnRetryIntegrator:
        """Passes first integrate, fails on second (after retry merge)."""
        def __init__(self, ctx): pass
        async def integrate(self, merged, task):
            integrate_call_count[0] += 1
            if integrate_call_count[0] == 1:
                return merged   # first call: pass
            raise ValueError("retry integration failed: retry workers all failed")

    class _BlockingReviewer:
        def __init__(self, ctx): pass
        async def review(self, merged, task):
            review_call_count[0] += 1
            return MagicMock(approved=False, summary="blocked", comments=[])

    agg_call_count = [0]

    class _TwoResultAggregator:
        def __init__(self, ctx): pass
        async def merge(self, patch_set):
            agg_call_count[0] += 1
            return ok_merged if agg_call_count[0] == 1 else failed_merged

    with patch("pipeline._clone_repo"), \
         patch("agents.worker.workspace.WorkerWorkspaceManager.validate_strategy",
               return_value=(True, "ok")), \
         patch("pipeline.ContextAgent") as MockCtx, \
         patch("pipeline.OrchestratorAgent") as MockOrch, \
         patch("pipeline.TestAgent") as MockTest, \
         patch("pipeline.AggregatorAgent", _TwoResultAggregator), \
         patch("pipeline.IntegratorAgent", _FailOnRetryIntegrator), \
         patch("pipeline.ReviewAgent", _BlockingReviewer):

        _make_shared_pipeline_mocks(MockCtx, MockOrch, MockTest)

        from pipeline import run_pipeline
        run = await run_pipeline("test", "org/repo")

    assert run.stage == "done"
    assert any("retry" in e.lower() for e in run.error_log)
    # Reviewer was called once (before the retry), then integrate failed → no second review
    assert review_call_count[0] == 1, \
        f"Expected exactly 1 review call before retry integrate fails, got {review_call_count[0]}"


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 6C: optional integration test command
# ═══════════════════════════════════════════════════════════════════════════════

def _make_proc(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


# ── 1. empty command → no subprocess, integrate succeeds ─────────────────────

@pytest.mark.asyncio
async def test_empty_command_skips_subprocess(ctx, task):
    """Default empty INTEGRATION_TEST_COMMAND must not spawn any subprocess"""
    with patch("config.settings.settings.INTEGRATION_TEST_COMMAND", ""), \
         patch("asyncio.create_subprocess_shell") as mock_shell:
        agent = IntegratorAgent(ctx)
        result = await agent.integrate(_ok_patch(), task)

    mock_shell.assert_not_called()
    assert isinstance(result, MergedPatch)


# ── 2. command exit 0 → integrate succeeds ───────────────────────────────────

@pytest.mark.asyncio
async def test_command_exit_0_integrate_succeeds(ctx, task):
    """exit_code=0 → integrate returns MergedPatch unchanged"""
    proc = _make_proc(stdout=b"All tests passed.", returncode=0)

    with patch("config.settings.settings.INTEGRATION_TEST_COMMAND", "pytest"), \
         patch("config.settings.settings.INTEGRATION_TEST_TIMEOUT", 30), \
         patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=proc)):
        agent = IntegratorAgent(ctx)
        original = _ok_patch()
        result = await agent.integrate(original, task)

    assert result is original          # same object, diff unchanged
    assert result.merged_diff == original.merged_diff


# ── 3. command exit non-0 → raises ValueError ────────────────────────────────

@pytest.mark.asyncio
async def test_command_nonzero_exit_raises_value_error(ctx, task):
    """Non-zero exit code → ValueError raised, message includes exit code"""
    proc = _make_proc(stderr=b"FAILED 3 tests", returncode=1)

    with patch("config.settings.settings.INTEGRATION_TEST_COMMAND", "pytest"), \
         patch("config.settings.settings.INTEGRATION_TEST_TIMEOUT", 30), \
         patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=proc)):
        agent = IntegratorAgent(ctx)
        with pytest.raises(ValueError) as exc_info:
            await agent.integrate(_ok_patch(), task)

    msg = str(exc_info.value)
    assert "integration tests failed" in msg.lower()
    assert "exit=1" in msg or "exit_code" in msg or "1" in msg


# ── 4. command times out → kills process, raises ValueError ──────────────────

@pytest.mark.asyncio
async def test_command_timeout_kills_process_and_raises(ctx, task):
    """Timeout → process killed, ValueError raised with timeout message"""
    proc = _make_proc()
    proc.returncode = None   # process still running

    with patch("config.settings.settings.INTEGRATION_TEST_COMMAND", "pytest"), \
         patch("config.settings.settings.INTEGRATION_TEST_TIMEOUT", 1), \
         patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=proc)), \
         patch("asyncio.wait_for", new=AsyncMock(side_effect=asyncio.TimeoutError())):
        agent = IntegratorAgent(ctx)
        with pytest.raises(ValueError) as exc_info:
            await agent.integrate(_ok_patch(), task)

    msg = str(exc_info.value)
    assert "timed out" in msg.lower()
    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


# ── 5. error message includes stderr and exit code ───────────────────────────

@pytest.mark.asyncio
async def test_error_message_contains_stderr_and_exit_code(ctx, task):
    """ValueError message must contain the stderr snippet and exit code"""
    proc = _make_proc(stderr=b"ImportError: missing module xyz", returncode=2)

    with patch("config.settings.settings.INTEGRATION_TEST_COMMAND", "pytest"), \
         patch("config.settings.settings.INTEGRATION_TEST_TIMEOUT", 30), \
         patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=proc)):
        agent = IntegratorAgent(ctx)
        with pytest.raises(ValueError) as exc_info:
            await agent.integrate(_ok_patch(), task)

    msg = str(exc_info.value)
    assert "ImportError: missing module xyz" in msg
    assert "2" in msg   # exit code 2


# ── 6. stdout used when stderr is empty ──────────────────────────────────────

@pytest.mark.asyncio
async def test_stdout_used_in_message_when_stderr_empty(ctx, task):
    """When stderr is empty, stdout is included in the error message"""
    proc = _make_proc(stdout=b"AssertionError at line 42", stderr=b"", returncode=1)

    with patch("config.settings.settings.INTEGRATION_TEST_COMMAND", "pytest"), \
         patch("config.settings.settings.INTEGRATION_TEST_TIMEOUT", 30), \
         patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=proc)):
        agent = IntegratorAgent(ctx)
        with pytest.raises(ValueError) as exc_info:
            await agent.integrate(_ok_patch(), task)

    assert "AssertionError at line 42" in str(exc_info.value)


# ── 7. diff not modified when tests pass ─────────────────────────────────────

@pytest.mark.asyncio
async def test_diff_not_modified_when_tests_pass(ctx, task):
    """integrate must return the original MergedPatch with diff untouched"""
    proc = _make_proc(stdout=b"ok", returncode=0)
    original_diff = "diff --git a/features/x.py b/features/x.py\n+code\n"

    with patch("config.settings.settings.INTEGRATION_TEST_COMMAND", "pytest"), \
         patch("config.settings.settings.INTEGRATION_TEST_TIMEOUT", 30), \
         patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=proc)):
        agent = IntegratorAgent(ctx)
        result = await agent.integrate(_ok_patch(diff=original_diff), task)

    assert result.merged_diff == original_diff


# ── 8. FAILED status → raises before running subprocess ──────────────────────

@pytest.mark.asyncio
async def test_failed_patch_raises_before_running_tests(ctx, task):
    """FAILED MergedPatch must raise ValueError without spawning subprocess"""
    with patch("config.settings.settings.INTEGRATION_TEST_COMMAND", "pytest"), \
         patch("asyncio.create_subprocess_shell") as mock_shell:
        agent = IntegratorAgent(ctx)
        with pytest.raises(ValueError):
            await agent.integrate(_failed_patch(), task)

    mock_shell.assert_not_called()


# ── 9. empty diff → raises before running subprocess ─────────────────────────

@pytest.mark.asyncio
async def test_empty_diff_raises_before_running_tests(ctx, task):
    """Empty diff must raise ValueError without spawning subprocess"""
    patch_empty = MergedPatch(
        feature_task_id="ft-001", merged_diff="   ", source_patch_ids=["s1"],
        status=PatchStatus.SUCCESS,
    )
    with patch("config.settings.settings.INTEGRATION_TEST_COMMAND", "pytest"), \
         patch("asyncio.create_subprocess_shell") as mock_shell:
        agent = IntegratorAgent(ctx)
        with pytest.raises(ValueError):
            await agent.integrate(patch_empty, task)

    mock_shell.assert_not_called()


# ── 10. default settings: INTEGRATION_TEST_COMMAND is empty ──────────────────

def test_default_integration_test_command_is_empty():
    """Default INTEGRATION_TEST_COMMAND must be empty string (safe default)"""
    from config.settings import Settings
    fields = getattr(Settings, "model_fields", None) or Settings.__fields__
    default = getattr(fields["INTEGRATION_TEST_COMMAND"], "default", None)
    assert default == ""


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 6D: IntegrationResult structured summary
# ═══════════════════════════════════════════════════════════════════════════════

from models.patch import IntegrationResult   # noqa: E402


# ── last_result is set after successful integrate ─────────────────────────────

@pytest.mark.asyncio
async def test_last_result_set_after_success(ctx, task):
    """last_result must not be None after a successful integrate()"""
    with patch("config.settings.settings.INTEGRATION_TEST_COMMAND", ""):
        agent = IntegratorAgent(ctx)
        await agent.integrate(_ok_patch(), task)
    assert agent.last_result is not None
    assert isinstance(agent.last_result, IntegrationResult)


@pytest.mark.asyncio
async def test_last_result_success_is_true(ctx, task):
    with patch("config.settings.settings.INTEGRATION_TEST_COMMAND", ""):
        agent = IntegratorAgent(ctx)
        await agent.integrate(_ok_patch(), task)
    assert agent.last_result.success is True


# ── diff statistics in last_result ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_last_result_line_count_correct(ctx, task):
    """line_count reflects the number of newlines in merged_diff"""
    diff = "diff --git a/features/x.py b/features/x.py\n+a\n+b\n+c\n"   # 4 newlines
    with patch("config.settings.settings.INTEGRATION_TEST_COMMAND", ""):
        agent = IntegratorAgent(ctx)
        await agent.integrate(_ok_patch(diff=diff), task)
    assert agent.last_result.line_count == diff.count("\n")


@pytest.mark.asyncio
async def test_last_result_source_patch_count(ctx, task):
    patch_obj = MergedPatch(
        feature_task_id="ft-001",
        merged_diff="diff --git a/features/x.py b/features/x.py\n+x\n",
        source_patch_ids=["s1", "s2", "s3"],
        status=PatchStatus.SUCCESS,
    )
    with patch("config.settings.settings.INTEGRATION_TEST_COMMAND", ""):
        agent = IntegratorAgent(ctx)
        await agent.integrate(patch_obj, task)
    assert agent.last_result.source_patch_count == 3


@pytest.mark.asyncio
async def test_last_result_conflicts_resolved(ctx, task):
    patch_obj = MergedPatch(
        feature_task_id="ft-001",
        merged_diff="diff --git a/features/x.py b/features/x.py\n+x\n",
        source_patch_ids=["s1"],
        conflicts_resolved=2,
        status=PatchStatus.SUCCESS,
    )
    with patch("config.settings.settings.INTEGRATION_TEST_COMMAND", ""):
        agent = IntegratorAgent(ctx)
        await agent.integrate(patch_obj, task)
    assert agent.last_result.conflicts_resolved == 2


@pytest.mark.asyncio
async def test_last_result_protected_path_count(ctx, task):
    """PR10: Protected-dir touches now raise ValueError (blocking, not warning)"""
    diff = (
        "diff --git a/core/auth.py b/core/auth.py\n+x\n"
        "diff --git a/features/y.py b/features/y.py\n+y\n"
    )
    with patch("config.settings.settings.INTEGRATION_TEST_COMMAND", ""):
        agent = IntegratorAgent(ctx)
        with pytest.raises(ValueError) as exc_info:
            await agent.integrate(_ok_patch(diff=diff), task)
    msg = str(exc_info.value)
    assert "core/auth.py" in msg or "permission" in msg.lower()


# ── test-command fields ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_last_result_tests_not_configured_when_command_empty(ctx, task):
    with patch("config.settings.settings.INTEGRATION_TEST_COMMAND", ""):
        agent = IntegratorAgent(ctx)
        await agent.integrate(_ok_patch(), task)
    ir = agent.last_result
    assert ir.tests_configured is False
    assert ir.tests_passed is None


@pytest.mark.asyncio
async def test_last_result_tests_configured_and_passed_on_exit_0(ctx, task):
    proc = _make_proc(stdout=b"ok", returncode=0)
    with patch("config.settings.settings.INTEGRATION_TEST_COMMAND", "pytest"), \
         patch("config.settings.settings.INTEGRATION_TEST_TIMEOUT", 30), \
         patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=proc)):
        agent = IntegratorAgent(ctx)
        await agent.integrate(_ok_patch(), task)
    ir = agent.last_result
    assert ir.tests_configured is True
    assert ir.tests_passed is True
    assert ir.test_exit_code == 0
    assert ir.test_command == "pytest"


# ── last_result on failure ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_last_result_set_when_tests_fail(ctx, task):
    """Even when tests fail (→ ValueError), last_result must be populated"""
    proc = _make_proc(stderr=b"FAILED 2 tests", returncode=1)
    with patch("config.settings.settings.INTEGRATION_TEST_COMMAND", "pytest"), \
         patch("config.settings.settings.INTEGRATION_TEST_TIMEOUT", 30), \
         patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=proc)):
        agent = IntegratorAgent(ctx)
        with pytest.raises(ValueError):
            await agent.integrate(_ok_patch(), task)
    ir = agent.last_result
    assert ir is not None
    assert ir.success is False
    assert ir.tests_passed is False
    assert ir.test_exit_code == 1
    assert ir.error is not None


@pytest.mark.asyncio
async def test_last_result_set_when_patch_is_failed(ctx, task):
    """FAILED MergedPatch → ValueError, but last_result.success=False is set"""
    agent = IntegratorAgent(ctx)
    with pytest.raises(ValueError):
        await agent.integrate(_failed_patch(), task)
    ir = agent.last_result
    assert ir is not None
    assert ir.success is False
    assert ir.error is not None


@pytest.mark.asyncio
async def test_last_result_set_when_diff_is_empty(ctx, task):
    """Empty diff → ValueError, last_result.success=False is set"""
    empty = MergedPatch(
        feature_task_id="ft-001", merged_diff="", source_patch_ids=["s1"],
        status=PatchStatus.SUCCESS,
    )
    agent = IntegratorAgent(ctx)
    with pytest.raises(ValueError):
        await agent.integrate(empty, task)
    ir = agent.last_result
    assert ir is not None
    assert ir.success is False


# ── merged_diff must be unchanged ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_integrate_returns_original_merged_patch_object(ctx, task):
    """integrate() must return the exact same MergedPatch (not a copy)"""
    original = _ok_patch()
    with patch("config.settings.settings.INTEGRATION_TEST_COMMAND", ""):
        agent = IntegratorAgent(ctx)
        result = await agent.integrate(original, task)
    assert result is original
