"""AgentRegistry 单元测试"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agents.registry import AgentRegistry, default_registry


def _mock_agent(name: str = "mock") -> MagicMock:
    agent = MagicMock()
    agent.__repr__ = lambda self: f"MockAgent({name})"
    return agent


# ── AgentRegistry 基本操作 ────────────────────────────────────────────────────

def test_register_and_get():
    registry = AgentRegistry()
    agent = _mock_agent()
    registry.register("my-agent", agent)
    assert registry.get("my-agent") is agent


def test_get_missing_returns_none():
    registry = AgentRegistry()
    assert registry.get("does-not-exist") is None


def test_has_returns_true_for_registered():
    registry = AgentRegistry()
    registry.register("x", _mock_agent())
    assert registry.has("x") is True


def test_has_returns_false_for_missing():
    registry = AgentRegistry()
    assert registry.has("missing") is False


def test_names_returns_all_registered():
    registry = AgentRegistry()
    registry.register("alpha", _mock_agent())
    registry.register("beta", _mock_agent())
    assert set(registry.names()) == {"alpha", "beta"}


def test_register_overwrites_existing():
    registry = AgentRegistry()
    first = _mock_agent("first")
    second = _mock_agent("second")
    registry.register("agent", first)
    registry.register("agent", second)
    assert registry.get("agent") is second
    assert len(registry.names()) == 1  # only one entry


def test_empty_registry_names_is_empty():
    registry = AgentRegistry()
    assert registry.names() == []


# ── default_registry 内容 ─────────────────────────────────────────────────────

def test_default_registry_has_claude_code():
    assert default_registry.has("claude-code")
    agent = default_registry.get("claude-code")
    assert agent is not None
    from agents.claude_code import ClaudeCodeAgent
    assert isinstance(agent, ClaudeCodeAgent)


def test_default_registry_has_codex():
    assert default_registry.has("codex")
    agent = default_registry.get("codex")
    assert agent is not None
    from agents.codex import CodexAgent
    assert isinstance(agent, CodexAgent)


def test_default_registry_names_contains_expected():
    names = default_registry.names()
    assert "claude-code" in names
    assert "codex" in names


def test_default_registry_does_not_have_unknown():
    assert default_registry.has("opencode") is False
    assert default_registry.has("aider") is False
    assert default_registry.get("router") is None
