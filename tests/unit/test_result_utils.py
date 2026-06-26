"""
tests/unit/test_result_utils.py

Unit tests for agents/result_utils.py.
Verifies that extract_agent_output logs the agent name from
result.metadata["agent"], not a hardcoded string.
No real CLI calls — all AgentResult objects are constructed inline.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from agents.base import AgentResult
from agents.result_utils import extract_agent_output


def _ok(agent: str = "codex", output: str = "ok-output") -> AgentResult:
    return AgentResult(
        success=True,
        output=output,
        exit_code=0,
        metadata={"agent": agent, "role": "reviewer", "task_id": "t1", "elapsed_seconds": 1.2},
    )


def _fail(agent: str = "codex", error: str = "boom") -> AgentResult:
    return AgentResult(
        success=False,
        output="",
        exit_code=-1,
        error=error,
        metadata={"agent": agent, "role": "reviewer", "task_id": "t1", "elapsed_seconds": 0.5},
    )


# ── success path ──────────────────────────────────────────────────────────────

def test_success_log_contains_agent_name_from_metadata():
    """成功时日志必须包含 metadata['agent']，不是硬编码的 'ClaudeCodeAgent'"""
    with patch("agents.result_utils.logger") as mock_log:
        result = extract_agent_output(_ok(agent="codex"), caller="Reviewer")

    assert result == "ok-output"
    assert mock_log.info.called
    logged = str(mock_log.info.call_args)
    assert "codex" in logged
    assert "ok" in logged
    assert "ClaudeCodeAgent" not in logged


def test_success_log_claude_code_agent_name():
    """claude-code 后端成功时日志显示 'claude-code'，不是硬编码"""
    with patch("agents.result_utils.logger") as mock_log:
        extract_agent_output(_ok(agent="claude-code"), caller="Planner")

    logged = str(mock_log.info.call_args)
    assert "claude-code" in logged
    assert "ClaudeCodeAgent" not in logged


def test_success_log_includes_task_id_and_elapsed():
    """成功日志同时包含 task_id 和 elapsed_seconds"""
    with patch("agents.result_utils.logger") as mock_log:
        extract_agent_output(_ok(), caller="Reviewer")

    logged = str(mock_log.info.call_args)
    assert "t1" in logged
    assert "1.2" in logged


# ── failure path ──────────────────────────────────────────────────────────────

def test_failure_log_contains_agent_name_from_metadata():
    """失败时日志必须包含 metadata['agent']，不是硬编码"""
    with patch("agents.result_utils.logger") as mock_log:
        result = extract_agent_output(_fail(agent="codex"), caller="Reviewer")

    assert result is None
    assert mock_log.warning.called
    logged = str(mock_log.warning.call_args)
    assert "codex" in logged
    assert "failed" in logged
    assert "ClaudeCodeAgent" not in logged


def test_failure_log_includes_exit_code_and_error():
    """失败日志包含 exit_code 和 error 字符串"""
    with patch("agents.result_utils.logger") as mock_log:
        extract_agent_output(_fail(error="connection refused"), caller="Reviewer")

    logged = str(mock_log.warning.call_args)
    assert "connection refused" in logged
    assert "-1" in logged


# ── missing agent key ─────────────────────────────────────────────────────────

def test_missing_agent_key_uses_default():
    """metadata 没有 'agent' 键时日志使用默认值 'agent'"""
    result = AgentResult(
        success=True,
        output="data",
        exit_code=0,
        metadata={"role": "reviewer", "task_id": "t2"},  # no "agent" key
    )
    with patch("agents.result_utils.logger") as mock_log:
        out = extract_agent_output(result, caller="Reviewer")

    assert out == "data"
    logged = str(mock_log.info.call_args)
    assert "agent" in logged          # default value used
    assert "ClaudeCodeAgent" not in logged


def test_empty_metadata_uses_default():
    """metadata 完全为空时不崩溃，使用默认 'agent'"""
    result = AgentResult(success=True, output="x", exit_code=0, metadata={})
    with patch("agents.result_utils.logger") as mock_log:
        out = extract_agent_output(result, caller="Test")

    assert out == "x"
    assert mock_log.info.called


# ── return-value contract ─────────────────────────────────────────────────────

def test_returns_none_on_empty_output_even_if_success_true():
    """success=True 但 output='' 时返回 None（触发 fallback）"""
    result = AgentResult(success=True, output="", exit_code=0, metadata={"agent": "codex"})
    with patch("agents.result_utils.logger"):
        assert extract_agent_output(result, "Reviewer") is None


def test_returns_none_on_failure():
    with patch("agents.result_utils.logger"):
        assert extract_agent_output(_fail(), "Reviewer") is None


def test_returns_output_string_on_success():
    with patch("agents.result_utils.logger"):
        assert extract_agent_output(_ok(output="the-json"), "Reviewer") == "the-json"
