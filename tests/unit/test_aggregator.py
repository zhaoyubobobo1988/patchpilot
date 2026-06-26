"""AggregatorAgent 单元测试"""
import pytest
from models.context import AgentContext
from models.patch import PatchResult, PatchSet, PatchStatus
from agents.aggregator.agent import AggregatorAgent


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


PATCH_A = """\
diff --git a/features/auth/login.py b/features/auth/login.py
--- a/features/auth/login.py
+++ b/features/auth/login.py
@@ -1,3 +1,6 @@
+import time
+
 def login(user, password):
     pass
"""

PATCH_B = """\
diff --git a/features/auth/service.py b/features/auth/service.py
--- a/features/auth/service.py
+++ b/features/auth/service.py
@@ -1,2 +1,4 @@
+MAX_ATTEMPTS = 5
+
 class AuthService:
     pass
"""


@pytest.mark.asyncio
async def test_merge_non_overlapping_patches(ctx):
    agg = AggregatorAgent(ctx)
    patch_set = PatchSet(
        feature_task_id="task-1",
        patches=[
            PatchResult(subtask_id="st1", worker_id="w1", patch_content=PATCH_A, affected_files=["features/auth/login.py"]),
            PatchResult(subtask_id="st2", worker_id="w2", patch_content=PATCH_B, affected_files=["features/auth/service.py"]),
        ],
    )
    merged = await agg.merge(patch_set)
    assert merged.status == PatchStatus.SUCCESS
    assert "features/auth/login.py" in merged.merged_diff
    assert "features/auth/service.py" in merged.merged_diff
    assert merged.conflicts_resolved == 0


@pytest.mark.asyncio
async def test_merge_empty_patches_returns_failed(ctx):
    agg = AggregatorAgent(ctx)
    patch_set = PatchSet(feature_task_id="task-1", patches=[])
    merged = await agg.merge(patch_set)
    assert merged.status == PatchStatus.FAILED


@pytest.mark.asyncio
async def test_group_by_file(ctx):
    agg = AggregatorAgent(ctx)
    patches = [
        PatchResult(subtask_id="st1", worker_id="w1", patch_content=PATCH_A, affected_files=[]),
        PatchResult(subtask_id="st2", worker_id="w2", patch_content=PATCH_B, affected_files=[]),
    ]
    groups = agg._group_by_file(patches)
    assert "features/auth/login.py" in groups
    assert "features/auth/service.py" in groups
