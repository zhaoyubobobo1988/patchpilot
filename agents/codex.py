"""
CodexAgent — wraps `codex exec` CLI subprocess.

Implements the same AgentAdapter interface as ClaudeCodeAgent so Reviewer
can switch backends without any other code changes.

Compatibility note:
  Codex CLI uses its own OpenAI/Codex configuration and authentication path.
  Custom base URL and OpenAI-compatible gateways may behave differently across
  Codex versions and gateway implementations, especially around Responses API
  and streaming/fallback behavior.  Keep REVIEW_AGENT_BACKEND=claude-code as
  the default for GLM users unless Codex has been verified in the local
  environment with a working OPENAI_API_KEY.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

from config.logging import get_logger
from config.settings import settings
from .base import AgentAdapter, AgentResult, AgentTask

logger = get_logger(__name__)

_CODEX_BIN = "codex.cmd" if sys.platform == "win32" else "codex"


class CodexAgent:
    """
    Runs `codex exec` non-interactively and converts the result to AgentResult.

    Codex flags used:
      exec                        non-interactive subcommand
      --ephemeral                 no session persistence
      --ignore-user-config        skip ~/.codex/config.toml
      --skip-git-repo-check       allow running outside a git repo
      -s read-only                sandbox: read-only (Reviewer only reads)
      -m <model>                  model from CODEX_MODEL setting
      -o <tmpfile>                write final message to file

    Prompt is passed via stdin.
    Output is read from -o file first, falling back to stdout.
    """

    # AgentAdapter Protocol compliance
    _: AgentAdapter  # type: ignore[assignment]

    async def run(self, task: AgentTask) -> AgentResult:
        process: asyncio.subprocess.Process | None = None
        output_path: str | None = None
        start = time.monotonic()
        result: AgentResult  # set in every branch; recorded before return

        try:
            # Create the -o output file before starting the process
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8"
            ) as f:
                output_path = f.name

            env = self._build_env()
            model = settings.CODEX_MODEL
            if not model:
                result = AgentResult(
                    success=False,
                    output="",
                    exit_code=-1,
                    error=(
                        "CODEX_MODEL is not configured. "
                        "Set it to a valid OpenAI/Codex model (e.g. o4-mini) "
                        "before enabling REVIEW_AGENT_BACKEND=codex."
                    ),
                    metadata=self._meta(task, time.monotonic() - start),
                )
                # record + clean up before early return
                from telemetry.execution_log import record_agent_result
                record_agent_result(result, task, "codex")
                Path(output_path).unlink(missing_ok=True)
                return result

            logger.info(
                f"[CodexAgent] task_id={task.task_id} role={task.role} "
                f"model={model} workspace={task.workspace}"
            )

            process = await asyncio.create_subprocess_exec(
                _CODEX_BIN, "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--skip-git-repo-check",
                "-s", "read-only",
                "-m", model,
                "-o", output_path,
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
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")

            logger.info(
                f"[CodexAgent] task_id={task.task_id} role={task.role} "
                f"exit={exit_code} elapsed={elapsed:.1f}s"
            )

            if exit_code != 0:
                result = AgentResult(
                    success=False,
                    output="",
                    exit_code=exit_code,
                    error=stderr_text[:400] or f"codex exec exited {exit_code}",
                    metadata=self._meta(task, elapsed),
                )
            else:
                output = self._read_output(output_path, stdout_bytes)
                result = AgentResult(
                    success=True,
                    output=output,
                    exit_code=0,
                    metadata=self._meta(task, elapsed),
                )

        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            logger.error(
                f"[CodexAgent] task_id={task.task_id} TIMEOUT "
                f"after {elapsed:.1f}s (limit={task.timeout_seconds}s)"
            )
            result = AgentResult(
                success=False,
                output="",
                exit_code=-1,
                error=f"codex exec timed out after {task.timeout_seconds}s",
                metadata=self._meta(task, elapsed),
            )

        except Exception as exc:
            elapsed = time.monotonic() - start
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            logger.error(
                f"[CodexAgent] task_id={task.task_id} ERROR "
                f"{type(exc).__name__}: {exc}"
            )
            result = AgentResult(
                success=False,
                output="",
                exit_code=-1,
                error=f"{type(exc).__name__}: {exc}",
                metadata=self._meta(task, elapsed),
            )

        finally:
            if output_path:
                Path(output_path).unlink(missing_ok=True)

        from telemetry.execution_log import record_agent_result
        record_agent_result(result, task, "codex")
        return result

    # ── private helpers ───────────────────────────────────────────────────────

    def _build_env(self) -> dict[str, str]:
        """
        Merge system env with Codex/OpenAI API configuration.

        - OPENAI_API_KEY is the primary Codex credential (standard OpenAI key).
        - OPENAI_BASE_URL is only overridden when settings.OPENAI_API_BASE is
          explicitly set; otherwise the system/process value is preserved.
          This avoids silently redirecting Codex to a gateway that may not
          fully support its Responses API or WebSocket transport.
        - No API key or token is written to any log line.
        """
        env = os.environ.copy()

        api_key = settings.OPENAI_API_KEY
        if api_key:
            env["OPENAI_API_KEY"] = api_key
            env["CODEX_OPENAI_API_KEY"] = api_key  # alias read by some Codex versions

        # Only override base URL when explicitly configured — don't hardcode.
        if settings.OPENAI_API_BASE:
            env["OPENAI_BASE_URL"] = settings.OPENAI_API_BASE

        env.update({
            "CI": "1",        # suppress Codex interactive prompts
            "NO_COLOR": "1",
        })
        return env

    def _read_output(self, output_path: str, stdout_bytes: bytes) -> str:
        """Read from -o file first; fall back to stdout bytes."""
        out_file = Path(output_path)
        if out_file.exists() and out_file.stat().st_size > 0:
            text = out_file.read_text(encoding="utf-8", errors="replace").strip()
            logger.info(f"[CodexAgent] output from -o file: {len(text)} chars")
            return text
        text = stdout_bytes.decode("utf-8", errors="replace").strip()
        logger.info(f"[CodexAgent] output from stdout: {len(text)} chars")
        return text

    def _meta(self, task: AgentTask, elapsed: float) -> dict:
        return {
            "agent": "codex",
            "role": task.role,
            "task_id": task.task_id,
            "elapsed_seconds": round(elapsed, 2),
        }
