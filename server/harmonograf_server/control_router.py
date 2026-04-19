"""In-process control routing.

Per §6 of docs/design/03-server.md, the ControlRouter owns the pub/sub
topology from the SendControl call site (frontend or PostAnnotation) to
the agent-facing SubscribeControl streams. Acks travel back on the
telemetry stream (not SubscribeControl) — the ingest pipeline calls
record_ack() here when it sees a ControlAck on a TelemetryUp.

Core guarantees:
  * deliver() returns UNAVAILABLE immediately if no live subscriptions
    exist for the target agent. Control is never queued across reconnects.
  * When multiple telemetry+control streams exist for one agent
    (multi-stream is allowed), the event is fanned out to every live
    subscription and the returned future resolves once either the first
    success arrives (require_all_acks=False) or every subscription has
    ack'd (require_all_acks=True).
  * A deliver() with a deadline that elapses resolves to
    DEADLINE_EXCEEDED with whatever partial acks have arrived by then.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional

from goldfive.pb.goldfive.v1 import control_pb2 as gf_control_pb2


logger = logging.getLogger(__name__)


class DeliveryResult(str, Enum):
    SUCCESS = "SUCCESS"
    UNAVAILABLE = "UNAVAILABLE"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    FAILED = "FAILED"


@dataclass
class AckRecord:
    stream_id: str
    result: gf_control_pb2.ControlAckResult
    detail: str
    acked_at: float


@dataclass
class DeliveryOutcome:
    control_id: str
    result: DeliveryResult
    acks: list[AckRecord] = field(default_factory=list)


class ControlSubscription:
    """One live SubscribeControl stream. The SubscribeControl RPC coroutine
    drains `queue` and writes each ControlEvent to gRPC."""

    def __init__(self, session_id: str, agent_id: str, stream_id: str) -> None:
        self.session_id = session_id
        self.agent_id = agent_id
        self.stream_id = stream_id
        self.queue: asyncio.Queue[gf_control_pb2.ControlEvent] = asyncio.Queue(maxsize=256)
        self._closed = False

    def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed


@dataclass
class _PendingDelivery:
    control_id: str
    expected_stream_ids: set[str]
    require_all: bool
    session_id: str = ""
    agent_id: str = ""
    kind: int = 0
    acks: list[AckRecord] = field(default_factory=list)
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())


class ControlRouter:
    def __init__(self) -> None:
        # agent_id -> { stream_id -> subscription }
        self._subs: dict[str, dict[str, ControlSubscription]] = {}
        # control_id -> pending delivery
        self._pending: dict[str, _PendingDelivery] = {}
        self._lock = asyncio.Lock()
        # callbacks invoked when a STATUS_QUERY ack arrives
        self._status_query_callbacks: list[Callable[[str, str, str, str], Awaitable[None]]] = []
        # sub_agent_id (ADK name) → stream_agent_id (transport UUID).
        # Populated by the ingest pipeline when a span arrives for an agent
        # whose id differs from the transport's registered Hello agent_id.
        # Lets controls sent to the ADK name reach the correct subscription.
        self._aliases: dict[str, str] = {}

    def register_alias(self, sub_agent_id: str, stream_agent_id: str) -> None:
        """Map an ADK sub-agent name to the stream's registered agent_id.

        Called from the ingest pipeline's ``_ensure_route`` whenever a span
        arrives with an agent_id that differs from the transport's Hello
        agent_id. Subsequent ``deliver()`` calls for the sub-agent name are
        forwarded to the stream that owns it.
        """
        if sub_agent_id and stream_agent_id and sub_agent_id != stream_agent_id:
            self._aliases[sub_agent_id] = stream_agent_id

    def clear_aliases_for_stream(self, stream_agent_id: str) -> None:
        """Remove all aliases that point to stream_agent_id. Thread-safe."""
        async def _do() -> None:
            async with self._lock:
                stale = [k for k, v in self._aliases.items() if v == stream_agent_id]
                for k in stale:
                    del self._aliases[k]
        # This method may be called from a non-async context; schedule if loop is running.
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_do())
            else:
                loop.run_until_complete(_do())
        except RuntimeError:
            pass

    def on_status_query_response(self, cb: Callable) -> None:
        """Register a callback invoked whenever a STATUS_QUERY ack arrives."""
        self._status_query_callbacks.append(cb)

    # ---- subscription lifecycle --------------------------------------

    async def subscribe(
        self, session_id: str, agent_id: str, stream_id: str
    ) -> ControlSubscription:
        sub = ControlSubscription(session_id, agent_id, stream_id)
        async with self._lock:
            self._subs.setdefault(agent_id, {})[stream_id] = sub
        logger.info(
            "control subscribe agent_id=%s stream_id=%s session_id=%s",
            agent_id,
            stream_id,
            session_id,
        )
        return sub

    async def unsubscribe(self, sub: ControlSubscription) -> None:
        sub.close()
        async with self._lock:
            bucket = self._subs.get(sub.agent_id)
            if bucket is not None:
                bucket.pop(sub.stream_id, None)
                if not bucket:
                    del self._subs[sub.agent_id]
            # Resolve any pending deliveries that were waiting on this stream
            # specifically — it will never ack.
            for pending in list(self._pending.values()):
                if sub.stream_id in pending.expected_stream_ids:
                    pending.expected_stream_ids.discard(sub.stream_id)
                    self._maybe_resolve(pending)
            # Remove aliases that pointed at this (now-dead) stream.
            if not self._subs.get(sub.agent_id):
                stale_aliases = [k for k, v in self._aliases.items() if v == sub.agent_id]
                for k in stale_aliases:
                    del self._aliases[k]
                    logger.debug("removed stale alias %s -> %s", k, sub.agent_id)
        logger.info(
            "control unsubscribe agent_id=%s stream_id=%s", sub.agent_id, sub.stream_id
        )

    def live_stream_ids(self, agent_id: str) -> list[str]:
        bucket = self._subs.get(agent_id)
        if not bucket:
            return []
        return [s for s, v in bucket.items() if not v.closed]

    # ---- send / ack --------------------------------------------------

    async def deliver(
        self,
        *,
        session_id: str,
        agent_id: str,
        event: gf_control_pb2.ControlEvent,
        timeout_s: float = 5.0,
        require_all_acks: bool = False,
    ) -> DeliveryOutcome:
        """Fan ``event`` out to every live SubscribeControl stream for ``agent_id``.

        ``event`` is a fully-formed ``goldfive.v1.ControlEvent``. Callers
        typically set ``event.id`` and ``event.target.agent_id`` themselves;
        if ``event.id`` is empty we allocate one and if ``event.issued_at``
        is unset we stamp it server-side so the wire is never ambiguous.
        """
        if not event.id:
            event.id = f"ctl_{uuid.uuid4().hex[:16]}"
        if not event.HasField("issued_at"):
            from google.protobuf.timestamp_pb2 import Timestamp

            ts = Timestamp()
            ts.GetCurrentTime()
            event.issued_at.CopyFrom(ts)
        if not event.target.agent_id:
            event.target.agent_id = agent_id
        control_id = event.id

        async with self._lock:
            bucket = dict(self._subs.get(agent_id, {}))
        # If no direct subscription found, check the alias map.  This handles
        # the common case where the frontend sends controls to the ADK agent
        # name ("weather_agent") but the transport subscribed with its
        # identity-file UUID ("agent_abc123…").
        if not bucket:
            aliased_id = self._aliases.get(agent_id, "")
            if aliased_id:
                async with self._lock:
                    bucket = dict(self._subs.get(aliased_id, {}))
        live_ids = [sid for sid, sub in bucket.items() if not sub.closed]
        if not live_ids:
            return DeliveryOutcome(control_id=control_id, result=DeliveryResult.UNAVAILABLE)

        loop = asyncio.get_event_loop()
        pending = _PendingDelivery(
            control_id=control_id,
            expected_stream_ids=set(live_ids),
            require_all=require_all_acks,
            session_id=session_id,
            agent_id=agent_id,
            kind=int(event.kind),
            future=loop.create_future(),
        )
        async with self._lock:
            self._pending[control_id] = pending

        # Enqueue to each subscription. If a queue is full we drop the event
        # for that stream and treat it as an immediate failure ack.
        for sid in live_ids:
            sub = bucket[sid]
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "control queue full control_id=%s stream_id=%s", control_id, sid
                )
                self._record_synthetic_ack(
                    pending,
                    AckRecord(
                        stream_id=sid,
                        result=gf_control_pb2.CONTROL_ACK_RESULT_FAILURE,
                        detail="control queue full",
                        acked_at=time.time(),
                    ),
                )

        try:
            await asyncio.wait_for(pending.future, timeout=timeout_s)
            return pending.future.result()
        except asyncio.TimeoutError:
            async with self._lock:
                self._pending.pop(control_id, None)
            return DeliveryOutcome(
                control_id=control_id,
                result=DeliveryResult.DEADLINE_EXCEEDED,
                acks=list(pending.acks),
            )

    def record_ack(
        self, ack: gf_control_pb2.ControlAck, *, stream_id: Optional[str] = None
    ) -> None:
        """Called from the telemetry ingest pipeline when a ControlAck arrives
        on a TelemetryUp. stream_id is optional: if the ingest layer knows
        which stream the ack came from, it should pass it; otherwise we match
        against all pending deliveries for the control_id.
        """
        pending = self._pending.get(ack.control_id)
        if pending is None:
            return
        record = AckRecord(
            stream_id=stream_id or "",
            result=ack.result,
            detail=ack.detail,
            acked_at=ack.acked_at.seconds + ack.acked_at.nanos / 1e9
            if ack.HasField("acked_at")
            else time.time(),
        )
        # If stream_id unknown, attribute it to any still-expected stream.
        if not record.stream_id:
            if pending.expected_stream_ids:
                record.stream_id = next(iter(pending.expected_stream_ids))
            else:
                return
        if record.stream_id not in pending.expected_stream_ids:
            return
        pending.expected_stream_ids.discard(record.stream_id)
        pending.acks.append(record)
        self._maybe_resolve(pending)

        # Fire STATUS_QUERY callbacks when the ack is a success response.
        if (
            pending.kind == gf_control_pb2.CONTROL_KIND_STATUS_QUERY
            and record.result == gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS
            and self._status_query_callbacks
        ):
            loop = asyncio.get_event_loop()
            for cb in self._status_query_callbacks:
                loop.create_task(
                    cb(pending.session_id, pending.agent_id, "", record.detail)
                )

    # ---- internals ---------------------------------------------------

    def _record_synthetic_ack(self, pending: _PendingDelivery, ack: AckRecord) -> None:
        if ack.stream_id in pending.expected_stream_ids:
            pending.expected_stream_ids.discard(ack.stream_id)
            pending.acks.append(ack)
            self._maybe_resolve(pending)

    def _maybe_resolve(self, pending: _PendingDelivery) -> None:
        if pending.future.done():
            return
        any_success = any(
            a.result == gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS for a in pending.acks
        )
        all_done = not pending.expected_stream_ids
        if pending.require_all:
            if not all_done:
                return
            result = (
                DeliveryResult.SUCCESS if any_success and all(
                    a.result == gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS for a in pending.acks
                ) else DeliveryResult.FAILED
            )
        else:
            if not any_success and not all_done:
                return
            result = DeliveryResult.SUCCESS if any_success else DeliveryResult.FAILED

        self._pending.pop(pending.control_id, None)
        pending.future.set_result(
            DeliveryOutcome(
                control_id=pending.control_id,
                result=result,
                acks=list(pending.acks),
            )
        )
