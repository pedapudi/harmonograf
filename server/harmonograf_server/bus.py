"""In-process pub/sub for SessionUpdate deltas.

Telemetry ingest publishes per-session events; WatchSession subscribers each
get their own asyncio.Queue. Subscribers that fall behind are dropped with a
backpressure signal; ingest is never blocked on a slow consumer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

from harmonograf_server.storage import (
    Agent,
    AgentStatus,
    Annotation,
    Span,
)


DELTA_AGENT_UPSERT = "agent_upsert"
DELTA_AGENT_STATUS = "agent_status"
DELTA_SPAN_START = "span_start"
DELTA_SPAN_UPDATE = "span_update"
DELTA_SPAN_END = "span_end"
DELTA_ANNOTATION = "annotation"
DELTA_HEARTBEAT = "heartbeat"
DELTA_BACKPRESSURE = "backpressure"


@dataclass
class Delta:
    """A single event to push to WatchSession subscribers.

    payload shape depends on kind; consumers map kind → field.
    """

    session_id: str
    kind: str
    payload: Any


class Subscription:
    def __init__(self, session_id: str, maxsize: int) -> None:
        self.session_id = session_id
        self.queue: asyncio.Queue[Delta] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self._closed = False

    def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed


class SessionBus:
    """Per-session fan-out.

    publish() is sync-safe-from-async: it never blocks the caller. If a
    subscriber's queue is full the event is dropped on that subscriber's
    floor and a backpressure delta is enqueued for it.
    """

    def __init__(self, queue_maxsize: int = 1024) -> None:
        self._subs: dict[str, list[Subscription]] = {}
        self._lock = asyncio.Lock()
        self._queue_maxsize = queue_maxsize

    async def subscribe(self, session_id: str) -> Subscription:
        sub = Subscription(session_id, self._queue_maxsize)
        async with self._lock:
            self._subs.setdefault(session_id, []).append(sub)
        return sub

    async def unsubscribe(self, sub: Subscription) -> None:
        sub.close()
        async with self._lock:
            lst = self._subs.get(sub.session_id)
            if not lst:
                return
            try:
                lst.remove(sub)
            except ValueError:
                pass
            if not lst:
                del self._subs[sub.session_id]

    def publish(self, delta: Delta) -> None:
        # Snapshot subscriber list without the async lock; subscribe/unsubscribe
        # under the lock atomically mutate the list object but reads of the
        # dict are fine from the same event loop.
        subs = list(self._subs.get(delta.session_id, ()))
        for sub in subs:
            if sub.closed:
                continue
            try:
                sub.queue.put_nowait(delta)
            except asyncio.QueueFull:
                sub.dropped += 1
                try:
                    sub.queue.put_nowait(
                        Delta(
                            session_id=delta.session_id,
                            kind=DELTA_BACKPRESSURE,
                            payload={"dropped": sub.dropped},
                        )
                    )
                except asyncio.QueueFull:
                    pass

    def subscriber_count(self, session_id: str) -> int:
        return len(self._subs.get(session_id, ()))

    # ---- convenience constructors ------------------------------------

    def publish_agent_upsert(self, agent: Agent) -> None:
        self.publish(Delta(agent.session_id, DELTA_AGENT_UPSERT, agent))

    def publish_agent_status(
        self, session_id: str, agent_id: str, status: AgentStatus, last_heartbeat: Optional[float]
    ) -> None:
        self.publish(
            Delta(
                session_id,
                DELTA_AGENT_STATUS,
                {"agent_id": agent_id, "status": status, "last_heartbeat": last_heartbeat},
            )
        )

    def publish_span_start(self, span: Span) -> None:
        self.publish(Delta(span.session_id, DELTA_SPAN_START, span))

    def publish_span_update(self, span: Span) -> None:
        self.publish(Delta(span.session_id, DELTA_SPAN_UPDATE, span))

    def publish_span_end(self, span: Span) -> None:
        self.publish(Delta(span.session_id, DELTA_SPAN_END, span))

    def publish_annotation(self, annotation: Annotation) -> None:
        self.publish(Delta(annotation.session_id, DELTA_ANNOTATION, annotation))

    def publish_heartbeat(self, session_id: str, agent_id: str, stats: dict) -> None:
        self.publish(
            Delta(session_id, DELTA_HEARTBEAT, {"agent_id": agent_id, **stats})
        )
