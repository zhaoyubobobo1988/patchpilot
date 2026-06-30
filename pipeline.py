"""
OpenClaw MVP Pipeline 入口

用法：
    uv run python pipeline.py --requirement "添加用户登录限流功能" --repo "org/repo"

可选参数：
    --base-branch   目标分支，默认 main
    --model         模型 ID，默认读取 LLM_MODEL 环境变量
"""
from __future__ import annotations

import argparse
import asyncio
import re
import subprocess as _sp
import time
import uuid
from pathlib import Path

from config.logging import configure_logging, get_logger
from config.settings import settings
from models.board import TaskBoard
from models.context import AgentContext, PipelineRun
from models.github import CIStatus, DebugContext
from models.patch import PatchResult, PatchSet, PatchStatus, QualityGateResult
from models.task import FeatureTask, SubTask, TaskStatus

from persistence.run_state import save_run_state
from telemetry.execution_log import ExecutionRecord, record_execution
from agents.context_agent.agent import ContextAgent
from agents.orchestrator.agent import OrchestratorAgent
from agents.test_agent.agent import TestAgent
from agents.worker.agent import ClaudeCodeWorker
from agents.worker.workspace import WorkerWorkspaceManager
from agents.aggregator.agent import AggregatorAgent
from agents.integrator.agent import IntegratorAgent
from agents.review_agent.agent import ReviewAgent
from agents.github_agent.agent import GitHubAgent
from agents.debug_agent.agent import DebugAgent

configure_logging()
logger = get_logger(__name__)

_MAX_REVIEW_RETRIES = 2


def _redact_secret(text: str) -> str:
    return re.sub(r"oauth2:[^@\s]+@", "oauth2:[REDACTED]@", text)


def _make_run_id() -> str:
    return str(uuid.uuid4()).replace("-", "")[:12]


def _slugify(text: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s_-]+", "-", slug).strip("-")[:40]


def _build_context(run_id: str, task: FeatureTask, model: str) -> AgentContext:
    workspace = Path(settings.WORKSPACE_BASE_PATH) / run_id
    workspace.mkdir(parents=True, exist_ok=True)
    return AgentContext(
        run_id=run_id,
        feature_task_id=task.id,
        repository=task.repository,
        base_branch=task.base_branch,
        workspace_path=str(workspace),
        model=model,
    )


def _clone_repo(workspace_path: str, repository: str, base_branch: str = "main") -> None:
    """把目标仓库 clone 到 workspace（已存在则跳过）。"""
    ws = Path(workspace_path)
    if (ws / ".git").exists():
        return
    clone_url = (
        f"https://oauth2:{settings.GITHUB_TOKEN}"
        f"@github.com/{repository}.git"
    )
    _run_git(["clone", clone_url, str(ws)], retries=2)
    _run_git(["fetch", "origin", base_branch], cwd=ws, retries=2)
    _run_git(["checkout", "-B", base_branch, f"origin/{base_branch}"], cwd=ws)
    _run_git(["config", "user.email", "openclaw@noreply.github.com"], cwd=ws)
    _run_git(["config", "user.name", "OpenClaw"], cwd=ws)
    logger.info(f"Cloned {repository} → {workspace_path}")


def _run_git(args: list[str], cwd: Path | None = None, retries: int = 0) -> str:
    last_error = ""
    for attempt in range(retries + 1):
        result = _sp.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            return result.stdout.strip()

        output = (result.stderr or result.stdout or "").strip()
        last_error = _redact_secret(output)
        if attempt < retries:
            time.sleep(1 + attempt)
            continue

    safe_args = [_redact_secret(part) for part in args]
    raise RuntimeError(
        f"git {' '.join(safe_args)} failed after {retries + 1} attempt(s): "
        f"{last_error[:1000]}"
    )


def _record_pipeline_completed(run: "PipelineRun", run_id: str) -> None:
    """
    Write a unified pipeline_completed event at every exit point of run_pipeline.
    success=True only when CI actually passed; all early exits → success=False.
    """
    record_execution(ExecutionRecord(
        run_id=run_id,
        event="pipeline_completed",
        success=run.ci_passed is True,
        metadata={
            "stage": run.stage,
            "pr_url": run.pr_url or "",
            "ci_passed": run.ci_passed,
            "debug_retry_count": run.debug_retry_count,
            "error_count": len(run.error_log),
        },
    ))


