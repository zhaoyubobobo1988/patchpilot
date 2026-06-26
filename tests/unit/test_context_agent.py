"""ContextAgent 单元测试"""
import pytest
from pathlib import Path
from models.context import AgentContext
from models.task import FeatureTask
from agents.context_agent.agent import ContextAgent


@pytest.fixture
def ctx():
    return AgentContext(
        run_id="testrun",
        feature_task_id="task-1",
        repository="org/repo",
        base_branch="main",
        workspace_path="/tmp/test",
    )


@pytest.fixture
def task():
    return FeatureTask(
        raw_requirement="Add rate limiting to login endpoint",
        feature_name="login-ratelimit",
        repository="org/repo",
    )


@pytest.mark.asyncio
async def test_gather_empty_workspace_returns_empty_context(ctx, task, tmp_path):
    agent = ContextAgent(ctx)
    result = await agent.gather(task, str(tmp_path))
    assert result.feature_task_id == task.id
    assert result.relevant_files == []
    assert result.dependency_map == {}


@pytest.mark.asyncio
async def test_gather_finds_relevant_files(ctx, task, tmp_path):
    feat_dir = tmp_path / "features" / "auth"
    feat_dir.mkdir(parents=True)
    (feat_dir / "login.py").write_text("def login(user, password):\n    pass\n")
    (feat_dir / "unrelated.py").write_text("def foo():\n    pass\n")

    agent = ContextAgent(ctx)
    result = await agent.gather(task, str(tmp_path))

    paths = [s.path for s in result.relevant_files]
    assert any("login" in p for p in paths), f"login.py not found in {paths}"


@pytest.mark.asyncio
async def test_gather_builds_dependency_map(ctx, task, tmp_path):
    feat_dir = tmp_path / "features" / "auth"
    feat_dir.mkdir(parents=True)
    (feat_dir / "login.py").write_text(
        "import time\nfrom features.auth.service import AuthService\n\ndef login(): pass\n"
    )

    agent = ContextAgent(ctx)
    result = await agent.gather(task, str(tmp_path))

    assert len(result.dependency_map) > 0
    deps = list(result.dependency_map.values())[0]
    assert "time" in deps


@pytest.mark.asyncio
async def test_gather_detects_async_pattern(ctx, task, tmp_path):
    feat_dir = tmp_path / "features" / "auth"
    feat_dir.mkdir(parents=True)
    (feat_dir / "login.py").write_text(
        "async def login(user, password):\n    pass\n"
    )

    agent = ContextAgent(ctx)
    result = await agent.gather(task, str(tmp_path))

    assert "async/await" in result.existing_patterns


@pytest.mark.asyncio
async def test_gather_nonexistent_workspace(ctx, task):
    agent = ContextAgent(ctx)
    result = await agent.gather(task, "/nonexistent/path/xyz")
    assert result.relevant_files == []
