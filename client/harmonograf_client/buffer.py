"""Bounded ring buffer with tiered drop policy.

Two independent buffers live here:

1. ``EventRingBuffer`` — span_start / span_update / span_end envelopes.
   Default capacity 2000 entries, approximately one minute of headroom
   at the design target of 30 events/second. On overflow the drop order
   follows docs/design/01 §4.5:

       updates  →  payload chunks  →  whole spans

   In buffer terms that means: when a push would exceed capacity, we
   first evict the oldest ``SpanUpdate``; if none exist we strip any
   payload reference on the oldest span envelope (payload bytes go
   through the separate PayloadBuffer anyway, so this drop is a signal
   to also evict from there); if the whole buffer is nothing but
   starts/ends we drop the oldest envelope wholesale. Every drop
   increments a counter reported via Heartbeat.

2. ``PayloadBuffer`` — content-addressed blobs awaiting upload.
   Default cap 16 MiB. Overflow drops oldest blobs by digest; the
   associated span envelope's ``payload_ref`` should be marked
   ``evicted`` by the caller before handing the envelope off to the
   transport.

Both buffers are thread-safe and non-blocking: ``push`` never waits.
The background transport worker ``pop_batch``es envelopes to drain
them onto the gRPC stream.

These classes intentionally do not import any protobuf types. They
hold opaque envelope objects (``SpanEnvelope``) whose ``kind`` field
tells the buffer how to handle it during drop decisions. The transport
layer is responsible for the pb conversion at dequeue time.
"""

from __future__ import annotations

import dataclasses
import enum
import threading
from collections import deque
from typing import Any, Deque, Iterator


class EnvelopeKind(enum.Enum):
    SPAN_START = "span_start"
    SPAN_UPDATE = "span_update"
    SPAN_END = "span_end"
    PAYLOAD_CHUNK = "payload_chunk"
    # Goldfive orchestration event; payload is a goldfive.v1.Event proto
    # (issue #2 migration).
    GOLDFIVE_EVENT = "goldfive_event"
    # Harmonograf-local refine-attempt observability variants
    # (goldfive#264). Same dict→proto pattern that pre-#190
    # ``invocation_cancelled`` followed: goldfive ships
    # ``refine_attempted`` / ``refine_failed`` dict envelopes for
    # forward-compat; the sink materializes them into the harmonograf-
    # local proto messages and routes on their own oneof slots
    # (``TelemetryUp.refine_attempted`` / ``.refine_failed``). When
    # goldfive Stream C (#256) promotes these to typed variants the
    # envelope kinds collapse the same way INVOCATION_CANCELLED did.
    REFINE_ATTEMPTED = "refine_attempted"
    REFINE_FAILED = "refine_failed"
    # User-authored message observed via ADK's
    # ``on_user_message_callback`` (harmonograf user-message UX gap).
    # Carries a ``harmonograf.v1.UserMessageReceived`` proto. Same
    # transport + Hello-routing semantics as the refine envelopes —
    # session_id is read off the proto's top-level field.
    USER_MESSAGE = "user_message"


@dataclasses.dataclass
class SpanEnvelope:
    """Opaque buffer entry.

    ``payload`` holds whatever the caller wants the transport layer to
    eventually serialize — typically a dict of span fields. The buffer
    itself never inspects it beyond the ``kind`` discriminator.
    """

    kind: EnvelopeKind
    span_id: str
    payload: Any
    has_payload_ref: bool = False


@dataclasses.dataclass
class BufferStats:
    buffered_events: int = 0
    dropped_updates: int = 0
    dropped_payload_refs: int = 0
    dropped_spans: int = 0
    buffered_payload_bytes: int = 0
    dropped_payload_bytes: int = 0

    @property
    def dropped_total(self) -> int:
        return self.dropped_updates + self.dropped_spans


