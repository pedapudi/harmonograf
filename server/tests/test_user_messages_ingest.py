"""Tests for the ``TelemetryUp.user_message`` ingest path
(harmonograf user-message UX gap).

Mirrors the structure of ``test_refine_events_ingest.py``: each new
envelope kind is dispatched, stashed on a per-session ring for
reconnect replay, and published onto the bus as a typed delta.

Not persisted in any storage table — the records ride on an in-memory
ring class identical to the refine_attempted / refine_failed rings.
Replay during WatchSession initial burst keeps every operator turn
visible across reconnects without a sqlite migration.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from harmonograf_server.bus import DELTA_USER_MESSAGE, SessionBus
from harmonograf_server.ingest import IngestPipeline, StreamContext
from harmonograf_server.pb import telemetry_pb2
from harmonograf_server.storage import make_store


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


def _stream_ctx(session_id: str = "sess_um") -> StreamContext:
    return StreamContext(
        stream_id="str_um",
        agent_id="agent_um",
        session_id=session_id,
        connected_at=1000.0,
        last_heartbeat=1000.0,
        seen_routes={(session_id, "agent_um")},
    )


def _make_user_message(
    *,
    run_id: str = "run-1",
    sequence: int = 7,
    session_id: str = "sess_um",
    content: str = "forget solar panels. tell me about solar flares.",
    author: str = "alice",
    mid_turn: bool = False,
    invocation_id: str = "",
) -> telemetry_pb2.UserMessageReceived:
    return telemetry_pb2.UserMessageReceived(
        run_id=run_id,
        sequence=sequence,
        session_id=session_id,
        content=content,
        author=author,
        mid_turn=mid_turn,
        invocation_id=invocation_id,
    )


def _wrap(msg: telemetry_pb2.UserMessageReceived) -> telemetry_pb2.TelemetryUp:
    return telemetry_pb2.TelemetryUp(user_message=msg)


# --- Tests -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_message_publishes_delta(pipeline):
    """A well-formed UserMessageReceived envelope publishes a
    DELTA_USER_MESSAGE on the bus with every payload field carried
    through."""
    pipe, bus, _ = pipeline
    sub = await bus.subscribe("sess_um")
    await pipe.handle_message(_stream_ctx(), _wrap(_make_user_message()))
    delta = sub.queue.get_nowait()
    assert delta.kind == DELTA_USER_MESSAGE
    p = delta.payload
    assert p["run_id"] == "run-1"
    assert p["sequence"] == 7
    assert p["content"] == "forget solar panels. tell me about solar flares."
    assert p["author"] == "alice"
    assert p["mid_turn"] is False
    assert p["invocation_id"] == ""
    assert p["recorded_at"] == 1_000_000.0


@pytest.mark.asyncio
async def test_mid_turn_message_carries_invocation_id(pipeline):
    """Mid-turn messages set ``mid_turn=True`` and carry the bare
    ADK invocation id."""
    pipe, bus, _ = pipeline
    sub = await bus.subscribe("sess_um")
    msg = _make_user_message(
        content="interject!",
        mid_turn=True,
        invocation_id="inv-42",
    )
    await pipe.handle_message(_stream_ctx(), _wrap(msg))
    delta = sub.queue.get_nowait()
    assert delta.payload["mid_turn"] is True
    assert delta.payload["invocation_id"] == "inv-42"


@pytest.mark.asyncio
async def test_messages_for_session_replay_order(pipeline):
    """The ingest ring exposes per-session records in arrival order so
    WatchSession initial-burst replay re-delivers every operator turn
    chronologically."""
    pipe, _, _ = pipeline
    for i in range(3):
        await pipe.handle_message(
            _stream_ctx(),
            _wrap(
                _make_user_message(
                    sequence=i,
                    content=f"turn {i}",
                )
            ),
        )
    msgs = pipe.user_messages_for_session("sess_um")
    assert [m["content"] for m in msgs] == ["turn 0", "turn 1", "turn 2"]


@pytest.mark.asyncio
async def test_user_message_ring_bounded(pipeline):
    """The ring caps at the configured max with oldest-dropped — same
    bound contract as the refine rings."""
    pipe, _, _ = pipeline
    pipe._user_message_ring_max = 3
    for i in range(5):
        await pipe.handle_message(
            _stream_ctx(),
            _wrap(_make_user_message(sequence=i, content=f"msg-{i}")),
        )
    msgs = pipe.user_messages_for_session("sess_um")
    assert [m["content"] for m in msgs] == ["msg-2", "msg-3", "msg-4"]


@pytest.mark.asyncio
async def test_empty_emitted_at_uses_recorded_at_fallback(pipeline):
    """When the envelope's emitted_at is not populated the ingest-side
    recorded_at carries the timestamp; the bus payload leaves
    emitted_at=None so the rpc translator's fallback path activates."""
    pipe, bus, _ = pipeline
    sub = await bus.subscribe("sess_um")
    msg = _make_user_message()
    assert not msg.HasField("emitted_at")
    await pipe.handle_message(_stream_ctx(), _wrap(msg))
    delta = sub.queue.get_nowait()
    assert delta.payload["emitted_at"] is None
    assert delta.payload["recorded_at"] == 1_000_000.0


@pytest.mark.asyncio
async def test_default_author_when_payload_omits_it(pipeline):
    """When the wire envelope's author field is empty string the
    ingest substitutes ``"user"`` so frontends always have a non-empty
    author label."""
    pipe, bus, _ = pipeline
    sub = await bus.subscribe("sess_um")
    msg = _make_user_message(author="")
    await pipe.handle_message(_stream_ctx(), _wrap(msg))
    delta = sub.queue.get_nowait()
    assert delta.payload["author"] == "user"
