"""CodexAgent 单元测试（不真实调用 Codex CLI）"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.base import AgentTask
from agents.codex import CodexAgent


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_task(timeout: int = 30) -> AgentTask:
    return AgentTask(
        task_id="codex-t1",
        role="reviewer",
        prompt="Review this diff: diff --git a/features/auth/x.py ...",
        workspace=Path("/tmp/ws"),
        timeout_seconds=timeout,
        output_format="json",
    )


def _make_proc(stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


_SETTINGS = {
    "config.settings.settings.GLM_API_KEY": "sk-test",
    "config.settings.settings.OPENAI_API_KEY": "",
    "config.settings.settings.CODEX_MODEL": "glm-4-flash",
    "config.settings.settings.CODEX_TIMEOUT": 30,
}


# ── 1. 成功执行（output from stdout fallback） ────────────────────────────────

@pytest.mark.asyncio
async def test_codex_success_via_stdout():
    """
    exit_code=0, -o file is empty → output falls back to stdout.
    AgentResult.success=True, metadata contains required keys.
    """
    proc = _make_proc(stdout=b'{"approved": true}')

    with patch("config.settings.settings.GLM_API_KEY", "sk-test"), \
         patch("config.settings.settings.OPENAI_API_KEY", ""), \
         patch("config.settings.settings.CODEX_MODEL", "glm-4-flash"), \
         patch("config.settings.settings.CODEX_TIMEOUT", 30), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        agent = CodexAgent()
        result = await agent.run(_make_task())

    assert result.success is True
    assert result.output == '{"approved": true}'
    assert result.exit_code == 0
    assert result.error is None
    meta = result.metadata
    assert meta["agent"] == "codex"
    assert meta["role"] == "reviewer"
    assert meta["task_id"] == "codex-t1"
    assert isinstance(meta["elapsed_seconds"], float)


# ── 2. 非零退出码 → success=False ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_codex_nonzero_exit_returns_failure():
    """exit_code != 0 → AgentResult.success=False, error 包含 stderr"""
    proc = _make_proc(stdout=b"", stderr=b"API auth failed", returncode=1)

    with patch("config.settings.settings.GLM_API_KEY", "sk-test"), \
         patch("config.settings.settings.OPENAI_API_KEY", ""), \
         patch("config.settings.settings.CODEX_MODEL", "glm-4-flash"), \
         patch("config.settings.settings.CODEX_TIMEOUT", 30), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        agent = CodexAgent()
        result = await agent.run(_make_task())

    assert result.success is False
    assert result.exit_code == 1
    assert "API auth failed" in (result.error or "")
    assert result.metadata["agent"] == "codex"


# ── 3. 超时 → 子进程被终止，success=False，exit_code=-1 ──────────────────────

@pytest.mark.asyncio
async def test_codex_timeout_kills_process():
    """超时时子进程被 kill()，返回 success=False, exit_code=-1"""
    proc = _make_proc(stdout=b"")
    proc.returncode = None  # process still running

    with patch("config.settings.settings.GLM_API_KEY", "sk-test"), \
         patch("config.settings.settings.OPENAI_API_KEY", ""), \
         patch("config.settings.settings.CODEX_MODEL", "glm-4-flash"), \
         patch("config.settings.settings.CODEX_TIMEOUT", 1), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)), \
         patch("asyncio.wait_for", new=AsyncMock(side_effect=asyncio.TimeoutError())):
        agent = CodexAgent()
        result = await agent.run(_make_task(timeout=1))

    assert result.success is False
    assert result.exit_code == -1
    assert "timed out" in (result.error or "").lower()
    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


# ── 4. 工作目录正确传入 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_codex_workspace_passed_as_cwd():
    """task.workspace が cwd として create_subprocess_exec に渡される"""
    proc = _make_proc(stdout=b"ok")
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs)
        return proc

    with patch("config.settings.settings.GLM_API_KEY", "sk-test"), \
         patch("config.settings.settings.OPENAI_API_KEY", ""), \
         patch("config.settings.settings.CODEX_MODEL", "glm-4-flash"), \
         patch("config.settings.settings.CODEX_TIMEOUT", 30), \
         patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        task = AgentTask(
            task_id="ws-test",
            role="reviewer",
            prompt="review",
            workspace=Path("/my/repo"),
            timeout_seconds=30,
        )
        agent = CodexAgent()
        await agent.run(task)

    from pathlib import Path as _P
    assert _P(captured.get("cwd", "")) == _P("/my/repo")


# ── 5. env 包含 OPENAI_API_KEY 和系统 PATH ────────────────────────────────────

@pytest.mark.asyncio
async def test_codex_env_contains_api_key_and_path():
    """subprocess env 包含 OPENAI_API_KEY（来自 settings.OPENAI_API_KEY）且系统 PATH 不丢失"""
    import sys
    proc = _make_proc(stdout=b"ok")
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs)
        return proc

    with patch("config.settings.settings.OPENAI_API_KEY", "sk-real-openai-key"), \
         patch("config.settings.settings.GLM_API_KEY", "sk-glm-should-not-be-used"), \
         patch("config.settings.settings.CODEX_MODEL", "glm-4-flash"), \
         patch("config.settings.settings.CODEX_TIMEOUT", 30), \
         patch("config.settings.settings.OPENAI_API_BASE", ""), \
         patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        agent = CodexAgent()
        await agent.run(_make_task())

    env = captured.get("env", {})
    # Must use the real OpenAI key, not the GLM key
    assert env.get("OPENAI_API_KEY") == "sk-real-openai-key"
    assert "PATH" in env or (sys.platform == "win32" and "Path" in env)


# ── 6. CODEX_MODEL 未配置时返回明确错误，不启动子进程 ─────────────────────────

@pytest.mark.asyncio
async def test_codex_empty_model_returns_failure_without_subprocess():
    """CODEX_MODEL 为空时立即返回 success=False，error 包含配置提示，不启动子进程"""
    with patch("config.settings.settings.CODEX_MODEL", ""), \
         patch("config.settings.settings.OPENAI_API_KEY", "sk-x"), \
         patch("config.settings.settings.GLM_API_KEY", ""), \
         patch("config.settings.settings.OPENAI_API_BASE", ""), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        agent = CodexAgent()
        result = await agent.run(_make_task())

    assert result.success is False
    assert result.exit_code == -1
    assert "CODEX_MODEL" in (result.error or "")
    mock_exec.assert_not_called()  # 子进程不应被启动
