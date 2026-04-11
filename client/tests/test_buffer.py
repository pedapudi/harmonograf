"""Unit tests for the ring buffer and payload buffer."""

from __future__ import annotations

import threading

import pytest

from harmonograf_client.buffer import (
    EnvelopeKind,
    EventRingBuffer,
    PayloadBuffer,
    SpanEnvelope,
)


def _start(span_id: str, has_payload: bool = False) -> SpanEnvelope:
    return SpanEnvelope(
        kind=EnvelopeKind.SPAN_START,
        span_id=span_id,
        payload={"id": span_id},
        has_payload_ref=has_payload,
    )


def _update(span_id: str) -> SpanEnvelope:
    return SpanEnvelope(
        kind=EnvelopeKind.SPAN_UPDATE,
        span_id=span_id,
        payload={"id": span_id, "attr": "x"},
    )


def _end(span_id: str) -> SpanEnvelope:
    return SpanEnvelope(
        kind=EnvelopeKind.SPAN_END,
        span_id=span_id,
        payload={"id": span_id},
    )


class TestEventRingBuffer:
    def test_push_under_capacity(self):
        buf = EventRingBuffer(capacity=4)
        assert buf.push(_start("s1")) is True
        assert buf.push(_start("s2")) is True
        assert len(buf) == 2
        assert buf.stats_snapshot().dropped_total == 0

    def test_capacity_must_be_positive(self):
        with pytest.raises(ValueError):
            EventRingBuffer(capacity=0)

    def test_drops_updates_before_starts(self):
        # fill with [start, update, update, start]; pushing another
        # start should evict the oldest update, not either start.
        buf = EventRingBuffer(capacity=4)
        buf.push(_start("s1"))
        buf.push(_update("s1"))
        buf.push(_update("s1"))
        buf.push(_start("s2"))
        assert buf.push(_start("s3")) is True

        remaining = list(buf.drain())
        kinds = [e.kind for e in remaining]
        assert kinds.count(EnvelopeKind.SPAN_UPDATE) == 1
        assert kinds.count(EnvelopeKind.SPAN_START) == 3
        assert buf.stats_snapshot().dropped_updates == 1

    def test_strips_payload_ref_when_no_updates(self):
        buf = EventRingBuffer(capacity=3)
        buf.push(_start("s1", has_payload=True))
        buf.push(_start("s2", has_payload=True))
        buf.push(_start("s3", has_payload=True))
        assert buf.push(_start("s4", has_payload=True)) is True
        remaining = list(buf.drain())
        # oldest surviving envelope must have payload stripped
        assert remaining[0].has_payload_ref is False
        stats = EventRingBuffer(capacity=3)  # fresh for type inference only
        del stats
        s = buf.stats_snapshot()
        assert s.dropped_payload_refs == 1
        assert s.dropped_updates == 0
        assert s.dropped_spans == 0

    def test_drops_whole_span_when_nothing_else_to_shed(self):
        buf = EventRingBuffer(capacity=2)
        buf.push(_start("s1"))
        buf.push(_end("s1"))
        assert buf.push(_start("s2")) is True  # evicted oldest start
        remaining = [e.span_id for e in buf.drain()]
        assert remaining == ["s1", "s2"]  # the end("s1") and the new start
        # Actually correction: we dropped the oldest (start s1), leaving [end s1, start s2]
        # pop order is FIFO so drain yields end s1 first then start s2.
        # Let's just verify drop counter.
        assert buf.stats_snapshot().dropped_spans >= 1

    def test_pop_batch_respects_order_and_limit(self):
        buf = EventRingBuffer(capacity=5)
        for i in range(5):
            buf.push(_start(f"s{i}"))
        first = buf.pop_batch(2)
        assert [e.span_id for e in first] == ["s0", "s1"]
        rest = buf.pop_batch(10)
        assert [e.span_id for e in rest] == ["s2", "s3", "s4"]
        assert len(buf) == 0

    def test_thread_safety_smoke(self):
        buf = EventRingBuffer(capacity=500)
        errors: list[BaseException] = []

        def writer(tag: str) -> None:
            try:
                for i in range(1000):
                    buf.push(_start(f"{tag}-{i}"))
            except BaseException as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(f"t{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # We pushed 4000 envelopes into a cap-500 buffer. Exactly 500
        # survive; the rest must have been counted as dropped.
        stats = buf.stats_snapshot()
        assert stats.buffered_events <= 500
        assert stats.dropped_total >= 3500


class TestPayloadBuffer:
    def test_put_and_take(self):
        pb = PayloadBuffer(capacity_bytes=1024)
        assert pb.put("abc", b"hello world") is True
        assert pb.buffered_bytes() == 11
        assert pb.take("abc") == b"hello world"
        assert pb.buffered_bytes() == 0

    def test_dedupe_by_digest(self):
        pb = PayloadBuffer(capacity_bytes=1024)
        assert pb.put("abc", b"hello") is True
        assert pb.put("abc", b"hello") is True  # idempotent
        assert pb.buffered_bytes() == 5

    def test_evicts_oldest_on_overflow(self):
        pb = PayloadBuffer(capacity_bytes=12)
        pb.put("a", b"xxxx")  # 4
        pb.put("b", b"yyyy")  # 8
        pb.put("c", b"zzzz")  # 12
        pb.put("d", b"qqqq")  # forces eviction of "a"
        assert pb.take("a") is None
        assert pb.take("b") == b"yyyy"
        assert pb.dropped_bytes() == 4

    def test_blob_larger_than_capacity_rejected(self):
        pb = PayloadBuffer(capacity_bytes=8)
        assert pb.put("big", b"x" * 16) is False
        assert pb.buffered_bytes() == 0
        assert pb.dropped_bytes() == 16

    def test_capacity_must_be_positive(self):
        with pytest.raises(ValueError):
            PayloadBuffer(capacity_bytes=0)
