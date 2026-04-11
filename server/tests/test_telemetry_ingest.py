"""Integration tests for the StreamTelemetry ingest handler.

These drive a real grpc.aio server over a TCP loopback port with an
InMemoryStore backing the pipeline. Asserts cover:
  - Hello auto-creates a session and assigns a stream_id
  - Spans arrive in the store and on the SessionBus
  - Multi-stream: two concurrent streams under one agent_id both land in
    the same logical agent row
  - Reconnect / duplicate span id is idempotent (first-write-wins)
  - Heartbeat updates last_heartbeat and publishes bus deltas
  - Payload upload in chunks with matching sha256 is persisted
  - Goodbye marks the agent DISCONNECTED once the last stream closes
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import AsyncIterator, Iterable

import grpc
import pytest
import pytest_asyncio
from google.protobuf.timestamp_pb2 import Timestamp

from harmonograf_server.bus import (
    DELTA_AGENT_STATUS,
    DELTA_AGENT_UPSERT,
    DELTA_HEARTBEAT,
    DELTA_SPAN_END,
    DELTA_SPAN_START,
    SessionBus,
)
from harmonograf_server.ingest import IngestPipeline
from harmonograf_server.pb import (
    service_pb2_grpc,
    telemetry_pb2,
    types_pb2,
)
from harmonograf_server.rpc.telemetry import TelemetryServicer
from harmonograf_server.storage import (
    AgentStatus,
    SessionStatus,
    make_store,
)


# ---- fixtures -------------------------------------------------------------


@pytest_asyncio.fixture
async def store():
    s = make_store("memory")
    await s.start()
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
async def running_server(store):
    bus = SessionBus()
    ingest = IngestPipeline(store, bus)
    servicer = TelemetryServicer(ingest)

    server = grpc.aio.server()
    service_pb2_grpc.add_HarmonografServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield {"server": server, "port": port, "bus": bus, "ingest": ingest}
    finally:
        await server.stop(grace=0.5)


@pytest_asyncio.fixture
async def channel(running_server):
    ch = grpc.aio.insecure_channel(f"127.0.0.1:{running_server['port']}")
    try:
        yield ch
    finally:
        await ch.close()


# ---- helpers --------------------------------------------------------------


def _ts(sec: float) -> Timestamp:
    t = Timestamp()
    t.seconds = int(sec)
    t.nanos = int((sec - int(sec)) * 1e9)
    return t


def _make_hello(agent_id: str = "agent-1", session_id: str = "") -> telemetry_pb2.TelemetryUp:
    hello = telemetry_pb2.Hello(
        agent_id=agent_id,
        session_id=session_id,
        name=agent_id,
        framework=types_pb2.FRAMEWORK_ADK,
        framework_version="0.1.0",
        capabilities=[types_pb2.CAPABILITY_PAUSE_RESUME],
    )
    return telemetry_pb2.TelemetryUp(hello=hello)


def _make_span_start(
    span_id: str, agent_id: str, session_id: str, start: float, name: str = "tool"
) -> telemetry_pb2.TelemetryUp:
    span = types_pb2.Span(
        id=span_id,
        session_id=session_id,
        agent_id=agent_id,
        kind=types_pb2.SPAN_KIND_TOOL_CALL,
        status=types_pb2.SPAN_STATUS_RUNNING,
        name=name,
    )
    span.start_time.CopyFrom(_ts(start))
    return telemetry_pb2.TelemetryUp(span_start=telemetry_pb2.SpanStart(span=span))


def _make_span_end(
    span_id: str, end: float, status=types_pb2.SPAN_STATUS_COMPLETED
) -> telemetry_pb2.TelemetryUp:
    se = telemetry_pb2.SpanEnd(span_id=span_id, status=status)
    se.end_time.CopyFrom(_ts(end))
    return telemetry_pb2.TelemetryUp(span_end=se)


def _make_heartbeat(buffered: int = 0) -> telemetry_pb2.TelemetryUp:
    hb = telemetry_pb2.Heartbeat(buffered_events=buffered)
    return telemetry_pb2.TelemetryUp(heartbeat=hb)


def _make_goodbye(reason: str = "") -> telemetry_pb2.TelemetryUp:
    return telemetry_pb2.TelemetryUp(goodbye=telemetry_pb2.Goodbye(reason=reason))


async def _gen(items: Iterable[telemetry_pb2.TelemetryUp], *, hold: bool = False):
    for item in items:
        yield item
    if hold:
        # Keep the generator alive so the server does not see EOF immediately.
        await asyncio.sleep(3600)


async def _drain_welcome(call) -> telemetry_pb2.Welcome:
    async for down in call:
        if down.WhichOneof("msg") == "welcome":
            return down.welcome
    raise AssertionError("stream closed before Welcome")


# ---- tests ----------------------------------------------------------------


async def test_hello_creates_session_and_welcome(running_server, channel):
    stub = service_pb2_grpc.HarmonografStub(channel)

    send_q: asyncio.Queue[telemetry_pb2.TelemetryUp] = asyncio.Queue()
    await send_q.put(_make_hello("agent-1"))

    async def requests():
        while True:
            item = await send_q.get()
            if item is None:
                return
            yield item

    call = stub.StreamTelemetry(requests())
    # Read Welcome
    down = await call.read()
    assert down.WhichOneof("msg") == "welcome"
    welcome = down.welcome
    assert welcome.accepted is True
    assert welcome.assigned_session_id.startswith("sess_")
    assert welcome.assigned_stream_id.startswith("str_")

    session_id = welcome.assigned_session_id
    store = running_server["ingest"].store
    sess = await store.get_session(session_id)
    assert sess is not None
    assert sess.status == SessionStatus.LIVE

    # Close client side
    await send_q.put(None)
    try:
        await asyncio.wait_for(call.done_writing(), timeout=1)
    except Exception:
        pass
    call.cancel()


async def test_spans_persist_and_publish_deltas(running_server, channel):
    stub = service_pb2_grpc.HarmonografStub(channel)
    bus = running_server["bus"]

    # Subscribe to the bus BEFORE sending anything. We do not yet know the
    # session_id, so we create a wildcard: subscribe after Hello lands.
    send_q: asyncio.Queue = asyncio.Queue()
    await send_q.put(_make_hello("agent-spans", session_id="sess_test_spans"))

    async def requests():
        while True:
            item = await send_q.get()
            if item is None:
                return
            yield item

    call = stub.StreamTelemetry(requests())
    down = await call.read()
    assert down.welcome.assigned_session_id == "sess_test_spans"

    sub = await bus.subscribe("sess_test_spans")

    await send_q.put(
        _make_span_start("span-1", "agent-spans", "sess_test_spans", start=100.0)
    )
    await send_q.put(_make_span_end("span-1", end=101.5))

    # Expect SPAN_START then SPAN_END on the bus
    d1 = await asyncio.wait_for(sub.queue.get(), timeout=2)
    assert d1.kind == DELTA_SPAN_START
    d2 = await asyncio.wait_for(sub.queue.get(), timeout=2)
    assert d2.kind == DELTA_SPAN_END

    # Verify storage
    store = running_server["ingest"].store
    spans = await store.get_spans("sess_test_spans")
    assert len(spans) == 1
    assert spans[0].id == "span-1"
    assert spans[0].end_time == 101.5

    await bus.unsubscribe(sub)
    await send_q.put(None)
    call.cancel()


async def test_duplicate_span_id_is_idempotent(running_server, channel):
    stub = service_pb2_grpc.HarmonografStub(channel)
    send_q: asyncio.Queue = asyncio.Queue()
    await send_q.put(_make_hello("agent-dup", session_id="sess_dup"))

    async def requests():
        while True:
            item = await send_q.get()
            if item is None:
                return
            yield item

    call = stub.StreamTelemetry(requests())
    await call.read()  # Welcome

    await send_q.put(_make_span_start("span-x", "agent-dup", "sess_dup", start=10.0))
    # Duplicate — should be a no-op
    await send_q.put(_make_span_start("span-x", "agent-dup", "sess_dup", start=10.0))
    await send_q.put(_make_heartbeat())  # flush
    await asyncio.sleep(0.1)

    store = running_server["ingest"].store
    spans = await store.get_spans("sess_dup")
    assert len(spans) == 1

    await send_q.put(None)
    call.cancel()


async def test_multi_stream_per_agent_id(running_server, channel):
    """Two concurrent StreamTelemetry RPCs with the same agent_id both
    land in one logical agent row; each gets a distinct stream_id."""
    stub = service_pb2_grpc.HarmonografStub(channel)

    q1: asyncio.Queue = asyncio.Queue()
    q2: asyncio.Queue = asyncio.Queue()

    async def reqs(q):
        while True:
            item = await q.get()
            if item is None:
                return
            yield item

    await q1.put(_make_hello("agent-multi", session_id="sess_multi"))
    await q2.put(_make_hello("agent-multi", session_id="sess_multi"))

    call1 = stub.StreamTelemetry(reqs(q1))
    call2 = stub.StreamTelemetry(reqs(q2))

    w1 = (await call1.read()).welcome
    w2 = (await call2.read()).welcome
    assert w1.assigned_stream_id != w2.assigned_stream_id
    assert w1.assigned_session_id == w2.assigned_session_id == "sess_multi"

    # Each stream emits its own span; both should end up in storage.
    await q1.put(_make_span_start("span-a", "agent-multi", "sess_multi", start=1.0))
    await q2.put(_make_span_start("span-b", "agent-multi", "sess_multi", start=2.0))
    await q1.put(_make_heartbeat())
    await q2.put(_make_heartbeat())
    await asyncio.sleep(0.15)

    store = running_server["ingest"].store
    spans = await store.get_spans("sess_multi")
    assert {s.id for s in spans} == {"span-a", "span-b"}

    # The ingest pipeline should know about two streams for this agent.
    ingest = running_server["ingest"]
    assert len(ingest.live_streams("agent-multi")) == 2

    await q1.put(None)
    await q2.put(None)
    call1.cancel()
    call2.cancel()


async def test_heartbeat_updates_agent_and_bus(running_server, channel):
    stub = service_pb2_grpc.HarmonografStub(channel)
    bus = running_server["bus"]
    send_q: asyncio.Queue = asyncio.Queue()
    await send_q.put(_make_hello("agent-hb", session_id="sess_hb"))

    async def reqs():
        while True:
            item = await send_q.get()
            if item is None:
                return
            yield item

    call = stub.StreamTelemetry(reqs())
    await call.read()  # welcome

    sub = await bus.subscribe("sess_hb")
    await send_q.put(_make_heartbeat(buffered=42))

    # Wait for the heartbeat delta on the bus.
    found_hb = False
    for _ in range(5):
        try:
            d = await asyncio.wait_for(sub.queue.get(), timeout=1)
        except asyncio.TimeoutError:
            break
        if d.kind == DELTA_HEARTBEAT:
            assert d.payload["buffered_events"] == 42
            found_hb = True
            break
    assert found_hb, "expected heartbeat delta on SessionBus"

    # Agent should be CONNECTED with a fresh last_heartbeat.
    store = running_server["ingest"].store
    agent = await store.get_agent("sess_hb", "agent-hb")
    assert agent is not None
    assert agent.status == AgentStatus.CONNECTED
    assert agent.last_heartbeat > 0

    await bus.unsubscribe(sub)
    await send_q.put(None)
    call.cancel()


async def test_payload_upload_persists_with_digest_verification(running_server, channel):
    stub = service_pb2_grpc.HarmonografStub(channel)
    send_q: asyncio.Queue = asyncio.Queue()
    await send_q.put(_make_hello("agent-pl", session_id="sess_pl"))

    async def reqs():
        while True:
            item = await send_q.get()
            if item is None:
                return
            yield item

    call = stub.StreamTelemetry(reqs())
    await call.read()

    body = b'{"prompt": "hello world"}'
    digest = hashlib.sha256(body).hexdigest()
    # Split into two chunks to exercise assembly.
    half = len(body) // 2
    await send_q.put(
        telemetry_pb2.TelemetryUp(
            payload=telemetry_pb2.PayloadUpload(
                digest=digest,
                total_size=len(body),
                mime="application/json",
                chunk=body[:half],
                last=False,
            )
        )
    )
    await send_q.put(
        telemetry_pb2.TelemetryUp(
            payload=telemetry_pb2.PayloadUpload(
                digest=digest,
                total_size=len(body),
                mime="application/json",
                chunk=body[half:],
                last=True,
            )
        )
    )
    await send_q.put(_make_heartbeat())
    await asyncio.sleep(0.1)

    store = running_server["ingest"].store
    rec = await store.get_payload(digest)
    assert rec is not None
    assert rec.bytes_ == body
    assert rec.meta.mime == "application/json"

    await send_q.put(None)
    call.cancel()


async def test_goodbye_disconnects_agent(running_server, channel):
    stub = service_pb2_grpc.HarmonografStub(channel)
    bus = running_server["bus"]
    send_q: asyncio.Queue = asyncio.Queue()
    await send_q.put(_make_hello("agent-bye", session_id="sess_bye"))

    async def reqs():
        while True:
            item = await send_q.get()
            if item is None:
                return
            yield item

    call = stub.StreamTelemetry(reqs())
    await call.read()
    sub = await bus.subscribe("sess_bye")

    await send_q.put(_make_goodbye("test"))
    await send_q.put(None)
    # Give the server a moment to process goodbye + EOF
    await asyncio.sleep(0.2)

    store = running_server["ingest"].store
    agent = await store.get_agent("sess_bye", "agent-bye")
    assert agent is not None
    assert agent.status == AgentStatus.DISCONNECTED

    # Should have seen an agent_status delta
    saw_disconnect = False
    while not sub.queue.empty():
        d = sub.queue.get_nowait()
        if d.kind == DELTA_AGENT_STATUS and d.payload.get("status") == AgentStatus.DISCONNECTED:
            saw_disconnect = True
            break
    assert saw_disconnect

    await bus.unsubscribe(sub)
    try:
        call.cancel()
    except Exception:
        pass


async def test_first_message_not_hello_rejected(running_server, channel):
    stub = service_pb2_grpc.HarmonografStub(channel)
    send_q: asyncio.Queue = asyncio.Queue()
    # Wrong first message: a SpanStart with no Hello
    await send_q.put(
        _make_span_start("span-x", "agent-?", "sess-?", start=0.0)
    )
    await send_q.put(None)

    async def reqs():
        while True:
            item = await send_q.get()
            if item is None:
                return
            yield item

    call = stub.StreamTelemetry(reqs())
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        async for _ in call:
            pass
    assert ei.value.code() == grpc.StatusCode.INVALID_ARGUMENT
