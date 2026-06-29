"""
tests/unit/test_spans.py — PR9 Span abstraction (written FIRST, before impl)

Red phase: all tests must FAIL before implementation is written.

Covers:
  1.  SpanKind enum values
  2.  SpanStatus enum values
  3.  SpanRecord model defaults
  4.  SpanRecord with full fields
  5.  Span async context manager measures elapsed time
  6.  Span creates unique span_ids
  7.  Span parent-child nesting sets parent_span_id
  8.  Span sets status=ERROR on exception, captures error message
  9.  Span does NOT suppress exceptions
  10. Span metadata is preserved in SpanRecord
  11. NullExporter.export() is no-op
  12. JsonlExporter writes span events via record_execution()
  13. MultiExporter delegates to all children
  14. MultiExporter swallows child exporter errors
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest


# ── 1 & 2. Enums ────────────────────────────────────────────────────────────

def test_span_kind_values():
    from telemetry.spans import SpanKind
    assert SpanKind.PIPELINE.value == "pipeline"
    assert SpanKind.STAGE.value == "stage"
    assert SpanKind.AGENT.value == "agent"
    assert SpanKind.INTERNAL.value == "internal"


def test_span_status_values():
    from telemetry.spans import SpanStatus
    assert SpanStatus.OK.value == "ok"
    assert SpanStatus.ERROR.value == "error"


# ── 3 & 4. SpanRecord model ─────────────────────────────────────────────────

def test_span_record_defaults():
    from telemetry.spans import SpanRecord, SpanStatus
    r = SpanRecord(
        span_id="abc123",
        name="test-span",
        start_time="2025-01-01T00:00:00Z",
        end_time="2025-01-01T00:00:01Z",
        elapsed_seconds=1.0,
    )
    assert r.span_id == "abc123"
    assert r.parent_span_id is None
    assert r.name == "test-span"
    assert r.status == SpanStatus.OK
    assert r.error is None
    assert r.metadata == {}


def test_span_record_full():
    from telemetry.spans import SpanRecord, SpanKind, SpanStatus
    r = SpanRecord(
        span_id="s1",
        parent_span_id="p1",
        name="stage:clone",
        kind=SpanKind.STAGE,
        start_time="2025-01-01T00:00:00Z",
        end_time="2025-01-01T00:00:05Z",
        elapsed_seconds=5.0,
        status=SpanStatus.ERROR,
        error="git clone failed",
        metadata={"repository": "org/repo"},
    )
    assert r.parent_span_id == "p1"
    assert r.kind == SpanKind.STAGE
    assert r.status == SpanStatus.ERROR
    assert r.error == "git clone failed"
    assert r.metadata["repository"] == "org/repo"


# ── 5. Span measures elapsed time ───────────────────────────────────────────

async def test_span_measures_elapsed_time():
    from telemetry.spans import Span

    records: list = []

    class CaptureExporter:
        def export(self, record):
            records.append(record)

    async with Span("test", exporter=CaptureExporter()) as s:
        await asyncio.sleep(0.01)

    assert len(records) == 1
    assert records[0].elapsed_seconds > 0
    assert s.elapsed is not None
    assert s.elapsed > 0


# ── 6. Unique span_ids ──────────────────────────────────────────────────────

async def test_span_creates_unique_span_ids():
    from telemetry.spans import Span

    class CaptureExporter:
        def __init__(self):
            self.records = []

        def export(self, record):
            self.records.append(record)

    cap = CaptureExporter()
    async with Span("a", exporter=cap):
        pass
    async with Span("b", exporter=cap):
        pass

    assert cap.records[0].span_id != cap.records[1].span_id
    assert len(cap.records[0].span_id) == 8  # short hex


# ── 7. Parent-child nesting ─────────────────────────────────────────────────

async def test_span_parent_child_relationship():
    from telemetry.spans import Span

    records: list = []

    class CaptureExporter:
        def export(self, record):
            records.append(record)

    async with Span("parent", exporter=CaptureExporter()) as parent:
        async with Span("child", parent=parent, exporter=CaptureExporter()):
            pass

    child_record = next(r for r in records if r.name == "child")
    parent_record = next(r for r in records if r.name == "parent")
    assert child_record.parent_span_id == parent_record.span_id


# ── 8. Span records error on exception ──────────────────────────────────────

async def test_span_records_error_on_exception():
    from telemetry.spans import Span, SpanStatus

    records: list = []

    class CaptureExporter:
        def export(self, record):
            records.append(record)

    with pytest.raises(ValueError, match="boom"):
        async with Span("failing", exporter=CaptureExporter()):
            raise ValueError("boom")

    assert len(records) == 1
    assert records[0].status == SpanStatus.ERROR
    assert "boom" in records[0].error


# ── 9. Span does not suppress exceptions ────────────────────────────────────

async def test_span_does_not_suppress_exceptions():
    from telemetry.spans import Span

    class NoopExporter:
        def export(self, record):
            pass

    with pytest.raises(RuntimeError, match="should propagate"):
        async with Span("test", exporter=NoopExporter()):
            raise RuntimeError("should propagate")


# ── 10. Metadata preserved ──────────────────────────────────────────────────

async def test_span_metadata_preserved():
    from telemetry.spans import Span

    records: list = []

    class CaptureExporter:
        def export(self, record):
            records.append(record)

    async with Span("test", exporter=CaptureExporter(), metadata={"key": "val"}) as s:
        pass

    assert records[0].metadata == {"key": "val"}


# ── 11. NullExporter ────────────────────────────────────────────────────────

def test_null_exporter_is_noop():
    from telemetry.spans import NullExporter, SpanRecord

    exporter = NullExporter()
    # Must not raise
    exporter.export(SpanRecord(
        span_id="x", name="x",
        start_time="t", end_time="t", elapsed_seconds=0,
    ))


# ── 12. JsonlExporter ───────────────────────────────────────────────────────

def test_jsonl_exporter_writes_via_record_execution():
    from telemetry.spans import JsonlExporter, SpanRecord, SpanKind

    exporter = JsonlExporter()
    record = SpanRecord(
        span_id="s1", parent_span_id="p1", name="stage:clone",
        kind=SpanKind.STAGE,
        start_time="2025-01-01T00:00:00Z",
        end_time="2025-01-01T00:00:01Z",
        elapsed_seconds=1.0,
    )

    with patch("telemetry.spans.record_execution") as mock_record:
        exporter.export(record)

    mock_record.assert_called_once()
    call_arg = mock_record.call_args[0][0]
    assert call_arg.event == "span"
    assert call_arg.run_id == ""
    assert call_arg.elapsed_seconds == 1.0
    assert call_arg.metadata["span_name"] == "stage:clone"
    assert call_arg.metadata["span_kind"] == "stage"


# ── 13 & 14. MultiExporter ──────────────────────────────────────────────────

def test_multi_exporter_delegates_to_all_children():
    from telemetry.spans import MultiExporter, SpanRecord

    calls = []

    class CountingExporter:
        def __init__(self, tag):
            self.tag = tag

        def export(self, record):
            calls.append(self.tag)

    exporter = MultiExporter([CountingExporter("a"), CountingExporter("b")])
    exporter.export(SpanRecord(
        span_id="x", name="x",
        start_time="t", end_time="t", elapsed_seconds=0,
    ))
    assert calls == ["a", "b"]


def test_multi_exporter_swallows_child_errors():
    from telemetry.spans import MultiExporter, SpanRecord

    class FailingExporter:
        def export(self, record):
            raise RuntimeError("exporter crash")

    class OkExporter:
        def __init__(self):
            self.called = False

        def export(self, record):
            self.called = True

    ok = OkExporter()
    exporter = MultiExporter([FailingExporter(), ok])
    exporter.export(SpanRecord(
        span_id="x", name="x",
        start_time="t", end_time="t", elapsed_seconds=0,
    ))
    assert ok.called  # second exporter still runs after first crashes
