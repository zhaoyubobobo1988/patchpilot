from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from models.errors import ClassifiedError


class FileSnippet(BaseModel):
    path: str
    content: str
    relevance_score: float = 0.0


class CodeContext(BaseModel):
    feature_task_id: str
    relevant_files: list[FileSnippet] = []
    dependency_map: dict[str, list[str]] = {}   # file → 它依赖的文件列表
    existing_patterns: list[str] = []            # 检测到的代码模式描述


class AgentContext(BaseModel):
    run_id: str
    feature_task_id: str
    repository: str
    base_branch: str
    workspace_path: str
    model: str = "deepseek/deepseek-chat"   # litellm 格式
    max_tokens: int = 8192
    extra: dict[str, Any] = {}


class PipelineRun(BaseModel):
    run_id: str
    feature_task_id: str
    stage: str = "init"
    debug_retry_count: int = 0
    max_debug_retries: int = 5
    pr_number: int | None = None
    pr_url: str | None = None
    ci_passed: bool | None = None
    error_log: list[str] = []
    classified_errors: list[ClassifiedError] = []
