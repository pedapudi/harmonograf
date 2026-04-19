"""Extensive IngestPipeline tests — drive the pipeline in-process.

No gRPC stack: feed synthetic TelemetryUp messages into the pipeline
directly. Exercises Hello → span_start → span_end, payload assembly,
control ack forwarding, task plan + task binding via hgraf.task_id,
heartbeat progress/stuck tracking, and goodbye semantics.
"""

from __future__ import annotations

import asyncio
import hashlib
import time

import pytest
import pytest_asyncio
from google.protobuf.timestamp_pb2 import Timestamp

from harmonograf_server.bus import (
    DELTA_AGENT_STATUS,
    DELTA_AGENT_UPSERT,
    DELTA_HEARTBEAT,
    DELTA_SPAN_END,
    DELTA_SPAN_START,
    DELTA_SPAN_UPDATE,
    DELTA_TASK_REPORT,
    SessionBus,
)
from goldfive.pb.goldfive.v1 import control_pb2 as gf_control_pb2

from harmonograf_server.ingest import IngestPipeline
from harmonograf_server.pb import telemetry_pb2, types_pb2
from harmonograf_server.storage import (
    AgentStatus,
    SessionStatus,
    SpanStatus,
    TaskStatus,
    make_store,
)


# ---- helpers --------------------------------------------------------------


def _ts(sec: float) -> Timestamp:
    t = Timestamp()
    t.seconds = int(sec)
    t.nanos = int((sec - int(sec)) * 1e9)
    return t


def _hello(agent_id="a1", session_id="sess_t", name="", framework_version="") -> telemetry_pb2.Hello:
    return telemetry_pb2.Hello(
        agent_id=agent_id,
        session_id=session_id,
        name=name or agent_id,
        framework=types_pb2.FRAMEWORK_ADK,
        framework_version=framework_version,
        capabilities=[types_pb2.CAPABILITY_PAUSE_RESUME],
    )


def _span_msg(span_id: str, *, agent_id="", session_id="", start=100.0,
              kind=types_pb2.SPAN_KIND_TOOL_CALL, attrs=None) -> telemetry_pb2.TelemetryUp:
    span = types_pb2.Span(
        id=span_id,
        session_id=session_id,
        agent_id=agent_id,
        kind=kind,
        status=types_pb2.SPAN_STATUS_RUNNING,
        name=f"span-{span_id}",
    )
    span.start_time.CopyFrom(_ts(start))
    if attrs:
        for k, v in attrs.items():
            span.attributes[k].string_value = v
    return telemetry_pb2.TelemetryUp(span_start=telemetry_pb2.SpanStart(span=span))


def _span_end(span_id: str, *, end=110.0,
              status=types_pb2.SPAN_STATUS_COMPLETED) -> telemetry_pb2.TelemetryUp:
    se = telemetry_pb2.SpanEnd(span_id=span_id, status=status)
    se.end_time.CopyFrom(_ts(end))
    return telemetry_pb2.TelemetryUp(span_end=se)


def _hb(buffered=0, progress=0, activity="") -> telemetry_pb2.TelemetryUp:
    return telemetry_pb2.TelemetryUp(
        heartbeat=telemetry_pb2.Heartbeat(
            buffered_events=buffered,
            progress_counter=progress,
            current_activity=activity,
        )
    )


def _goodbye(reason="bye") -> telemetry_pb2.TelemetryUp:
    return telemetry_pb2.TelemetryUp(goodbye=telemetry_pb2.Goodbye(reason=reason))


class _AckSink:
    """Fake ControlAckSink that records every ack it is told about."""

    def __init__(self):
        self.acks: list[tuple[gf_control_pb2.ControlAck, str]] = []

    def record_ack(self, ack, *, stream_id=None):
        self.acks.append((ack, stream_id or ""))

    def register_alias(self, sub, stream):
        pass


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
async def pipeline(store):
    bus = SessionBus()
    sink = _AckSink()
    clock = [1_000_000.0]

    def now():
        return clock[0]

    pipe = IngestPipeline(store, bus, control_sink=sink, now_fn=now)
    pipe._sink = sink  # type: ignore[attr-defined]
    pipe._clock = clock  # type: ignore[attr-defined]
    yield pipe


