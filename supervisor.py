"""
supervisor.py — SupervisorLoop: runs StageExecutors with retry/abort decisions.

The loop calls each stage's execute(), passes the StageResult to a decide()
callback, and acts on the returned OrchestratorDecision:
  - CONTINUE → move to the next stage
  - RETRY    → re-run the same stage (up to decision.max_retries times)
  - ABORT    → return the current PipelineState immediately
"""
from __future__ import annotations

from typing import Callable

from config.logging import get_logger
from models.decision import DecisionKind, OrchestratorDecision
from models.errors import FailureCategory, classify_failure
from pipeline_stages import PipelineState, StageExecutor, StageResult

logger = get_logger(__name__)


def _default_decide(result: StageResult, stage_name: str, attempt: int) -> OrchestratorDecision:
    # ── success path ───────────────────────────────────────────────────────
    if not result.done:
        return OrchestratorDecision(kind=DecisionKind.CONTINUE)

    # ── failure path: use classified_error when available ──────────────────
    classified = result.classified_error
    if classified is None:
        # Backward compat: classify from raw error string
        classified = classify_failure(
            message=result.error or "",
            stage=stage_name,
        )

    # ── category-driven decision ──────────────────────────────────────────
    if classified.category == FailureCategory.TRANSIENT:
        return OrchestratorDecision(
            kind=DecisionKind.RETRY,
            reason=f"{stage_name!r}: transient failure — {classified.message}",
            max_retries=3,
            classified_error=classified,
        )

    # PERMANENT, CONFIG, EXTERNAL, RESOURCE, UNKNOWN → ABORT
    return OrchestratorDecision(
        kind=DecisionKind.ABORT,
        reason=(
            f"{stage_name!r}: {classified.category.value} failure — "
            f"{classified.message or classified.recovery_hint}"
        ),
        classified_error=classified,
    )


class SupervisorLoop:
    def __init__(
        self,
        stages: list[StageExecutor],
        decide: Callable[[StageResult, str, int], OrchestratorDecision] | None = None,
    ) -> None:
        self._stages = stages
        self._decide = decide or _default_decide

    async def run(self, state: PipelineState) -> PipelineState:
        for stage in self._stages:
            attempt = 0
            while True:
                result = await stage.execute(state)
                decision = self._decide(result, stage.name, attempt)

                if decision.kind == DecisionKind.CONTINUE:
                    logger.debug(f"[supervisor] stage={stage.name!r} attempt={attempt} → CONTINUE")
                    break

                if decision.kind == DecisionKind.ABORT:
                    logger.warning(
                        f"[supervisor] stage={stage.name!r} attempt={attempt} → ABORT: {decision.reason}"
                    )
                    return state

                # RETRY
                if attempt >= decision.max_retries:
                    logger.warning(
                        f"[supervisor] stage={stage.name!r} retries exhausted "
                        f"(max={decision.max_retries}) → ABORT"
                    )
                    return state

                attempt += 1
                logger.info(
                    f"[supervisor] stage={stage.name!r} retry attempt {attempt}/{decision.max_retries}"
                )

        return state
