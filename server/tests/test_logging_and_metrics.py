"""Tests for JSON log formatter and the metrics loop."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time

import pytest
import pytest_asyncio

from harmonograf_server.bus import SessionBus
from harmonograf_server.ingest import IngestPipeline
from harmonograf_server.logging_setup import JSONFormatter, configure_logging
from harmonograf_server.metrics import metrics_loop
from harmonograf_server.storage import (
    Agent,
    AgentStatus,
    Framework,
    Session,
    Span,
    SpanKind,
    SpanStatus,
    make_store,
)


# ---- JSONFormatter -------------------------------------------------------


def _format(record: logging.LogRecord) -> dict:
    return json.loads(JSONFormatter().format(record))


def test_json_formatter_basic_fields():
    rec = logging.LogRecord(
        name="foo", level=logging.INFO, pathname=__file__, lineno=1,
        msg="hello %s", args=("world",), exc_info=None,
    )
    out = _format(rec)
    assert out["level"] == "INFO"
    assert out["logger"] == "foo"
    assert out["msg"] == "hello world"
    assert isinstance(out["ts"], float)


def test_json_formatter_extra_fields_merged():
    rec = logging.LogRecord(
        name="foo", level=logging.INFO, pathname=__file__, lineno=1,
        msg="m", args=(), exc_info=None,
    )
    rec.metric = True
    rec.sessions = 3
    out = _format(rec)
    assert out["metric"] is True
    assert out["sessions"] == 3


def test_json_formatter_stringifies_nonserializable_extras():
    rec = logging.LogRecord(
        name="foo", level=logging.INFO, pathname=__file__, lineno=1,
        msg="m", args=(), exc_info=None,
    )
    rec.thing = object()  # not json-serializable
    out = _format(rec)
    assert "thing" in out
    assert isinstance(out["thing"], str)


def test_configure_logging_installs_single_handler():
    configure_logging("DEBUG", "json")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JSONFormatter)
    # Switching to text should replace, not append.
    configure_logging("INFO", "text")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert not isinstance(root.handlers[0].formatter, JSONFormatter)


# ---- metrics_loop --------------------------------------------------------


@pytest_asyncio.fixture
async def pipeline():
    store = make_store("memory")
    await store.start()
    bus = SessionBus()
    ingest = IngestPipeline(store, bus)
    try:
        yield ingest, store
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_metrics_loop_emits_snapshot(pipeline, caplog):
    ingest, store = pipeline
    now = time.time()
    await store.create_session(Session(id="sess", title="t", created_at=now))
    await store.register_agent(
        Agent(
            id="a", session_id="sess", name="a",
            framework=Framework.CUSTOM, capabilities=[],
            connected_at=now, last_heartbeat=now, status=AgentStatus.CONNECTED,
        )
    )
    await store.append_span(
        Span(
            id="sp", session_id="sess", agent_id="a",
            kind=SpanKind.TOOL_CALL, name="t",
            start_time=now, end_time=now + 0.01,
            status=SpanStatus.COMPLETED,
        )
    )

    with caplog.at_level(logging.INFO, logger="harmonograf_server.metrics"):
        task = asyncio.create_task(metrics_loop(ingest, store, 0.03))
        await asyncio.sleep(0.08)
        task.cancel()
        await task

    msgs = [r for r in caplog.records if r.name == "harmonograf_server.metrics"]
    assert msgs, "expected at least one metrics record"
    r = msgs[0]
    assert getattr(r, "metric", False) is True
    assert getattr(r, "sessions") == 1
    assert getattr(r, "spans") == 1
    assert getattr(r, "active_streams") == 0


@pytest.mark.asyncio
async def test_metrics_loop_zero_interval_is_noop(pipeline):
    ingest, store = pipeline
    await asyncio.wait_for(metrics_loop(ingest, store, 0.0), timeout=0.5)