# ---- tests ----------------------------------------------------------------


async def test_handle_hello_rejects_missing_agent_id(pipeline):
    with pytest.raises(ValueError):
        await pipeline.handle_hello(telemetry_pb2.Hello())


async def test_handle_hello_rejects_invalid_session_id(pipeline):
    with pytest.raises(ValueError):
        await pipeline.handle_hello(_hello(session_id="bad id with spaces!!"))


async def test_handle_hello_auto_generates_session_id(pipeline):
    ctx, sess = await pipeline.handle_hello(_hello(session_id=""))
    assert sess.id.startswith("sess_")
    assert sess.status == SessionStatus.LIVE
    assert ctx.stream_id.startswith("str_")


async def test_handle_hello_reuses_existing_session(pipeline, store):
    await pipeline.handle_hello(_hello(agent_id="a1", session_id="sess_reuse"))
    ctx2, sess2 = await pipeline.handle_hello(_hello(agent_id="a2", session_id="sess_reuse"))
    assert sess2.id == "sess_reuse"
    agents = await store.list_agents_for_session("sess_reuse")
    assert {a.id for a in agents} == {"a1", "a2"}


async def test_span_start_persists_and_publishes(pipeline, store):
    ctx, sess = await pipeline.handle_hello(_hello(session_id="sess_sp"))
    sub = await pipeline.bus.subscribe("sess_sp")
    await pipeline.handle_message(ctx, _span_msg("sp1", session_id="sess_sp"))
    kinds = set()
    while not sub.queue.empty():
        kinds.add(sub.queue.get_nowait().kind)
    assert DELTA_SPAN_START in kinds
    spans = await store.get_spans("sess_sp")
    assert {s.id for s in spans} == {"sp1"}


async def test_span_start_dedup_fast_path(pipeline, store):
    ctx, _ = await pipeline.handle_hello(_hello(session_id="sess_dup"))
    await pipeline.handle_message(ctx, _span_msg("sp1", session_id="sess_dup"))
    await pipeline.handle_message(ctx, _span_msg("sp1", session_id="sess_dup"))
    spans = await store.get_spans("sess_dup")
    assert len(spans) == 1


async def test_span_start_requires_id(pipeline):
    ctx, _ = await pipeline.handle_hello(_hello(session_id="sess_noid"))
    bad = types_pb2.Span(kind=types_pb2.SPAN_KIND_TOOL_CALL)
    msg = telemetry_pb2.TelemetryUp(span_start=telemetry_pb2.SpanStart(span=bad))
    with pytest.raises(ValueError):
        await pipeline.handle_message(ctx, msg)


async def test_span_end_sets_status_and_bus_delta(pipeline, store):
    ctx, _ = await pipeline.handle_hello(_hello(session_id="sess_end"))
    sub = await pipeline.bus.subscribe("sess_end")
    await pipeline.handle_message(ctx, _span_msg("sp1"))
    await pipeline.handle_message(ctx, _span_end("sp1", end=110.0))
    stored = await store.get_span("sp1")
    assert stored.status == SpanStatus.COMPLETED
    assert stored.end_time == 110.0
    # Drain bus; should contain SPAN_END.
    kinds = set()
    while not sub.queue.empty():
        kinds.add(sub.queue.get_nowait().kind)
    assert DELTA_SPAN_END in kinds


async def test_span_end_for_unknown_span_is_noop(pipeline):
    ctx, _ = await pipeline.handle_hello(_hello(session_id="sess_unk"))
    # Does not raise.
    await pipeline.handle_message(ctx, _span_end("ghost"))


async def test_span_update_status_and_attributes(pipeline, store):
    ctx, _ = await pipeline.handle_hello(_hello(session_id="sess_upd"))
    await pipeline.handle_message(ctx, _span_msg("sp1"))
    upd = telemetry_pb2.SpanUpdate(span_id="sp1", status=types_pb2.SPAN_STATUS_RUNNING)
    upd.attributes["k"].string_value = "v"
    await pipeline.handle_message(ctx, telemetry_pb2.TelemetryUp(span_update=upd))
    sp = await store.get_span("sp1")
    assert sp.attributes.get("k") == "v"


