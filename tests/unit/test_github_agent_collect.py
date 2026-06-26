"""
tests/unit/test_github_agent_collect.py

Unit tests for GitHubAgent._collect_worker_changes().

Verifies that patches from multiple Worker workspaces are applied via
git apply (not file-copy), so two Workers editing the same file at
different locations both have their changes preserved, and two Workers
editing the same hunk raise MergeConflictError explicitly.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents.github_agent.agent import GitHubAgent, MergeConflictError
from models.context import AgentContext


# ── helpers ───────────────────────────────────────────────────────────────────

def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return r.stdout.strip()


def _make_main_repo(base: Path) -> Path:
    """Create a bare main workspace with one committed file."""
    repo = base / "run-abc123"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@openclaw.local")
    _git(repo, "config", "user.name", "OpenClaw-Test")

    (repo / "features").mkdir()
    target = repo / "features" / "auth.py"
    target.write_text(
        "def login():\n"
        "    # line 1\n"
        "    # line 2\n"
        "    # line 3\n"
        "    # line 4\n"
        "    # line 5\n"
        "    return True\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    _git(repo, "checkout", "-b", "openclaw/feat/abc123")
    return repo


def _make_worker_repo(base: Path, main_repo: Path, worker_id: str) -> Path:
    """Clone main_repo as a Worker workspace and return its path."""
    run_id = main_repo.name          # "run-abc123"
    ws = base / f"{run_id}-{worker_id}"
    subprocess.run(
        ["git", "clone", "--local", str(main_repo), str(ws)],
        check=True,
        capture_output=True,
    )
    _git(ws, "config", "user.email", "worker@openclaw.local")
    _git(ws, "config", "user.name", "Worker")
    return ws


def _make_agent(main_repo: Path) -> GitHubAgent:
    ctx = AgentContext(
        run_id="abc123",
        feature_task_id="task-1",
        repository="org/repo",
        base_branch="main",
        workspace_path=str(main_repo),
    )
    return GitHubAgent(ctx)


# ── tests ─────────────────────────────────────────────────────────────────────

def test_two_workers_different_files(tmp_path):
    """Two Workers edit different files — both changes land in main workspace."""
    main = _make_main_repo(tmp_path)
    w1 = _make_worker_repo(tmp_path, main, "worker-1")
    w2 = _make_worker_repo(tmp_path, main, "worker-2")

    # Worker 1 creates a new file
    (w1 / "features" / "logging.py").write_text("import logging\n", encoding="utf-8")
    _git(w1, "add", "-A")

    # Worker 2 creates another new file
    (w2 / "features" / "metrics.py").write_text("import time\n", encoding="utf-8")
    _git(w2, "add", "-A")

    agent = _make_agent(main)
    agent._collect_worker_changes(main, task=None)  # type: ignore[arg-type]

    assert (main / "features" / "logging.py").read_text(encoding="utf-8") == "import logging\n"
    assert (main / "features" / "metrics.py").read_text(encoding="utf-8") == "import time\n"


def test_two_workers_same_file_different_hunks(tmp_path):
    """Two Workers edit the same file at non-overlapping locations — both preserved."""
    main = _make_main_repo(tmp_path)
    w1 = _make_worker_repo(tmp_path, main, "worker-1")
    w2 = _make_worker_repo(tmp_path, main, "worker-2")

    # Worker 1 edits line 1
    (w1 / "features" / "auth.py").write_text(
        "def login():\n"
        "    # WORKER1 was here\n"
        "    # line 2\n"
        "    # line 3\n"
        "    # line 4\n"
        "    # line 5\n"
        "    return True\n",
        encoding="utf-8",
    )
    _git(w1, "add", "-A")

    # Worker 2 edits line 5 (far from worker 1's change)
    (w2 / "features" / "auth.py").write_text(
        "def login():\n"
        "    # line 1\n"
        "    # line 2\n"
        "    # line 3\n"
        "    # line 4\n"
        "    # WORKER2 was here\n"
        "    return True\n",
        encoding="utf-8",
    )
    _git(w2, "add", "-A")

    agent = _make_agent(main)
    agent._collect_worker_changes(main, task=None)  # type: ignore[arg-type]

    result = (main / "features" / "auth.py").read_text(encoding="utf-8")
    assert "WORKER1 was here" in result
    assert "WORKER2 was here" in result


def test_worker_with_no_changes_is_skipped(tmp_path):
    """A Worker workspace with no staged changes does not cause an error."""
    main = _make_main_repo(tmp_path)
    w1 = _make_worker_repo(tmp_path, main, "worker-1")
    # w1 makes no changes — nothing staged

    agent = _make_agent(main)
    agent._collect_worker_changes(main, task=None)  # type: ignore[arg-type]

    # No exception; auth.py should be unchanged
    content = (main / "features" / "auth.py").read_text(encoding="utf-8")
    assert "line 1" in content


def test_two_workers_same_hunk_raises_conflict(tmp_path):
    """Two Workers edit the exact same line — must raise MergeConflictError."""
    main = _make_main_repo(tmp_path)
    w1 = _make_worker_repo(tmp_path, main, "worker-1")
    w2 = _make_worker_repo(tmp_path, main, "worker-2")

    # Both Workers replace line 3 with different content
    (w1 / "features" / "auth.py").write_text(
        "def login():\n"
        "    # line 1\n"
        "    # line 2\n"
        "    # WORKER1_CONFLICT\n"
        "    # line 4\n"
        "    # line 5\n"
        "    return True\n",
        encoding="utf-8",
    )
    _git(w1, "add", "-A")

    (w2 / "features" / "auth.py").write_text(
        "def login():\n"
        "    # line 1\n"
        "    # line 2\n"
        "    # WORKER2_CONFLICT\n"
        "    # line 4\n"
        "    # line 5\n"
        "    return True\n",
        encoding="utf-8",
    )
    _git(w2, "add", "-A")

    agent = _make_agent(main)
    with pytest.raises(MergeConflictError):
        agent._collect_worker_changes(main, task=None)  # type: ignore[arg-type]
