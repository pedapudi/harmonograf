"""Tests for the goldfive-event ``invocation_cancelled`` ingest path
post goldfive#262 (harmonograf Wave 2 / A8).

History
-------
Originally (PR #187) the cancel rode on a dedicated
``TelemetryUp.invocation_cancelled`` oneof slot carrying a placeholder
``harmonograf.v1.InvocationCancelled`` message, because goldfive was
still shipping the event as a dict envelope. goldfive#262 promoted the
event to a typed ``goldfive.v1.InvocationCancelled`` payload variant on
the standard ``Event`` envelope; this PR removed the harmonograf
placeholder and the dedicated TelemetryUp slot.

Coverage now
------------
* The ingest dispatcher routes ``Event.invocation_cancelled`` through
  :meth:`IngestPipeline._handle_goldfive_event` → ``_on_invocation_cancelled``
  → ``SessionBus.publish_invocation_cancelled``. Live WatchSession
  subscribers see ``DELTA_INVOCATION_CANCELLED`` with the same payload
  shape as before.
* The event is persisted in the ``goldfive_events`` table like every
  other goldfive Event variant — reconnect replay reads it back from
  storage instead of from the in-memory ring (which was removed).
* Optional fields (drift_id, tool_name, …) propagate verbatim;
  envelope metadata (run_id, sequence, emitted_at) reads off the
  parent ``Event``, not the payload.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from google.protobuf import timestamp_pb2

from harmonograf_server.bus import (
    DELTA_INVOCATION_CANCELLED,
    SessionBus,
)
from harmonograf_server.ingest import IngestPipeline, StreamContext
from harmonograf_server.pb import telemetry_pb2  # noqa: F401 — grafts goldfive.v1
from harmonograf_server.storage import (
    Session,
    SessionStatus,
    make_store,
)

from goldfive.v1 import events_pb2 as goldfive_events_pb2  # noqa: E402


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


def _make_cancel_event(
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
    emitted_at: timestamp_pb2.Timestamp | None = None,
) -> goldfive_events_pb2.Event:
    """Build a goldfive Event with the typed
    ``invocation_cancelled`` payload — matches the wire shape produced
    by goldfive's ``invocation_cancelled_event`` factory."""
    evt = goldfive_events_pb2.Event(
        run_id=run_id,
        sequence=sequence,
        session_id=session_id,
    )
    if emitted_at is not None:
        evt.emitted_at.CopyFrom(emitted_at)
    evt.invocation_cancelled.invocation_id = invocation_id
    evt.invocation_cancelled.agent_name = agent_name
    evt.invocation_cancelled.reason = reason
    evt.invocation_cancelled.severity = severity
    evt.invocation_cancelled.drift_id = drift_id
    evt.invocation_cancelled.drift_kind = drift_kind
    evt.invocation_cancelled.detail = detail
    evt.invocation_cancelled.tool_name = tool_name
    return evt


def _wrap(event: goldfive_events_pb2.Event):
    return telemetry_pb2.TelemetryUp(goldfive_event=event)


@pytest.mark.asyncio
async def test_invocation_cancelled_publishes_delta(pipeline, store):
    pipe, bus, _ = pipeline
    await _ensure_session(store)
    sub = await bus.subscribe("sess_c")
    evt = _make_cancel_event()
    await pipe.handle_message(_stream_ctx(), _wrap(evt))
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
async def test_invocation_cancelled_persisted_in_goldfive_events(
    pipeline, store
):
    """The cancel lands in the same ``goldfive_events`` table as every
    other typed goldfive event. The dedicated in-memory ring that #187
    used (because the dict-sourced shape couldn't be persisted as
    proto bytes) is gone; reconnect replay reads from storage now."""
    pipe, _, _ = pipeline
    await _ensure_session(store)
    e1 = _make_cancel_event(invocation_id="inv-A", sequence=1)
    e2 = _make_cancel_event(
        invocation_id="inv-B",
        sequence=2,
        reason="user_steer",
        drift_kind="user_steer",
        detail="user asked to stop",
    )
    await pipe.handle_message(_stream_ctx(), _wrap(e1))
    await pipe.handle_message(_stream_ctx(), _wrap(e2))
    records = await store.list_goldfive_events(
        "sess_c", kind="invocation_cancelled"
    )
    assert len(records) == 2
    parsed = [
        goldfive_events_pb2.Event.FromString(r.payload_bytes) for r in records
    ]
    invocation_ids = sorted(p.invocation_cancelled.invocation_id for p in parsed)
    assert invocation_ids == ["inv-A", "inv-B"]
    # Reasons round-trip — confirms the persisted bytes carry the typed
    # payload, not just envelope metadata.
    reasons = {p.invocation_cancelled.reason for p in parsed}
    assert reasons == {"drift", "user_steer"}


@pytest.mark.asyncio
async def test_invocation_cancelled_ring_attribute_removed(pipeline):
    """Migration regression: the per-session in-memory ring + helper
    method were removed alongside the dict-conversion path. Storage is
    now the single source of truth for replay."""
    pipe, _, _ = pipeline
    assert not hasattr(pipe, "_invocation_cancels_by_session")
    assert not hasattr(pipe, "invocation_cancels_for_session")


@pytest.mark.asyncio
async def test_invocation_cancelled_tool_name_field_propagates(pipeline, store):
    pipe, bus, _ = pipeline
    await _ensure_session(store)
    sub = await bus.subscribe("sess_c")
    evt = _make_cancel_event(tool_name="search_web")
    await pipe.handle_message(_stream_ctx(), _wrap(evt))
    delta = sub.queue.get_nowait()
    assert delta.payload["tool_name"] == "search_web"


@pytest.mark.asyncio
async def test_invocation_cancelled_empty_emitted_at_uses_recorded_at(
    pipeline, store
):
    """An envelope with emitted_at unset still has a usable timestamp —
    the bus carries both fields and the frontend.py translator picks
    the fallback."""
    pipe, bus, _ = pipeline
    await _ensure_session(store)
    sub = await bus.subscribe("sess_c")
    evt = _make_cancel_event()  # emitted_at not populated
    assert not evt.HasField("emitted_at")
    await pipe.handle_message(_stream_ctx(), _wrap(evt))
    delta = sub.queue.get_nowait()
    assert delta.payload["emitted_at"] is None
    assert delta.payload["recorded_at"] == 1_000_000.0


@pytest.mark.asyncio
async def test_invocation_cancelled_emitted_at_propagates(pipeline, store):
    pipe, bus, _ = pipeline
    await _ensure_session(store)
    sub = await bus.subscribe("sess_c")
    ts = timestamp_pb2.Timestamp(seconds=1_700_000_000, nanos=123_000_000)
    evt = _make_cancel_event(emitted_at=ts)
    await pipe.handle_message(_stream_ctx(), _wrap(evt))
    delta = sub.queue.get_nowait()
    assert delta.payload["emitted_at"] == pytest.approx(1_700_000_000.123)
