"""
Minimal Agent Registry — maps string names to AgentAdapter instances.

This is a plain name → instance lookup table.
No automatic selection, no priority, no scoring, no routing logic.
"""
from __future__ import annotations

from agents.base import AgentAdapter
from config.logging import get_logger

logger = get_logger(__name__)


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentAdapter] = {}

    def register(self, name: str, agent: AgentAdapter) -> None:
        """Add or overwrite a named agent."""
        if name in self._agents:
            logger.debug(f"[AgentRegistry] overwriting existing registration: {name!r}")
        self._agents[name] = agent

    def get(self, name: str) -> AgentAdapter | None:
        """Return the agent registered under *name*, or None if not found."""
        return self._agents.get(name)

    def has(self, name: str) -> bool:
        return name in self._agents

    def names(self) -> list[str]:
        return list(self._agents.keys())


# ── Default registry ──────────────────────────────────────────────────────────
# Imported at the bottom to avoid circular imports; both Agent classes depend
# only on agents/base.py and config/, so no cycle is introduced here.
from agents.claude_code import ClaudeCodeAgent  # noqa: E402
from agents.codex import CodexAgent             # noqa: E402

default_registry = AgentRegistry()
default_registry.register("claude-code", ClaudeCodeAgent())
default_registry.register("codex", CodexAgent())
