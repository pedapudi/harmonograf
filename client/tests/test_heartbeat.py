"""Unit tests for heartbeat assembly."""

from __future__ import annotations

from harmonograf_client.buffer import (
    EnvelopeKind,
    EventRingBuffer,
    PayloadBuffer,
    SpanEnvelope,
)
from harmonograf_client.heartbeat import build_heartbeat, read_self_cpu_pct


def _env(kind: EnvelopeKind, sid: str) -> SpanEnvelope:
    return SpanEnvelope(kind=kind, span_id=sid, payload={})


def test_reports_empty_buffers():
    events = EventRingBuffer(capacity=10)
    payloads = PayloadBuffer(capacity_bytes=1024)
    hb = build_heartbeat(events, payloads, now=1000.0)
    assert hb.buffered_events == 0
    assert hb.dropped_events == 0
    assert hb.buffered_payload_bytes == 0
    assert hb.sent_at_unix == 1000.0


def test_reports_live_buffer_state():
    events = EventRingBuffer(capacity=10)
    events.push(_env(EnvelopeKind.SPAN_START, "s1"))
    events.push(_env(EnvelopeKind.SPAN_UPDATE, "s1"))
    payloads = PayloadBuffer(capacity_bytes=1024)
    payloads.put("d1", b"hello")
    hb = build_heartbeat(events, payloads, cpu_self_pct=12.5, now=42.0)
    assert hb.buffered_events == 2
    assert hb.buffered_payload_bytes == 5
    assert hb.cpu_self_pct == 12.5


def test_reports_dropped_counts():
    events = EventRingBuffer(capacity=2)
    events.push(_env(EnvelopeKind.SPAN_START, "s1"))
    events.push(_env(EnvelopeKind.SPAN_UPDATE, "s1"))
    events.push(_env(EnvelopeKind.SPAN_START, "s2"))  # evicts the update
    payloads = PayloadBuffer(capacity_bytes=1024)
    hb = build_heartbeat(events, payloads, now=1.0)
    assert hb.dropped_events >= 1


def test_read_self_cpu_pct_is_safe():
    # Just make sure it returns a float and never raises.
    v = read_self_cpu_pct()
    assert isinstance(v, float)
    assert v >= 0.0
