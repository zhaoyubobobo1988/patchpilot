"""
models/errors.py — Fine-grained failure classification for pipeline/supervisor decisions.

PR8 introduces a structured error taxonomy so the supervisor can make
category-driven decisions: TRANSIENT errors get retried, PERMANENT/CONFIG
errors abort immediately with recovery hints.
"""
from __future__ import annotations

import json
from enum import Enum

from pydantic import BaseModel


# ── Enums ────────────────────────────────────────────────────────────────────

class FailureCategory(str, Enum):
    """Fine-grained failure classification for pipeline/supervisor decisions."""
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    CONFIG = "config"
    EXTERNAL = "external"
    RESOURCE = "resource"
    UNKNOWN = "unknown"


class RecoveryAction(str, Enum):
    """Suggested recovery action derived from failure category."""
    RETRY = "retry"
    SKIP = "skip"
    ABORT = "abort"
    FALLBACK = "fallback"


# ── Model ────────────────────────────────────────────────────────────────────

class ClassifiedError(BaseModel):
    """Structured error carrying category, provenance, and a recovery hint."""
    category: FailureCategory
    source: str
    message: str
    exception_type: str = ""
    recovery_hint: str = ""
    raw_error: str | None = None


# ── Classification function ──────────────────────────────────────────────────

# Exception-type → (category, recovery_hint) mapping.
# Order matters: first isinstance() match wins.
# Put subclasses before their parents (e.g. JSONDecodeError before ValueError).
_EXCEPTION_TYPE_MAP: list[tuple[type, FailureCategory, str]] = [
    # ── TRANSIENT ────────────────────────────────────────────────────────
    (TimeoutError,          FailureCategory.TRANSIENT, "Operation timed out; retry may succeed"),
    (ConnectionRefusedError, FailureCategory.TRANSIENT, "Network connection refused; retry after backoff"),
    (ConnectionError,       FailureCategory.TRANSIENT, "Network issue; retry after backoff"),
    # ── PERMANENT — subclass checks before their parents ─────────────────
    (json.JSONDecodeError,  FailureCategory.PERMANENT, "Agent output was not valid JSON"),
    (ValueError,            FailureCategory.PERMANENT, "Invalid value; check input"),
    # ── CONFIG ───────────────────────────────────────────────────────────
    (SyntaxError,           FailureCategory.PERMANENT, "Generated code contains a syntax error"),
    (IndentationError,      FailureCategory.PERMANENT, "Generated code contains indentation error"),
    (PermissionError,       FailureCategory.CONFIG,    "File permission denied; check workspace permissions"),
    (FileNotFoundError,     FailureCategory.CONFIG,    "Required file not found; check configuration"),
    # ── RESOURCE ─────────────────────────────────────────────────────────
    (MemoryError,           FailureCategory.RESOURCE,  "Out of memory; increase available RAM"),
]

# Message-pattern → category mapping (case-insensitive).
# First match wins; checked only when exception-type mapping didn't fire.
_MESSAGE_PATTERNS: list[tuple[str, FailureCategory]] = [
    # TRANSIENT
    ("timeout",             FailureCategory.TRANSIENT),
    ("timed out",           FailureCategory.TRANSIENT),
    ("deadline exceeded",   FailureCategory.TRANSIENT),
    ("rate limit",          FailureCategory.TRANSIENT),
    ("too many requests",   FailureCategory.TRANSIENT),
    ("429",                 FailureCategory.TRANSIENT),
    ("connection refused",  FailureCategory.TRANSIENT),
    ("connection reset",    FailureCategory.TRANSIENT),
    ("network",             FailureCategory.TRANSIENT),
    ("temporarily unavailable", FailureCategory.TRANSIENT),
    ("503",                 FailureCategory.TRANSIENT),
    ("502",                 FailureCategory.TRANSIENT),
    # PERMANENT
    ("syntax error",        FailureCategory.PERMANENT),
    ("invalid syntax",      FailureCategory.PERMANENT),
    ("indentation",         FailureCategory.PERMANENT),
    ("json decode",         FailureCategory.PERMANENT),
    ("parse error",         FailureCategory.PERMANENT),
    ("not a valid",         FailureCategory.PERMANENT),
    ("unexpected token",    FailureCategory.PERMANENT),
    # CONFIG
    ("api key",             FailureCategory.CONFIG),
    ("unauthorized",        FailureCategory.CONFIG),
    ("401",                 FailureCategory.CONFIG),
    ("403",                 FailureCategory.CONFIG),
    ("permission denied",   FailureCategory.CONFIG),
    ("access denied",       FailureCategory.CONFIG),
    ("not configured",      FailureCategory.CONFIG),
    ("missing config",      FailureCategory.CONFIG),
    # RESOURCE
    ("disk full",           FailureCategory.RESOURCE),
    ("no space",            FailureCategory.RESOURCE),
    ("enospc",              FailureCategory.RESOURCE),
    ("out of memory",       FailureCategory.RESOURCE),
    ("cannot allocate",     FailureCategory.RESOURCE),
    # EXTERNAL
    ("git clone",           FailureCategory.EXTERNAL),
    ("clone failed",        FailureCategory.EXTERNAL),
    ("remote rejected",     FailureCategory.EXTERNAL),
    ("ci failed",           FailureCategory.EXTERNAL),
    ("check failed",        FailureCategory.EXTERNAL),
]

