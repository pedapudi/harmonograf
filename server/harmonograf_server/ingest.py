"""Telemetry ingest pipeline.

Owns:
  - Session auto-create on first Hello
  - Agent registration, multi-stream support (many streams per agent_id)
  - SpanStart/Update/End persistence with idempotent dedup
  - PayloadUpload chunk assembly and digest verification
  - Heartbeat tracking + background liveness check (>15s → DISCONNECTED)
  - ControlAck forwarding to a ControlRouter (if wired up)
  - Goodbye handling: mark the stream's owning agent DISCONNECTED if no
    other live streams remain for that agent_id.

The gRPC surface lives in rpc/telemetry.py — this module is transport
agnostic so it can be unit-tested with synthetic TelemetryUp iterators.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, Protocol

from harmonograf_server.bus import SessionBus
from harmonograf_server.convert import (
    attr_map_to_dict,
    hello_to_agent,
    pb_span_to_storage,
    span_status_from_pb,
    ts_to_float,
)
from harmonograf_server.pb import telemetry_pb2, types_pb2
from harmonograf_server.storage import (
    Agent,
    AgentStatus,
    Framework,
    Session,
    SessionStatus,
    Store,
)


logger = logging.getLogger(__name__)


HEARTBEAT_TIMEOUT_S = 15.0
HEARTBEAT_CHECK_INTERVAL_S = 5.0
PAYLOAD_MAX_BYTES = 64 * 1024 * 1024  # hard ceiling per digest — guards against runaway uploads

_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


class ControlAckSink(Protocol):
    """Minimal interface the ingest pipeline needs from a ControlRouter.

    The stream_id lets the router attribute acks to the specific
    SubscribeControl subscription that delivered the event, which matters
    when the same agent has multiple concurrent control streams.
    """

    def record_ack(
        self, ack: types_pb2.ControlAck, *, stream_id: Optional[str] = None
    ) -> None: ...


class _NullControlAckSink:
    def record_ack(
        self, ack: types_pb2.ControlAck, *, stream_id: Optional[str] = None
    ) -> None:  # pragma: no cover - trivial
        return None


@dataclass
class _PayloadAssembler:
    digest: str
    mime: str
    total_size: int
    chunks: list[bytes] = field(default_factory=list)
    received_bytes: int = 0
    evicted: bool = False

    def add(self, chunk: bytes) -> None:
        self.received_bytes += len(chunk)
        if self.received_bytes > PAYLOAD_MAX_BYTES:
            raise ValueError(f"payload {self.digest} exceeds {PAYLOAD_MAX_BYTES} bytes")
        self.chunks.append(chunk)

    def finalize(self) -> bytes:
        return b"".join(self.chunks)


@dataclass
class StreamContext:
    """Per-stream state. One StreamTelemetry RPC == one StreamContext."""

    stream_id: str
    agent_id: str
    session_id: str
    connected_at: float
    last_heartbeat: float
    name: str = ""
    framework: int = 0
    framework_version: str = ""
    capabilities: tuple[int, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)
    payloads: dict[str, _PayloadAssembler] = field(default_factory=dict)
    # seen span ids (for fast dedup ahead of storage)
    seen_span_ids: set[str] = field(default_factory=set)
    # (session_id, agent_id) tuples we've already auto-registered on this
    # stream — avoids re-creating sessions/agents for every span.
    seen_routes: set[tuple[str, str]] = field(default_factory=set)


class IngestPipeline:
    """Stateful ingest logic shared across all telemetry streams."""

    def __init__(
        self,
        store: Store,
        bus: SessionBus,
        *,
        control_sink: Optional[ControlAckSink] = None,
        now_fn: Callable[[], float] = time.time,
        heartbeat_timeout_s: float = HEARTBEAT_TIMEOUT_S,
    ) -> None:
        self._store = store
        self._bus = bus
        self._control_sink = control_sink or _NullControlAckSink()
        self._now = now_fn
        self._heartbeat_timeout_s = heartbeat_timeout_s

        # per-agent connection registry: agent_id -> {stream_id: StreamContext}
        self._streams_by_agent: dict[str, dict[str, StreamContext]] = {}
        self._stream_seq = 0
        self._lock = asyncio.Lock()

        # daily session counter for auto-generated session ids
        self._session_day_counters: dict[str, int] = {}

    # ---- public API ---------------------------------------------------

    @property
    def bus(self) -> SessionBus:
        return self._bus

    @property
    def store(self) -> Store:
        return self._store

    def live_streams(self, agent_id: str) -> list[StreamContext]:
        return list(self._streams_by_agent.get(agent_id, {}).values())

    def active_stream_count(self) -> int:
        return sum(len(b) for b in self._streams_by_agent.values())

    async def handle_hello(self, hello: telemetry_pb2.Hello) -> tuple[StreamContext, Session]:
        """Process a Hello and register a new StreamContext. Returns the
        context and the (possibly just-created) Session row.
        """
        if not hello.agent_id:
            raise ValueError("Hello.agent_id is required")

        now = self._now()
        session_id = hello.session_id or self._generate_session_id(now)
        if not _SESSION_ID_RE.match(session_id):
            raise ValueError(
                f"invalid session_id: must match [a-zA-Z0-9_-]{{1,128}}: {session_id!r}"
            )

        session = await self._store.get_session(session_id)
        if session is None:
            title = hello.session_title or session_id
            session = Session(
                id=session_id,
                title=title,
                created_at=now,
                status=SessionStatus.LIVE,
                metadata=dict(hello.metadata) if hello.metadata else {},
            )
            session = await self._store.create_session(session)
            logger.info("session created session_id=%s agent_id=%s", session_id, hello.agent_id)

        agent = hello_to_agent(
            hello, session_id=session_id, connected_at=now, last_heartbeat=now
        )
        await self._store.register_agent(agent)
        self._bus.publish_agent_upsert(agent)

        async with self._lock:
            self._stream_seq += 1
            stream_id = f"str_{int(now)}_{self._stream_seq}"
            ctx = StreamContext(
                stream_id=stream_id,
                agent_id=hello.agent_id,
                session_id=session_id,
                connected_at=now,
                last_heartbeat=now,
                name=hello.name or hello.agent_id,
                framework=int(hello.framework),
                framework_version=hello.framework_version or "",
                capabilities=tuple(int(c) for c in hello.capabilities),
                metadata=dict(hello.metadata) if hello.metadata else {},
                seen_routes={(session_id, hello.agent_id)},
            )
            self._streams_by_agent.setdefault(hello.agent_id, {})[stream_id] = ctx

        logger.info(
            "stream opened session_id=%s agent_id=%s stream_id=%s",
            session_id,
            hello.agent_id,
            stream_id,
        )
        return ctx, session

    async def handle_message(
        self, ctx: StreamContext, msg: telemetry_pb2.TelemetryUp
    ) -> None:
        """Dispatch a single TelemetryUp message (not Hello — that is handled
        by handle_hello)."""
        kind = msg.WhichOneof("msg")
        if kind == "span_start":
            await self._handle_span_start(ctx, msg.span_start)
        elif kind == "span_update":
            await self._handle_span_update(ctx, msg.span_update)
        elif kind == "span_end":
            await self._handle_span_end(ctx, msg.span_end)
        elif kind == "payload":
            await self._handle_payload(ctx, msg.payload)
        elif kind == "heartbeat":
            await self._handle_heartbeat(ctx, msg.heartbeat)
        elif kind == "control_ack":
            self._control_sink.record_ack(msg.control_ack, stream_id=ctx.stream_id)
        elif kind == "goodbye":
            await self._handle_goodbye(ctx, msg.goodbye)
        elif kind == "hello":
            raise ValueError("Hello may only be the first TelemetryUp on a stream")
        else:
            logger.debug("ignoring unknown TelemetryUp kind: %s", kind)

    async def close_stream(self, ctx: StreamContext, *, reason: str = "") -> None:
        """Called when a StreamTelemetry RPC exits for any reason.

        Removes the stream from the registry. If no other live streams remain
        for the same agent_id, marks the agent DISCONNECTED.
        """
        async with self._lock:
            bucket = self._streams_by_agent.get(ctx.agent_id)
            if bucket is not None:
                bucket.pop(ctx.stream_id, None)
                if not bucket:
                    del self._streams_by_agent[ctx.agent_id]
                    remaining = 0
                else:
                    remaining = len(bucket)
            else:
                remaining = 0

        if remaining == 0:
            now = self._now()
            await self._store.update_agent_status(
                ctx.session_id, ctx.agent_id, AgentStatus.DISCONNECTED, last_heartbeat=now
            )
            self._bus.publish_agent_status(
                ctx.session_id, ctx.agent_id, AgentStatus.DISCONNECTED, now
            )
            logger.info(
                "stream closed session_id=%s agent_id=%s stream_id=%s reason=%s",
                ctx.session_id,
                ctx.agent_id,
                ctx.stream_id,
                reason or "eof",
            )

    # ---- heartbeat sweep ---------------------------------------------

    async def sweep_heartbeats(self) -> list[StreamContext]:
        """Scan live streams; return those whose last_heartbeat is older than
        the timeout. Callers (the RPC layer) are responsible for shutting the
        corresponding streams down.
        """
        now = self._now()
        expired: list[StreamContext] = []
        async with self._lock:
            for bucket in self._streams_by_agent.values():
                for ctx in bucket.values():
                    if now - ctx.last_heartbeat > self._heartbeat_timeout_s:
                        expired.append(ctx)
        return expired

    # ---- internals ----------------------------------------------------

    def _generate_session_id(self, now: float) -> str:
        day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        self._session_day_counters[day] = self._session_day_counters.get(day, 0) + 1
        return f"sess_{day}_{self._session_day_counters[day]:04d}"

    async def _handle_span_start(
        self, ctx: StreamContext, msg: telemetry_pb2.SpanStart
    ) -> None:
        pb_span = msg.span
        if not pb_span.id:
            raise ValueError("SpanStart.span.id is required")
        if pb_span.id in ctx.seen_span_ids:
            return  # fast local dedup
        ctx.seen_span_ids.add(pb_span.id)

        # Per-span agent_id / session_id overrides — these let one
        # client emit on behalf of multiple sub-agents and multiple
        # sessions over a single StreamTelemetry RPC. Falls back to the
        # stream's defaults from Hello when not set.
        agent_id = pb_span.agent_id or ctx.agent_id
        session_id = pb_span.session_id or ctx.session_id
        await self._ensure_route(ctx, session_id, agent_id, name=pb_span.name)

        span = pb_span_to_storage(
            pb_span, agent_id=agent_id, session_id=session_id
        )
        stored = await self._store.append_span(span)
        self._bus.publish_span_start(stored)

    async def _ensure_route(
        self,
        ctx: StreamContext,
        session_id: str,
        agent_id: str,
        *,
        name: str = "",
    ) -> None:
        """Auto-register (session, agent) the first time we see them on
        this stream. The session inherits the stream's framework metadata
        from Hello so cross-session views still know what produced it.
        """
        key = (session_id, agent_id)
        if key in ctx.seen_routes:
            return
        ctx.seen_routes.add(key)
        now = self._now()
        if await self._store.get_session(session_id) is None:
            session = Session(
                id=session_id,
                title=session_id,
                created_at=now,
                status=SessionStatus.LIVE,
                metadata={},
            )
            try:
                await self._store.create_session(session)
            except Exception as e:
                logger.warning(
                    "auto session create failed session_id=%s agent_id=%s: %s",
                    session_id,
                    agent_id,
                    e,
                )
                return
            logger.info(
                "auto session created session_id=%s by agent_id=%s",
                session_id,
                agent_id,
            )
        if await self._store.get_agent(session_id, agent_id) is None:
            agent = Agent(
                id=agent_id,
                session_id=session_id,
                name=name or agent_id,
                framework=Framework.UNKNOWN,
                framework_version=ctx.framework_version or "",
                capabilities=[],
                metadata={},
                connected_at=now,
                last_heartbeat=now,
                status=AgentStatus.CONNECTED,
            )
            await self._store.register_agent(agent)
            self._bus.publish_agent_upsert(agent)
            logger.info(
                "auto agent registered session_id=%s agent_id=%s",
                session_id,
                agent_id,
            )

    async def _handle_span_update(
        self, ctx: StreamContext, msg: telemetry_pb2.SpanUpdate
    ) -> None:
        status = None
        if msg.status != types_pb2.SPAN_STATUS_UNSPECIFIED:
            status = span_status_from_pb(msg.status)
        attrs = attr_map_to_dict(msg.attributes) if msg.attributes else None
        payload_kwargs = _payload_ref_kwargs(msg.payload_refs)
        updated = await self._store.update_span(
            msg.span_id,
            status=status,
            attributes=attrs,
            **payload_kwargs,
        )
        if updated is not None:
            self._bus.publish_span_update(updated)

    async def _handle_span_end(
        self, ctx: StreamContext, msg: telemetry_pb2.SpanEnd
    ) -> None:
        end_time = ts_to_float(msg.end_time) if msg.HasField("end_time") else self._now()
        status = span_status_from_pb(msg.status)
        error = None
        if msg.HasField("error"):
            error = {
                "type": msg.error.type,
                "message": msg.error.message,
                "stack": msg.error.stack,
            }
        ended = await self._store.end_span(msg.span_id, end_time, status, error=error)
        if ended is None:
            return
        if msg.attributes or len(msg.payload_refs):
            attrs = attr_map_to_dict(msg.attributes) if msg.attributes else None
            payload_kwargs = _payload_ref_kwargs(msg.payload_refs)
            ended = await self._store.update_span(
                msg.span_id, attributes=attrs, **payload_kwargs
            ) or ended
        self._bus.publish_span_end(ended)

    async def _handle_payload(
        self, ctx: StreamContext, msg: telemetry_pb2.PayloadUpload
    ) -> None:
        if not msg.digest:
            raise ValueError("PayloadUpload.digest is required")
        if msg.evicted:
            # Client told us it dropped this payload under backpressure. Nothing
            # to store, but clear any partial assembler state.
            ctx.payloads.pop(msg.digest, None)
            return
        assembler = ctx.payloads.get(msg.digest)
        if assembler is None:
            assembler = _PayloadAssembler(
                digest=msg.digest, mime=msg.mime, total_size=msg.total_size
            )
            ctx.payloads[msg.digest] = assembler
        if msg.chunk:
            assembler.add(msg.chunk)
        if msg.last:
            data = assembler.finalize()
            actual = hashlib.sha256(data).hexdigest()
            if actual != msg.digest:
                logger.warning(
                    "payload digest mismatch expected=%s actual=%s agent_id=%s",
                    msg.digest,
                    actual,
                    ctx.agent_id,
                )
                ctx.payloads.pop(msg.digest, None)
                raise ValueError(
                    f"payload digest mismatch: declared={msg.digest} actual={actual}"
                )
            summary = _summarize(data, assembler.mime)
            await self._store.put_payload(
                msg.digest, data, assembler.mime, summary=summary
            )
            ctx.payloads.pop(msg.digest, None)

    async def _handle_heartbeat(
        self, ctx: StreamContext, msg: telemetry_pb2.Heartbeat
    ) -> None:
        now = self._now()
        ctx.last_heartbeat = now
        await self._store.update_agent_status(
            ctx.session_id, ctx.agent_id, AgentStatus.CONNECTED, last_heartbeat=now
        )
        self._bus.publish_heartbeat(
            ctx.session_id,
            ctx.agent_id,
            {
                "buffered_events": msg.buffered_events,
                "dropped_events": msg.dropped_events,
                "dropped_spans_critical": msg.dropped_spans_critical,
                "buffered_payload_bytes": msg.buffered_payload_bytes,
                "payloads_evicted": msg.payloads_evicted,
                "cpu_self_pct": msg.cpu_self_pct,
                "last_heartbeat": now,
            },
        )

    async def _handle_goodbye(
        self, ctx: StreamContext, msg: telemetry_pb2.Goodbye
    ) -> None:
        await self.close_stream(ctx, reason=msg.reason or "goodbye")


def _payload_ref_kwargs(refs) -> dict:
    """Extract the first PayloadRef's metadata as update_span kwargs.

    Returns an empty dict when no refs are present so the caller can splat it
    without triggering a spurious digest clear.
    """
    if not len(refs):
        return {}
    ref = refs[0]
    return {
        "payload_digest": ref.digest,
        "payload_mime": ref.mime,
        "payload_size": ref.size,
        "payload_summary": ref.summary,
        "payload_role": ref.role,
        "payload_evicted": ref.evicted,
    }


def _summarize(data: bytes, mime: str) -> str:
    if mime.startswith("text/") or mime in ("application/json",):
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        return text[:200]
    return f"<{mime} {len(data)} bytes>"
