from __future__ import annotations

from pathlib import Path

from models.context import PipelineRun


def _state_path(base_path: str, run_id: str) -> Path:
    return Path(base_path) / ".openclaw" / f"run_{run_id}.json"


def save_run_state(run: PipelineRun, base_path: str) -> None:
    """Persist PipelineRun to {base_path}/.openclaw/run_{run_id}.json.

    Overwrites any existing file so the on-disk record always reflects
    the current stage. Errors are silently swallowed — persistence failure
    must never crash the pipeline.
    """
    try:
        path = _state_path(base_path, run.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(run.model_dump_json(), encoding="utf-8")
    except Exception:
        pass


def load_run_state(run_id: str, base_path: str) -> PipelineRun | None:
    """Load a previously saved PipelineRun; return None if not found."""
    path = _state_path(base_path, run_id)
    if not path.exists():
        return None
    return PipelineRun.model_validate_json(path.read_text(encoding="utf-8"))
