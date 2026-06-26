"""TestAgent 单元测试"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from models.context import AgentContext, CodeContext
from models.task import SubTask
from agents.test_agent.agent import TestAgent


@pytest.fixture
def ctx():
    return AgentContext(
        run_id="testrun",
        feature_task_id="task-1",
        repository="org/repo",
        base_branch="main",
        workspace_path="/tmp/test",
        model="deepseek/deepseek-chat",
    )


@pytest.fixture
def subtask():
    return SubTask(
        id="st001",
        feature="auth",
        goal="Add rate limiting to login endpoint",
        files=["features/auth/login.py"],
        constraints=["must be thread-safe"],
    )


@pytest.fixture
def code_context():
    return CodeContext(feature_task_id="task-1")


@pytest.mark.asyncio
async def test_generate_returns_pytest_code(ctx, subtask, code_context):
    agent = TestAgent(ctx)
    mock_response = MagicMock()
    mock_response.choices[0].message.content = (
        "import pytest\n\ndef test_login_rate_limit():\n    assert True\n"
    )
    with patch("litellm.acompletion", new=AsyncMock(return_value=mock_response)):
        spec = await agent.generate(subtask, code_context)
    assert "import pytest" in spec.test_code
    assert spec.subtask_id == "st001"


@pytest.mark.asyncio
async def test_generate_test_file_in_features_dir(ctx, subtask, code_context):
    agent = TestAgent(ctx)
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "import pytest\n"
    with patch("litellm.acompletion", new=AsyncMock(return_value=mock_response)):
        spec = await agent.generate(subtask, code_context)
    assert spec.test_file_path.startswith("features/")


@pytest.mark.asyncio
async def test_generate_fails_gracefully(ctx, subtask, code_context):
    agent = TestAgent(ctx)
    with patch("litellm.acompletion", new=AsyncMock(side_effect=Exception("LLM error"))):
        spec = await agent.generate(subtask, code_context)
    assert spec.subtask_id == "st001"
    assert spec.test_code == ""
    assert spec.test_file_path.startswith("features/")
