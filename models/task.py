from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class SubTask(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    feature: str
    goal: str
    files: list[str]
    constraints: list[str] = []
    status: TaskStatus = TaskStatus.PENDING
    assigned_worker_id: str | None = None


class FeatureTask(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    raw_requirement: str
    feature_name: str
    repository: str                  # "org/repo"
    base_branch: str = "main"
    subtasks: list[SubTask] = []
    status: TaskStatus = TaskStatus.PENDING
    created_at: str = ""


class GeneratedTestCase(BaseModel):
    description: str
    input_spec: str
    expected_output: str


class GeneratedTestSpec(BaseModel):
    subtask_id: str
    test_file_path: str              # 建议写入的测试文件路径（features/ 下）
    test_code: str                   # 生成的 pytest 代码
    test_cases: list[GeneratedTestCase] = []


class TaskGraph(BaseModel):
    feature_task: FeatureTask
    parallel_groups: list[list[str]]         # SubTask id 分组，同组可并行
    dependencies: dict[str, list[str]] = {}  # subtask_id -> [依赖的 subtask_id]

    def get_subtask(self, subtask_id: str) -> SubTask:
        for st in self.feature_task.subtasks:
            if st.id == subtask_id:
                return st
        raise KeyError(f"SubTask '{subtask_id}' not found in TaskGraph")
