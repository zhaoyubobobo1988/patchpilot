"""
CLI entry point for the execution log viewer.

Usage:
  python -m telemetry.log_viewer [--path FILE] [--run-id RUN_ID]

If --path is omitted, reads settings.EXECUTION_LOG_PATH.
Exit code 0 on success, 1 on error.
"""
from __future__ import annotations

import argparse
import sys

from telemetry.log_viewer import view_run


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m telemetry.log_viewer",
        description="View OpenClaw execution log (JSONL)",
    )
    parser.add_argument(
        "--path", default="",
        help="Path to JSONL log file. Defaults to EXECUTION_LOG_PATH from settings.",
    )
    parser.add_argument(
        "--run-id", default="",
        help="Filter to a specific pipeline run_id.",
    )
    args = parser.parse_args()

    path = args.path
    if not path:
        try:
            from config.settings import settings
            path = settings.EXECUTION_LOG_PATH
        except Exception:
            pass

    if not path:
        print(
            "EXECUTION_LOG_PATH is not configured.\n"
            "Set it in .env or pass --path <file>.",
            file=sys.stderr,
        )
        return 1

    print(view_run(path, run_id=args.run_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