def _log_integration_result(integrator: "IntegratorAgent", run_id: str) -> None:
    """Log a one-line summary of the most recent IntegratorAgent result."""
    ir = getattr(integrator, "last_result", None)
    if ir is None:
        return
    logger.info(
        f"[{run_id}] Integrator result: "
        f"lines={ir.line_count}  "
        f"patches={ir.source_patch_count}  "
        f"conflicts={ir.conflicts_resolved}  "
        f"tests_passed={ir.tests_passed}"
    )


async def _integrate_or_stop(
    integrator: "IntegratorAgent",
    merged: "MergedPatch",
    task: "FeatureTask",
    run: "PipelineRun",
    run_id: str,
) -> "MergedPatch | None":
    """
    Run integrator.integrate(); on ValueError log the error, update run, return None.
    Callers should check for None and return run immediately.
    """
    try:
        return await integrator.integrate(merged, task)
    except ValueError as exc:
        logger.error(f"[{run_id}] Integration failed: {exc}")
        run.error_log.append(str(exc))
        run.stage = "done"
        return None


def _with_extra_constraint(subtask: SubTask, extra: str) -> SubTask:
    """返回一个添加了额外约束的 SubTask 副本（不修改原对象）。"""
    return SubTask(
        id=subtask.id,
        feature=subtask.feature,
        goal=subtask.goal,
        files=subtask.files,
        constraints=subtask.constraints + [extra],
        status=subtask.status,
    )


async def _run_quality_gate(
    command: str,
    cwd: str,
    level: str,
    timeout: int = 120,
) -> QualityGateResult:
    """Run a shell command as a quality gate; return structured result."""
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=timeout)
        exit_code = process.returncode or 0
        output = (stderr_b or stdout_b).decode("utf-8", errors="replace")[:500]
        return QualityGateResult(
            level=level, passed=(exit_code == 0),
            command=command, exit_code=exit_code, output_summary=output,
        )
    except asyncio.TimeoutError:
        return QualityGateResult(
            level=level, passed=False, command=command,
            exit_code=-1, output_summary=f"timed out after {timeout}s",
        )
    except Exception as exc:
        return QualityGateResult(
            level=level, passed=False, command=command,
            exit_code=-1, output_summary=str(exc),
        )


async def _run_workers(
    task_graph,
    ctx: AgentContext,
    label: str = "worker",
    board: "TaskBoard | None" = None,
) -> list[PatchResult]:
    """Run Workers with bounded concurrency and dependency enforcement.

    A single asyncio.Semaphore(MAX_PARALLEL_WORKERS) spans all parallel groups
    so the total number of live Worker subprocesses never exceeds the configured
    limit, regardless of how many tasks a group contains.

    Before each subtask starts, its declared dependencies (task_graph.dependencies)
    are checked against the set of successfully completed subtask IDs.  Tasks whose
    dependencies failed are skipped with status=FAILED rather than run with broken
    inputs, preventing cascading patch errors.

    If `board` is provided, each status transition is mirrored to it so callers
    can observe real-time progress via board.snapshot().
    """
    sem = asyncio.Semaphore(settings.MAX_PARALLEL_WORKERS)
    completed_ids: set[str] = set()   # subtask IDs that finished with SUCCESS
    all_patches: list[PatchResult] = []

    for group_idx, group in enumerate(task_graph.parallel_groups):
        subtasks = [task_graph.get_subtask(sid) for sid in group]

        async def _run_one(worker_label: str, subtask: SubTask) -> PatchResult:
            # Skip if any declared dependency did not complete successfully
            deps = task_graph.dependencies.get(subtask.id, [])
            unmet = [d for d in deps if d not in completed_ids]
            if unmet:
                logger.warning(
                    f"[{label}] Skipping subtask {subtask.id!r}: "
                    f"dependencies not satisfied: {unmet}"
                )
                subtask.status = TaskStatus.FAILED
                if board is not None:
                    board.update(subtask.id, TaskStatus.FAILED, worker_id=worker_label)
                return PatchResult(
                    subtask_id=subtask.id,
                    worker_id=worker_label,
                    patch_content="",
                    affected_files=[],
                    status=PatchStatus.FAILED,
                    error_message=f"Dependencies not satisfied: {unmet}",
                )

            subtask.status = TaskStatus.IN_PROGRESS
            if board is not None:
                board.update(subtask.id, TaskStatus.IN_PROGRESS, worker_id=worker_label)
            async with sem:
                worker = ClaudeCodeWorker(worker_label, ctx)
                result = await worker.execute(subtask)

            if result.status == PatchStatus.SUCCESS:
                subtask.status = TaskStatus.COMPLETED
                completed_ids.add(subtask.id)
                if board is not None:
                    board.update(subtask.id, TaskStatus.COMPLETED, worker_id=worker_label)
            else:
                subtask.status = TaskStatus.FAILED
                if board is not None:
                    board.update(subtask.id, TaskStatus.FAILED, worker_id=worker_label)

            return result

        results = await asyncio.gather(
            *[_run_one(f"{label}-{group_idx}-{i}", st) for i, st in enumerate(subtasks)]
        )
        all_patches.extend(results)

    return all_patches


