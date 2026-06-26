"""
Phase-2 tests: AgentResult metadata + Planner / Worker / Reviewer fallback behaviour.
No real claude CLI or API calls — all agents mocked.
"""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from agents.base import AgentResult, AgentTask
from agents.claude_code import ClaudeCodeAgent
from models.context import AgentContext
from models.patch import PatchStatus
from models.task import FeatureTask, SubTask


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def ctx():
    return AgentContext(
        run_id="run-test",
        feature_task_id="ft-001",
        repository="org/repo",
        base_branch="main",
        workspace_path="/tmp/test-ws",
        model="openai/glm-4-flash",
    )


@pytest.fixture
def feature_task():
    t = FeatureTask(
        raw_requirement="add login rate limit",
        feature_name="login-rate-limit",
        repository="org/repo",
    )
    return t


@pytest.fixture
def subtask():
    return SubTask(
        id="st-001",
        feature="auth",
        goal="add rate limiter",
        files=["features/auth/login.py"],
        constraints=[],
    )


def _failing_result(task_id: str = "run-test") -> AgentResult:
    return AgentResult(
        success=False,
        output="",
        exit_code=-1,
        error="simulated timeout",
        metadata={"agent": "claude-code", "role": "planner",
                  "task_id": task_id, "elapsed_seconds": 300.0},
    )


VALID_PLAN_JSON = """{
  "feature_name": "login-rate-limit",
  "subtasks": [
    {"id": "t1", "feature": "auth", "goal": "add tracker",
     "files": ["features/auth/tracker.py"], "constraints": []}
  ],
  "parallel_groups": [["t1"]],
  "dependencies": {}
}"""

VALID_REVIEW_JSON = '{"approved": true, "summary": "ok", "comments": []}'


# ── 1. AgentResult metadata 字段完整性 ───────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_result_metadata_contains_required_fields():
    """ClaudeCodeAgent 返回的 metadata 必须包含 agent / role / task_id / elapsed_seconds"""
    import asyncio, json
    envelope = json.dumps({"result": "hello"}).encode()
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(envelope, b""))
    proc.kill = MagicMock()
    proc.wait = AsyncMock()

    with patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://example.com"), \
         patch("config.settings.settings.GLM_API_KEY", "sk-test"), \
         patch("config.settings.settings.ANTHROPIC_API_KEY", ""), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        agent = ClaudeCodeAgent()
        result = await agent.run(AgentTask(
            task_id="meta-check",
            role="planner",
            prompt="hi",
            workspace=Path("/tmp"),
        ))

    assert result.success
    meta = result.metadata
    assert meta.get("agent") == "claude-code"
    assert meta.get("role") == "planner"
    assert meta.get("task_id") == "meta-check"
    assert isinstance(meta.get("elapsed_seconds"), float)


# ── 2. Planner fallback when ClaudeCodeAgent fails ───────────────────────────

@pytest.mark.asyncio
async def test_planner_falls_back_to_litellm_on_agent_failure(ctx, feature_task):
    """ClaudeCodeAgent 失败时 Planner 回退到 litellm，且最终能正常返回 TaskGraph"""
    from agents.orchestrator.agent import OrchestratorAgent

    with patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://example.com"), \
         patch("agents.orchestrator.agent._claude_agent.run",
               new=AsyncMock(return_value=_failing_result())), \
         patch("agents.orchestrator.agent.llm_complete",
               new=AsyncMock(return_value=VALID_PLAN_JSON)):
        orch = OrchestratorAgent(ctx)
        task_graph = await orch.decompose(feature_task)

    assert task_graph is not None
    assert len(task_graph.feature_task.subtasks) == 1
    assert task_graph.feature_task.subtasks[0].id == "t1"


# ── 3. Worker returns FAILED when AgentResult.success is False ───────────────

@pytest.mark.asyncio
async def test_worker_returns_failed_when_agent_result_is_failure(ctx, subtask):
    """_claude_agent.run 返回 success=False 时 Worker 应返回 PatchStatus.FAILED"""
    from agents.worker.agent import ClaudeCodeWorker

    fail = AgentResult(
        success=False, output="", exit_code=-1,
        error="test error",
        metadata={"task_id": "run-test-w1-st-001", "elapsed_seconds": 1.0},
    )

    with patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://example.com"), \
         patch("agents.worker.workspace.WorkerWorkspaceManager.prepare",
               new=AsyncMock(return_value="/tmp/test-worker")), \
         patch("agents.worker.agent._claude_agent.run",
               new=AsyncMock(return_value=fail)):
        worker = ClaudeCodeWorker("w1", ctx)
        result = await worker.execute(subtask)

    assert result.status == PatchStatus.FAILED
    assert "test error" in (result.error_message or "")


# ── 4. Reviewer fallback when ClaudeCodeAgent fails ──────────────────────────

@pytest.mark.asyncio
async def test_reviewer_falls_back_to_litellm_on_agent_failure(ctx):
    """ClaudeCodeAgent 失败時 Reviewer 回退到 litellm，返回 approved=True"""
    from agents.registry import AgentRegistry
    from agents.review_agent.agent import ReviewAgent
    from models.patch import MergedPatch

    merged = MergedPatch(
        feature_task_id="ft-001",
        merged_diff="diff --git a/features/auth/x.py b/features/auth/x.py\n",
        source_patch_ids=["t1"],
    )
    ft = FeatureTask(raw_requirement="test", feature_name="test", repository="org/repo")

    mock_claude = MagicMock()
    mock_claude.run = AsyncMock(return_value=_failing_result())
    registry = AgentRegistry()
    registry.register("claude-code", mock_claude)

    with patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://example.com"), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.review_agent.agent.llm_complete",
               new=AsyncMock(return_value=VALID_REVIEW_JSON)):
        reviewer = ReviewAgent(ctx)
        review_result = await reviewer.review(merged, ft)

    assert review_result.approved is True
    assert review_result.summary == "ok"


# ── 5. Reviewer safe default when both agent AND litellm fail ────────────────

@pytest.mark.asyncio
async def test_reviewer_safe_default_when_all_backends_fail(ctx):
    """agent 和 litellm 都失败时 Reviewer 返回安全默认值（approved=True，不阻塞流程）"""
    from agents.registry import AgentRegistry
    from agents.review_agent.agent import ReviewAgent
    from models.patch import MergedPatch

    merged = MergedPatch(
        feature_task_id="ft-001",
        merged_diff="diff --git a/features/auth/x.py b/features/auth/x.py\n",
        source_patch_ids=["t1"],
    )
    ft = FeatureTask(raw_requirement="test", feature_name="test", repository="org/repo")

    mock_claude = MagicMock()
    mock_claude.run = AsyncMock(return_value=_failing_result())
    registry = AgentRegistry()
    registry.register("claude-code", mock_claude)

    with patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://example.com"), \
         patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.review_agent.agent.llm_complete",
               new=AsyncMock(side_effect=RuntimeError("litellm also down"))):
        reviewer = ReviewAgent(ctx)
        review_result = await reviewer.review(merged, ft)

    # Must not raise; pipeline must continue
    assert review_result.approved is True
