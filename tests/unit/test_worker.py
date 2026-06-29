"""ClaudeCodeWorker 单元测试"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from models.context import AgentContext
from models.task import SubTask
from models.patch import PatchStatus
from agents.worker.agent import ClaudeCodeWorker


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
def subtask():
    return SubTask(
        id="st001",
        feature="auth",
        goal="Add rate limiting to login endpoint",
        files=["features/auth/login.py"],
        constraints=["must be thread-safe"],
    )


def test_extract_diff_from_code_block(ctx):
    worker = ClaudeCodeWorker("w1", ctx)
    raw = "```diff\ndiff --git a/features/auth/login.py b/features/auth/login.py\n--- a/features/auth/login.py\n+++ b/features/auth/login.py\n```"
    diff = worker._extract_diff(raw)
    assert diff.startswith("diff --git")


def test_extract_diff_bare(ctx):
    worker = ClaudeCodeWorker("w1", ctx)
    raw = "diff --git a/features/auth/login.py b/features/auth/login.py\n--- a\n+++ b\n"
    assert worker._extract_diff(raw.strip()).startswith("diff --git")


def test_extract_diff_fails_on_noise(ctx):
    worker = ClaudeCodeWorker("w1", ctx)
    with pytest.raises(ValueError):
        worker._extract_diff("Here is my explanation of the change...")


def test_validate_diff_passes_features(ctx):
    worker = ClaudeCodeWorker("w1", ctx)
    diff = "diff --git a/features/auth/login.py b/features/auth/login.py\n"
    subtask = SubTask(
        id="st001", feature="auth", goal="add login",
        files=["features/auth/login.py"],
    )
    assert worker._validate_diff(diff, subtask) is True


def test_validate_diff_fails_core(ctx):
    worker = ClaudeCodeWorker("w1", ctx)
    diff = "diff --git a/core/auth.py b/core/auth.py\n"
    subtask = SubTask(
        id="st001", feature="auth", goal="add login",
        files=["features/auth/login.py"],
    )
    assert worker._validate_diff(diff, subtask) is False


@pytest.mark.asyncio
async def test_execute_returns_failed_on_exception(ctx, subtask):
    worker = ClaudeCodeWorker("w1", ctx)
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "not a diff"
    with patch("config.settings.settings.ANTHROPIC_BASE_URL", ""), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_response)):
        result = await worker.execute(subtask)
    assert result.status == PatchStatus.FAILED
    assert result.error_message is not None


@pytest.mark.asyncio
async def test_execute_success(ctx, subtask):
    worker = ClaudeCodeWorker("w1", ctx)
    diff_text = "diff --git a/features/auth/login.py b/features/auth/login.py\n--- a/features/auth/login.py\n+++ b/features/auth/login.py\n@@ -1 +1,2 @@\n+import time\n"
    mock_response = MagicMock()
    mock_response.choices[0].message.content = diff_text
    with patch("config.settings.settings.ANTHROPIC_BASE_URL", ""), \
         patch("litellm.acompletion", new=AsyncMock(return_value=mock_response)):
        result = await worker.execute(subtask)
    assert result.status == PatchStatus.SUCCESS
    assert result.patch_content == diff_text.strip()


# ── Claude Code subprocess 模式测试 ───────────────────────────────────────────

def _make_mock_proc(stdout: bytes, returncode: int = 0):
    mock_proc = MagicMock()
    mock_proc.returncode = returncode
    mock_proc.communicate = AsyncMock(return_value=(stdout, b""))
    mock_proc.kill = MagicMock()
    return mock_proc


DIFF_TEXT = (
    "diff --git a/features/auth/login.py b/features/auth/login.py\n"
    "--- a/features/auth/login.py\n"
    "+++ b/features/auth/login.py\n"
    "@@ -1 +1,2 @@\n"
    "+import time\n"
)


def _patch_subprocess_mode(mock_proc, glm_key="sk-test"):
    """返回 subprocess 模式所需的所有 patch 的 context manager 列表。"""
    return [
        patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://open.bigmodel.cn/api/anthropic"),
        patch("config.settings.settings.GLM_API_KEY", glm_key),
        # Phase-5A: workspace logic now lives in WorkerWorkspaceManager
        patch("agents.worker.workspace.WorkerWorkspaceManager.prepare",
              new=AsyncMock(return_value="/tmp/test-worker")),
        patch("agents.worker.workspace.WorkerWorkspaceManager.git_diff",
              return_value=DIFF_TEXT),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)),
    ]


@pytest.mark.asyncio
async def test_worker_uses_subprocess_when_base_url_set(ctx, subtask):
    """ANTHROPIC_BASE_URL 有值时走 claude CLI 而非 litellm"""
    mock_proc = _make_mock_proc(b"some claude output")
    patches = _patch_subprocess_mode(mock_proc)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        worker = ClaudeCodeWorker("w1", ctx)
        result = await worker.execute(subtask)
    assert result.status == PatchStatus.SUCCESS
    assert result.patch_content.strip() == DIFF_TEXT.strip()


@pytest.mark.asyncio
async def test_worker_subprocess_env_contains_auth(ctx, subtask):
    """subprocess env 中必须包含 ANTHROPIC_BASE_URL 和 ANTHROPIC_AUTH_TOKEN"""
    mock_proc = _make_mock_proc(b"some claude output")
    captured_kwargs = {}

    async def fake_exec(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return mock_proc

    with patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://open.bigmodel.cn/api/anthropic"), \
         patch("config.settings.settings.GLM_API_KEY", "sk-glm-123"), \
         patch("agents.worker.workspace.WorkerWorkspaceManager.prepare",
               new=AsyncMock(return_value="/tmp/test-worker")), \
         patch("agents.worker.workspace.WorkerWorkspaceManager.git_diff", return_value=DIFF_TEXT), \
         patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        worker = ClaudeCodeWorker("w1", ctx)
        await worker.execute(subtask)

    env = captured_kwargs.get("env", {})
    assert env.get("ANTHROPIC_BASE_URL") == "https://open.bigmodel.cn/api/anthropic"
    assert env.get("ANTHROPIC_AUTH_TOKEN") == "sk-glm-123"


@pytest.mark.asyncio
async def test_worker_subprocess_timeout_returns_failed(ctx, subtask):
    """subprocess 超时时返回 FAILED"""
    mock_proc = _make_mock_proc(b"", returncode=0)

    with patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://open.bigmodel.cn/api/anthropic"), \
         patch("config.settings.settings.GLM_API_KEY", "sk-test"), \
         patch("agents.worker.workspace.WorkerWorkspaceManager.prepare",
               new=AsyncMock(return_value="/tmp/test-worker")), \
         patch("agents.worker.workspace.WorkerWorkspaceManager.git_diff", return_value=DIFF_TEXT), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)), \
         patch("asyncio.wait_for", new=AsyncMock(side_effect=asyncio.TimeoutError())):
        worker = ClaudeCodeWorker("w1", ctx)
        result = await worker.execute(subtask)
    assert result.status == PatchStatus.FAILED
    assert "timed out" in (result.error_message or "").lower()


@pytest.mark.asyncio
async def test_worker_subprocess_nonzero_exit_returns_failed(ctx, subtask):
    """subprocess 非零退出码返回 FAILED"""
    mock_proc = _make_mock_proc(b"", returncode=1)
    mock_proc.communicate = AsyncMock(return_value=(b"", b"some error"))

    with patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://open.bigmodel.cn/api/anthropic"), \
         patch("config.settings.settings.GLM_API_KEY", "sk-test"), \
         patch("agents.worker.workspace.WorkerWorkspaceManager.prepare",
               new=AsyncMock(return_value="/tmp/test-worker")), \
         patch("agents.worker.workspace.WorkerWorkspaceManager.git_diff", return_value=DIFF_TEXT), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        worker = ClaudeCodeWorker("w1", ctx)
        result = await worker.execute(subtask)
    assert result.status == PatchStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# PR10 — 权限边界加固测试
# ═══════════════════════════════════════════════════════════════════════════════

def test_validate_diff_rejects_out_of_scope_features(ctx):
    """Worker 触碰了未分配的 features/ 文件，应被拒绝"""
    worker = ClaudeCodeWorker("w1", ctx)
    diff = "diff --git a/features/other.py b/features/other.py\n+code\n"
    subtask = SubTask(
        id="st001", feature="auth", goal="add login",
        files=["features/auth.py"],
    )
    assert worker._validate_diff(diff, subtask) is False


def test_validate_diff_rejects_traversal(ctx):
    """路径穿越 'features/../core/x.py' 归一化后变成 'core/x.py'，应被拒绝"""
    worker = ClaudeCodeWorker("w1", ctx)
    diff = "diff --git a/features/../core/x.py b/features/../core/x.py\n+code\n"
    subtask = SubTask(
        id="st001", feature="auth", goal="add login",
        files=["features/auth.py"],
    )
    assert worker._validate_diff(diff, subtask) is False


def test_validate_diff_rejects_ci_path(ctx):
    """CI/CD 路径 .github/workflows/ci.yml 应被拒绝"""
    worker = ClaudeCodeWorker("w1", ctx)
    diff = "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n+code\n"
    subtask = SubTask(
        id="st001", feature="auth", goal="add login",
        files=["features/auth.py"],
    )
    assert worker._validate_diff(diff, subtask) is False


def test_validate_diff_respects_allowed_files(ctx):
    """Worker 仅触碰分配的 files 时验证通过"""
    worker = ClaudeCodeWorker("w1", ctx)
    diff = "diff --git a/features/auth.py b/features/auth.py\n+code\n"
    subtask = SubTask(
        id="st001", feature="auth", goal="add login",
        files=["features/auth.py", "features/login.py"],
    )
    assert worker._validate_diff(diff, subtask) is True
