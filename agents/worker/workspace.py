"""
WorkerWorkspaceManager — isolated workspace preparation for ClaudeCodeWorker.

Supports two strategies, selected by settings.WORKER_WORKSPACE_STRATEGY:

  "clone"    (default) — git clone --local
             Hard-links the object store; gives each Worker a fully
             independent git repo with its own index and HEAD.

  "worktree" (optional) — git worktree add -B <branch> <path> HEAD
             Shares the object store with the source workspace; lower
             disk usage when many Workers run in parallel.

In both cases the directory is named <parent_of_source>/<run_id>-<worker_id>
so that GitHubAgent._collect_worker_changes can discover all worker workspaces
using the same prefix-match logic.  This naming MUST NOT change.

git_diff() is identical for both strategies.
Worktree cleanup (git worktree prune/remove) is not implemented in this phase.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from config.logging import get_logger
from config.settings import settings

logger = get_logger(__name__)

_VALID_STRATEGIES = frozenset({"clone", "worktree"})


class WorkerWorkspaceManager:
    """
    Prepares and queries isolated git working directories for Workers.

    Public interface
    ----------------
    validate_strategy(source_workspace) -> (ok, message)
        Read-only pre-flight check.  Does NOT modify any state.
    prepare(run_id, worker_id, source_workspace) -> str
        Return the path to the worker's workspace, creating it if needed.
    git_diff(workspace) -> str
        Stage all pending changes and return the cached unified diff.
    """

    # ── pre-flight ─────────────────────────────────────────────────────────────

    def validate_strategy(self, source_workspace: str) -> tuple[bool, str]:
        """
        Check whether the configured WORKER_WORKSPACE_STRATEGY is usable.

        Returns (True, info_message) when the strategy is ready to use.
        Returns (False, diagnostic_message) when a problem is detected.

        Read-only: runs at most two git commands, creates nothing.
        Not called automatically by prepare(); intended for startup diagnostics
        or a future pipeline preflight hook.

        Strategy resolution mirrors prepare():
          - unknown value → treated as "clone", returns (True, fallback note)
          - "clone"       → always ok (no git checks needed)
          - "worktree"    → runs rev-parse + worktree list against source_workspace
        """
        strategy = settings.WORKER_WORKSPACE_STRATEGY

        if strategy not in _VALID_STRATEGIES:
            return (
                True,
                f"Unknown WORKER_WORKSPACE_STRATEGY={strategy!r}, "
                f"will fall back to 'clone'",
            )

        if strategy == "clone":
            return (True, "clone strategy is available")

        # "worktree" — two read-only sanity checks
        # 1. Is source_workspace inside a git repository?
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=source_workspace,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            return (False, "worktree strategy requires git to be installed and on PATH")
        except Exception as exc:
            return (False, f"worktree pre-check failed (rev-parse): {type(exc).__name__}: {exc}")

        if r.returncode != 0:
            return (
                False,
                f"worktree strategy requires {source_workspace!r} to be inside a git "
                f"repository (git rev-parse --is-inside-work-tree failed: "
                f"{r.stderr.strip()[:200]})",
            )

        # 2. Is 'git worktree' subcommand available?
        try:
            r2 = subprocess.run(
                ["git", "worktree", "list"],
                cwd=source_workspace,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            return (False, "worktree strategy requires git to be installed and on PATH")
        except Exception as exc:
            return (False, f"worktree pre-check failed (worktree list): {type(exc).__name__}: {exc}")

        if r2.returncode != 0:
            return (
                False,
                f"worktree strategy: 'git worktree list' failed in {source_workspace!r}: "
                f"{r2.stderr.strip()[:200]}",
            )

        return (True, f"worktree strategy is available (source={source_workspace!r})")

    # ── prepare ───────────────────────────────────────────────────────────────

    async def prepare(
        self,
        run_id: str,
        worker_id: str,
        source_workspace: str,
    ) -> str:
        """
        Return the path to a worker-local workspace.

        Re-entrant: if <workspace>/.git already exists the path is returned
        immediately, regardless of which strategy is configured.

        Strategy is read from settings.WORKER_WORKSPACE_STRATEGY at call time.
        Unknown values log a warning and fall back to "clone".
        validate_strategy() is NOT called here — it is for external diagnostics.
        """
        worker_ws = Path(source_workspace).parent / f"{run_id}-{worker_id}"

        if (worker_ws / ".git").exists():
            return str(worker_ws)

        strategy = settings.WORKER_WORKSPACE_STRATEGY
        if strategy not in _VALID_STRATEGIES:
            logger.warning(
                f"[WorkspaceManager] Unknown WORKER_WORKSPACE_STRATEGY={strategy!r}, "
                f"falling back to clone"
            )
            strategy = "clone"

        if strategy == "worktree":
            await self._prepare_worktree(source_workspace, worker_ws, run_id, worker_id)
        else:
            await self._prepare_clone(source_workspace, worker_ws)

        return str(worker_ws)

    # ── git_diff ──────────────────────────────────────────────────────────────

    def git_diff(self, workspace: str) -> str:
        """
        Stage all changes in *workspace* with `git add -A` and return the
        output of `git diff --cached` (the staged unified diff).
        Identical for both clone and worktree strategies.
        """
        subprocess.run(
            ["git", "add", "-A"],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        result = subprocess.run(
            ["git", "diff", "--cached"],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return result.stdout.strip()

    # ── private: clone strategy ───────────────────────────────────────────────

    async def _prepare_clone(self, source: str, worker_ws: Path) -> None:
        """Create worker workspace via git clone --local."""
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                ["git", "clone", "--local", source, str(worker_ws)],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            ),
        )
        self._set_git_user(worker_ws)
        logger.info(f"[WorkspaceManager] clone  {source!r} → {worker_ws}")

    # ── private: worktree strategy ────────────────────────────────────────────

    async def _prepare_worktree(
        self,
        source: str,
        worker_ws: Path,
        run_id: str,
        worker_id: str,
    ) -> None:
        """
        Create worker workspace via git worktree add.

        Raises RuntimeError (not CalledProcessError) with a human-readable
        message including branch_name and worker_ws so that Worker.execute()
        can surface a clear error_message instead of a raw subprocess traceback.
        """
        branch_name = f"openclaw/{run_id}/{worker_id}"
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    ["git", "worktree", "add", "-B", branch_name, str(worker_ws), "HEAD"],
                    cwd=source,
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                ),
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()[:300]
            raise RuntimeError(
                f"git worktree add failed  "
                f"branch={branch_name!r}  workspace={worker_ws!r}: "
                f"{stderr or f'exit {exc.returncode}'}"
            ) from exc

        self._set_git_user(worker_ws)
        logger.info(f"[WorkspaceManager] worktree  branch={branch_name!r} → {worker_ws}")

    # ── shared ────────────────────────────────────────────────────────────────

    def _set_git_user(self, worker_ws: Path) -> None:
        """Configure git user identity in the new workspace."""
        for key, value in [
            ("user.email", "openclaw@noreply.github.com"),
            ("user.name", "OpenClaw"),
        ]:
            subprocess.run(
                ["git", "config", key, value],
                cwd=worker_ws,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
