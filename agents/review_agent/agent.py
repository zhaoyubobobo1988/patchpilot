from __future__ import annotations

import json
import re
from pathlib import Path

from agents.base import AgentAdapter, AgentTask
from agents.registry import default_registry
from agents.result_utils import extract_agent_output
from config.llm import llm_complete
from config.logging import get_logger
from config.settings import settings
from models.context import AgentContext
from models.patch import MergedPatch, ReviewResult, ReviewComment, ReviewSeverity
from models.task import FeatureTask

logger = get_logger(__name__)

_VALID_BACKENDS = frozenset({"claude-code", "codex"})

_SYSTEM_PROMPT = """You are the Reviewer Agent in the OpenClaw multi-agent system.
Your role: review a unified diff patch and output a structured JSON verdict.

Rules (apply in order, first match wins):
1. If the diff modifies any file under core/ or infra/ → approved=false, severity=block
2. If the diff has no test file changes (no test_*.py or *_test.py) → severity=warn
3. If the total diff is over 500 lines → severity=warn
4. Otherwise → approved=true, severity=ok

Output ONLY valid JSON — no prose, no markdown fences.
Format:
{"approved": true, "summary": "...", "comments": [{"file": "...", "line_hint": "...", "message": "...", "severity": "ok|warn|block"}]}"""


