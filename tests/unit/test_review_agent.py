"""ReviewAgent 单元测试"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from models.context import AgentContext
from models.patch import MergedPatch, PatchStatus, ReviewSeverity
from models.task import FeatureTask
from agents.base import AgentResult
from agents.registry import AgentRegistry
from agents.review_agent.agent import ReviewAgent


@pytest.fixture
def ctx():
    return AgentContext(
        run_id="testrun",
        feature_task_id="task-1",
        repository="org/repo",
        base_branch="main",
        workspace_path="/tmp/test",
        model="deepseek/deepseek-chat",
    )


@pytest.fixture
def task():
    return FeatureTask(
        raw_requirement="Add rate limiting",
        feature_name="login-ratelimit",
        repository="org/repo",
    )


def _make_merged(diff: str, task_id: str = "task-1") -> MergedPatch:
    return MergedPatch(
        feature_task_id=task_id,
        merged_diff=diff,
        source_patch_ids=["st1"],
    )


FEATURES_DIFF = (
    "diff --git a/features/auth/login.py b/features/auth/login.py\n"
    "--- a/features/auth/login.py\n"
    "+++ b/features/auth/login.py\n"
    "@@ -1 +1,2 @@\n"
    "+import time\n"
)

CORE_DIFF = (
    "diff --git a/core/auth.py b/core/auth.py\n"
    "--- a/core/auth.py\n"
    "+++ b/core/auth.py\n"
    "@@ -1 +1,2 @@\n"
    "+# changed\n"
)


@pytest.mark.asyncio
async def test_review_blocks_core_modification_without_llm(ctx, task):
    """core/ 修改直接在前置检查中拒绝，不调用 LLM"""
    agent = ReviewAgent(ctx)
    merged = _make_merged(CORE_DIFF)
    result = await agent.review(merged, task)
    assert result.approved is False
    assert any(c.severity == ReviewSeverity.BLOCK for c in result.comments)


@pytest.mark.asyncio
async def test_review_approves_features_only_patch(ctx, task):
    agent = ReviewAgent(ctx)
    merged = _make_merged(FEATURES_DIFF)
    mock_response = MagicMock()
    mock_response.choices[0].message.content = (
        '{"approved": true, "summary": "Looks good", "comments": []}'
    )
    with patch("litellm.acompletion", new=AsyncMock(return_value=mock_response)):
        result = await agent.review(merged, task)
    assert result.approved is True


@pytest.mark.asyncio
async def test_review_fails_gracefully_defaults_approved(ctx, task):
    """LLM 异常时默认 approved=True，不阻塞流程"""
    agent = ReviewAgent(ctx)
    merged = _make_merged(FEATURES_DIFF)
    with patch("litellm.acompletion", new=AsyncMock(side_effect=Exception("LLM error"))):
        result = await agent.review(merged, task)
    assert result.approved is True


@pytest.mark.asyncio
async def test_review_blocks_infra_modification(ctx, task):
    infra_diff = CORE_DIFF.replace("core/", "infra/")
    agent = ReviewAgent(ctx)
    merged = _make_merged(infra_diff)
    result = await agent.review(merged, task)
    assert result.approved is False


# ── Backend switching tests ───────────────────────────────────────────────────

def _ok_agent_result(agent_name: str) -> AgentResult:
    return AgentResult(
        success=True,
        output='{"approved": true, "summary": "ok", "comments": []}',
        exit_code=0,
        metadata={"agent": agent_name, "role": "reviewer",
                  "task_id": "testrun", "elapsed_seconds": 1.0},
    )


def _fail_agent_result() -> AgentResult:
    return AgentResult(
        success=False, output="", exit_code=-1,
        error="simulated failure",
        metadata={"agent": "codex", "role": "reviewer",
                  "task_id": "testrun", "elapsed_seconds": 0.5},
    )


def _make_mock_agent(mock_result: AgentResult) -> MagicMock:
    """Return a mock AgentAdapter whose run() resolves to *mock_result*."""
    m = MagicMock()
    m.run = AsyncMock(return_value=mock_result)
    return m


def _make_test_registry(**agents) -> AgentRegistry:
    """Build a fresh AgentRegistry containing the provided named mock agents."""
    registry = AgentRegistry()
    for name, agent in agents.items():
        registry.register(name, agent)
    return registry


@pytest.mark.asyncio
async def test_reviewer_uses_codex_when_backend_is_codex(ctx, task):
    """REVIEW_AGENT_BACKEND='codex' → Reviewer 通过 registry 获取 CodexAgent"""
    mock_codex = _make_mock_agent(_ok_agent_result("codex"))
    registry = _make_test_registry(codex=mock_codex)

    with patch("config.settings.settings.REVIEW_AGENT_BACKEND", "codex"), \
         patch("config.settings.settings.CODEX_TIMEOUT", 30), \
         patch("agents.review_agent.agent.default_registry", registry):
        agent = ReviewAgent(ctx)
        result = await agent.review(_make_merged(FEATURES_DIFF), task)

    assert result.approved is True
    assert result.summary == "ok"
    mock_codex.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_reviewer_uses_claude_when_backend_is_claude_code(ctx, task):
    """REVIEW_AGENT_BACKEND='claude-code' + ANTHROPIC_BASE_URL 有值 → ClaudeCodeAgent"""
    mock_claude = _make_mock_agent(_ok_agent_result("claude-code"))
    registry = _make_test_registry(**{"claude-code": mock_claude})

    with patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://example.com"), \
         patch("config.settings.settings.CLAUDE_CODE_TIMEOUT", 30), \
         patch("agents.review_agent.agent.default_registry", registry):
        agent = ReviewAgent(ctx)
        result = await agent.review(_make_merged(FEATURES_DIFF), task)

    assert result.approved is True
    mock_claude.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_reviewer_falls_back_to_litellm_when_codex_fails(ctx, task):
    """CodexAgent 失败 → 回退到 litellm，不阻塞流程"""
    mock_codex = _make_mock_agent(_fail_agent_result())
    registry = _make_test_registry(codex=mock_codex)
    mock_llm = MagicMock()
    mock_llm.choices[0].message.content = (
        '{"approved": true, "summary": "fallback ok", "comments": []}'
    )
    with patch("config.settings.settings.REVIEW_AGENT_BACKEND", "codex"), \
         patch("config.settings.settings.CODEX_TIMEOUT", 30), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_llm)):
        agent = ReviewAgent(ctx)
        result = await agent.review(_make_merged(FEATURES_DIFF), task)

    assert result.approved is True
    assert result.summary == "fallback ok"


@pytest.mark.asyncio
async def test_reviewer_safe_default_when_codex_and_litellm_both_fail(ctx, task):
    """CodexAgent + litellm 都失败 → 安全默认 approved=True，不抛异常"""
    mock_codex = _make_mock_agent(_fail_agent_result())
    registry = _make_test_registry(codex=mock_codex)

    with patch("config.settings.settings.REVIEW_AGENT_BACKEND", "codex"), \
         patch("config.settings.settings.CODEX_TIMEOUT", 30), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("litellm.acompletion",
               new=AsyncMock(side_effect=RuntimeError("litellm down"))):
        agent = ReviewAgent(ctx)
        result = await agent.review(_make_merged(FEATURES_DIFF), task)

    assert result.approved is True  # must not raise; pipeline must continue


# ── 3.5 加固测试 ──────────────────────────────────────────────────────────────

def test_default_review_agent_backend_is_claude_code():
    """REVIEW_AGENT_BACKEND 默认值必须是 'claude-code'"""
    from config.settings import Settings
    # model_fields is the Pydantic v2 API; fall back to __fields__ for v1
    fields = getattr(Settings, "model_fields", None) or Settings.__fields__
    field = fields["REVIEW_AGENT_BACKEND"]
    default = getattr(field, "default", None)
    assert default == "claude-code"


@pytest.mark.asyncio
async def test_invalid_backend_falls_back_to_claude_code_and_logs_warning(ctx, task):
    """
    REVIEW_AGENT_BACKEND 为非法值时：
    - 记录 warning 并 fallback 到 claude-code
    - 主流程不崩溃，最终调用 ClaudeCodeAgent（通过 registry）
    """
    mock_claude = _make_mock_agent(_ok_agent_result("claude-code"))
    registry = _make_test_registry(**{"claude-code": mock_claude})

    with patch("config.settings.settings.REVIEW_AGENT_BACKEND", "totally-unknown"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://example.com"), \
         patch("config.settings.settings.CLAUDE_CODE_TIMEOUT", 30), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("agents.review_agent.agent.logger") as mock_logger:
        agent = ReviewAgent(ctx)
        result = await agent.review(_make_merged(FEATURES_DIFF), task)

    assert result.approved is True

    # warning 必须包含非法值和 fallback 目标
    warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
    assert any("totally-unknown" in msg and "claude-code" in msg for msg in warning_calls)

    # ClaudeCodeAgent 被调用（通过 registry）
    mock_claude.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_claude_code_backend_falls_back_to_litellm_when_agent_fails(ctx, task):
    """backend=claude-code 且 ClaudeCodeAgent 失败时 → 回退到 litellm"""
    mock_claude = _make_mock_agent(_fail_agent_result())
    registry = _make_test_registry(**{"claude-code": mock_claude})
    mock_llm = MagicMock()
    mock_llm.choices[0].message.content = (
        '{"approved": true, "summary": "litellm saved it", "comments": []}'
    )
    with patch("config.settings.settings.REVIEW_AGENT_BACKEND", "claude-code"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://example.com"), \
         patch("config.settings.settings.CLAUDE_CODE_TIMEOUT", 30), \
         patch("agents.review_agent.agent.default_registry", registry), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_llm)):
        agent = ReviewAgent(ctx)
        result = await agent.review(_make_merged(FEATURES_DIFF), task)

    assert result.approved is True
    assert result.summary == "litellm saved it"


@pytest.mark.asyncio
async def test_registry_missing_backend_causes_litellm_fallback(ctx, task):
    """registry 中没有所请求的 backend 时 Reviewer 不崩溃，fallback 到 litellm"""
    empty_registry = AgentRegistry()  # nothing registered
    mock_llm = MagicMock()
    mock_llm.choices[0].message.content = (
        '{"approved": true, "summary": "registry empty fallback", "comments": []}'
    )
    with patch("config.settings.settings.REVIEW_AGENT_BACKEND", "codex"), \
         patch("config.settings.settings.CODEX_TIMEOUT", 30), \
         patch("agents.review_agent.agent.default_registry", empty_registry), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_llm)):
        agent = ReviewAgent(ctx)
        result = await agent.review(_make_merged(FEATURES_DIFF), task)

    assert result.approved is True
    assert result.summary == "registry empty fallback"


def test_codex_agent_env_does_not_contain_glm_key_by_default():
    """
    _build_env() 使用 OPENAI_API_KEY，不默认注入 GLM_API_KEY 给 Codex。
    API key 不出现在日志（通过代码审查保证；此测试验证 env 来源正确）。
    """
    from agents.codex import CodexAgent

    with patch("config.settings.settings.OPENAI_API_KEY", "real-openai-key"), \
         patch("config.settings.settings.GLM_API_KEY", "glm-secret"), \
         patch("config.settings.settings.OPENAI_API_BASE", ""):
        agent = CodexAgent()
        env = agent._build_env()

    # Codex 应使用 OPENAI_API_KEY，不是 GLM key
    assert env.get("OPENAI_API_KEY") == "real-openai-key"
    assert env.get("CODEX_OPENAI_API_KEY") == "real-openai-key"
    # OPENAI_BASE_URL 未被强制覆盖（OPENAI_API_BASE 为空）
    assert env.get("OPENAI_BASE_URL") != "real-openai-key"


def test_codex_agent_sets_base_url_only_when_configured():
    """OPENAI_API_BASE 有值时 _build_env() 设置 OPENAI_BASE_URL；为空时不覆盖"""
    from agents.codex import CodexAgent
    import os

    # Case 1: OPENAI_API_BASE configured
    with patch("config.settings.settings.OPENAI_API_KEY", "sk-x"), \
         patch("config.settings.settings.GLM_API_KEY", ""), \
         patch("config.settings.settings.OPENAI_API_BASE", "https://my-gateway.com/v1"):
        env = CodexAgent()._build_env()
    assert env["OPENAI_BASE_URL"] == "https://my-gateway.com/v1"

    # Case 2: OPENAI_API_BASE empty — system OPENAI_BASE_URL should not be forcibly set
    # (system env may or may not have it; we just check we don't inject the GLM URL)
    with patch("config.settings.settings.OPENAI_API_KEY", "sk-x"), \
         patch("config.settings.settings.GLM_API_KEY", ""), \
         patch("config.settings.settings.OPENAI_API_BASE", ""), \
         patch.dict(os.environ, {"OPENAI_BASE_URL": "https://existing-system-url.com"}, clear=False):
        env = CodexAgent()._build_env()
    # System value preserved, GLM URL not injected
    assert env.get("OPENAI_BASE_URL") == "https://existing-system-url.com"


# ═══════════════════════════════════════════════════════════════════════════════
# PR10 — 权限边界加固测试
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_review_blocks_ci_modification(ctx, task):
    """CI/CD 修改（.github/workflows/ci.yml）应在前置检查中被阻止"""
    ci_diff = (
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
        "--- a/.github/workflows/ci.yml\n"
        "+++ b/.github/workflows/ci.yml\n"
        "@@ -1 +1,2 @@\n"
        "+# changed\n"
    )
    agent = ReviewAgent(ctx)
    merged = _make_merged(ci_diff)
    result = await agent.review(merged, task)
    assert result.approved is False
    assert any(c.severity == ReviewSeverity.BLOCK for c in result.comments)


@pytest.mark.asyncio
async def test_review_blocks_traversal(ctx, task):
    """路径穿越 'features/../build.py' 逃逸到仓库根目录，应被阻止（旧检查漏掉此路径）"""
    traversal_diff = (
        "diff --git a/features/../build.py b/features/../build.py\n"
        "--- a/features/../build.py\n"
        "+++ b/features/../build.py\n"
        "@@ -1 +1,2 @@\n"
        "+# changed\n"
    )
    agent = ReviewAgent(ctx)
    merged = _make_merged(traversal_diff)
    result = await agent.review(merged, task)
    assert result.approved is False
    assert any(c.severity == ReviewSeverity.BLOCK for c in result.comments)
