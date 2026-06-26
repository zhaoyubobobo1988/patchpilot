"""
tests/integration/test_smoke.py

End-to-end smoke test for the OpenClaw pipeline.

Uses real GLM API calls (via ANTHROPIC_BASE_URL + GLM_API_KEY / OPENAI_API_KEY).
Mocks only the GitHub-facing parts (clone, PR creation, CI polling) so no
actual GitHub repository or network push is needed.

Run:
    OPENCLAW_SMOKE=1 uv run pytest tests/integration/test_smoke.py -v -s
    OPENCLAW_SMOKE=1 uv run pytest -m smoke -v -s
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ── Skip guard ────────────────────────────────────────────────────────────────
# Must opt in explicitly; these tests call the real GLM API and take ~2-3 min.
pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENCLAW_SMOKE"),
    reason="Set OPENCLAW_SMOKE=1 to run smoke tests (uses real GLM API)",
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _init_smoke_repo(workspace_path: str, repository: str) -> None:
    """
    Replace _clone_repo: turn the already-created workspace dir into a real
    git repo with a minimal features/auth/login.py so Workers have something
    to edit and ClaudeCodeWorker._validate_diff passes (files/ prefix check).
    """
    ws = Path(workspace_path)

    def _git(*args: str) -> None:
        subprocess.run(
            list(args), cwd=ws, check=True,
            capture_output=True, text=True, encoding="utf-8",
        )

    _git("git", "init")
    _git("git", "config", "user.email", "smoke@openclaw.local")
    _git("git", "config", "user.name", "OpenClaw-Smoke")

    auth = ws / "features" / "auth"
    auth.mkdir(parents=True)
    (auth / "__init__.py").write_text("")
    (auth / "login.py").write_text(
        "def login(username: str, password: str) -> bool:\n"
        "    # Validate credentials against hardcoded values (demo only)\n"
        "    return username == 'admin' and password == 'secret'\n"
    )

    _git("git", "add", "-A")
    _git("git", "commit", "-m", "chore: initial smoke-test repo")


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def github_mocks():
    """Patch all GitHub-touching methods; return the mock objects for assertions."""
    from models.github import CICheckResult, CIStatus, PRRequest, PRResult

    pr_request = PRRequest(
        repository="smoke-org/smoke-repo",
        title="feat(add-logging-to-login): Add logging to login",
        body="OpenClaw smoke test PR",
        head_branch="openclaw/add-logging/sm0000",
        base_branch="main",
    )
    pr_result = PRResult(
        pr_number=42,
        pr_url="https://github.com/smoke-org/smoke-repo/pull/42",
        head_branch="openclaw/add-logging/sm0000",
    )
    ci_ok = CICheckResult(pr_number=42, status=CIStatus.SUCCESS)

    with (
        patch("pipeline._clone_repo", side_effect=_init_smoke_repo),
        patch(
            "agents.github_agent.agent.GitHubAgent.apply_and_push",
            new_callable=AsyncMock, return_value=pr_request,
        ) as mock_push,
        patch(
            "agents.github_agent.agent.GitHubAgent.create_pr",
            new_callable=AsyncMock, return_value=pr_result,
        ) as mock_create_pr,
        patch(
            "agents.github_agent.agent.GitHubAgent.poll_ci",
            new_callable=AsyncMock, return_value=ci_ok,
        ) as mock_poll,
    ):
        yield {
            "push": mock_push,
            "create_pr": mock_create_pr,
            "poll_ci": mock_poll,
            "pr_result": pr_result,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Smoke tests
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.smoke
async def test_pipeline_smoke_end_to_end(github_mocks):
    """
    Full pipeline smoke test.

    Stages exercised with real GLM API calls:
      context → orchestrate → test-gen → worker → aggregate → integrate → review

    Stages mocked:
      clone (→ local git repo)   apply_and_push   create_pr   poll_ci

    Expected runtime: 1-4 minutes.
    Hard timeout: 5 minutes.
    """
    from pipeline import run_pipeline

    requirement = (
        "Add Python logging to the login function in features/auth/login.py. "
        "Import the logging module at the top of the file and add a call to "
        "logging.info('login attempt: %s', username) inside the login function."
    )

    result = await asyncio.wait_for(
        run_pipeline(
            raw_requirement=requirement,
            repository="smoke-org/smoke-repo",
            base_branch="main",
        ),
        timeout=300.0,
    )

    # ── Progress output (visible with -s) ─────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"[SMOKE] run_id        = {result.run_id}")
    print(f"[SMOKE] stage         = {result.stage}")
    print(f"[SMOKE] ci_passed     = {result.ci_passed}")
    print(f"[SMOKE] debug_retries = {result.debug_retry_count}")
    print(f"[SMOKE] errors        = {result.error_log}")
    print(f"{'─'*60}")

    # ── Assertions ─────────────────────────────────────────────────────────────

    # Pipeline must reach "done" — not stall at clone/context/orchestrate
    assert result.stage == "done", (
        f"Pipeline stuck at stage={result.stage!r}\n"
        f"errors={result.error_log}"
    )

    # run_id must be populated
    assert result.run_id, "run_id must be non-empty"

    # GitHub mock received calls
    github_mocks["push"].assert_called_once()
    github_mocks["create_pr"].assert_called_once()
    github_mocks["poll_ci"].assert_called_once()

    # CI mock returned SUCCESS so ci_passed must be True
    assert result.ci_passed is True, f"ci_passed={result.ci_passed}, errors={result.error_log}"

    # No workspace/preflight errors (those indicate setup problems)
    workspace_errors = [
        e for e in result.error_log
        if any(kw in e.lower() for kw in ("preflight", "workspace", "clone", "git"))
    ]
    assert not workspace_errors, f"Workspace errors: {workspace_errors}"


@pytest.mark.smoke
async def test_pipeline_smoke_telemetry_written(github_mocks):
    """Verify the JSONL telemetry file is written after a successful run."""
    import json
    from config.settings import settings
    from pipeline import run_pipeline

    log_path = settings.EXECUTION_LOG_PATH
    if not log_path:
        pytest.skip("EXECUTION_LOG_PATH not configured — telemetry disabled")

    initial_size = Path(log_path).stat().st_size if Path(log_path).exists() else 0

    await asyncio.wait_for(
        run_pipeline(
            raw_requirement=(
                "Add a docstring to the login function in features/auth/login.py "
                "explaining the parameters and return value."
            ),
            repository="smoke-org/smoke-repo",
            base_branch="main",
        ),
        timeout=300.0,
    )

    assert Path(log_path).exists(), f"Execution log not created: {log_path}"
    new_size = Path(log_path).stat().st_size
    assert new_size > initial_size, (
        f"No new records written to {log_path} "
        f"(before={initial_size}, after={new_size})"
    )

    # All lines must be valid JSON
    lines = Path(log_path).read_text(encoding="utf-8").strip().splitlines()
    bad = []
    for i, line in enumerate(lines[-20:], 1):   # check last 20 records
        try:
            json.loads(line)
        except json.JSONDecodeError as e:
            bad.append(f"line {i}: {e}")
    assert not bad, f"Invalid JSONL records: {bad}"

    print(f"\n[SMOKE] exec.jsonl has {len(lines)} total records after run")
