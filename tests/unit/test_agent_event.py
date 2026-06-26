"""
tests/unit/test_agent_event.py  —  PR3 AgentEvent protocol (written FIRST, before impl)

Red phase: all tests here must FAIL before implementation is written.

Covers:
  1. AgentEvent model in models.events with kind/agent_id/timestamp/payload
  2. AgentEventKind enum has at least: started, completed, failed
  3. AgentResult.events field exists and defaults to []
  4. ClaudeCodeAgent emits [started, completed] on success
  5. ClaudeCodeAgent emits [started, failed] on non-zero exit
  6. ClaudeCodeAgent emits [started, failed] on timeout
  7. started event is always first
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── 1 & 2. AgentEvent model + enum ──────────────────────────────────────────

def test_agent_event_model_exists():
    from models.events import AgentEvent  # noqa: F401


def test_agent_event_kind_enum_exists():
    from models.events import AgentEventKind  # noqa: F401


def test_agent_event_kind_has_required_values():
    from models.events import AgentEventKind
    assert AgentEventKind.STARTED.value == "started"
    assert AgentEventKind.COMPLETED.value == "completed"
    assert AgentEventKind.FAILED.value == "failed"


def test_agent_event_has_expected_fields():
    from models.events import AgentEvent, AgentEventKind
    ev = AgentEvent(kind=AgentEventKind.STARTED, agent_id="t1")
    assert ev.kind == AgentEventKind.STARTED
    assert ev.agent_id == "t1"
    assert isinstance(ev.timestamp, float)
    assert ev.payload == {}


def test_agent_event_accepts_payload():
    from models.events import AgentEvent, AgentEventKind
    ev = AgentEvent(kind=AgentEventKind.COMPLETED, agent_id="t1", payload={"elapsed": 1.5})
    assert ev.payload["elapsed"] == 1.5


# ── 3. AgentResult.events field ──────────────────────────────────────────────

def test_agent_result_has_events_field():
    from agents.base import AgentResult
    r = AgentResult(success=True, output="ok", exit_code=0)
    assert hasattr(r, "events")
    assert r.events == []


def test_agent_result_events_accepts_list():
    from agents.base import AgentResult
    from models.events import AgentEvent, AgentEventKind
    ev = AgentEvent(kind=AgentEventKind.STARTED, agent_id="t1")
    r = AgentResult(success=True, output="ok", exit_code=0, events=[ev])
    assert len(r.events) == 1
    assert r.events[0].kind == AgentEventKind.STARTED


# ── 4. ClaudeCodeAgent success → [started, completed] ────────────────────────

async def test_success_emits_started_and_completed(tmp_path):
    from agents.claude_code import ClaudeCodeAgent
    from agents.base import AgentTask
    from models.events import AgentEventKind

    agent = ClaudeCodeAgent()
    task = AgentTask(task_id="t1", role="worker", prompt="hello", workspace=tmp_path)

    mock_proc = MagicMock()
    mock_proc.returncode = 0

    with (
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)),
        patch("asyncio.wait_for", new=AsyncMock(return_value=(b'{"result": "ok"}', b""))),
        patch("telemetry.execution_log.record_agent_result"),
    ):
        result = await agent.run(task)

    kinds = [e.kind for e in result.events]
    assert AgentEventKind.STARTED in kinds, f"expected STARTED in {kinds}"
    assert AgentEventKind.COMPLETED in kinds, f"expected COMPLETED in {kinds}"
    assert kinds[0] == AgentEventKind.STARTED, "STARTED must be first"


def test_success_started_event_carries_role(tmp_path):
    """The started event payload includes the agent role."""
    import asyncio as _asyncio

    async def _run():
        from agents.claude_code import ClaudeCodeAgent
        from agents.base import AgentTask
        from models.events import AgentEventKind

        agent = ClaudeCodeAgent()
        task = AgentTask(task_id="t2", role="planner", prompt="plan", workspace=tmp_path)
        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)),
            patch("asyncio.wait_for", new=AsyncMock(return_value=(b'{"result": "ok"}', b""))),
            patch("telemetry.execution_log.record_agent_result"),
        ):
            result = await agent.run(task)

        started = next(e for e in result.events if e.kind == AgentEventKind.STARTED)
        assert started.agent_id == "t2"
        return started

    _asyncio.run(_run())


# ── 5. Non-zero exit → [started, failed] ─────────────────────────────────────

async def test_nonzero_exit_emits_started_and_failed(tmp_path):
    from agents.claude_code import ClaudeCodeAgent
    from agents.base import AgentTask
    from models.events import AgentEventKind

    agent = ClaudeCodeAgent()
    task = AgentTask(task_id="t1", role="worker", prompt="hello", workspace=tmp_path)

    mock_proc = MagicMock()
    mock_proc.returncode = 1

    with (
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)),
        patch("asyncio.wait_for", new=AsyncMock(return_value=(b"", b"some error"))),
        patch("telemetry.execution_log.record_agent_result"),
    ):
        result = await agent.run(task)

    assert result.success is False
    kinds = [e.kind for e in result.events]
    assert AgentEventKind.STARTED in kinds, f"expected STARTED in {kinds}"
    assert AgentEventKind.FAILED in kinds, f"expected FAILED in {kinds}"
    assert kinds[0] == AgentEventKind.STARTED, "STARTED must be first"


# ── 6. Timeout → [started, failed] ───────────────────────────────────────────

async def test_timeout_emits_started_and_failed(tmp_path):
    from agents.claude_code import ClaudeCodeAgent
    from agents.base import AgentTask
    from models.events import AgentEventKind

    agent = ClaudeCodeAgent()
    task = AgentTask(task_id="t1", role="worker", prompt="hello", workspace=tmp_path,
                     timeout_seconds=1)

    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()

    with (
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)),
        patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()),
        patch("telemetry.execution_log.record_agent_result"),
    ):
        result = await agent.run(task)

    assert result.success is False
    kinds = [e.kind for e in result.events]
    assert AgentEventKind.STARTED in kinds, f"expected STARTED in {kinds}"
    assert AgentEventKind.FAILED in kinds, f"expected FAILED in {kinds}"
    assert kinds[0] == AgentEventKind.STARTED, "STARTED must be first"
