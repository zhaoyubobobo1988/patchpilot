"""
Minimal agent abstraction for OpenClaw.

AgentTask  — input to any agent subprocess
AgentResult — output from any agent subprocess
AgentAdapter — Protocol that ClaudeCodeAgent (and future adapters) satisfy
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class AgentTask:
    task_id: str
    role: str               # "planner" | "worker" | "reviewer"
    prompt: str
    workspace: Path
    timeout_seconds: int = 300
    # "json"  → parse claude's {"result": "..."} envelope  (Planner / Reviewer)
    # "text"  → return raw stdout; caller does git-diff post-processing (Worker)
    output_format: str = "json"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    success: bool
    output: str             # parsed result text or raw stdout
    exit_code: int
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class AgentAdapter(Protocol):
    async def run(self, task: AgentTask) -> AgentResult:
        ...
