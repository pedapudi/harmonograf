"""Protocol-level tests for :mod:`harmonograf_client.transport`.

The real transport spins up a daemon thread + asyncio loop and talks
gRPC. These tests bypass that machinery and exercise the pure methods:
``_envelope_to_up``, ``_build_hello``, ``_build_heartbeat``,
``_dispatch_control``, ``enqueue_payload`` (chunk math), and
``_maybe_send_chunk`` driven by an in-memory asyncio.Queue.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from harmonograf_client.buffer import (
    EnvelopeKind,
    EventRingBuffer,
    PayloadBuffer,
    SpanEnvelope,
)
from harmonograf_client.pb import telemetry_pb2, types_pb2
from harmonograf_client.transport import (
    ControlAckSpec,
    Transport,
    TransportConfig,
)


def _make_transport(**overrides) -> Transport:
    events = overrides.pop("events", EventRingBuffer(capacity=64))
    payloads = overrides.pop("payloads", PayloadBuffer(capacity_bytes=1 << 20))
    cfg = overrides.pop("config", TransportConfig(payload_chunk_bytes=16))
    return Transport(
        events=events,
        payloads=payloads,
        agent_id=overrides.pop("agent_id", "agent-X"),
        session_id=overrides.pop("session_id", "sess"),
        name=overrides.pop("name", "n"),
        framework=overrides.pop("framework", "CUSTOM"),
        framework_version=overrides.pop("framework_version", "1.2"),
        capabilities=overrides.pop("capabilities", ["STEERING"]),
        metadata=overrides.pop("metadata", {"k": "v"}),
        session_title=overrides.pop("session_title", "my session"),
        config=cfg,
        progress_fn=overrides.pop("progress_fn", lambda: (7, "working")),
    )


class TestEnvelopeToUp:
    def test_span_start_maps_to_span_start_oneof(self):
        t = _make_transport()
        span_start = telemetry_pb2.SpanStart(span=types_pb2.Span(id="s1"))
        env = SpanEnvelope(kind=EnvelopeKind.SPAN_START, span_id="s1", payload=span_start)
        up = t._envelope_to_up(env, telemetry_pb2)
        assert up.WhichOneof("msg") == "span_start"
        assert up.span_start.span.id == "s1"

    def test_span_update_maps_to_span_update_oneof(self):
        t = _make_transport()
        payload = telemetry_pb2.SpanUpdate(span_id="s1")
        env = SpanEnvelope(kind=EnvelopeKind.SPAN_UPDATE, span_id="s1", payload=payload)
        up = t._envelope_to_up(env, telemetry_pb2)
        assert up.WhichOneof("msg") == "span_update"

    def test_span_end_maps_to_span_end_oneof(self):
        t = _make_transport()
        payload = telemetry_pb2.SpanEnd(span_id="s1")
        env = SpanEnvelope(kind=EnvelopeKind.SPAN_END, span_id="s1", payload=payload)
        up = t._envelope_to_up(env, telemetry_pb2)
        assert up.WhichOneof("msg") == "span_end"

    def test_goldfive_event_maps_to_goldfive_event_oneof(self):
        """Phase A of the goldfive migration (issue #2) replaced the
        TaskPlan / UpdatedTaskStatus variants with a single
        ``goldfive_event`` envelope that carries a goldfive.v1.Event.
        """
        from goldfive.v1 import events_pb2

        t = _make_transport()
        ev = events_pb2.Event(run_id="run-1", sequence=0)
        ev.run_started.goal_summary = "demo"
        env = SpanEnvelope(kind=EnvelopeKind.GOLDFIVE_EVENT, span_id="", payload=ev)
        up = t._envelope_to_up(env, telemetry_pb2)
        assert up.WhichOneof("msg") == "goldfive_event"
        assert up.goldfive_event.run_id == "run-1"
        assert up.goldfive_event.WhichOneof("payload") == "run_started"

    def test_payload_chunk_returns_none(self):
        t = _make_transport()
        env = SpanEnvelope(kind=EnvelopeKind.PAYLOAD_CHUNK, span_id="", payload=None)
        assert t._envelope_to_up(env, telemetry_pb2) is None


class TestBuildHello:
    def test_hello_populates_identity_and_caps(self):
        t = _make_transport(
            agent_id="A", session_id="S", name="name", framework="CUSTOM",
            framework_version="9.9", capabilities=["STEERING"], metadata={"x": "1"},
            session_title="t",
        )
        hello = t._build_hello(telemetry_pb2)
        assert hello.agent_id == "A"
        assert hello.session_id == "S"
        assert hello.name == "name"
        assert hello.framework_version == "9.9"
        assert hello.session_title == "t"
        assert dict(hello.metadata) == {"x": "1"}
        assert hello.framework == types_pb2.FRAMEWORK_CUSTOM
        assert types_pb2.CAPABILITY_STEERING in list(hello.capabilities)

    def test_hello_ignores_unknown_capability(self):
        t = _make_transport(capabilities=["NOT_A_REAL_CAP"])
        hello = t._build_hello(telemetry_pb2)
        assert list(hello.capabilities) == []

    def test_hello_carries_resume_token(self):
        t = _make_transport()
        t._resume_token = "last-span-99"
        hello = t._build_hello(telemetry_pb2)
        assert hello.resume_token == "last-span-99"


class TestBuildHeartbeat:
    def test_heartbeat_reports_buffer_stats(self):
        events = EventRingBuffer(capacity=8)
        events.push(SpanEnvelope(
            kind=EnvelopeKind.SPAN_START, span_id="s1", payload=object()
        ))
        payloads = PayloadBuffer(capacity_bytes=64)
        payloads.put("d", b"xxxx")
        t = _make_transport(events=events, payloads=payloads,
                            progress_fn=lambda: (42, "deep work"))
        hb = t._build_heartbeat(telemetry_pb2)
        assert hb.buffered_events == 1
        assert hb.buffered_payload_bytes == 4
        assert hb.progress_counter == 42
        assert hb.current_activity == "deep work"
        assert hb.dropped_events == 0

    def test_heartbeat_with_no_progress_fn_is_zero(self):
        t = _make_transport(progress_fn=None)
        hb = t._build_heartbeat(telemetry_pb2)
        assert hb.progress_counter == 0
        assert hb.current_activity == ""


class TestDispatchControl:
    def test_unknown_kind_returns_unsupported(self):
        t = _make_transport()
        evt = types_pb2.ControlEvent(id="c1", kind=types_pb2.CONTROL_KIND_PAUSE)
        ack = t._dispatch_control(evt, types_pb2)
        assert ack.control_id == "c1"
        assert ack.result == types_pb2.CONTROL_ACK_RESULT_UNSUPPORTED

    def test_handler_success(self):
        t = _make_transport()

        def h(event):
            return ControlAckSpec(result="success", detail="ok")

        t.register_control_handler("PAUSE", h)
        evt = types_pb2.ControlEvent(id="c2", kind=types_pb2.CONTROL_KIND_PAUSE)
        ack = t._dispatch_control(evt, types_pb2)
        assert ack.result == types_pb2.CONTROL_ACK_RESULT_SUCCESS
        assert ack.detail == "ok"

    def test_handler_none_means_success(self):
        t = _make_transport()
        t.register_control_handler("CANCEL", lambda e: None)
        evt = types_pb2.ControlEvent(id="c3", kind=types_pb2.CONTROL_KIND_CANCEL)
        ack = t._dispatch_control(evt, types_pb2)
        assert ack.result == types_pb2.CONTROL_ACK_RESULT_SUCCESS

    def test_handler_exception_maps_to_failure(self):
        t = _make_transport()

        def boom(event):
            raise RuntimeError("nope")

        t.register_control_handler("STEER", boom)
        evt = types_pb2.ControlEvent(id="c4", kind=types_pb2.CONTROL_KIND_STEER)
        ack = t._dispatch_control(evt, types_pb2)
        assert ack.result == types_pb2.CONTROL_ACK_RESULT_FAILURE
        assert "nope" in ack.detail


class TestEnqueuePayloadChunking:
    def test_single_chunk_when_small(self):
        t = _make_transport(config=TransportConfig(payload_chunk_bytes=1024))
        ok = t.enqueue_payload("d1", b"small", "text/plain")
        assert ok is True
        assert len(t._chunk_queue) == 1
        digest, mime, total, offset, last = t._chunk_queue[0]
        assert digest == "d1"
        assert mime == "text/plain"
        assert total == 5
        assert offset == 0
        assert last is True

    def test_multi_chunk_last_flag(self):
        t = _make_transport(config=TransportConfig(payload_chunk_bytes=4))
        data = b"0123456789"
        t.enqueue_payload("d2", data, "application/octet-stream")
        # 10 bytes, 4-byte chunks -> 3 chunks
        assert len(t._chunk_queue) == 3
        offsets = [entry[3] for entry in t._chunk_queue]
        lasts = [entry[4] for entry in t._chunk_queue]
        assert offsets == [0, 4, 8]
        assert lasts == [False, False, True]
        assert all(entry[2] == 10 for entry in t._chunk_queue)

    def test_rejected_payload_increments_evicted(self):
        payloads = PayloadBuffer(capacity_bytes=4)
        t = _make_transport(payloads=payloads)
        ok = t.enqueue_payload("x", b"x" * 32, "application/octet-stream")
        assert ok is False
        assert t._payloads_evicted == 1
        assert t._chunk_queue == []


class TestMaybeSendChunk:
    def test_flushes_a_single_chunk(self):
        t = _make_transport(config=TransportConfig(payload_chunk_bytes=1024))
        data = b"payload-bytes"
        t.enqueue_payload("dd", data, "text/plain")
        q: asyncio.Queue = asyncio.Queue()

        async def run():
            pushed = await t._maybe_send_chunk(q, telemetry_pb2)
            return pushed

        assert asyncio.run(run()) is True
        assert q.qsize() == 1
        up = q.get_nowait()
        assert up.WhichOneof("msg") == "payload"
        upload = up.payload
        assert upload.digest == "dd"
        assert upload.total_size == len(data)
        assert upload.mime == "text/plain"
        assert upload.last is True
        assert upload.chunk == data

    def test_returns_false_on_empty_queue(self):
        t = _make_transport()
        q: asyncio.Queue = asyncio.Queue()

        async def run():
            return await t._maybe_send_chunk(q, telemetry_pb2)

        assert asyncio.run(run()) is False

    def test_marks_evicted_when_payload_missing(self):
        t = _make_transport(config=TransportConfig(payload_chunk_bytes=16))
        # Manually enqueue a chunk entry pointing at a digest that isn't
        # in the PayloadBuffer (simulates a race where the bytes were
        # evicted between enqueue and drain).
        t._chunk_queue.append(("ghost", "text/plain", 10, 0, True))
        q: asyncio.Queue = asyncio.Queue()

        async def run():
            return await t._maybe_send_chunk(q, telemetry_pb2)

        assert asyncio.run(run()) is True
        up = q.get_nowait()
        assert up.payload.evicted is True
        assert up.payload.last is True
        assert up.payload.chunk == b""


class TestControlHandlerRegistry:
    def test_register_and_replace(self):
        t = _make_transport()
        t.register_control_handler("PAUSE", lambda e: ControlAckSpec())
        assert "PAUSE" in t._handlers
        t.register_control_handler("PAUSE", lambda e: ControlAckSpec(result="failure"))
        # Second registration wins.
        evt = types_pb2.ControlEvent(id="c", kind=types_pb2.CONTROL_KIND_PAUSE)
        ack = t._dispatch_control(evt, types_pb2)
        assert ack.result == types_pb2.CONTROL_ACK_RESULT_FAILURE


class TestBreakerState:
    def test_initial_state(self):
        t = _make_transport()
        assert t.breaker_state == "closed"
        assert t.consecutive_failures == 0

    def test_failed_attempts_accumulate(self):
        t = _make_transport(config=TransportConfig(breaker_failure_threshold=3))
        for _ in range(2):
            t._on_failed_attempt()
        assert t.breaker_state == "closed"
        assert t.consecutive_failures == 2
        t._on_failed_attempt()
        assert t.breaker_state == "open"

    def test_healthy_reset(self):
        t = _make_transport(config=TransportConfig(breaker_failure_threshold=2))
        t._on_failed_attempt()
        t._on_failed_attempt()
        assert t.breaker_state == "open"
        t._mark_healthy()
        assert t.breaker_state == "closed"
        assert t.consecutive_failures == 0