async def run_pipeline(
    raw_requirement: str,
    repository: str,
    base_branch: str = "main",
    model: str = "",
) -> PipelineRun:
    run_id = _make_run_id()
    model = model or settings.LLM_MODEL

    task = FeatureTask(
        raw_requirement=raw_requirement,
        feature_name=_slugify(raw_requirement[:40]),
        repository=repository,
        base_branch=base_branch,
    )
    ctx = _build_context(run_id, task, model)
    run = PipelineRun(run_id=run_id, feature_task_id=task.id)

    # ── Stage 0: Clone 目标仓库到 workspace ─────────────────────────────────────
    run.stage = "clone"
    save_run_state(run, settings.WORKSPACE_BASE_PATH)
    logger.info(f"[{run_id}] Stage: clone  {repository}")
    _clone_repo(ctx.workspace_path, repository, base_branch=task.base_branch)

    # ── Preflight: workspace strategy check ──────────────────────────────────────
    # validate_strategy does NOT create anything — read-only git checks only.
    # Runs after clone so ctx.workspace_path is already a git repository.
    _wm = WorkerWorkspaceManager()
    _preflight_ok, _preflight_msg = _wm.validate_strategy(ctx.workspace_path)
    if _preflight_ok:
        logger.info(f"[{run_id}] Workspace strategy preflight ok: {_preflight_msg}")
    else:
        logger.error(f"[{run_id}] Workspace strategy preflight failed: {_preflight_msg}")
        run.error_log.append(_preflight_msg)
        run.stage = "done"
        record_execution(ExecutionRecord(
            run_id=run_id,
            event="pipeline_preflight_failed",
            success=False,
            error=_preflight_msg[:1000],
        ))
        _record_pipeline_completed(run, run_id)
        return run

    # ── Stage 1: ContextAgent — 扫描仓库，提取相关文件和代码模式 ────────────────
    run.stage = "context"
    save_run_state(run, settings.WORKSPACE_BASE_PATH)
    logger.info(f"[{run_id}] Stage: context")
    context_agent = ContextAgent(ctx)
    code_context = await context_agent.gather(task, ctx.workspace_path)
    logger.info(
        f"[{run_id}] Context: {len(code_context.relevant_files)} relevant files, "
        f"patterns={code_context.existing_patterns}"
    )

    # ── Stage 1: OrchestratorAgent — 拆解任务 ───────────────────────────────────
    run.stage = "orchestrate"
    logger.info(f"[{run_id}] Stage: orchestrate")
    orchestrator = OrchestratorAgent(ctx)
    task_graph = await orchestrator.decompose(task)
    all_subtasks = [task_graph.get_subtask(sid) for group in task_graph.parallel_groups for sid in group]
    logger.info(
        f"[{run_id}] Decomposed into {len(all_subtasks)} subtasks, "
        f"{len(task_graph.parallel_groups)} parallel groups"
    )

    # ── Stage 2: TestAgent — TDD 先写测试规范 ────────────────────────────────────
    run.stage = "test-gen"
    logger.info(f"[{run_id}] Stage: test-gen ({len(all_subtasks)} subtasks)")
    test_agent = TestAgent(ctx)
    test_specs = await asyncio.gather(
        *[test_agent.generate(st, code_context) for st in all_subtasks]
    )
    # 将测试文件写入 workspace，Worker subprocess 可以看到这些测试
    for spec in test_specs:
        if spec.test_code:
            test_path = Path(ctx.workspace_path) / spec.test_file_path
            test_path.parent.mkdir(parents=True, exist_ok=True)
            test_path.write_text(spec.test_code, encoding="utf-8")
            logger.info(f"[{run_id}] TestAgent wrote {spec.test_file_path}")

    # ── Stage 3: Worker — 并行生成 patch ────────────────────────────────────────
    run.stage = "worker"
    save_run_state(run, settings.WORKSPACE_BASE_PATH)
    logger.info(f"[{run_id}] Stage: worker")
    all_patches = await _run_workers(task_graph, ctx, label="worker")

    # ── Stage 4: AggregatorAgent — 合并 patch ────────────────────────────────────
    run.stage = "aggregate"
    save_run_state(run, settings.WORKSPACE_BASE_PATH)
    logger.info(f"[{run_id}] Stage: aggregate ({len(all_patches)} patches)")
    aggregator = AggregatorAgent(ctx)
    integrator = IntegratorAgent(ctx)
    patch_set = PatchSet(feature_task_id=task.id, patches=all_patches)
    merged_raw = await aggregator.merge(patch_set)
    merged = await _integrate_or_stop(integrator, merged_raw, task, run, run_id)
    if merged is None:
        _record_pipeline_completed(run, run_id)
        return run
    _log_integration_result(integrator, run_id)

    # ── Stage 5: ReviewAgent — 审查 patch，blocked 则回退重做 ────────────────────
    review_agent = ReviewAgent(ctx)
    for review_attempt in range(_MAX_REVIEW_RETRIES + 1):
        run.stage = f"review-{review_attempt + 1}"
        logger.info(f"[{run_id}] Stage: review (attempt {review_attempt + 1})")
        review_result = await review_agent.review(merged, task)

        if review_result.approved:
            logger.info(f"[{run_id}] Review approved: {review_result.summary}")
            break

        block_summary = review_result.summary
        block_msgs = "; ".join(c.message for c in review_result.comments)
        logger.warning(f"[{run_id}] Review blocked: {block_summary} | {block_msgs}")

        if review_attempt >= _MAX_REVIEW_RETRIES:
            run.error_log.append(f"Review blocked after {_MAX_REVIEW_RETRIES} retries: {block_summary}")
            logger.error(f"[{run_id}] Max review retries reached. Aborting.")
            run.stage = "done"
            _record_pipeline_completed(run, run_id)
            return run

        # 把 review 反馈作为额外约束，重新驱动 Worker
        extra_constraint = f"Fix review issues: {block_msgs or block_summary}"
        # 修改 task_graph 中的 subtask 约束（原地替换）
        for sid in [sid for group in task_graph.parallel_groups for sid in group]:
            st = task_graph.get_subtask(sid)
            task_graph.feature_task.subtasks = [
                _with_extra_constraint(s, extra_constraint) if s.id == sid else s
                for s in task_graph.feature_task.subtasks
            ]
        retry_patches = await _run_workers(task_graph, ctx, label=f"worker-review{review_attempt + 1}")
        patch_set = PatchSet(feature_task_id=task.id, patches=retry_patches)
        retry_raw = await aggregator.merge(patch_set)
        merged = await _integrate_or_stop(integrator, retry_raw, task, run, run_id)
        if merged is None:
            _record_pipeline_completed(run, run_id)
            return run  # re-integration failed after review retry
        _log_integration_result(integrator, run_id)

    # ── Stage 5b: Pre-publish quality gate (lint / typecheck) ───────────────────
    for gate_cmd, gate_name in [
        (settings.LINT_COMMAND, "lint"),
        (settings.TYPECHECK_COMMAND, "typecheck"),
    ]:
        if not gate_cmd:
            continue
        gate_result = await _run_quality_gate(
            command=gate_cmd,
            cwd=ctx.workspace_path,
            level="pre_publish",
        )
        if gate_result.passed:
            logger.info(f"[{run_id}] Quality gate [{gate_name}] passed")
        elif settings.QUALITY_GATE_WARN_ONLY:
            logger.warning(
                f"[{run_id}] Quality gate [{gate_name}] failed (warn-only): "
                f"{gate_result.output_summary[:200]}"
            )
        else:
            msg = (
                f"Quality gate [{gate_name}] blocked publish: "
                f"{gate_result.output_summary[:200]}"
            )
            logger.error(f"[{run_id}] {msg}")
            run.error_log.append(msg)
            run.stage = "done"
            _record_pipeline_completed(run, run_id)
            return run

    # ── Stage 6: GitHubAgent — 创建 PR ──────────────────────────────────────────
    run.stage = "github"
    save_run_state(run, settings.WORKSPACE_BASE_PATH)
    logger.info(f"[{run_id}] Stage: github")
    github_agent = GitHubAgent(ctx)
    pr_request = await github_agent.apply_and_push(merged, task)
    pr_result = await github_agent.create_pr(pr_request)
    run.pr_number = pr_result.pr_number
    run.pr_url = pr_result.pr_url
    logger.info(f"[{run_id}] PR created: {pr_result.pr_url}")

    # ── Stage 7: CI 轮询 + DebugAgent 重试循环 ───────────────────────────────────
    run.stage = "ci"
    save_run_state(run, settings.WORKSPACE_BASE_PATH)
    debug_agent = DebugAgent(ctx)
    current_merged = merged

    for attempt in range(settings.MAX_DEBUG_RETRIES + 1):
        ci_result = await github_agent.poll_ci(pr_result)

        if ci_result.status == CIStatus.SUCCESS:
            run.ci_passed = True
            logger.info(f"[{run_id}] CI passed!")
            break

        if attempt >= settings.MAX_DEBUG_RETRIES:
            run.ci_passed = False
            run.error_log.append(f"CI failed after {settings.MAX_DEBUG_RETRIES} debug retries")
            logger.error(f"[{run_id}] Max debug retries reached. CI failed.")
            break

        run.stage = f"debug-{attempt + 1}"
        run.debug_retry_count = attempt + 1
        logger.info(f"[{run_id}] CI failed, debug attempt #{attempt + 1}")

        debug_ctx = DebugContext(
            original_patch=current_merged.merged_diff,
            ci_log=ci_result.raw_log or "",
            failed_checks=ci_result.failed_checks,
            retry_attempt=attempt + 1,
            subtask_id=task.id,
            subtask_goal=task.raw_requirement,
            subtask_files=current_merged.source_patch_ids,
        )
        fixed_patch = await debug_agent.fix(debug_ctx)
        fixed_patch_set = PatchSet(feature_task_id=task.id, patches=[fixed_patch])
        current_merged = await aggregator.merge(fixed_patch_set)
        pr_request = await github_agent.apply_and_push(current_merged, task)
        await github_agent.close_pr(pr_result.pr_number)
        pr_result = await github_agent.create_pr(pr_request)
        run.pr_number = pr_result.pr_number
        run.pr_url = pr_result.pr_url

    run.stage = "done"
    logger.info(f"[{run_id}] Pipeline done. PR: {run.pr_url}, CI: {run.ci_passed}")
    _record_pipeline_completed(run, run_id)
    return run


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenClaw Pipeline")
    parser.add_argument("--requirement", required=True, help="自然语言需求描述")
    parser.add_argument("--repo", required=True, help="GitHub 仓库，格式 org/repo")
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--model", default="", help="模型 ID（默认读取 LLM_MODEL 环境变量）")
    args = parser.parse_args()

    result = asyncio.run(
        run_pipeline(
            raw_requirement=args.requirement,
            repository=args.repo,
            base_branch=args.base_branch,
            model=args.model,
        )
    )
    print(f"\n{'='*60}")
    print(f"Run ID  : {result.run_id}")
    print(f"PR URL  : {result.pr_url}")
    print(f"CI Pass : {result.ci_passed}")
    print(f"Retries : {result.debug_retry_count}")
    if result.error_log:
        print(f"Errors  : {result.error_log}")
    print("="*60)


if __name__ == "__main__":
    main()
