"""
IntegratorAgent — lightweight integration check between Aggregator and Reviewer.

Pipeline position:
    AggregatorAgent.merge(...)
        ↓
    IntegratorAgent.integrate(...)   ← here
        ↓
    ReviewAgent.review(...)

Checks performed (all deterministic — no LLM, no AgentRegistry):
  1. Refuse FAILED MergedPatch  → ValueError
  2. Refuse empty merged_diff   → ValueError
  3. Log diff statistics
  4. Block on permission violations (core/, infra/, CI/CD paths, traversals)
  5. Optionally run INTEGRATION_TEST_COMMAND (default: empty → skip)
     → failure or timeout → ValueError → pipeline terminates early

Every call to integrate() populates self.last_result (IntegrationResult) so
callers can read structured check data without parsing logs.

Intentionally NOT doing in this phase:
  - Automatic conflict resolution
  - Complex git merge
  - LLM / Claude / Codex calls
  - AgentRegistry lookup
"""
from __future__ import annotations

import asyncio

from config.logging import get_logger
from config.settings import settings
from libs.permissions import PermissionChecker
from models.context import AgentContext
from models.patch import IntegrationResult, MergedPatch, PatchStatus
from models.task import FeatureTask
from telemetry.execution_log import ExecutionRecord, record_execution

logger = get_logger(__name__)

_OUTPUT_TRUNCATE = 1000   # max chars of stdout/stderr in error messages