async def test_per_span_overrides_auto_register_route(pipeline, store):
    ctx, _ = await pipeline.handle_hello(_hello(agent_id="host", session_id="sess_multi"))
    msg = _span_msg("sp1", agent_id="sub-agent", session_id="sess_multi")
    await pipeline.handle_message(ctx, msg)
    assert await store.get_agent("sess_multi", "sub-agent") is not None


async def test_payload_upload_end_to_end(pipeline, store):
    ctx, _ = await pipeline.handle_hello(_hello(session_id="sess_pl"))
    data = b"hello payload upload"
    digest = hashlib.sha256(data).hexdigest()
    half = len(data) // 2
    for chunk, last in ((data[:half], False), (data[half:], True)):
        up = telemetry_pb2.PayloadUpload(
            digest=digest,
            total_size=len(data),
            mime="text/plain",
            chunk=chunk,
            last=last,
        )
        await pipeline.handle_message(ctx, telemetry_pb2.TelemetryUp(payload=up))
    rec = await store.get_payload(digest)
    assert rec is not None
    assert rec.bytes_ == data


async def test_payload_digest_mismatch_raises(pipeline):
    ctx, _ = await pipeline.handle_hello(_hello(session_id="sess_mm"))
    up = telemetry_pb2.PayloadUpload(
        digest="deadbeef" * 8,
        total_size=3,
        mime="text/plain",
        chunk=b"abc",
        last=True,
    )
    with pytest.raises(ValueError):
        await pipeline.handle_message(ctx, telemetry_pb2.TelemetryUp(payload=up))


async def test_payload_evicted_clears_assembler(pipeline):
    ctx, _ = await pipeline.handle_hello(_hello(session_id="sess_ev"))
    # Seed one chunk, then mark evicted.
    partial = telemetry_pb2.PayloadUpload(
        digest="x" * 64, total_size=10, mime="text/plain", chunk=b"abcd", last=False
    )
    await pipeline.handle_message(ctx, telemetry_pb2.TelemetryUp(payload=partial))
    evict = telemetry_pb2.PayloadUpload(digest="x" * 64, evicted=True)
    await pipeline.handle_message(ctx, telemetry_pb2.TelemetryUp(payload=evict))
    assert "x" * 64 not in ctx.payloads


async def test_payload_missing_digest_raises(pipeline):
    ctx, _ = await pipeline.handle_hello(_hello(session_id="sess_bd"))
    up = telemetry_pb2.PayloadUpload(total_size=0, mime="text/plain", last=True)
    with pytest.raises(ValueError):
        await pipeline.handle_message(ctx, telemetry_pb2.TelemetryUp(payload=up))


async def test_heartbeat_updates_last_heartbeat_and_connected(pipeline, store):
    ctx, _ = await pipeline.handle_hello(_hello(session_id="sess_hb"))
    pipeline._clock[0] = 1_000_100.0  # advance clock
    await pipeline.handle_message(ctx, _hb(buffered=5))
    agent = await store.get_agent("sess_hb", "a1")
    assert agent.status == AgentStatus.CONNECTED
    assert agent.last_heartbeat == 1_000_100.0


async def test_heartbeat_stuck_detection_fires_after_threshold(pipeline):
    ctx, _ = await pipeline.handle_hello(_hello(session_id="sess_stuck"))
    sub = await pipeline.bus.subscribe("sess_stuck")
    # First hb establishes baseline; next three identical → stuck (count>=3).
    for _ in range(4):
        await pipeline.handle_message(ctx, _hb(progress=5, activity="loop"))
    # Check ctx reflects stuck flag.
    assert ctx.is_stuck is True
    kinds = []
    while not sub.queue.empty():
        kinds.append(sub.queue.get_nowait().kind)
    # At least one AGENT_STATUS delta with stuck=True is published on transition.
    assert DELTA_AGENT_STATUS in kinds


