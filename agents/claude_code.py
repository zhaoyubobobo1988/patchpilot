"""
ClaudeCodeAgent — единственный место, где вызывается `claude` CLI.

Все вызовы Planner / Worker / Reviewer проходят через этот класс.
Бизнес-уровень OpenClaw работает только с AgentTask / AgentResult.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Mapping

from config.logging import get_logger
from config.settings import settings
from models.events import AgentEvent, AgentEventKind
from .base import AgentAdapter, AgentResult, AgentTask

logger = get_logger(__name__)

# Windows ships claude as a .cmd wrapper; Unix uses a plain binary.
_CLAUDE_BIN = "claude.cmd" if sys.platform == "win32" else "claude"


class ClaudeCodeAgent:
    """
    Runs `claude --print` as a subprocess and converts the result to AgentResult.

    Flags used (inherited from the project's working configuration):
      --print                        non-interactive, one-shot
      --output-format json|text      json → parse envelope; text → raw stdout
      --dangerously-skip-permissions no confirmation prompts (workspace is disposable)
      --bare                         skip global CLAUDE.md / user hooks

    Prompt is passed via stdin to avoid Windows cmd.exe newline truncation.

    GLM-via-Anthropic-shim env vars (ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN)
    are injected on top of os.environ so PATH and other system vars are preserved.
    """

    # AgentAdapter Protocol compliance
    _: AgentAdapter  # type: ignore[assignment]

    def __init__(self, extra_env: Mapping[str, str] | None = None) -> None:
        # extra_env lets callers inject additional vars without losing system env
        self._extra_env: dict[str, str] = dict(extra_env) if extra_env else {}

    async def run(self, task: AgentTask) -> AgentResult:
        process: asyncio.subprocess.Process | None = None
        start = time.monotonic()
        result: AgentResult  # set in every branch; recorded before return

        events: list[AgentEvent] = [
            AgentEvent(
                kind=AgentEventKind.STARTED,
                agent_id=task.task_id,
                payload={"role": task.role},
            )
        ]

        try:
            env = self._build_env()

            logger.info(
                f"[ClaudeCodeAgent] task_id={task.task_id} role={task.role} "
                f"workspace={task.workspace} output_format={task.output_format}"
            )

            # asyncio.create_subprocess_exec and asyncio.wait_for are referenced
            # through the asyncio module so unit-test patches remain effective.
            process = await asyncio.create_subprocess_exec(
                _CLAUDE_BIN,
                "--print",
                "--output-format", task.output_format,
                "--dangerously-skip-permissions",
                "--bare",
                cwd=str(task.workspace),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(input=task.prompt.encode("utf-8")),
                timeout=task.timeout_seconds,
            )

            elapsed = time.monotonic() - start
            exit_code = process.returncode or 0
            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")

            logger.info(
                f"[ClaudeCodeAgent] task_id={task.task_id} role={task.role} "
                f"exit={exit_code} elapsed={elapsed:.1f}s success={exit_code == 0}"
            )

            if exit_code != 0:
                events.append(AgentEvent(
                    kind=AgentEventKind.FAILED,
                    agent_id=task.task_id,
                    payload={"exit_code": exit_code},
                ))
                result = AgentResult(
                    success=False,
                    output="",
                    exit_code=exit_code,
                    error=stderr_text[:400] or f"claude CLI exited {exit_code}",
                    metadata=self._meta(task, elapsed),
                    events=events,
                )
            else:
                output = self._parse_output(stdout_text, task.output_format)
                events.append(AgentEvent(
                    kind=AgentEventKind.COMPLETED,
                    agent_id=task.task_id,
                    payload={"elapsed": round(elapsed, 2)},
                ))
                result = AgentResult(
                    success=True,
                    output=output,
                    exit_code=0,
                    metadata=self._meta(task, elapsed),
                    events=events,
                )

        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            logger.error(
                f"[ClaudeCodeAgent] task_id={task.task_id} role={task.role} "
                f"TIMEOUT after {elapsed:.1f}s (limit={task.timeout_seconds}s)"
            )
            events.append(AgentEvent(
                kind=AgentEventKind.FAILED,
                agent_id=task.task_id,
                payload={"reason": f"timeout after {task.timeout_seconds}s"},
            ))
            result = AgentResult(
                success=False,
                output="",
                exit_code=-1,
                error=f"claude CLI timed out after {task.timeout_seconds}s",
                metadata=self._meta(task, elapsed),
                events=events,
            )

        except Exception as exc:
            elapsed = time.monotonic() - start
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            logger.error(
                f"[ClaudeCodeAgent] task_id={task.task_id} role={task.role} "
                f"ERROR {type(exc).__name__}: {exc}"
            )
            events.append(AgentEvent(
                kind=AgentEventKind.FAILED,
                agent_id=task.task_id,
                payload={"reason": f"{type(exc).__name__}: {exc}"},
            ))
            result = AgentResult(
                success=False,
                output="",
                exit_code=-1,
                error=f"{type(exc).__name__}: {exc}",
                metadata=self._meta(task, elapsed),
                events=events,
            )

        from telemetry.execution_log import record_agent_result
        record_agent_result(result, task, "claude-code")
        return result

    # ── private helpers ───────────────────────────────────────────────────────

    def _build_env(self) -> dict[str, str]:
        """Merge system env → GLM shim vars → caller-supplied extras."""
        env = os.environ.copy()
        env.update({
            "ANTHROPIC_BASE_URL": settings.ANTHROPIC_BASE_URL,
            "ANTHROPIC_AUTH_TOKEN": settings.GLM_API_KEY or settings.ANTHROPIC_API_KEY,
        })
        env.update(self._extra_env)
        return env

    def _parse_output(self, raw: str, output_format: str) -> str:
        if output_format == "json":
            try:
                envelope = json.loads(raw.strip())
                return envelope.get("result", "")
            except json.JSONDecodeError:
                # Claude may output plain text in some edge cases; return as-is
                return raw.strip()
        # "text" mode: caller (Worker) will use git diff; return raw stdout
        return raw.strip()

    def _meta(self, task: AgentTask, elapsed: float) -> dict:
        return {
            "agent": "claude-code",
            "role": task.role,
            "task_id": task.task_id,
            "elapsed_seconds": round(elapsed, 2),
        }
