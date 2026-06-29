"""
telemetry/spans.py — Lightweight span/trace abstraction for the pipeline.

Provides:
  - SpanKind / SpanStatus enums
  - SpanRecord pydantic model (one completed span)
  - SpanExporter Protocol (pluggable export)
  - NullExporter, JsonlExporter, MultiExporter
  - Span async context manager (measures wall-clock time)
"""
from __future__ import annotations

import datetime
import time
import uuid
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel

from config.logging import get_logger
from telemetry.execution_log import ExecutionRecord, record_execution

logger = get_logger(__name__)


# ── Enums ────────────────────────────────────────────────────────────────────

class SpanKind(str, Enum):
    PIPELINE = "pipeline"
    STAGE = "stage"
    AGENT = "agent"
    INTERNAL = "internal"


class SpanStatus(str, Enum):
    OK = "ok"
    ERROR = "error"


# ── SpanRecord ───────────────────────────────────────────────────────────────

class SpanRecord(BaseModel):
    """The structured event emitted when a span closes."""
    span_id: str
    parent_span_id: str | None = None
    name: str
    kind: SpanKind = SpanKind.INTERNAL
    start_time: str
    end_time: str
    elapsed_seconds: float
    status: SpanStatus = SpanStatus.OK
    error: str | None = None
    metadata: dict[str, Any] = {}


# ── SpanExporter Protocol ────────────────────────────────────────────────────

class SpanExporter(Protocol):
    def export(self, record: SpanRecord) -> None:
        """Emit a completed span. Must not raise."""
        ...


# ── Concrete exporters ───────────────────────────────────────────────────────

class NullExporter:
    """No-op exporter — safe default when observability is disabled."""
    def export(self, record: SpanRecord) -> None:
        pass


class JsonlExporter:
    """Writes span events through the existing record_execution() pipeline."""
    def export(self, record: SpanRecord) -> None:
        try:
            meta = {
                "span_name": record.name,
                "span_kind": record.kind.value if record.kind else "",
                "parent_span_id": record.parent_span_id or "",
            }
            # merge caller metadata
            if record.metadata:
                meta.update(record.metadata)

            record_execution(ExecutionRecord(
                run_id="",              # caller can override via metadata["run_id"]
                event="span",
                success=(record.status == SpanStatus.OK),
                elapsed_seconds=record.elapsed_seconds,
                error=record.error,
                metadata=meta,
            ))
        except Exception as exc:
            logger.warning(f"[telemetry] JsonlExporter failed: {exc}")


class MultiExporter:
    """Delegates to multiple child exporters. One child failure doesn't affect others."""
    def __init__(self, exporters: list[SpanExporter]) -> None:
        self._exporters = list(exporters)

    def export(self, record: SpanRecord) -> None:
        for exporter in self._exporters:
            try:
                exporter.export(record)
            except Exception as exc:
                logger.debug(f"[telemetry] exporter {exporter!r} failed: {exc}")


# ── Default exporter singleton ───────────────────────────────────────────────

_default_exporter: SpanExporter = NullExporter()


def set_default_exporter(exporter: SpanExporter) -> None:
    global _default_exporter
    _default_exporter = exporter


def get_default_exporter() -> SpanExporter:
    return _default_exporter


# ── Span (async context manager) ─────────────────────────────────────────────

class Span:
    """Async context manager that measures wall-clock time and exports a SpanRecord.

    Usage:
        async with Span("stage:clone", kind=SpanKind.STAGE) as s:
            await do_work()
            # s.elapsed is available after the block exits
    """

    def __init__(
        self,
        name: str,
        kind: SpanKind = SpanKind.INTERNAL,
        parent: Span | None = None,
        exporter: SpanExporter | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._name = name
        self._kind = kind
        self._span_id = uuid.uuid4().hex[:8]
        self._parent_span_id: str | None = parent.span_id if parent else None
        self._exporter = exporter
        self._metadata = dict(metadata) if metadata else {}
        self._start_instant: float | None = None
        self._start_time_iso: str = ""
        self._elapsed: float | None = None
        self._error: str | None = None

    @property
    def span_id(self) -> str:
        return self._span_id

    @property
    def elapsed(self) -> float | None:
        """Elapsed seconds; None until the span closes."""
        return self._elapsed

    async def __aenter__(self) -> Span:
        self._start_instant = time.monotonic()
        self._start_time_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        elapsed = time.monotonic() - self._start_instant if self._start_instant else 0.0
        self._elapsed = elapsed
        end_time_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        status = SpanStatus.OK
        if exc_type is not None:
            status = SpanStatus.ERROR
            self._error = str(exc_val)

        record = SpanRecord(
            span_id=self._span_id,
            parent_span_id=self._parent_span_id,
            name=self._name,
            kind=self._kind,
            start_time=self._start_time_iso,
            end_time=end_time_iso,
            elapsed_seconds=elapsed,
            status=status,
            error=self._error,
            metadata=self._metadata,
        )

        exporter = self._exporter or get_default_exporter()
        try:
            exporter.export(record)
        except Exception as exc:
            logger.debug(f"[telemetry] Span exporter failed: {exc}")

        return False  # never suppress exceptions
