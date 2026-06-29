"""
telemetry/metrics.py — Pipeline-level metrics collection.

PipelineMetrics is instantiated per pipeline run and accumulates counters
for each stage. It is a pure pydantic model — no I/O, no global state.
"""
from __future__ import annotations

import datetime

from pydantic import BaseModel


class StageMetric(BaseModel):
    """Accumulated metrics for one pipeline stage."""
    stage_name: str
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    total_elapsed_seconds: float = 0.0
    last_error: str | None = None
    last_error_category: str | None = None  # FailureCategory value


class PipelineMetrics(BaseModel):
    """Per-run pipeline metrics accumulator."""
    run_id: str
    start_time: str = ""
    end_time: str = ""
    stage_metrics: dict[str, StageMetric] = {}
    total_stages: int = 0
    successful_stages: int = 0
    failed_stages: int = 0
    aborted_stages: int = 0
    total_retries: int = 0
    pipeline_status: str = "running"  # "running" | "success" | "failed" | "aborted"

    def record_stage_attempt(
        self,
        stage_name: str,
        elapsed_seconds: float,
        success: bool,
        error: str | None = None,
        error_category: str | None = None,
    ) -> None:
        """Record one stage execution attempt. Creates StageMetric on first call."""
        if not self.start_time:
            self.start_time = datetime.datetime.now(datetime.timezone.utc).isoformat()

        if stage_name not in self.stage_metrics:
            self.stage_metrics[stage_name] = StageMetric(stage_name=stage_name)
            self.total_stages += 1

        sm = self.stage_metrics[stage_name]
        sm.attempts += 1
        sm.total_elapsed_seconds += elapsed_seconds

        if success:
            sm.successes += 1
            self.successful_stages += 1
        else:
            sm.failures += 1
            self.failed_stages += 1
            if error:
                sm.last_error = error
            if error_category:
                sm.last_error_category = error_category

        # Count retries: attempts beyond the first per stage
        if sm.attempts > 1:
            self.total_retries += 1

    def mark_pipeline_complete(self, status: str) -> None:
        """Set the final pipeline status and end time."""
        self.pipeline_status = status
        self.end_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
