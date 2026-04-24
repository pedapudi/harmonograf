"""Tests for the ``TelemetryUp.invocation_cancelled`` ingest path
(goldfive#251 Stream C / harmonograf PR).

Harmonograf's ingest pipeline dispatches the new oneof variant to
``_handle_invocation_cancelled`` which (a) stashes a replay record on
the per-session ring and (b) publishes a ``DELTA_INVOCATION_CANCELLED``
bus delta so live WatchSession subscribers see the marker.

Unlike ``goldfive_event`` the cancel is not persisted in the
``goldfive_events`` table — the event originates as a dict on the
goldfive side and the ingest path for that table is strictly
proto-serialized. Reconnect replay rides on the in-memory ring.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from harmonograf_server.bus import (
    DELTA_INVOCATION_CANCELLED,
    SessionBus,
)
from harmonograf_server.ingest import IngestPipeline, StreamContext
from harmonograf_server.pb import telemetry_pb2
from harmonograf_server.storage import (
    Session,
    SessionStatus,
    make_store,
)


@pytest_asyncio.fixture
async def store():
    s = make_store("memory")
    await s.start()
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
async def pipeline(store):
    bus = SessionBus()
    pipe = IngestPipeline(store, bus, now_fn=lambda: 1_000_000.0)
    yield pipe, bus, store


def _stream_ctx(session_id: str = "sess_c") -> StreamContext:
    return StreamContext(
        stream_id="str_test",
        agent_id="agent_c",
        session_id=session_id,
        connected_at=1000.0,
        last_heartbeat=1000.0,
        seen_routes={(session_id, "agent_c")},
    )


async def _ensure_session(store, session_id: str = "sess_c") -> None:
    await store.create_session(
        Session(
            id=session_id,
            title=session_id,
            created_at=1.0,
            status=SessionStatus.LIVE,
        )
    )


def _make_cancel(
    *,
    run_id: str = "run-1",
    sequence: int = 5,
    session_id: str = "sess_c",
    agent_name: str = "presentation-orchestrated-abc:researcher_agent",
    reason: str = "drift",
    severity: str = "critical",
    drift_id: str = "drift-uuid-1",
    drift_kind: str = "off_topic",
    detail: str = "assistant veered off task",
    tool_name: str = "",
    invocation_id: str = "inv-42",
) -> telemetry_pb2.InvocationCancelled:
    return telemetry_pb2.InvocationCancelled(
        run_id=run_id,
        sequence=sequence,
        session_id=session_id,
        invocation_id=invocation_id,
        agent_name=agent_name,
        reason=reason,
        severity=severity,
        drift_id=drift_id,
        drift_kind=drift_kind,
        detail=detail,
        tool_name=tool_name,
    )


def _wrap(msg: telemetry_pb2.InvocationCancelled) -> telemetry_pb2.TelemetryUp:
    return telemetry_pb2.TelemetryUp(invocation_cancelled=msg)


@pytest.mark.asyncio
async def test_invocation_cancelled_publishes_delta(pipeline):
    pipe, bus, _ = pipeline
    sub = await bus.subscribe("sess_c")
    msg = _make_cancel()
    await pipe.handle_message(_stream_ctx(), _wrap(msg))
    delta = sub.queue.get_nowait()
    assert delta.kind == DELTA_INVOCATION_CANCELLED
    p = delta.payload
    assert p["run_id"] == "run-1"
    assert p["sequence"] == 5
    assert p["invocation_id"] == "inv-42"
    assert p["agent_name"] == "presentation-orchestrated-abc:researcher_agent"
    assert p["reason"] == "drift"
    assert p["severity"] == "critical"
    assert p["drift_id"] == "drift-uuid-1"
    assert p["drift_kind"] == "off_topic"
    assert p["detail"] == "assistant veered off task"
    # recorded_at stamped from the ingest-side now_fn (1_000_000).
    assert p["recorded_at"] == 1_000_000.0


@pytest.mark.asyncio
async def test_invocation_cancelled_stashed_on_replay_ring(pipeline):
    pipe, _, _ = pipeline
    msg1 = _make_cancel(invocation_id="inv-A", sequence=1)
    msg2 = _make_cancel(
        invocation_id="inv-B",
        sequence=2,
        reason="user_steer",
        drift_kind="user_steer",
        detail="user asked to stop",
    )
    await pipe.handle_message(_stream_ctx(), _wrap(msg1))
    await pipe.handle_message(_stream_ctx(), _wrap(msg2))
    records = pipe.invocation_cancels_for_session("sess_c")
    assert len(records) == 2
    # Ordered oldest-first.
    assert records[0]["invocation_id"] == "inv-A"
    assert records[1]["invocation_id"] == "inv-B"
    assert records[1]["reason"] == "user_steer"
    assert records[1]["drift_kind"] == "user_steer"


@pytest.mark.asyncio
async def test_invocation_cancelled_ring_bounded(pipeline):
    """The ring tops out at the configured max — oldest dropped."""
    pipe, _, _ = pipeline
    # Tighten the ring for the test so we don't emit 600 events.
    pipe._invocation_cancel_ring_max = 3
    for i in range(5):
        await pipe.handle_message(
            _stream_ctx(), _wrap(_make_cancel(invocation_id=f"inv-{i}", sequence=i))
        )
    records = pipe.invocation_cancels_for_session("sess_c")
    assert [r["invocation_id"] for r in records] == ["inv-2", "inv-3", "inv-4"]


@pytest.mark.asyncio
async def test_invocation_cancelled_tool_name_field_propagates(pipeline):
    pipe, bus, _ = pipeline
    sub = await bus.subscribe("sess_c")
    msg = _make_cancel(tool_name="search_web")
    await pipe.handle_message(_stream_ctx(), _wrap(msg))
    delta = sub.queue.get_nowait()
    assert delta.payload["tool_name"] == "search_web"


@pytest.mark.asyncio
async def test_invocation_cancelled_empty_emitted_at_uses_recorded_at(pipeline):
    """An envelope with emitted_at unset still has a usable timestamp —
    the bus carries both fields and the frontend.py translator picks
    the fallback."""
    pipe, bus, _ = pipeline
    sub = await bus.subscribe("sess_c")
    msg = _make_cancel()  # emitted_at not populated
    assert not msg.HasField("emitted_at")
    await pipe.handle_message(_stream_ctx(), _wrap(msg))
    delta = sub.queue.get_nowait()
    assert delta.payload["emitted_at"] is None
    assert delta.payload["recorded_at"] == 1_000_000.0