class EventRingBuffer:
    """Bounded deque of span envelopes with tiered overflow.

    Not a ring in the classic fixed-array sense — backed by a deque so
    arbitrary mid-buffer removals (drop oldest update) are O(n) worst
    case but cheap in practice since n is capped at ~2000.
    """

    def __init__(self, capacity: int = 2000) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._dq: Deque[SpanEnvelope] = deque()
        self._lock = threading.Lock()
        self._stats = BufferStats()

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        with self._lock:
            return len(self._dq)

    def stats_snapshot(self) -> BufferStats:
        with self._lock:
            return dataclasses.replace(self._stats, buffered_events=len(self._dq))

    def push(self, env: SpanEnvelope) -> bool:
        """Enqueue an envelope, evicting per policy if over capacity.

        Returns True if the envelope was stored, False if the envelope
        itself was dropped (which only happens when the buffer is full
        of starts/ends with no payload refs and the caller is also
        pushing a start/end).
        """
        with self._lock:
            if len(self._dq) < self._capacity:
                self._dq.append(env)
                return True

            # At capacity — run eviction tiers until we free a slot
            # or decide to drop the incoming envelope.
            if self._evict_one_locked():
                self._dq.append(env)
                return True

            # Nothing evictable and we're full: drop incoming.
            self._stats.dropped_spans += 1
            return False

    def _evict_one_locked(self) -> bool:
        # Tier 1: drop the oldest SpanUpdate.
        for i, e in enumerate(self._dq):
            if e.kind is EnvelopeKind.SPAN_UPDATE:
                del self._dq[i]
                self._stats.dropped_updates += 1
                return True

        # Tier 2: strip oldest payload_ref (span envelope stays, but
        # its attached payload is marked evicted by the caller when it
        # sees has_payload_ref flipped to False).
        for e in self._dq:
            if e.has_payload_ref:
                e.has_payload_ref = False
                self._stats.dropped_payload_refs += 1
                return True

        # Tier 3: drop oldest whole span envelope.
        if self._dq:
            self._dq.popleft()
            self._stats.dropped_spans += 1
            return True

        return False

    def pop_batch(self, max_items: int) -> list[SpanEnvelope]:
        if max_items <= 0:
            return []
        with self._lock:
            n = min(max_items, len(self._dq))
            out = [self._dq.popleft() for _ in range(n)]
            return out

    def drain(self) -> Iterator[SpanEnvelope]:
        with self._lock:
            while self._dq:
                yield self._dq.popleft()


class PayloadBuffer:
    """Content-addressed staging area for payload bytes.

    Keyed by sha256 digest so duplicate uploads (system prompts, repeated
    tool args) cost only one slot. Eviction is oldest-first by insertion
    order.
    """

    def __init__(self, capacity_bytes: int = 16 * 1024 * 1024) -> None:
        if capacity_bytes <= 0:
            raise ValueError("capacity_bytes must be positive")
        self._capacity_bytes = capacity_bytes
        self._blobs: dict[str, bytes] = {}
        self._order: Deque[str] = deque()
        self._bytes = 0
        self._lock = threading.Lock()
        self._dropped_bytes = 0

    @property
    def capacity_bytes(self) -> int:
        return self._capacity_bytes

    def put(self, digest: str, data: bytes) -> bool:
        """Store a blob. Returns False if the blob itself was too big
        to fit even after evicting everything else, in which case the
        caller should mark its payload_ref as evicted immediately.
        """
        size = len(data)
        if size > self._capacity_bytes:
            with self._lock:
                self._dropped_bytes += size
            return False

        with self._lock:
            if digest in self._blobs:
                return True  # dedupe — already stored
            while self._bytes + size > self._capacity_bytes and self._order:
                victim = self._order.popleft()
                v_data = self._blobs.pop(victim, None)
                if v_data is not None:
                    self._bytes -= len(v_data)
                    self._dropped_bytes += len(v_data)
            self._blobs[digest] = data
            self._order.append(digest)
            self._bytes += size
            return True

    def take(self, digest: str) -> bytes | None:
        with self._lock:
            data = self._blobs.pop(digest, None)
            if data is not None:
                try:
                    self._order.remove(digest)
                except ValueError:
                    pass
                self._bytes -= len(data)
            return data

    def buffered_bytes(self) -> int:
        with self._lock:
            return self._bytes

    def dropped_bytes(self) -> int:
        with self._lock:
            return self._dropped_bytes
