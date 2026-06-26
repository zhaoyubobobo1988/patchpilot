"""ClaudeCodeAgent 单元测试（不真实调用 API）"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.base import AgentTask
from agents.claude_code import ClaudeCodeAgent


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_task(
    role: str = "planner",
    output_format: str = "json",
    timeout: int = 30,
    workspace: str = "/tmp/test-ws",
) -> AgentTask:
    return AgentTask(
        task_id="t001",
        role=role,
        prompt="test prompt",
        workspace=Path(workspace),
        timeout_seconds=timeout,
        output_format=output_format,
    )


def _make_proc(stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


def _json_envelope(text: str) -> bytes:
    return json.dumps({"type": "result", "subtype": "success", "result": text}).encode()


_SETTINGS_PATCH = {
    "config.settings.settings.ANTHROPIC_BASE_URL": "https://open.bigmodel.cn/api/anthropic",
    "config.settings.settings.GLM_API_KEY": "sk-test-key",
    "config.settings.settings.ANTHROPIC_API_KEY": "",
}


# ── 1. 成功执行（json 模式） ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_success_json_mode():
    """json 模式：解析 envelope["result"] 并返回 AgentResult(success=True)"""
    expected = '{"feature_name": "test"}'
    proc = _make_proc(stdout=_json_envelope(expected))

    with patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://example.com"), \
         patch("config.settings.settings.GLM_API_KEY", "sk-test"), \
         patch("config.settings.settings.ANTHROPIC_API_KEY", ""), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        agent = ClaudeCodeAgent()
        result = await agent.run(_make_task(role="planner", output_format="json"))

    assert result.success is True
    assert result.output == expected
    assert result.exit_code == 0
    assert result.error is None
    assert result.metadata["role"] == "planner"


# ── 2. 成功执行（text 模式） ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_success_text_mode():
    """text 模式：返回原始 stdout（Worker 用；后续由 git diff 提取 patch）"""
    raw_stdout = b"some worker output\n"
    proc = _make_proc(stdout=raw_stdout)

    with patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://example.com"), \
         patch("config.settings.settings.GLM_API_KEY", "sk-test"), \
         patch("config.settings.settings.ANTHROPIC_API_KEY", ""), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        agent = ClaudeCodeAgent()
        result = await agent.run(_make_task(role="worker", output_format="text"))

    assert result.success is True
    assert result.output == "some worker output"
    assert result.exit_code == 0


# ── 3. CLI 退出码非零 → FAILED ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_nonzero_exit_returns_failure():
    """非零退出码返回 success=False，error 包含 stderr 内容"""
    proc = _make_proc(stdout=b"", stderr=b"Permission denied", returncode=1)

    with patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://example.com"), \
         patch("config.settings.settings.GLM_API_KEY", "sk-test"), \
         patch("config.settings.settings.ANTHROPIC_API_KEY", ""), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        agent = ClaudeCodeAgent()
        result = await agent.run(_make_task())

    assert result.success is False
    assert result.exit_code == 1
    assert "Permission denied" in (result.error or "")


# ── 4. 超时 → 子进程被终止，返回 FAILED ──────────────────────────────────────

@pytest.mark.asyncio
async def test_timeout_kills_process_and_returns_failure():
    """超时：子进程被 kill()，返回 success=False, exit_code=-1"""
    proc = _make_proc(stdout=b"")
    proc.returncode = None   # 进程仍在运行

    with patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://example.com"), \
         patch("config.settings.settings.GLM_API_KEY", "sk-test"), \
         patch("config.settings.settings.ANTHROPIC_API_KEY", ""), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)), \
         patch("asyncio.wait_for", new=AsyncMock(side_effect=asyncio.TimeoutError())):
        agent = ClaudeCodeAgent()
        result = await agent.run(_make_task(timeout=1))

    assert result.success is False
    assert result.exit_code == -1
    assert "timed out" in (result.error or "").lower()
    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


# ── 5. 工作目录正确传入 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_workspace_passed_as_cwd():
    """workspace 路径作为 cwd 传入 create_subprocess_exec"""
    proc = _make_proc(stdout=_json_envelope("ok"))
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs)
        return proc

    with patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://example.com"), \
         patch("config.settings.settings.GLM_API_KEY", "sk-test"), \
         patch("config.settings.settings.ANTHROPIC_API_KEY", ""), \
         patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        agent = ClaudeCodeAgent()
        await agent.run(_make_task(workspace="/my/workspace"))

    assert Path(captured.get("cwd", "")) == Path("/my/workspace")


# ── 6. 环境变量：自定义 + 系统均保留 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_env_merges_system_and_glm_vars():
    """
    subprocess env 包含：
    - ANTHROPIC_BASE_URL（GLM shim）
    - ANTHROPIC_AUTH_TOKEN（GLM key）
    - 系统 PATH（os.environ 不丢失）
    - extra_env 中的自定义变量
    """
    proc = _make_proc(stdout=_json_envelope("ok"))
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs)
        return proc

    with patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://glm.example.com"), \
         patch("config.settings.settings.GLM_API_KEY", "sk-glm-xyz"), \
         patch("config.settings.settings.ANTHROPIC_API_KEY", ""), \
         patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        agent = ClaudeCodeAgent(extra_env={"MY_CUSTOM_VAR": "hello"})
        await agent.run(_make_task())

    env = captured.get("env", {})
    assert env.get("ANTHROPIC_BASE_URL") == "https://glm.example.com"
    assert env.get("ANTHROPIC_AUTH_TOKEN") == "sk-glm-xyz"
    assert env.get("MY_CUSTOM_VAR") == "hello"
    # System PATH must not be lost
    assert "PATH" in env or sys.platform == "win32" and "Path" in env


# ── 7. Planner / Worker / Reviewer 三角色兼容性 ────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("role,output_format,stdout,expected_output", [
    ("planner",  "json", _json_envelope('{"ok":true}'), '{"ok":true}'),
    ("worker",   "text", b"raw worker output",           "raw worker output"),
    ("reviewer", "json", _json_envelope('{"approved":true}'), '{"approved":true}'),
])
async def test_three_roles_compatible(role, output_format, stdout, expected_output):
    """Planner / Worker / Reviewer 三种角色都能正确运行并返回预期输出"""
    proc = _make_proc(stdout=stdout)

    with patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://example.com"), \
         patch("config.settings.settings.GLM_API_KEY", "sk-test"), \
         patch("config.settings.settings.ANTHROPIC_API_KEY", ""), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        agent = ClaudeCodeAgent()
        result = await agent.run(_make_task(role=role, output_format=output_format))

    assert result.success is True
    assert result.output == expected_output
    assert result.metadata["role"] == role