# Stage-name → default category for unclassified RuntimeError.
_STAGE_DEFAULT_MAP: dict[str, FailureCategory] = {
    "clone":        FailureCategory.EXTERNAL,
    "context":      FailureCategory.CONFIG,
    "orchestrate":  FailureCategory.PERMANENT,
}

# Recovery hint defaults per category.
_DEFAULT_HINTS: dict[FailureCategory, str] = {
    FailureCategory.TRANSIENT: "Operation timed out; retry may succeed",
    FailureCategory.PERMANENT: "Error is not retryable; check input or generated output",
    FailureCategory.CONFIG:    "Configuration error; check settings and credentials",
    FailureCategory.EXTERNAL:  "External service failure; check connectivity and upstream status",
    FailureCategory.RESOURCE:  "Resource exhausted; increase capacity or free resources",
    FailureCategory.UNKNOWN:   "Unknown failure; manual investigation required",
}


def classify_failure(
    exception: BaseException | None = None,
    *,
    message: str = "",
    stage: str = "",
    context: dict | None = None,
) -> ClassifiedError:
    """Classify a pipeline failure by exception type, message patterns, and stage.

    Classification order (first match wins):
      1. Exception-type mapping (isinstance checks against known types)
      2. Message/keyword pattern matching (case-insensitive)
      3. Stage-name hint (for RuntimeError that wasn't matched above)
      4. Default fallback → UNKNOWN
    """
    category = FailureCategory.UNKNOWN
    hint = ""
    exc_type = ""

    # ── Tier 1: exception-type mapping ────────────────────────────────────
    if exception is not None:
        exc_type = type(exception).__qualname__

        # --- MergeConflictError (extends RuntimeError) check by name ---
        # We avoid importing MergeConflictError to prevent circular deps.
        if exc_type == "MergeConflictError":
            category = FailureCategory.PERMANENT
            hint = "Merge conflict cannot be auto-resolved"

        # --- OSError with errno 28 (ENOSPC) check ---
        elif isinstance(exception, OSError) and getattr(exception, 'errno', None) == 28:
            category = FailureCategory.RESOURCE
            hint = "Disk full; free space or increase quota"

        # --- RuntimeError with git-like message → EXTERNAL ---
        elif isinstance(exception, RuntimeError) and _matches_any(
            str(exception), ["git clone", "clone failed", "remote rejected"]
        ):
            category = FailureCategory.EXTERNAL
            hint = _DEFAULT_HINTS[FailureCategory.EXTERNAL]

        # --- Generic exception-type table ---
        else:
            for exc_cls, cat, h in _EXCEPTION_TYPE_MAP:
                if isinstance(exception, exc_cls):
                    category = cat
                    hint = h
                    break

    # ── Tier 2: message-pattern matching ──────────────────────────────────
    if category == FailureCategory.UNKNOWN and message:
        msg_lower = message.lower()
        for pattern, cat in _MESSAGE_PATTERNS:
            if pattern in msg_lower:
                category = cat
                break

    # ── Tier 3: stage-name hint for unclassified RuntimeError ─────────────
    if (category == FailureCategory.UNKNOWN
            and exception is not None
            and isinstance(exception, RuntimeError)
            and stage in _STAGE_DEFAULT_MAP):
        category = _STAGE_DEFAULT_MAP[stage]

    # ── Build result ──────────────────────────────────────────────────────
    effective_message = message or (str(exception) if exception is not None else "")
    effective_hint = hint or _DEFAULT_HINTS.get(category, "")

    return ClassifiedError(
        category=category,
        source=stage,
        message=effective_message,
        exception_type=exc_type,
        recovery_hint=effective_hint,
        raw_error=effective_message,
    )


def _matches_any(text: str, keywords: list[str]) -> bool:
    """Check if text contains any keyword (case-insensitive)."""
    lower = text.lower()
    return any(kw.lower() in lower for kw in keywords)
