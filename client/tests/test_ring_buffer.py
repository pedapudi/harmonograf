"""Additional invariants for :mod:`harmonograf_client.buffer`.

The baseline happy-path coverage lives in ``test_buffer.py``. These
cases focus on the harder-to-observe pieces: tier ordering under mixed
workloads, payload buffer eviction accounting, interleaved
multi-threaded producers + drainers, and len/stats snapshot semantics.
"""

from __future__ import annotations

import threading

import pytest

from harmonograf_client.buffer import (
    EnvelopeKind,
    EventRingBuffer,
    PayloadBuffer,
    SpanEnvelope,
)


def _env(kind: EnvelopeKind, sid: str, has_payload: bool = False) -> SpanEnvelope:
    return SpanEnvelope(kind=kind, span_id=sid, payload=sid, has_payload_ref=has_payload)


class TestEventBufferTierOrdering:
    def test_update_tier_precedes_payload_ref_tier(self):
        buf = EventRingBuffer(capacity=3)
        buf.push(_env(EnvelopeKind.SPAN_START, "s1", has_payload=True))
        buf.push(_env(EnvelopeKind.SPAN_UPDATE, "s1"))
        buf.push(_env(EnvelopeKind.SPAN_START, "s2", has_payload=True))
        # Pushing another envelope should drop the update first, not a ref.
        buf.push(_env(EnvelopeKind.SPAN_END, "s2"))
        stats = buf.stats_snapshot()
        assert stats.dropped_updates == 1
        assert stats.dropped_payload_refs == 0
        # Both starts retain their payload refs.
        remaining = list(buf.drain())
        kinds_with_refs = [e for e in remaining if e.has_payload_ref]
        assert len(kinds_with_refs) == 2

    def test_payload_ref_tier_oldest_first(self):
        buf = EventRingBuffer(capacity=3)
        s1 = _env(EnvelopeKind.SPAN_START, "s1", has_payload=True)
        s2 = _env(EnvelopeKind.SPAN_START, "s2", has_payload=True)
        s3 = _env(EnvelopeKind.SPAN_START, "s3", has_payload=True)
        buf.push(s1)
        buf.push(s2)
        buf.push(s3)
        buf.push(_env(EnvelopeKind.SPAN_START, "s4", has_payload=True))
        # s1 had its payload ref stripped (still in the buffer).
        remaining = list(buf.drain())
        assert remaining[0].span_id == "s1"
        assert remaining[0].has_payload_ref is False
        assert remaining[1].has_payload_ref is True
        assert remaining[2].has_payload_ref is True
        assert remaining[3].has_payload_ref is True

    def test_drop_span_tier_when_nothing_else(self):
        buf = EventRingBuffer(capacity=2)
        buf.push(_env(EnvelopeKind.SPAN_END, "s1"))
        buf.push(_env(EnvelopeKind.SPAN_END, "s2"))
        # Neither updates nor payload refs — pushing another triggers tier 3.
        assert buf.push(_env(EnvelopeKind.SPAN_END, "s3")) is True
        stats = buf.stats_snapshot()
        assert stats.dropped_spans == 1


class TestEventBufferLenAndStats:
    def test_len_tracks_live_entries(self):
        buf = EventRingBuffer(capacity=10)
        assert len(buf) == 0
        for i in range(5):
            buf.push(_env(EnvelopeKind.SPAN_START, f"s{i}"))
        assert len(buf) == 5
        buf.pop_batch(3)
        assert len(buf) == 2

    def test_stats_snapshot_buffered_events(self):
        buf = EventRingBuffer(capacity=10)
        for i in range(3):
            buf.push(_env(EnvelopeKind.SPAN_START, f"s{i}"))
        stats = buf.stats_snapshot()
        assert stats.buffered_events == 3
        assert stats.dropped_total == 0

    def test_pop_batch_zero_returns_empty(self):
        buf = EventRingBuffer(capacity=3)
        buf.push(_env(EnvelopeKind.SPAN_START, "s"))
        assert buf.pop_batch(0) == []
        assert len(buf) == 1

    def test_drain_empties(self):
        buf = EventRingBuffer(capacity=3)
        buf.push(_env(EnvelopeKind.SPAN_START, "s1"))
        buf.push(_env(EnvelopeKind.SPAN_END, "s1"))
        drained = list(buf.drain())
        assert len(drained) == 2
        assert len(buf) == 0


class TestEventBufferConcurrency:
    def test_concurrent_push_and_drain(self):
        buf = EventRingBuffer(capacity=200)
        errors: list[BaseException] = []

        def writer(tag: str) -> None:
            try:
                for i in range(500):
                    buf.push(_env(EnvelopeKind.SPAN_START, f"{tag}-{i}"))
            except BaseException as e:  # pragma: no cover
                errors.append(e)

        def draier() -> None:
            try:
                for _ in range(100):
                    buf.pop_batch(32)
            except BaseException as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(f"w{i}",)) for i in range(3)]
        threads.append(threading.Thread(target=draier))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        # Buffer invariant: never exceeds capacity.
        assert len(buf) <= 200


class TestPayloadBuffer:
    def test_take_missing_returns_none(self):
        pb = PayloadBuffer(capacity_bytes=64)
        assert pb.take("nonexistent") is None

    def test_put_accumulates_bytes(self):
        pb = PayloadBuffer(capacity_bytes=64)
        pb.put("a", b"xxxx")
        pb.put("b", b"yyyy")
        assert pb.buffered_bytes() == 8

    def test_eviction_counts_dropped_bytes_cumulatively(self):
        pb = PayloadBuffer(capacity_bytes=8)
        pb.put("a", b"aaaa")
        pb.put("b", b"bbbb")
        pb.put("c", b"cccc")  # evicts a (4)
        pb.put("d", b"dddd")  # evicts b (4)
        assert pb.dropped_bytes() == 8
        assert pb.take("a") is None
        assert pb.take("b") is None
        assert pb.take("c") == b"cccc"
        assert pb.take("d") == b"dddd"

    def test_dedupe_does_not_double_count(self):
        pb = PayloadBuffer(capacity_bytes=64)
        pb.put("digest", b"hello")
        pb.put("digest", b"hello")
        pb.put("digest", b"hello")
        assert pb.buffered_bytes() == 5

    def test_blob_too_large_is_rejected_without_evicting(self):
        pb = PayloadBuffer(capacity_bytes=8)
        pb.put("a", b"aaaa")
        pb.put("b", b"bbbb")
        assert pb.put("big", b"z" * 32) is False
        # existing entries untouched
        assert pb.take("a") == b"aaaa"
        assert pb.take("b") == b"bbbb"
        assert pb.dropped_bytes() == 32

    def test_take_shrinks_buffered_bytes(self):
        pb = PayloadBuffer(capacity_bytes=64)
        pb.put("a", b"xxxxx")
        assert pb.buffered_bytes() == 5
        pb.take("a")
        assert pb.buffered_bytes() == 0

    def test_concurrent_put(self):
        pb = PayloadBuffer(capacity_bytes=2048)
        errors: list[BaseException] = []

        def writer(tag: int) -> None:
            try:
                for i in range(100):
                    pb.put(f"{tag}-{i}", b"x" * 4)
            except BaseException as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        # Buffered bytes never exceeds the cap.
        assert pb.buffered_bytes() <= 2048