async def test_heartbeat_progress_change_clears_stuck(pipeline):
    ctx, _ = await pipeline.handle_hello(_hello(session_id="sess_prog"))
    for _ in range(4):
        await pipeline.handle_message(ctx, _hb(progress=1))
    assert ctx.is_stuck is True
    await pipeline.handle_message(ctx, _hb(progress=2))
    assert ctx.is_stuck is False


async def test_control_ack_forwarded_to_sink(pipeline):
    ctx, _ = await pipeline.handle_hello(_hello(session_id="sess_ack"))
    ack = gf_control_pb2.ControlAck(
        control_id="ctl-1",
        result=gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS,
    )
    await pipeline.handle_message(ctx, telemetry_pb2.TelemetryUp(control_ack=ack))
    sink = pipeline._sink  # type: ignore[attr-defined]
    assert len(sink.acks) == 1
    assert sink.acks[0][0].control_id == "ctl-1"
    assert sink.acks[0][1] == ctx.stream_id


async def test_hello_as_subsequent_message_raises(pipeline):
    ctx, _ = await pipeline.handle_hello(_hello(session_id="sess_twice"))
    with pytest.raises(ValueError):
        await pipeline.handle_message(
            ctx, telemetry_pb2.TelemetryUp(hello=_hello(session_id="sess_twice"))
        )


async def test_goodbye_marks_agent_disconnected(pipeline, store):
    ctx, _ = await pipeline.handle_hello(_hello(session_id="sess_bye"))
    await pipeline.handle_message(ctx, _goodbye("done"))
    agent = await store.get_agent("sess_bye", "a1")
    assert agent.status == AgentStatus.DISCONNECTED


async def test_close_stream_leaves_agent_connected_when_other_stream_live(pipeline, store):
    ctx1, _ = await pipeline.handle_hello(_hello(agent_id="shared", session_id="sess_shr"))
    ctx2, _ = await pipeline.handle_hello(_hello(agent_id="shared", session_id="sess_shr"))
    await pipeline.close_stream(ctx1)
    agent = await store.get_agent("sess_shr", "shared")
    # Still one live stream, so agent remains CONNECTED.
    assert agent.status == AgentStatus.CONNECTED
    # Closing the last stream flips to DISCONNECTED.
    await pipeline.close_stream(ctx2)
    agent = await store.get_agent("sess_shr", "shared")
    assert agent.status == AgentStatus.DISCONNECTED


async def test_sweep_heartbeats_returns_expired_streams(pipeline):
    ctx, _ = await pipeline.handle_hello(_hello(session_id="sess_exp"))
    pipeline._clock[0] = 1_000_000.0 + 60.0  # Advance past timeout.
    expired = await pipeline.sweep_heartbeats()
    assert ctx in expired


async def test_live_streams_and_active_stream_count(pipeline):
    ctx, _ = await pipeline.handle_hello(_hello(agent_id="lc", session_id="sess_lc"))
    assert pipeline.active_stream_count() == 1
    assert len(pipeline.live_streams("lc")) == 1
    await pipeline.close_stream(ctx)
    assert pipeline.active_stream_count() == 0


# task_plan / task_status_update wire-level ingestion tests removed in Phase A
# of the goldfive migration (issue #2). The TelemetryUp.task_plan /
# task_status_update variants are reserved; plan and task state now travel in
# TelemetryUp.goldfive_event. Phase B rewires the ingest pipeline around that
# new path and restores these assertions.


async def test_task_report_attr_on_span_publishes_task_report(pipeline):
    ctx, _ = await pipeline.handle_hello(_hello(session_id="sess_tr"))
    sub = await pipeline.bus.subscribe("sess_tr")
    span_msg = _span_msg("sp-tr", attrs={"task_report": "phase 1 done"})
    await pipeline.handle_message(ctx, span_msg)
    kinds = {}
    while not sub.queue.empty():
        d = sub.queue.get_nowait()
        kinds[d.kind] = d.payload
    assert DELTA_TASK_REPORT in kinds
    assert kinds[DELTA_TASK_REPORT]["report"] == "phase 1 done"
