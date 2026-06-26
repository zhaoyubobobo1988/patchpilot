"""数据模型基础验证测试"""
import pytest
from models.task import FeatureTask, SubTask, TaskGraph, TaskStatus, GeneratedTestSpec, GeneratedTestCase
from models.patch import PatchResult, PatchSet, MergedPatch, PatchStatus, ReviewResult, ReviewSeverity, ReviewComment
from models.context import AgentContext, PipelineRun, CodeContext, FileSnippet
from models.github import CIStatus, PRRequest, PRResult, CICheckResult


def test_subtask_default_id():
    st = SubTask(feature="auth", goal="add ratelimit", files=["features/auth/login.py"])
    assert len(st.id) == 8


def test_feature_task_default_status():
    task = FeatureTask(
        raw_requirement="add login ratelimit",
        feature_name="login-ratelimit",
        repository="org/repo",
    )
    assert task.status == TaskStatus.PENDING
    assert task.base_branch == "main"


def test_task_graph_get_subtask():
    st = SubTask(id="abc12345", feature="auth", goal="test", files=[])
    task = FeatureTask(
        raw_requirement="req",
        feature_name="feat",
        repository="org/repo",
        subtasks=[st],
    )
    graph = TaskGraph(feature_task=task, parallel_groups=[["abc12345"]])
    assert graph.get_subtask("abc12345") == st
    with pytest.raises(KeyError):
        graph.get_subtask("nonexistent")


def test_patch_result_defaults():
    pr = PatchResult(
        subtask_id="st1",
        worker_id="w1",
        patch_content="diff --git a/features/x.py b/features/x.py",
        affected_files=["features/x.py"],
    )
    assert pr.status == PatchStatus.SUCCESS
    assert pr.retry_count == 0


def test_merged_patch():
    mp = MergedPatch(
        feature_task_id="task-1",
        merged_diff="diff --git ...",
        source_patch_ids=["st1", "st2"],
    )
    assert mp.conflicts_resolved == 0
    assert mp.status == PatchStatus.SUCCESS


def test_agent_context_defaults():
    ctx = AgentContext(
        run_id="abc",
        feature_task_id="task-1",
        repository="org/repo",
        base_branch="main",
        workspace_path="/tmp/test",
    )
    assert ctx.model == "deepseek/deepseek-chat"
    assert ctx.max_tokens == 8192


def test_pr_request_draft_default():
    req = PRRequest(
        repository="org/repo",
        title="test PR",
        body="body",
        head_branch="feature/test",
    )
    assert req.draft is True
    assert req.base_branch == "main"


def test_ci_check_result():
    result = CICheckResult(pr_number=42, status=CIStatus.SUCCESS)
    assert result.failed_checks == []


# ── 新模型测试 ────────────────────────────────────────────────────────────────

def test_code_context_defaults():
    ctx = CodeContext(feature_task_id="task-1")
    assert ctx.relevant_files == []
    assert ctx.dependency_map == {}
    assert ctx.existing_patterns == []


def test_file_snippet():
    snippet = FileSnippet(path="features/auth/login.py", content="def login(): pass")
    assert snippet.relevance_score == 0.0


def test_generated_test_spec_defaults():
    spec = GeneratedTestSpec(
        subtask_id="st1",
        test_file_path="features/auth/test_login.py",
        test_code="import pytest\ndef test_login(): pass",
    )
    assert spec.test_cases == []


def test_generated_test_case():
    tc = GeneratedTestCase(
        description="login with valid creds",
        input_spec="user='admin', password='secret'",
        expected_output="returns True",
    )
    assert tc.description == "login with valid creds"


def test_review_result_defaults():
    r = ReviewResult(patch_id="st1", approved=True)
    assert r.comments == []
    assert r.summary == ""


def test_review_result_blocked():
    comment = ReviewComment(
        file="core/auth.py",
        line_hint="diff --git a/core/auth.py",
        message="修改了核心模块，不允许",
        severity=ReviewSeverity.BLOCK,
    )
    r = ReviewResult(patch_id="st1", approved=False, comments=[comment])
    assert not r.approved
    assert r.comments[0].severity == ReviewSeverity.BLOCK
