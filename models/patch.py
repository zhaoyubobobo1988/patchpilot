from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class ReviewSeverity(str, Enum):
    OK = "ok"
    WARN = "warn"
    BLOCK = "block"


class ReviewComment(BaseModel):
    file: str
    line_hint: str
    message: str
    severity: ReviewSeverity = ReviewSeverity.OK


class ReviewResult(BaseModel):
    patch_id: str
    approved: bool
    comments: list[ReviewComment] = []
    summary: str = ""


class PatchStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    CONFLICT = "conflict"


class PatchResult(BaseModel):
    subtask_id: str
    worker_id: str
    patch_content: str               # unified diff 文本
    affected_files: list[str]
    status: PatchStatus = PatchStatus.SUCCESS
    error_message: str | None = None
    retry_count: int = 0


class PatchSet(BaseModel):
    feature_task_id: str
    patches: list[PatchResult]


class MergedPatch(BaseModel):
    feature_task_id: str
    merged_diff: str                 # 最终可直接 apply 的 diff
    source_patch_ids: list[str]      # 来源 subtask_id 列表
    conflicts_resolved: int = 0
    status: PatchStatus = PatchStatus.SUCCESS
    error_details: str | None = None


class QualityGateResult(BaseModel):
    level: str                        # "task" | "integration" | "pre_publish"
    passed: bool
    command: str
    exit_code: int | None = None
    output_summary: str = ""
    warn_only: bool = False


class IntegrationResult(BaseModel):
    """Structured summary produced by IntegratorAgent after each integrate() call."""

    success: bool
    summary: str

    # diff statistics
    line_count: int = 0
    source_patch_count: int = 0
    conflicts_resolved: int = 0
    protected_path_count: int = 0   # files touching core/ or infra/

    # integration test fields (populated only when INTEGRATION_TEST_COMMAND is set)
    tests_configured: bool = False
    tests_passed: bool | None = None    # None = not run
    test_command: str = ""
    test_exit_code: int | None = None
    test_output_summary: str = ""

    # set on failure (FAILED patch / empty diff / test failure)
    error: str | None = None
