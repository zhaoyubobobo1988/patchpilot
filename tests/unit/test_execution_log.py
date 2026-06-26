"""
tests/unit/test_execution_log.py

Unit tests for telemetry/execution_log.py.
No real CLI calls — ClaudeCodeAgent / CodexAgent subprocess mocked.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.base import AgentResult, AgentTask
from telemetry.execution_log import ExecutionRecord, record_execution


# ── helpers ───────────────────────────────────────────────────────────────────

def _record(**kw) -> ExecutionRecord:
    return ExecutionRecord(run_id="run-test", event="agent_result", **kw)


def _read_jsonl(path: str) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _make_task(role: str = "worker") -> AgentTask:
    return AgentTask(
        task_id="run-test-w1-s1",
        role=role,
        prompt="PROMPT NOT LOGGED",
        workspace=Path("/tmp"),
    )


def _make_proc(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


# ── record_execution: no-op when path empty ──────────────────────────────────

def test_record_execution_noop_when_path_empty(tmp_path):
    """Default empty EXECUTION_LOG_PATH: no file written, no error"""
    with patch("config.settings.settings.EXECUTION_LOG_PATH", ""):
        record_execution(_record())
    # No file should exist anywhere
    assert list(tmp_path.iterdir()) == []


def test_record_execution_noop_does_not_raise():
    with patch("config.settings.settings.EXECUTION_LOG_PATH", ""):
        record_execution(_record(error="x" * 5000))   # huge error, should not raise


# ── record_execution: writes valid JSON line ──────────────────────────────────

def test_record_execution_writes_jsonl(tmp_path):
    """Non-empty path: appends one valid JSON line per call"""
    log = str(tmp_path / "exec.jsonl")
    with patch("config.settings.settings.EXECUTION_LOG_PATH", log):
        record_execution(_record(success=True))
        record_execution(_record(success=False, error="oops"))

    lines = _read_jsonl(log)
    assert len(lines) == 2
    assert lines[0]["success"] is True
    assert lines[1]["success"] is False
    assert lines[1]["error"] == "oops"


def test_record_execution_output_is_valid_json(tmp_path):
    log = str(tmp_path / "exec.jsonl")
    with patch("config.settings.settings.EXECUTION_LOG_PATH", log):
        record_execution(_record(metadata={"key": 42, "flag": True}))
    data = _read_jsonl(log)[0]
    assert data["metadata"]["key"] == 42


# ── parent directory auto-created ─────────────────────────────────────────────

def test_record_execution_creates_parent_dirs(tmp_path):
    nested = str(tmp_path / "a" / "b" / "exec.jsonl")
    assert not Path(nested).parent.exists()
    with patch("config.settings.settings.EXECUTION_LOG_PATH", nested):
        record_execution(_record())
    assert Path(nested).exists()


# ── error truncation ──────────────────────────────────────────────────────────

def test_record_execution_truncates_error_to_1000_chars(tmp_path):
    log = str(tmp_path / "exec.jsonl")
    long_error = "e" * 2000
    with patch("config.settings.settings.EXECUTION_LOG_PATH", log):
        record_execution(_record(error=long_error))
    data = _read_jsonl(log)[0]
    assert len(data["error"]) <= 1000


def test_record_execution_none_error_stays_none(tmp_path):
    log = str(tmp_path / "exec.jsonl")
    with patch("config.settings.settings.EXECUTION_LOG_PATH", log):
        record_execution(_record(error=None))
    data = _read_jsonl(log)[0]
    assert data["error"] is None


# ── metadata is JSON-serialisable ─────────────────────────────────────────────

def test_record_execution_metadata_json_safe(tmp_path):
    log = str(tmp_path / "exec.jsonl")
    with patch("config.settings.settings.EXECUTION_LOG_PATH", log):
        record_execution(_record(metadata={"n": 1, "ok": True, "s": "hi"}))
    data = _read_jsonl(log)[0]
    assert data["metadata"] == {"n": 1, "ok": True, "s": "hi"}


# ── ClaudeCodeAgent records agent_result ─────────────────────────────────────

@pytest.mark.asyncio
async def test_claude_code_agent_records_on_success(tmp_path):
    """Successful ClaudeCodeAgent.run() writes one agent_result to log"""
    import json as _json
    log = str(tmp_path / "exec.jsonl")
    import json
    envelope = json.dumps({"result": "hello"}).encode()
    proc = _make_proc(stdout=envelope, returncode=0)

    with patch("config.settings.settings.EXECUTION_LOG_PATH", log), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("config.settings.settings.GLM_API_KEY", "sk-x"), \
         patch("config.settings.settings.ANTHROPIC_API_KEY", ""), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        from agents.claude_code import ClaudeCodeAgent
        agent = ClaudeCodeAgent()
        result = await agent.run(_make_task())

    assert result.success is True
    lines = _read_jsonl(log)
    assert len(lines) == 1
    assert lines[0]["event"] == "agent_result"
    assert lines[0]["agent"] == "claude-code"
    assert lines[0]["success"] is True


@pytest.mark.asyncio
async def test_claude_code_agent_records_on_failure(tmp_path):
    """Failed ClaudeCodeAgent.run() also writes agent_result"""
    log = str(tmp_path / "exec.jsonl")
    proc = _make_proc(stdout=b"", stderr=b"auth error", returncode=1)

    with patch("config.settings.settings.EXECUTION_LOG_PATH", log), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://x.com"), \
         patch("config.settings.settings.GLM_API_KEY", "sk-x"), \
         patch("config.settings.settings.ANTHROPIC_API_KEY", ""), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        from agents.claude_code import ClaudeCodeAgent
        agent = ClaudeCodeAgent()
        result = await agent.run(_make_task())

    assert result.success is False
    lines = _read_jsonl(log)
    assert lines[0]["success"] is False
    assert lines[0]["agent"] == "claude-code"


def test_claude_code_agent_does_not_log_prompt(tmp_path):
    """Prompt text must NEVER appear in the JSONL file"""
    log = str(tmp_path / "exec.jsonl")
    with patch("config.settings.settings.EXECUTION_LOG_PATH", log):
        record_execution(ExecutionRecord(
            run_id="r", event="agent_result", agent="claude-code",
            role="worker", metadata={"task_id": "t1"},
        ))
    content = Path(log).read_text()
    assert "PROMPT NOT LOGGED" not in content


# ── CodexAgent records agent_result ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_codex_agent_records_on_failure(tmp_path):
    """CodexAgent.run() failure (no model) still records agent_result"""
    log = str(tmp_path / "exec.jsonl")

    with patch("config.settings.settings.EXECUTION_LOG_PATH", log), \
         patch("config.settings.settings.CODEX_MODEL", ""), \
         patch("config.settings.settings.OPENAI_API_KEY", "sk-x"), \
         patch("config.settings.settings.GLM_API_KEY", ""), \
         patch("config.settings.settings.OPENAI_API_BASE", ""):
        from agents.codex import CodexAgent
        agent = CodexAgent()
        result = await agent.run(_make_task(role="reviewer"))

    assert result.success is False
    lines = _read_jsonl(log)
    assert lines[0]["agent"] == "codex"
    assert lines[0]["success"] is False


# ── IntegratorAgent records integration_result ────────────────────────────────

@pytest.mark.asyncio
async def test_integrator_records_on_success(tmp_path):
    from agents.integrator.agent import IntegratorAgent
    from models.context import AgentContext
    from models.patch import MergedPatch, PatchStatus
    from models.task import FeatureTask

    log = str(tmp_path / "exec.jsonl")
    ctx = AgentContext(
        run_id="r1", feature_task_id="ft1", repository="org/repo",
        base_branch="main", workspace_path="/tmp", model="m",
    )
    task = FeatureTask(raw_requirement="r", feature_name="f", repository="org/repo")
    merged = MergedPatch(
        feature_task_id="ft1",
        merged_diff="diff --git a/features/x.py b/features/x.py\n+x\n",
        source_patch_ids=["s1"],
        status=PatchStatus.SUCCESS,
    )

    with patch("config.settings.settings.EXECUTION_LOG_PATH", log), \
         patch("config.settings.settings.INTEGRATION_TEST_COMMAND", ""):
        agent = IntegratorAgent(ctx)
        await agent.integrate(merged, task)

    lines = _read_jsonl(log)
    assert lines[0]["event"] == "integration_result"
    assert lines[0]["success"] is True
    assert lines[0]["agent"] == "integrator"


@pytest.mark.asyncio
async def test_integrator_records_on_failure(tmp_path):
    from agents.integrator.agent import IntegratorAgent
    from models.context import AgentContext
    from models.patch import MergedPatch, PatchStatus
    from models.task import FeatureTask

    log = str(tmp_path / "exec.jsonl")
    ctx = AgentContext(
        run_id="r1", feature_task_id="ft1", repository="org/repo",
        base_branch="main", workspace_path="/tmp", model="m",
    )
    task = FeatureTask(raw_requirement="r", feature_name="f", repository="org/repo")
    failed = MergedPatch(
        feature_task_id="ft1", merged_diff="", source_patch_ids=[],
        status=PatchStatus.FAILED, error_details="no patches",
    )

    with patch("config.settings.settings.EXECUTION_LOG_PATH", log):
        agent = IntegratorAgent(ctx)
        with pytest.raises(ValueError):
            await agent.integrate(failed, task)

    lines = _read_jsonl(log)
    assert lines[0]["event"] == "integration_result"
    assert lines[0]["success"] is False


# ── pipeline events ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pipeline_records_preflight_failed(tmp_path):
    """Preflight failure → pipeline_preflight_failed event"""
    log = str(tmp_path / "exec.jsonl")

    with patch("config.settings.settings.EXECUTION_LOG_PATH", log), \
         patch("pipeline._clone_repo"), \
         patch("agents.worker.workspace.WorkerWorkspaceManager.validate_strategy",
               return_value=(False, "not a git repo")):
        from pipeline import run_pipeline
        run = await run_pipeline("test", "org/repo")

    assert run.stage == "done"
    lines = _read_jsonl(log)
    preflight_lines = [l for l in lines if l["event"] == "pipeline_preflight_failed"]
    assert len(preflight_lines) == 1
    assert preflight_lines[0]["success"] is False
    assert "not a git repo" in (preflight_lines[0]["error"] or "")


@pytest.mark.asyncio
async def test_pipeline_records_completed_on_early_termination(tmp_path):
    """
    Every early termination (preflight fail, integrate fail, review blocked)
    must write a pipeline_completed event.  We use preflight failure because
    it terminates the run immediately with no other mocking required.
    """
    log = str(tmp_path / "exec.jsonl")

    with patch("config.settings.settings.EXECUTION_LOG_PATH", log), \
         patch("pipeline._clone_repo"), \
         patch("agents.worker.workspace.WorkerWorkspaceManager.validate_strategy",
               return_value=(False, "not a git repo — early exit")):
        from pipeline import run_pipeline
        run = await run_pipeline("test", "org/repo")

    assert run.stage == "done"
    lines = _read_jsonl(log)
    completed = [l for l in lines if l["event"] == "pipeline_completed"]
    assert len(completed) == 1, f"Expected 1 pipeline_completed, got {completed}"
    ev = completed[0]
    assert ev["success"] is False
    assert ev["metadata"]["stage"] == "done"
    assert "error_count" in ev["metadata"]


@pytest.mark.asyncio
async def test_pipeline_completed_metadata_fields(tmp_path):
    """
    pipeline_completed metadata must contain stage, pr_url, ci_passed,
    debug_retry_count, error_count regardless of exit path.
    """
    log = str(tmp_path / "exec.jsonl")

    with patch("config.settings.settings.EXECUTION_LOG_PATH", log), \
         patch("pipeline._clone_repo"), \
         patch("agents.worker.workspace.WorkerWorkspaceManager.validate_strategy",
               return_value=(False, "err")):
        from pipeline import run_pipeline
        await run_pipeline("test", "org/repo")

    lines = _read_jsonl(log)
    ev = next(l for l in lines if l["event"] == "pipeline_completed")
    for key in ("stage", "pr_url", "ci_passed", "debug_retry_count", "error_count"):
        assert key in ev["metadata"], f"missing key: {key}"