class ReviewAgent:
    """
    Reviewer agent — inspects the merged patch and approves or blocks it.

    Backend resolution (8C):
      Step 1 — _resolve_backend_name(task) determines the effective backend:
        • ACTIVE=False, DRY_RUN=False → no Router; returns REVIEW_AGENT_BACKEND
        • ACTIVE=False, DRY_RUN=True  → Router called, saves last_route_decision,
                                         but still returns REVIEW_AGENT_BACKEND
        • ACTIVE=True                 → Router called, returns decision.selected_backend
                                         (with validation + fallback on error)
      Step 2 — _select_backend(backend_name) looks up the agent in the registry.
      Step 3 — _call_reviewer(prompt, backend_name) executes the backend.

    If the selected agent backend is unavailable or returns failure, the call falls
    through to litellm, then to a safe approved=True default.
    """

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx
        # Stores the last AgentRouter decision (Phase 8B/8C).
        # None when Router is not called or after a Router exception.
        self.last_route_decision = None

    async def review(self, merged: MergedPatch, task: FeatureTask) -> ReviewResult:
        logger.info(f"[Reviewer] reviewing patch for task {task.id}")

        # Reset at the top of every call so last_route_decision is never stale
        # regardless of which code path terminates the review (protected-dir early
        # return, Router exception, litellm fallback, etc.).
        self.last_route_decision = None

        # Rule-based pre-check first — fast, deterministic, no Router or LLM needed.
        if self._touches_protected_dirs(merged.merged_diff):
            return ReviewResult(
                patch_id=merged.feature_task_id,
                approved=False,
                summary="Patch modifies protected directories (core/ or infra/)",
                comments=[ReviewComment(
                    file=self._find_protected_file(merged.merged_diff),
                    line_hint="diff --git",
                    message="Modification of core/ or infra/ is forbidden",
                    severity=ReviewSeverity.BLOCK,
                )],
            )

        # Determine effective backend (Router may be consulted here).
        backend_name = self._resolve_backend_name(task)

        prompt = self._build_reviewer_prompt(merged, task)
        raw = await self._call_reviewer(prompt, backend_name)
        return self._parse_response(raw, merged.feature_task_id)

    # ── Stats loader (Phase 8D) ───────────────────────────────────────────────

    def _load_router_failure_categories(self) -> list[str]:
        """
        Load recent error categories from the telemetry JSONL log.
        These are injected into AgentRouteRequest.failure_categories so the
        Router can make data-informed recommendations.

        Returns [] when:
          - ENABLE_REVIEW_ROUTER_STATS=False (default)
          - EXECUTION_LOG_PATH is not configured
          - The log file is missing or unreadable
          - Any exception occurs

        Never raises. Does not output prompt / diff / API keys / tokens.
        """
        if not settings.ENABLE_REVIEW_ROUTER_STATS:
            return []
        if not settings.EXECUTION_LOG_PATH:
            return []
        try:
            from telemetry.log_viewer import load_execution_records
            from telemetry.log_stats import summarize_all_runs
            records = load_execution_records(settings.EXECUTION_LOG_PATH)
            stats = summarize_all_runs(
                records,
                recent_limit=settings.REVIEW_ROUTER_STATS_RECENT_LIMIT,
            )
            # Collect categories from recent_runs, deduplicated, first-seen order
            seen: set[str] = set()
            categories: list[str] = []
            for run in stats.get("recent_runs", []):
                for cat in run.get("error_categories", []):
                    if cat and cat not in seen:
                        seen.add(cat)
                        categories.append(cat)
            return categories
        except Exception as exc:
            logger.warning(f"[ReviewAgent] Stats loading failed (non-fatal): {exc}")
            return []

    # ── Backend resolution (Phase 8B/8C/8D/8E) ───────────────────────────────

    def _resolve_backend_name(self, task: FeatureTask) -> str:
        """
        Return the effective reviewer backend name for this review call.

        Modes (evaluated in order):
          ACTIVE=False, DRY_RUN=False → no Router; return preferred
          ACTIVE=False, DRY_RUN=True  → call Router; save decision; return preferred
          ACTIVE=True                 → call Router; save decision; return decision.selected_backend
                                        (invalid backend → preferred; exception → preferred)

        In both call paths a router_decision event is written to the execution log (8E).
        Never raises — all Router failures are caught and logged as warnings.
        """
        active = settings.ENABLE_REVIEW_ROUTER_ACTIVE
        dry_run = settings.ENABLE_REVIEW_ROUTER_DRY_RUN
        preferred = settings.REVIEW_AGENT_BACKEND
        stats_enabled = settings.ENABLE_REVIEW_ROUTER_STATS

        # Neither mode enabled → no Router involvement; skip stats + log too.
        if not active and not dry_run:
            return preferred

        # Load stats BEFORE the try block so they're available in exception path.
        failure_categories = self._load_router_failure_categories()

        try:
            from agents.router import AgentRouteRequest, AgentRouter
            request = AgentRouteRequest(
                role="reviewer",
                task_id=task.id,
                run_id=self.ctx.run_id,
                preferred_backend=preferred,
                failure_categories=failure_categories,
            )
            decision = AgentRouter().route(request)
            self.last_route_decision = decision

            mode = "active" if active else "dry-run"
            logger.info(
                f"[ReviewAgent] Router mode={mode} "
                f"preferred={preferred!r} recommended={decision.selected_backend!r} "
                f"fallback={decision.fallback_used} reason={decision.reason!r} "
                f"task_id={task.id} run_id={self.ctx.run_id}"
            )

            if active:
                selected = decision.selected_backend
                if selected not in _VALID_BACKENDS:
                    logger.warning(
                        f"[ReviewAgent] Router returned invalid backend {selected!r}; "
                        f"falling back to preferred={preferred!r}"
                    )
                    applied_backend = preferred
                else:
                    applied_backend = selected
            else:
                applied_backend = preferred

            self._record_router_decision(
                task,
                preferred_backend=preferred,
                decision=decision,
                active=active,
                dry_run=dry_run,
                stats_enabled=stats_enabled,
                failure_categories=failure_categories,
                applied_backend=applied_backend,
            )
            return applied_backend

        except Exception as exc:
            logger.warning(f"[ReviewAgent] Router call failed (non-fatal): {exc}")
            self.last_route_decision = None
            self._record_router_decision(
                task,
                preferred_backend=preferred,
                decision=None,
                active=active,
                dry_run=dry_run,
                stats_enabled=stats_enabled,
                # Use [] so exception events don't carry the (possibly non-empty)
                # loaded categories — the Router never processed them successfully.
                failure_categories=[],
                applied_backend=preferred,
                error=str(exc),
            )
            return preferred

    def _record_router_decision(
        self,
        task: FeatureTask,
        *,
        preferred_backend: str,
        decision: "AgentRouteDecision | None",
        active: bool,
        dry_run: bool,
        stats_enabled: bool,
        failure_categories: list[str],
        applied_backend: str,
        error: str | None = None,
    ) -> None:
        """
        Write a router_decision event to the execution JSONL log.

        Never raises. EXECUTION_LOG_PATH="" → record_execution is a no-op.
        All free-text fields (error, reason) are redacted and truncated before
        writing so that credentials accidentally included in exceptions are not
        persisted to disk.
        """
        try:
            from telemetry.execution_log import ExecutionRecord, record_execution
            from telemetry.log_viewer import redact_sensitive_text

            def _safe(text: str | None, max_len: int = 300) -> str | None:
                if not text:
                    return None
                return redact_sensitive_text(text)[:max_len]

            raw_reason = decision.reason if decision and decision.reason else None

            metadata = {
                "preferred_backend": preferred_backend,
                "selected_backend": decision.selected_backend if decision else None,
                "active": active,
                "dry_run": dry_run,
                "stats_enabled": stats_enabled,
                "fallback_used": decision.fallback_used if decision else None,
                "reason": _safe(raw_reason),
                "failure_categories": [c for c in failure_categories
                                       if isinstance(c, str)],
                "applied_backend": applied_backend,
            }
            record_execution(ExecutionRecord(
                run_id=self.ctx.run_id,
                task_id=task.id,
                role="reviewer",
                agent="router",
                event="router_decision",
                success=error is None,
                error=_safe(error),
                metadata=metadata,
            ))
        except Exception as exc:
            logger.warning(f"[ReviewAgent] Failed to record router decision: {exc}")

    # ── Reviewer subprocess ───────────────────────────────────────────────────

    def _build_reviewer_prompt(self, merged: MergedPatch, task: FeatureTask) -> str:
        lines = merged.merged_diff.count("\n")
        return (
            f"{_SYSTEM_PROMPT}\n\n"
            "---\n"
            f"## Feature task: {task.raw_requirement}\n"
            f"## Diff ({lines} lines):\n\n"
            f"{merged.merged_diff[:4000]}\n\n"
            "Output ONLY the JSON verdict — first char must be `{`, last must be `}`."
        )

    def _select_backend(self, backend_name: str) -> tuple[AgentAdapter | None, int]:
        """
        Return (backend_instance, timeout_seconds) for *backend_name* from the registry.

        Unknown / invalid names warn and fall back to "claude-code".
        Returns (None, 0) when the backend is not configured or not in registry
        → caller falls through to litellm.
        """
        if backend_name not in _VALID_BACKENDS:
            logger.warning(
                f"[Reviewer] Unknown backend={backend_name!r}, "
                f"falling back to claude-code"
            )
            backend_name = "claude-code"

        logger.info(f"[Reviewer] backend={backend_name}")

        if backend_name == "codex":
            agent = default_registry.get("codex")
            if agent is None:
                logger.warning("[Reviewer] 'codex' not found in registry, falling back to litellm")
                return None, 0
            return agent, settings.CODEX_TIMEOUT or 180

        # "claude-code" — only usable when ANTHROPIC_BASE_URL is configured
        if not settings.ANTHROPIC_BASE_URL:
            return None, 0
        agent = default_registry.get("claude-code")
        if agent is None:
            logger.warning("[Reviewer] 'claude-code' not found in registry, falling back to litellm")
            return None, 0
        return agent, settings.CLAUDE_CODE_TIMEOUT or 300

    async def _call_reviewer(self, prompt: str, backend_name: str) -> str:
        """Run the resolved backend as Reviewer. Falls back to litellm if unavailable."""
        backend, timeout = self._select_backend(backend_name)
        if backend is not None:
            task = AgentTask(
                task_id=self.ctx.run_id,
                role="reviewer",
                prompt=prompt,
                workspace=Path(self.ctx.workspace_path),
                timeout_seconds=timeout,
                output_format="json",
            )
            result = await backend.run(task)
            output = extract_agent_output(result, "Reviewer")
            if output:
                return output
            # agent failed → fall through to litellm

        # litellm fallback
        try:
            return (await llm_complete(
                model=self.ctx.model,
                max_tokens=1024,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )).strip()
        except Exception as exc:
            logger.error(f"[Reviewer] litellm fallback also failed: {exc}")
            return '{"approved": true, "summary": "Review skipped due to error", "comments": []}'

    # ── helpers ───────────────────────────────────────────────────────────────

    def _touches_protected_dirs(self, diff: str) -> bool:
        for line in diff.splitlines():
            if line.startswith("diff --git"):
                if "/core/" in line or " core/" in line or "/infra/" in line or " infra/" in line:
                    return True
        return False

    def _find_protected_file(self, diff: str) -> str:
        for line in diff.splitlines():
            if line.startswith("diff --git") and ("/core/" in line or "/infra/" in line):
                parts = line.split()
                return parts[-1].lstrip("b/") if parts else "unknown"
        return "unknown"

    def _parse_response(self, raw: str, patch_id: str) -> ReviewResult:
        try:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(match.group() if match else raw)
            comments = [
                ReviewComment(
                    file=c.get("file", ""),
                    line_hint=c.get("line_hint", ""),
                    message=c.get("message", ""),
                    severity=ReviewSeverity(c.get("severity", "ok")),
                )
                for c in data.get("comments", [])
            ]
            return ReviewResult(
                patch_id=patch_id,
                approved=bool(data.get("approved", True)),
                summary=data.get("summary", ""),
                comments=comments,
            )
        except Exception:
            return ReviewResult(
                patch_id=patch_id,
                approved=True,
                summary="Parse error, defaulting approved",
            )
