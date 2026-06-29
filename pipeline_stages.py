"""
pipeline_stages.py — StageExecutor protocol + concrete stage implementations.

Each stage is a discrete, testable unit that reads from and writes to
PipelineState.  The pipeline orchestrates them in sequence; the Supervisor
Loop (PR7) will wrap them to add retry/decision logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, TYPE_CHECKING, runtime_checkable

from pydantic import BaseModel

from agents.context_agent.agent import ContextAgent
from agents.orchestrator.agent import OrchestratorAgent
from config.logging import get_logger
from models.context import AgentContext, PipelineRun
from models.errors import ClassifiedError, classify_failure

if TYPE_CHECKING:
    from models.task import FeatureTask, TaskGraph
    from models.patch import MergedPatch
    from models.context import CodeContext

logger = get_logger(__name__)


# ── shared helpers (imported from pipeline to avoid duplication) ──────────────

def _clone_repo(workspace_path: str, repository: str) -> None:
    from pipeline import _clone_repo as _impl
    _impl(workspace_path, repository)


# ── StageResult ───────────────────────────────────────────────────────────────

class StageResult(BaseModel):
    done: bool = False          # True → pipeline should stop (early exit)
    error: str | None = None    # set when done=True due to an error
    classified_error: ClassifiedError | None = None  # PR8: structured failure info


# ── PipelineState ─────────────────────────────────────────────────────────────

@dataclass
class PipelineState:
    """Mutable shared state bag threaded through every StageExecutor."""
    run: PipelineRun
    ctx: AgentContext
    task: FeatureTask
    code_context: CodeContext | None = None
    task_graph: TaskGraph | None = None
    merged: MergedPatch | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ── StageExecutor Protocol ────────────────────────────────────────────────────

@runtime_checkable
class StageExecutor(Protocol):
    name: str

    async def execute(self, state: PipelineState) -> StageResult:
        ...


# ── Concrete stages ───────────────────────────────────────────────────────────

class CloneStage:
    name = "clone"

    async def execute(self, state: PipelineState) -> StageResult:
        try:
            _clone_repo(state.ctx.workspace_path, state.task.repository)
            return StageResult()
        except Exception as exc:
            logger.error(f"[{state.run.run_id}] CloneStage failed: {exc}")
            classified = classify_failure(
                exc, message=str(exc), stage=self.name,
                context={"repository": state.task.repository},
            )
            state.run.classified_errors.append(classified)
            return StageResult(done=True, error=str(exc), classified_error=classified)


class ContextStage:
    name = "context"

    async def execute(self, state: PipelineState) -> StageResult:
        try:
            agent = ContextAgent(state.ctx)
            state.code_context = await agent.gather(state.task, state.ctx.workspace_path)
            logger.info(
                f"[{state.run.run_id}] Context: "
                f"{len(state.code_context.relevant_files)} relevant files"
            )
            return StageResult()
        except Exception as exc:
            logger.error(f"[{state.run.run_id}] ContextStage failed: {exc}")
            classified = classify_failure(
                exc, message=str(exc), stage=self.name,
                context={"model": state.ctx.model},
            )
            state.run.classified_errors.append(classified)
            return StageResult(done=True, error=str(exc), classified_error=classified)


class OrchestrateStage:
    name = "orchestrate"

    async def execute(self, state: PipelineState) -> StageResult:
        try:
            orchestrator = OrchestratorAgent(state.ctx)
            state.task_graph = await orchestrator.decompose(state.task)
            all_subtasks = [
                state.task_graph.get_subtask(sid)
                for group in state.task_graph.parallel_groups
                for sid in group
            ]
            logger.info(
                f"[{state.run.run_id}] Decomposed into {len(all_subtasks)} subtasks, "
                f"{len(state.task_graph.parallel_groups)} parallel groups"
            )
            return StageResult()
        except Exception as exc:
            logger.error(f"[{state.run.run_id}] OrchestrateStage failed: {exc}")
            classified = classify_failure(
                exc, message=str(exc), stage=self.name,
                context={"model": state.ctx.model},
            )
            state.run.classified_errors.append(classified)
            return StageResult(done=True, error=str(exc), classified_error=classified)