class IntegratorAgent:
    """
    Deterministic integration checkpoint.

    integrate(merged, task) -> MergedPatch
      Returns the same MergedPatch object unchanged on success.
      Raises ValueError for unintegrable inputs or test failures.
      Always sets self.last_result (IntegrationResult) before returning or raising.
    """

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx
        self.last_result: IntegrationResult | None = None

    async def integrate(self, merged: MergedPatch, task: FeatureTask) -> MergedPatch:
        logger.info(f"[Integrator] checking merged patch for task {task.id}")

        # ── Check 1: aggregation failed ───────────────────────────────────────
        if merged.status == PatchStatus.FAILED:
            error_msg = (
                f"Integrator: aggregation failed for task {task.id} "
                f"(status={merged.status.value}, "
                f"error_details={merged.error_details!r}, "
                f"source_patch_ids={merged.source_patch_ids})"
            )
            self.last_result = IntegrationResult(
                success=False,
                summary="aggregation failed",
                error=error_msg,
            )
            self._record(task)
            raise ValueError(error_msg)

        # ── Check 2: empty diff ───────────────────────────────────────────────
        if not merged.merged_diff.strip():
            error_msg = (
                f"Integrator: merged diff is empty for task {task.id} "
                f"(source_patch_ids={merged.source_patch_ids}); "
                f"all worker patches may have failed"
            )
            self.last_result = IntegrationResult(
                success=False,
                summary="empty merged diff",
                source_patch_count=len(merged.source_patch_ids),
                error=error_msg,
            )
            self._record(task)
            raise ValueError(error_msg)

        # ── Check 3: diff statistics ──────────────────────────────────────────
        line_count = merged.merged_diff.count("\n")
        logger.info(
            f"[Integrator] diff stats: "
            f"lines={line_count}  "
            f"source_patches={len(merged.source_patch_ids)}  "
            f"conflicts_resolved={merged.conflicts_resolved}"
        )

        # ── Check 4: permission boundary (PR10: blocking, defense-in-depth) ──
        is_valid, violations = PermissionChecker.validate_diff(merged.merged_diff)
        if not is_valid:
            error_msg = (
                f"[Integrator] BLOCKED: merged diff violates permission boundary — "
                f"violation(s): {', '.join(violations[:5])}"
            )
            logger.warning(error_msg)
            self._record(task)
            raise ValueError(error_msg)

        # ── Check 5: optional integration test command ────────────────────────
        tests_configured = bool(settings.INTEGRATION_TEST_COMMAND)
        ok, test_summary, test_exit_code, test_output = await self._run_integration_tests()

        if not ok:
            error_msg = (
                f"Integrator: integration tests failed for task {task.id}: {test_summary}"
            )
            self.last_result = IntegrationResult(
                success=False,
                summary="integration tests failed",
                line_count=line_count,
                source_patch_count=len(merged.source_patch_ids),
                conflicts_resolved=merged.conflicts_resolved,
                protected_path_count=len(violations),
                tests_configured=tests_configured,
                tests_passed=False,
                test_command=settings.INTEGRATION_TEST_COMMAND,
                test_exit_code=test_exit_code,
                test_output_summary=test_output,
                error=error_msg,
            )
            self._record(task)
            raise ValueError(error_msg)

        # ── Success ───────────────────────────────────────────────────────────
        self.last_result = IntegrationResult(
            success=True,
            summary="integration check passed",
            line_count=line_count,
            source_patch_count=len(merged.source_patch_ids),
            conflicts_resolved=merged.conflicts_resolved,
            protected_path_count=len(violations),
            tests_configured=tests_configured,
            tests_passed=True if tests_configured else None,
            test_command=settings.INTEGRATION_TEST_COMMAND,
            test_exit_code=test_exit_code,
            test_output_summary=test_output,
        )
        self._record(task)
        logger.info(f"[Integrator] integration check passed for task {task.id}")
        return merged

    # ── telemetry helper ──────────────────────────────────────────────────────

    def _record(self, task: FeatureTask) -> None:
        """Write current last_result to execution log. Never raises."""
        if self.last_result is None:
            return
        ir = self.last_result
        record_execution(ExecutionRecord(
            run_id=self.ctx.run_id,
            task_id=task.id,
            role="integrator",
            agent="integrator",
            event="integration_result",
            success=ir.success,
            error=(ir.error or "")[:1000] or None,
            metadata={
                "line_count": ir.line_count,
                "source_patch_count": ir.source_patch_count,
                "conflicts_resolved": ir.conflicts_resolved,
                "protected_path_count": ir.protected_path_count,
                "tests_configured": ir.tests_configured,
                "tests_passed": ir.tests_passed,
                "test_exit_code": ir.test_exit_code,
            },
        ))

    # ── private ───────────────────────────────────────────────────────────────

    async def _run_integration_tests(
        self,
    ) -> tuple[bool, str, int | None, str]:
        """
        Run settings.INTEGRATION_TEST_COMMAND in ctx.workspace_path.

        Returns (ok, summary, exit_code, output_summary):
          - ok=True, exit_code=None, output="" when no command is configured.
          - ok=True, exit_code=0       when command succeeds.
          - ok=False, exit_code!=0     when command fails.
          - ok=False, exit_code=-1     on timeout (process killed).
        """
        cmd = settings.INTEGRATION_TEST_COMMAND
        if not cmd:
            return (True, "integration test command not configured", None, "")

        timeout = settings.INTEGRATION_TEST_TIMEOUT or 120
        logger.info(
            f"[Integrator] running: {cmd!r}  "
            f"cwd={self.ctx.workspace_path}  timeout={timeout}s"
        )

        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_shell(
                cmd,
                cwd=self.ctx.workspace_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
            exit_code = process.returncode or 0
            stdout_text = stdout_bytes.decode("utf-8", errors="replace")[:_OUTPUT_TRUNCATE]
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")[:_OUTPUT_TRUNCATE]

            if exit_code == 0:
                logger.info(f"[Integrator] tests passed (exit=0)")
                return (True, "tests passed (exit=0)", 0, "")

            output_hint = stderr_text or stdout_text
            summary = f"tests failed (exit={exit_code}): {output_hint}"
            logger.error(f"[Integrator] {summary[:200]}")
            return (False, summary, exit_code, output_hint)

        except asyncio.TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            summary = f"tests timed out after {timeout}s"
            logger.error(f"[Integrator] {summary}")
            return (False, summary, -1, "")

        except Exception as exc:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            summary = f"test command error: {type(exc).__name__}: {exc}"
            logger.error(f"[Integrator] {summary}")
            return (False, summary, -1, "")
