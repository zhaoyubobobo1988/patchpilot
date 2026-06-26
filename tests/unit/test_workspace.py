"""
tests/unit/test_workspace.py

Unit tests for WorkerWorkspaceManager.
No real git operations — subprocess.run is mocked throughout.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from agents.worker.workspace import WorkerWorkspaceManager


@pytest.fixture
def manager():
    return WorkerWorkspaceManager()


# ── helpers ───────────────────────────────────────────────────────────────────

def _fake_run(side_effects: dict | None = None, default_stdout: str = ""):
    """
    Returns a mock for subprocess.run that records calls and fakes outputs.
    *side_effects* maps a tuple-of-first-tokens to a stdout string.
    """
    def _run(cmd, **kwargs):
        key = tuple(cmd[:3])
        stdout = (side_effects or {}).get(key, default_stdout)
        r = MagicMock()
        r.stdout = stdout
        r.returncode = 0
        return r
    return _run


# ── prepare(): workspace path ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prepare_returns_expected_path_naming(tmp_path, manager):
    """prepare 返回路径形如 <parent>/<run_id>-<worker_id>"""
    source = str(tmp_path / "main")
    worker_ws = tmp_path / "run42-w3"
    worker_ws.mkdir()
    (worker_ws / ".git").mkdir()  # simulate existing workspace

    result = await manager.prepare("run42", "w3", source)
    assert Path(result) == worker_ws


# ── default strategy ──────────────────────────────────────────────────────────

def test_default_workspace_strategy_is_clone():
    """WORKER_WORKSPACE_STRATEGY 默认值必须是 'clone'"""
    from config.settings import Settings
    fields = getattr(Settings, "model_fields", None) or Settings.__fields__
    field = fields["WORKER_WORKSPACE_STRATEGY"]
    assert getattr(field, "default", None) == "clone"


# ── prepare(): already-exists branch ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_prepare_skips_clone_when_git_already_exists(tmp_path, manager):
    """.git 已存在时直接返回路径，不调用 subprocess.run"""
    source = str(tmp_path / "main")
    worker_ws = tmp_path / "run1-w1"
    worker_ws.mkdir()
    (worker_ws / ".git").mkdir()

    with patch("agents.worker.workspace.subprocess.run") as mock_sub:
        result = await manager.prepare("run1", "w1", source)

    mock_sub.assert_not_called()
    assert Path(result) == worker_ws


# ── prepare(): first-time clone branch ───────────────────────────────────────

@pytest.mark.asyncio
async def test_prepare_clones_with_local_flag_when_missing(tmp_path, manager):
    """workspace 不存在时，用 git clone --local 创建"""
    source = str(tmp_path / "main")
    worker_ws = tmp_path / "run2-w2"
    assert not worker_ws.exists()

    recorded = []

    def fake_run(cmd, **kwargs):
        recorded.append(list(cmd))
        # Simulate clone by creating the .git dir so re-entrant call would skip
        if cmd[1] == "clone":
            worker_ws.mkdir(parents=True, exist_ok=True)
            (worker_ws / ".git").mkdir()
        r = MagicMock()
        r.stdout = ""
        return r

    with patch("agents.worker.workspace.subprocess.run", side_effect=fake_run):
        result = await manager.prepare("run2", "w2", source)

    clone_cmds = [c for c in recorded if len(c) >= 2 and c[1] == "clone"]
    assert len(clone_cmds) == 1, "git clone must be called exactly once"
    assert "--local" in clone_cmds[0]
    assert str(worker_ws) in clone_cmds[0]
    assert Path(result) == worker_ws


@pytest.mark.asyncio
async def test_prepare_sets_git_user_config_after_clone(tmp_path, manager):
    """clone 后应配置 user.email 和 user.name"""
    source = str(tmp_path / "main")
    worker_ws = tmp_path / "run3-w3"

    recorded = []

    def fake_run(cmd, **kwargs):
        recorded.append(list(cmd))
        if cmd[1] == "clone":
            worker_ws.mkdir(parents=True, exist_ok=True)
            (worker_ws / ".git").mkdir()
        r = MagicMock()
        r.stdout = ""
        return r

    with patch("agents.worker.workspace.subprocess.run", side_effect=fake_run):
        await manager.prepare("run3", "w3", source)

    config_cmds = [c for c in recorded if len(c) >= 2 and c[1] == "config"]
    config_keys = [c[2] for c in config_cmds]
    assert "user.email" in config_keys
    assert "user.name" in config_keys


# ── git_diff() ────────────────────────────────────────────────────────────────

def test_git_diff_calls_add_then_diff_in_order(tmp_path, manager):
    """git_diff 先调用 git add -A，再调用 git diff --cached"""
    order = []

    def fake_run(cmd, **kwargs):
        order.append(cmd[1])  # "add" or "diff"
        r = MagicMock()
        r.stdout = "diff --git a/f b/f\n" if cmd[1] == "diff" else ""
        return r

    with patch("agents.worker.workspace.subprocess.run", side_effect=fake_run):
        manager.git_diff(str(tmp_path))

    assert order == ["add", "diff"]


def test_git_diff_passes_correct_flags(tmp_path, manager):
    """git add 用 -A，git diff 用 --cached"""
    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        r = MagicMock()
        r.stdout = ""
        return r

    with patch("agents.worker.workspace.subprocess.run", side_effect=fake_run):
        manager.git_diff(str(tmp_path))

    assert ["-A"] == captured[0][2:]          # git add -A
    assert ["--cached"] == captured[1][2:]     # git diff --cached


def test_git_diff_returns_stripped_output(tmp_path, manager):
    """git_diff 返回值去掉首尾空白"""
    def fake_run(cmd, **kwargs):
        r = MagicMock()
        r.stdout = "  diff --git a/x b/x  \n" if cmd[1] == "diff" else ""
        return r

    with patch("agents.worker.workspace.subprocess.run", side_effect=fake_run):
        result = manager.git_diff(str(tmp_path))

    assert result == "diff --git a/x b/x"


def test_git_diff_empty_when_no_changes(tmp_path, manager):
    """変更なし → 空文字列"""
    def fake_run(cmd, **kwargs):
        r = MagicMock()
        r.stdout = ""
        return r

    with patch("agents.worker.workspace.subprocess.run", side_effect=fake_run):
        assert manager.git_diff(str(tmp_path)) == ""


# ── ClaudeCodeWorker integration ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_worker_uses_workspace_manager_for_prepare_and_diff():
    """
    ClaudeCodeWorker._run_claude_code は workspace_manager.prepare と
    workspace_manager.git_diff を呼ぶ（Agent や git は mock）
    """
    from agents.base import AgentResult
    from agents.worker.agent import ClaudeCodeWorker
    from models.context import AgentContext
    from models.task import SubTask

    ctx = AgentContext(
        run_id="r1", feature_task_id="f1", repository="org/repo",
        base_branch="main", workspace_path="/tmp/main", model="m",
    )
    subtask = SubTask(id="s1", feature="auth", goal="add limit",
                      files=["features/auth/x.py"], constraints=[])

    DIFF = "diff --git a/features/auth/x.py b/features/auth/x.py\n"
    ok_result = AgentResult(
        success=True, output="", exit_code=0,
        metadata={"agent": "claude-code", "role": "worker",
                  "task_id": "r1-w1-s1", "elapsed_seconds": 1.0},
    )

    mock_manager = MagicMock(spec=WorkerWorkspaceManager)
    mock_manager.prepare = AsyncMock(return_value="/tmp/worker-ws")
    mock_manager.git_diff = MagicMock(return_value=DIFF)

    with patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://example.com"), \
         patch("agents.worker.agent._claude_agent.run", new=AsyncMock(return_value=ok_result)):
        worker = ClaudeCodeWorker("w1", ctx, workspace_manager=mock_manager)
        from models.patch import PatchStatus
        result = await worker.execute(subtask)

    assert result.status == PatchStatus.SUCCESS
    assert result.patch_content == DIFF
    mock_manager.prepare.assert_awaited_once_with("r1", "w1", "/tmp/main")
    mock_manager.git_diff.assert_called_once_with("/tmp/worker-ws")


# ── strategy="worktree" ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_strategy_worktree_calls_git_worktree_add(tmp_path, manager):
    """strategy='worktree' 时调用 git worktree add，不调用 git clone"""
    source = str(tmp_path / "main")
    worker_ws = tmp_path / "run10-w1"

    recorded = []

    def fake_run(cmd, **kwargs):
        recorded.append(list(cmd))
        if cmd[0] == "git" and cmd[1] == "worktree":
            worker_ws.mkdir(parents=True, exist_ok=True)
            (worker_ws / ".git").mkdir()
        r = MagicMock()
        r.stdout = ""
        return r

    with patch("config.settings.settings.WORKER_WORKSPACE_STRATEGY", "worktree"), \
         patch("agents.worker.workspace.subprocess.run", side_effect=fake_run):
        result = await manager.prepare("run10", "w1", source)

    worktree_cmds = [c for c in recorded if len(c) >= 2 and c[1] == "worktree"]
    clone_cmds = [c for c in recorded if len(c) >= 2 and c[1] == "clone"]
    assert len(worktree_cmds) == 1, "git worktree must be called exactly once"
    assert len(clone_cmds) == 0, "git clone must NOT be called in worktree mode"
    assert Path(result) == worker_ws


@pytest.mark.asyncio
async def test_strategy_worktree_uses_correct_flags(tmp_path, manager):
    """worktree 命令必须包含 add -B <branch> <path> HEAD"""
    source = str(tmp_path / "main")
    worker_ws = tmp_path / "run11-w2"

    recorded = []

    def fake_run(cmd, **kwargs):
        recorded.append(list(cmd))
        if cmd[1] == "worktree":
            worker_ws.mkdir(parents=True, exist_ok=True)
            (worker_ws / ".git").mkdir()
        r = MagicMock()
        r.stdout = ""
        return r

    with patch("config.settings.settings.WORKER_WORKSPACE_STRATEGY", "worktree"), \
         patch("agents.worker.workspace.subprocess.run", side_effect=fake_run):
        await manager.prepare("run11", "w2", source)

    wt = next(c for c in recorded if len(c) >= 2 and c[1] == "worktree")
    assert wt[2] == "add"
    assert "-B" in wt
    branch_idx = wt.index("-B") + 1
    assert wt[branch_idx] == "openclaw/run11/w2"
    assert str(worker_ws) in wt
    assert "HEAD" in wt


@pytest.mark.asyncio
async def test_strategy_worktree_sets_git_user_config(tmp_path, manager):
    """worktree 创建后同样配置 user.email 和 user.name"""
    source = str(tmp_path / "main")
    worker_ws = tmp_path / "run12-w3"

    recorded = []

    def fake_run(cmd, **kwargs):
        recorded.append(list(cmd))
        if cmd[1] == "worktree":
            worker_ws.mkdir(parents=True, exist_ok=True)
            (worker_ws / ".git").mkdir()
        r = MagicMock()
        r.stdout = ""
        return r

    with patch("config.settings.settings.WORKER_WORKSPACE_STRATEGY", "worktree"), \
         patch("agents.worker.workspace.subprocess.run", side_effect=fake_run):
        await manager.prepare("run12", "w3", source)

    config_keys = [c[2] for c in recorded if len(c) >= 3 and c[1] == "config"]
    assert "user.email" in config_keys
    assert "user.name" in config_keys


# ── invalid strategy fallback ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_invalid_strategy_falls_back_to_clone_with_warning(tmp_path, manager):
    """非法 strategy 记录 warning 并回退到 git clone --local"""
    source = str(tmp_path / "main")
    worker_ws = tmp_path / "run20-w1"

    recorded = []

    def fake_run(cmd, **kwargs):
        recorded.append(list(cmd))
        if cmd[1] == "clone":
            worker_ws.mkdir(parents=True, exist_ok=True)
            (worker_ws / ".git").mkdir()
        r = MagicMock()
        r.stdout = ""
        return r

    with patch("config.settings.settings.WORKER_WORKSPACE_STRATEGY", "totally-invalid"), \
         patch("agents.worker.workspace.subprocess.run", side_effect=fake_run), \
         patch("agents.worker.workspace.logger") as mock_log:
        result = await manager.prepare("run20", "w1", source)

    # 必须记录 warning，包含非法值
    warnings = [str(c) for c in mock_log.warning.call_args_list]
    assert any("totally-invalid" in w and "clone" in w for w in warnings)

    # 实际执行的是 clone，不是 worktree
    clone_cmds = [c for c in recorded if len(c) >= 2 and c[1] == "clone"]
    worktree_cmds = [c for c in recorded if len(c) >= 2 and c[1] == "worktree"]
    assert len(clone_cmds) == 1
    assert len(worktree_cmds) == 0
    assert Path(result) == worker_ws


# ── already-exists skips both strategies ─────────────────────────────────────

@pytest.mark.asyncio
async def test_worktree_strategy_skips_when_git_already_exists(tmp_path, manager):
    """.git 已存在时，worktree 策略也跳过，不调用任何 git 命令"""
    source = str(tmp_path / "main")
    worker_ws = tmp_path / "run30-w1"
    worker_ws.mkdir()
    (worker_ws / ".git").mkdir()

    with patch("config.settings.settings.WORKER_WORKSPACE_STRATEGY", "worktree"), \
         patch("agents.worker.workspace.subprocess.run") as mock_sub:
        result = await manager.prepare("run30", "w1", source)

    mock_sub.assert_not_called()
    assert Path(result) == worker_ws


# ── Worker is unaware of strategy ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_worker_does_not_know_about_clone_vs_worktree():
    """
    ClaudeCodeWorker 只调用 workspace_manager.prepare，
    不感知 clone/worktree 策略（通过注入 mock manager 验证）
    """
    from agents.base import AgentResult
    from agents.worker.agent import ClaudeCodeWorker
    from models.context import AgentContext
    from models.patch import PatchStatus
    from models.task import SubTask

    ctx = AgentContext(
        run_id="r99", feature_task_id="f1", repository="org/repo",
        base_branch="main", workspace_path="/tmp/main", model="m",
    )
    subtask = SubTask(id="s1", feature="auth", goal="do it",
                      files=["features/auth/x.py"], constraints=[])
    DIFF = "diff --git a/features/auth/x.py b/features/auth/x.py\n"
    ok_result = AgentResult(
        success=True, output="", exit_code=0,
        metadata={"agent": "claude-code", "role": "worker",
                  "task_id": "r99-w1-s1", "elapsed_seconds": 1.0},
    )

    mock_manager = MagicMock(spec=WorkerWorkspaceManager)
    mock_manager.prepare = AsyncMock(return_value="/tmp/ws")
    mock_manager.git_diff = MagicMock(return_value=DIFF)

    # Regardless of strategy setting, Worker just calls manager.prepare
    with patch("config.settings.settings.WORKER_WORKSPACE_STRATEGY", "worktree"), \
         patch("config.settings.settings.ANTHROPIC_BASE_URL", "https://example.com"), \
         patch("agents.worker.agent._claude_agent.run", new=AsyncMock(return_value=ok_result)):
        worker = ClaudeCodeWorker("w1", ctx, workspace_manager=mock_manager)
        result = await worker.execute(subtask)

    assert result.status == PatchStatus.SUCCESS
    # Worker called prepare — it does NOT pass strategy; that's the manager's job
    mock_manager.prepare.assert_awaited_once_with("r99", "w1", "/tmp/main")


# ═══════════════════════════════════════════════════════════════════════════════
# Phase-5C: validate_strategy + improved worktree error messages
# ═══════════════════════════════════════════════════════════════════════════════

# ── validate_strategy: clone / invalid ───────────────────────────────────────

def test_validate_strategy_clone_returns_ok(manager):
    """strategy='clone' → (True, ...) — 不执行任何 git 命令"""
    with patch("config.settings.settings.WORKER_WORKSPACE_STRATEGY", "clone"), \
         patch("agents.worker.workspace.subprocess.run") as mock_sub:
        ok, msg = manager.validate_strategy("/any/path")

    assert ok is True
    assert "clone" in msg
    mock_sub.assert_not_called()


def test_validate_strategy_invalid_returns_ok_with_fallback_note(manager):
    """非法 strategy → (True, ...) 并说明 fallback 到 clone"""
    with patch("config.settings.settings.WORKER_WORKSPACE_STRATEGY", "badvalue"), \
         patch("agents.worker.workspace.subprocess.run") as mock_sub:
        ok, msg = manager.validate_strategy("/any/path")

    assert ok is True
    assert "badvalue" in msg
    assert "clone" in msg
    mock_sub.assert_not_called()


# ── validate_strategy: worktree — success ────────────────────────────────────

def test_validate_strategy_worktree_ok_when_both_checks_pass(tmp_path, manager):
    """worktree 策略，rev-parse 和 worktree list 都成功 → (True, ...)"""
    def fake_run(cmd, **kwargs):
        r = MagicMock()
        r.returncode = 0
        r.stdout = "true"
        r.stderr = ""
        return r

    with patch("config.settings.settings.WORKER_WORKSPACE_STRATEGY", "worktree"), \
         patch("agents.worker.workspace.subprocess.run", side_effect=fake_run):
        ok, msg = manager.validate_strategy(str(tmp_path))

    assert ok is True
    assert "worktree" in msg.lower()


# ── validate_strategy: worktree — failure cases ──────────────────────────────

def test_validate_strategy_worktree_fails_if_not_git_repo(tmp_path, manager):
    """rev-parse 失败 → (False, message 说明不是 git 仓库)"""
    def fake_run(cmd, **kwargs):
        r = MagicMock()
        r.returncode = 128
        r.stdout = ""
        r.stderr = "not a git repository"
        return r

    with patch("config.settings.settings.WORKER_WORKSPACE_STRATEGY", "worktree"), \
         patch("agents.worker.workspace.subprocess.run", side_effect=fake_run):
        ok, msg = manager.validate_strategy(str(tmp_path))

    assert ok is False
    assert "git repository" in msg.lower() or "rev-parse" in msg.lower()


def test_validate_strategy_worktree_fails_if_worktree_list_fails(tmp_path, manager):
    """worktree list 失败 → (False, message 包含 stderr)"""
    call_count = [0]

    def fake_run(cmd, **kwargs):
        call_count[0] += 1
        r = MagicMock()
        if cmd[1] == "rev-parse":
            r.returncode = 0
            r.stdout = "true"
            r.stderr = ""
        else:
            r.returncode = 1
            r.stdout = ""
            r.stderr = "worktree not supported here"
        return r

    with patch("config.settings.settings.WORKER_WORKSPACE_STRATEGY", "worktree"), \
         patch("agents.worker.workspace.subprocess.run", side_effect=fake_run):
        ok, msg = manager.validate_strategy(str(tmp_path))

    assert ok is False
    assert "worktree" in msg.lower()
    assert "worktree not supported here" in msg or "worktree list" in msg


def test_validate_strategy_worktree_fails_if_git_not_on_path(tmp_path, manager):
    """git 不在 PATH → FileNotFoundError → (False, 提示安装 git)"""
    with patch("config.settings.settings.WORKER_WORKSPACE_STRATEGY", "worktree"), \
         patch("agents.worker.workspace.subprocess.run",
               side_effect=FileNotFoundError("git not found")):
        ok, msg = manager.validate_strategy(str(tmp_path))

    assert ok is False
    assert "PATH" in msg or "install" in msg.lower() or "git" in msg.lower()


# ── _prepare_worktree: clear RuntimeError on failure ─────────────────────────

@pytest.mark.asyncio
async def test_prepare_worktree_raises_runtime_error_with_readable_message(tmp_path, manager):
    """git worktree add 失败时 RuntimeError 必须包含 branch_name 和 worker_ws"""
    source = str(tmp_path / "main")

    import subprocess as _sp

    def fake_run(cmd, **kwargs):
        if cmd[1] == "worktree":
            exc = _sp.CalledProcessError(128, cmd, stderr="branch already exists")
            raise exc
        r = MagicMock()
        r.stdout = ""
        return r

    with patch("config.settings.settings.WORKER_WORKSPACE_STRATEGY", "worktree"), \
         patch("agents.worker.workspace.subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError) as exc_info:
            await manager.prepare("runX", "wY", source)

    msg = str(exc_info.value)
    assert "openclaw/runX/wY" in msg      # branch_name
    assert "branch already exists" in msg  # stderr surfaced


@pytest.mark.asyncio
async def test_prepare_worktree_failure_propagates_to_caller(tmp_path, manager):
    """prepare で worktree が失敗したとき RuntimeError が外に出る (fallback しない)"""
    source = str(tmp_path / "main")

    import subprocess as _sp

    def fake_run(cmd, **kwargs):
        if cmd[1] == "worktree":
            raise _sp.CalledProcessError(1, cmd, stderr="some error")
        r = MagicMock()
        r.stdout = ""
        return r

    with patch("config.settings.settings.WORKER_WORKSPACE_STRATEGY", "worktree"), \
         patch("agents.worker.workspace.subprocess.run", side_effect=fake_run):
        with pytest.raises(RuntimeError):
            await manager.prepare("runA", "wB", source)


# ── validate_strategy not called by prepare ───────────────────────────────────

@pytest.mark.asyncio
async def test_validate_strategy_not_called_by_prepare(tmp_path, manager):
    """prepare() 不自动调用 validate_strategy()"""
    source = str(tmp_path / "main")
    worker_ws = tmp_path / "runV-wV"

    def fake_run(cmd, **kwargs):
        if cmd[1] == "clone":
            worker_ws.mkdir(parents=True, exist_ok=True)
            (worker_ws / ".git").mkdir()
        r = MagicMock()
        r.stdout = ""
        return r

    with patch("config.settings.settings.WORKER_WORKSPACE_STRATEGY", "clone"), \
         patch("agents.worker.workspace.subprocess.run", side_effect=fake_run):
        # Spy on validate_strategy — it must NOT be called
        original = manager.validate_strategy
        called = []
        manager.validate_strategy = lambda *a, **kw: called.append(True) or original(*a, **kw)
        await manager.prepare("runV", "wV", source)

    assert called == [], "validate_strategy must not be called by prepare()"
