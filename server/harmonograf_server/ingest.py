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
    _drift_kind_pb_to_string,
    _drift_severity_pb_to_string,
    attr_map_to_dict,
    goldfive_pb_plan_to_storage,
    hello_to_agent,
    pb_span_to_storage,
    span_status_from_pb,
    task_status_from_pb,
    ts_to_float,
)
from goldfive.pb.goldfive.v1 import control_pb2 as gf_control_pb2

from harmonograf_server.pb import telemetry_pb2, types_pb2
from harmonograf_server.storage import (
    Agent,
    AgentStatus,
    Framework,
    Session,
    SessionStatus,
    SpanKind,
    SpanStatus,
    Store,
    TaskPlan,
    TaskStatus,
)

# Only leaf execution spans can bind a plan task to a lifecycle status.
# Wrapper spans (INVOCATION, TRANSFER) don't represent task work and
# their lifecycles must never flip task state — binding an INVOCATION
# to a task would mark the task COMPLETED the moment the outer agent
# finishes its first turn, long before the actual work runs.
_TASK_BINDING_SPAN_KINDS = frozenset({SpanKind.LLM_CALL, SpanKind.TOOL_CALL})


logger = logging.getLogger(__name__)


HEARTBEAT_TIMEOUT_S = 15.0
HEARTBEAT_CHECK_INTERVAL_S = 5.0
STUCK_THRESHOLD_BEATS = 3  # 3 consecutive unchanged heartbeats ≈ 15s
PAYLOAD_MAX_BYTES = 64 * 1024 * 1024  # hard ceiling per digest — guards against runaway uploads

_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


class ControlAckSink(Protocol):
    """Minimal interface the ingest pipeline needs from a ControlRouter.

    The stream_id lets the router attribute acks to the specific
    SubscribeControl subscription that delivered the event, which matters
    when the same agent has multiple concurrent control streams.
    """

    def record_ack(
        self, ack: gf_control_pb2.ControlAck, *, stream_id: Optional[str] = None
    ) -> None: ...


class _NullControlAckSink:
    def record_ack(
        self, ack: gf_control_pb2.ControlAck, *, stream_id: Optional[str] = None
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


class _SessionView:
    """Read-only StreamContext overlay that pins ``session_id`` to a new
    value while proxying every other attribute back to the underlying
    :class:`StreamContext`.

    Created when a goldfive event carries a ``session_id`` different
    from the stream's Hello session — downstream handlers read
    ``ctx.session_id`` for bus fan-out and storage writes, so rather
    than thread the target session through every handler signature we
    hand them a ctx-shaped view with the correct session pinned. All
    mutable state (``seen_span_ids``, ``seen_routes``, payload
    assemblers, heartbeat counters) remains on the real StreamContext
    so cross-session bookkeeping still works.
    """

    __slots__ = ("_inner", "session_id")

    def __init__(self, inner: "StreamContext", session_id: str) -> None:
        # session_id is stored on self so ``ctx.session_id`` reads
        # return the overridden value; everything else falls through.
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "session_id", session_id)

    def __getattr__(self, name: str) -> Any:
        # Only reached when the attribute isn't on the view itself.
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        # Writes pass through to the underlying StreamContext so state
        # shared across sessions (seen_span_ids, heartbeat counters,
        # etc.) stays consistent. ``session_id`` is the only name we
        # deliberately shadow and it's frozen for the view's lifetime.
        if name == "session_id":
            raise AttributeError("_SessionView.session_id is read-only")
        setattr(self._inner, name, value)


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
    last_progress_counter: int = -1
    stuck_heartbeat_count: int = 0
    current_activity: str = ""
    is_stuck: bool = False


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

        # In-memory index: session_id -> {task_id: plan_id}. Populated as
        # TaskPlan messages arrive; used to resolve `hgraf.task_id` span
        # attributes without a full table scan on every span start.
        self._task_index: dict[str, dict[str, str]] = {}

        # Per-session ring of recent DriftDetected events. Frontend
        # replays these on WatchSession initial burst so synthetic-actor
        # rows (user / goldfive) and trajectory drift markers survive
        # reconnects without requiring a Store schema migration. Bounded
        # so a long-running session with many drifts cannot balloon RAM.
        self._drifts_by_session: dict[str, list[dict[str, Any]]] = {}
        self._drift_ring_max = 500

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
        elif kind == "goldfive_event":
            await self._handle_goldfive_event(ctx, msg.goldfive_event)
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
        # Harvest per-ADK-agent hints stamped by the telemetry plugin
        # (harmonograf#74) — the first span from a new ADK agent
        # carries hgraf.agent.{name,parent_id,kind,branch} so the
        # server can register the row with the right human-readable
        # name + parent link without needing a dedicated
        # AgentRegister wire event.
        agent_hints = _extract_agent_hints(pb_span.attributes) if pb_span.attributes else {}
        await self._ensure_route(
            ctx, session_id, agent_id, name=pb_span.name, agent_hints=agent_hints
        )

        span = pb_span_to_storage(
            pb_span, agent_id=agent_id, session_id=session_id
        )
        stored = await self._store.append_span(span)
        self._bus.publish_span_start(stored)

        # Proactive task report via span attribute.
        if pb_span.attributes:
            task_report_attr = pb_span.attributes.get("task_report")
            if task_report_attr is not None and task_report_attr.HasField("string_value"):
                self._bus.publish_task_report(session_id, agent_id, task_report_attr.string_value)

            # Span-to-task binding: spans that execute a planned task carry
            # an `hgraf.task_id` string attribute. Transition the matching
            # task to RUNNING and record the span id. Only leaf execution
            # spans (LLM_CALL / TOOL_CALL) can bind — wrapper spans like
            # INVOCATION / TRANSFER are ignored even if stamped, because
            # their lifecycles don't correspond to task execution.
            task_id_attr = pb_span.attributes.get("hgraf.task_id")
            if task_id_attr is not None and task_id_attr.HasField("string_value"):
                task_id_val = task_id_attr.string_value
                if task_id_val and span.kind in _TASK_BINDING_SPAN_KINDS:
                    await self._bind_task_to_span(
                        session_id, task_id_val, pb_span.id, TaskStatus.RUNNING
                    )
                elif task_id_val:
                    logger.debug(
                        "ignoring hgraf.task_id=%s on non-leaf span kind=%s",
                        task_id_val,
                        span.kind,
                    )

    async def _ensure_route(
        self,
        ctx: StreamContext,
        session_id: str,
        agent_id: str,
        *,
        name: str = "",
        agent_hints: Optional[dict[str, str]] = None,
    ) -> None:
        """Auto-register (session, agent) the first time we see them on
        this stream. The session inherits the stream's framework metadata
        from Hello so cross-session views still know what produced it.

        ``agent_hints`` (harmonograf#74) carries ``hgraf.agent.*``
        attributes harvested from the first span emitted by this
        per-ADK-agent id. The plugin stamps them on first-sight so the
        server can register the agent with a human-readable name and
        parent-agent link without a new wire event. Subsequent spans
        from the same agent skip the stamp (``seen_routes`` short-
        circuits the whole method), so hints only matter once.
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
            # Prefer explicit ADK agent name from the span hints over
            # the bare first-span name (span.name is the LLM model for
            # LLM_CALL / tool name for TOOL_CALL — not useful as an
            # agent label). Fall back to the span name, then the
            # agent_id itself.
            hints = agent_hints or {}
            adk_name = hints.get("hgraf.agent.name", "")
            display_name = adk_name or name or agent_id
            meta: dict[str, str] = {}
            if adk_name:
                meta["adk.agent.name"] = adk_name
            parent_id = hints.get("hgraf.agent.parent_id", "")
            if parent_id:
                meta["harmonograf.parent_agent_id"] = parent_id
            kind = hints.get("hgraf.agent.kind", "")
            if kind:
                meta["harmonograf.agent_kind"] = kind
            branch = hints.get("hgraf.agent.branch", "")
            if branch:
                meta["adk.agent.branch"] = branch
            agent = Agent(
                id=agent_id,
                session_id=session_id,
                name=display_name,
                framework=Framework.ADK if adk_name else Framework.UNKNOWN,
                framework_version=ctx.framework_version or "",
                capabilities=[],
                metadata=meta,
                connected_at=now,
                last_heartbeat=now,
                status=AgentStatus.CONNECTED,
            )
            await self._store.register_agent(agent)
            self._bus.publish_agent_upsert(agent)
            logger.info(
                "auto agent registered session_id=%s agent_id=%s name=%s kind=%s parent=%s",
                session_id,
                agent_id,
                display_name,
                kind or "<none>",
                parent_id or "<none>",
            )
            # If the span's agent_id differs from the transport's registered
            # agent_id (e.g. ADK sub-agent name vs identity-file UUID), tell
            # the control router so controls for the sub-agent name are
            # forwarded to the stream that actually owns it.
            if agent_id != ctx.agent_id and hasattr(self._control_sink, "register_alias"):
                self._control_sink.register_alias(agent_id, ctx.agent_id)
                logger.debug(
                    "control alias registered sub=%s stream=%s", agent_id, ctx.agent_id
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

            # Proactive task report via span attribute.
            if msg.attributes:
                task_report_attr = msg.attributes.get("task_report")
                if task_report_attr is not None and task_report_attr.HasField("string_value"):
                    agent_id = updated.agent_id
                    session_id = updated.session_id
                    self._bus.publish_task_report(session_id, agent_id, task_report_attr.string_value)

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

        # Task completion is driven EXCLUSIVELY by explicit client
        # ``task_status_update`` messages — a single LLM/TOOL span ending
        # is one of N calls while executing the task, not "task done".
        # Terminal FAILED/CANCELLED still propagates: an errored leaf
        # span is a real signal that the task itself failed.
        task_id_val = (ended.attributes or {}).get("hgraf.task_id")
        if (
            isinstance(task_id_val, str)
            and task_id_val
            and ended.kind in _TASK_BINDING_SPAN_KINDS
        ):
            task_status = _span_status_to_task_status(ended.status)
            if task_status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                await self._bind_task_to_span(
                    ended.session_id, task_id_val, ended.id, task_status
                )
        elif isinstance(task_id_val, str) and task_id_val:
            logger.debug(
                "ignoring hgraf.task_id=%s on ended non-leaf span kind=%s",
                task_id_val,
                ended.kind,
            )

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

        if msg.progress_counter > 0 and msg.progress_counter == ctx.last_progress_counter:
            ctx.stuck_heartbeat_count += 1
        else:
            ctx.stuck_heartbeat_count = 0
            ctx.last_progress_counter = msg.progress_counter
        ctx.current_activity = msg.current_activity
        now_stuck = ctx.stuck_heartbeat_count >= STUCK_THRESHOLD_BEATS
        if now_stuck != ctx.is_stuck:
            ctx.is_stuck = now_stuck
            self._bus.publish_agent_status(
                ctx.session_id,
                ctx.agent_id,
                AgentStatus.CONNECTED,
                now,
                current_activity=ctx.current_activity,
                progress_counter=ctx.last_progress_counter,
                stuck=ctx.is_stuck,
            )

        # Context-window telemetry: persist + fan out. Zero-valued
        # samples mean "client has no current LLM context observation"
        # and are intentionally skipped so the series stays signal-only.
        tokens = int(msg.context_window_tokens)
        limit_tokens = int(msg.context_window_limit_tokens)
        if tokens > 0 or limit_tokens > 0:
            from harmonograf_server.storage.base import ContextWindowSample

            sample = ContextWindowSample(
                session_id=ctx.session_id,
                agent_id=ctx.agent_id,
                recorded_at=now,
                tokens=tokens,
                limit_tokens=limit_tokens,
            )
            try:
                await self._store.append_context_window_sample(sample)
            except Exception as exc:  # noqa: BLE001
                logger.debug("append_context_window_sample failed: %s", exc)
            self._bus.publish_context_window_sample(
                ctx.session_id,
                ctx.agent_id,
                tokens,
                limit_tokens,
                now,
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
                "current_activity": msg.current_activity,
                "progress_counter": msg.progress_counter,
                "stuck": ctx.is_stuck,
            },
        )

    async def _handle_goldfive_event(self, ctx: StreamContext, event: Any) -> None:
        """Dispatch a ``goldfive.v1.Event`` to the per-kind handler.

        Goldfive owns orchestration after the Phase A proto migration
        (issue #2); plans, task state transitions, drift markers, and
        run lifecycle signals all arrive here wrapped in a single
        ``TelemetryUp.goldfive_event`` envelope. Each payload variant
        drives a storage mutation and/or bus fan-out so the frontend
        Gantt stays live. Unknown payload variants are logged and
        ignored so adding a new event kind in goldfive is
        forward-compatible with an older harmonograf server.

        Routing: goldfive#155 / PR #157 added ``Event.session_id`` and
        the Runner/Steerer/Executors stamp it on every emitted event so
        one transport stream can multiplex events across sessions
        (e.g. ADK's AgentTool mints sub-Runner sessions inside a single
        adk-web run). When the event carries a session_id we route to
        it, auto-creating the session+agent row if unseen. An empty
        ``session_id`` falls back to the stream's Hello session — the
        pre-#155 behavior — for back-compat with older goldfive clients.
        """
        if event is None:
            return
        kind = event.WhichOneof("payload")
        run_id = event.run_id
        sequence = event.sequence

        event_sid = getattr(event, "session_id", "") or ""
        if event_sid and event_sid != ctx.session_id:
            # Route this event onto its declared session. Use a shallow
            # per-session StreamContext proxy so downstream handlers
            # (which read ctx.session_id / ctx.agent_id for bus fan-out
            # and storage writes) see the right session id without
            # having to thread session_id through every handler
            # signature. Auto-register the (session, agent) route the
            # same way span ingest does so the session row exists
            # before any task-plan / drift fan-out references it.
            await self._ensure_route(ctx, event_sid, ctx.agent_id)
            target_ctx = _SessionView(ctx, event_sid)
        else:
            target_ctx = ctx

        logger.debug(
            "goldfive event session_id=%s run_id=%s sequence=%s kind=%s",
            target_ctx.session_id,
            run_id,
            sequence,
            kind,
        )
        if kind == "run_started":
            await self._on_run_started(target_ctx, event.run_started, run_id)
        elif kind == "goal_derived":
            await self._on_goal_derived(target_ctx, event.goal_derived, run_id)
        elif kind == "plan_submitted":
            await self._on_plan_submitted(target_ctx, event.plan_submitted, run_id)
        elif kind == "plan_revised":
            await self._on_plan_revised(target_ctx, event.plan_revised, run_id)
        elif kind == "task_started":
            await self._on_task_started(target_ctx, event.task_started, run_id)
        elif kind == "task_progress":
            self._on_task_progress(target_ctx, event.task_progress, run_id)
        elif kind == "task_completed":
            await self._on_task_completed(target_ctx, event.task_completed, run_id)
        elif kind == "task_failed":
            await self._on_task_failed(target_ctx, event.task_failed, run_id)
        elif kind == "task_blocked":
            await self._on_task_blocked(target_ctx, event.task_blocked, run_id)
        elif kind == "task_cancelled":
            await self._on_task_cancelled(target_ctx, event.task_cancelled, run_id)
        elif kind == "drift_detected":
            self._on_drift_detected(target_ctx, event.drift_detected, run_id)
        elif kind == "run_completed":
            await self._on_run_completed(target_ctx, event.run_completed, run_id)
        elif kind == "run_aborted":
            await self._on_run_aborted(target_ctx, event.run_aborted, run_id)
        elif kind == "agent_invocation_started":
            self._on_agent_invocation_started(
                target_ctx, event.agent_invocation_started, run_id
            )
        elif kind == "agent_invocation_completed":
            self._on_agent_invocation_completed(
                target_ctx, event.agent_invocation_completed, run_id
            )
        elif kind == "delegation_observed":
            self._on_delegation_observed(target_ctx, event.delegation_observed, run_id)
        else:
            logger.debug(
                "ignoring unknown goldfive event payload kind=%s on session_id=%s",
                kind,
                target_ctx.session_id,
            )

    # ---- goldfive event handlers -------------------------------------

    async def _on_run_started(
        self, ctx: StreamContext, payload: Any, run_id: str
    ) -> None:
        started_at: Optional[float] = None
        if payload.HasField("started_at"):
            started_at = ts_to_float(payload.started_at)
        self._bus.publish_run_started(
            ctx.session_id,
            run_id,
            goal_summary=payload.goal_summary or "",
            started_at=started_at,
        )

    async def _on_goal_derived(
        self, ctx: StreamContext, payload: Any, run_id: str
    ) -> None:
        goals = [
            {
                "id": g.id,
                "summary": g.summary,
                "metadata": dict(g.metadata),
                "has_success_predicate": bool(
                    g.HasField("has_success_predicate") and g.has_success_predicate
                ),
            }
            for g in payload.goals
        ]
        self._bus.publish_goal_derived(ctx.session_id, run_id, goals)

    async def _on_plan_submitted(
        self, ctx: StreamContext, payload: Any, run_id: str
    ) -> None:
        await self._upsert_plan(ctx, payload.plan, run_id=run_id)

    async def _on_plan_revised(
        self, ctx: StreamContext, payload: Any, run_id: str
    ) -> None:
        # The revision metadata lives both on ``payload.plan`` (inline in
        # goldfive's ``Plan``) and as flattened top-level fields on the
        # PlanRevised event. Prefer the event-level fields when the plan
        # itself is missing them — goldfive emits the flattened copy to
        # save sinks from unpacking the plan.
        stored = await self._upsert_plan(ctx, payload.plan, run_id=run_id)
        if stored is None:
            return
        overwrites: dict[str, Any] = {}
        if not stored.revision_kind:
            kind_str = _drift_kind_pb_to_string(payload.drift_kind)
            if kind_str:
                overwrites["revision_kind"] = kind_str
        if not stored.revision_severity:
            sev_str = _drift_severity_pb_to_string(payload.severity)
            if sev_str:
                overwrites["revision_severity"] = sev_str
        if not stored.revision_reason and payload.reason:
            overwrites["revision_reason"] = payload.reason
        if payload.revision_index and not stored.revision_index:
            overwrites["revision_index"] = int(payload.revision_index)
        # harmonograf#99 / goldfive#199: persist the envelope-level
        # ``trigger_event_id`` so the intervention aggregator can
        # strict-id-merge the plan-revision row onto its originating
        # annotation or drift. Prefer the envelope value; fall through
        # to ``plan.revision_trigger_event_id`` (populated by goldfive's
        # ``_apply_revision``) only if the envelope stamp is missing.
        envelope_trig = getattr(payload, "trigger_event_id", "") or ""
        if envelope_trig and not stored.trigger_event_id:
            overwrites["trigger_event_id"] = envelope_trig
        if overwrites:
            for k, v in overwrites.items():
                setattr(stored, k, v)
            await self._store.put_task_plan(stored)
            self._bus.publish_task_plan(stored)

    async def _upsert_plan(
        self, ctx: StreamContext, pb_plan: Any, *, run_id: str
    ) -> Optional[TaskPlan]:
        """Translate a ``goldfive.v1.Plan`` and persist + fan out the result.

        Returns the stored ``TaskPlan`` so callers (PlanRevised) can
        enrich it with the flattened revision metadata if needed.
        """
        if not pb_plan.id:
            logger.warning(
                "goldfive plan missing id on session_id=%s run_id=%s; dropping",
                ctx.session_id,
                run_id,
            )
            return None
        created_at = ts_to_float(pb_plan.created_at) if pb_plan.HasField("created_at") else self._now()
        stored = goldfive_pb_plan_to_storage(
            pb_plan,
            session_id=ctx.session_id,
            created_at=created_at,
            planner_agent_id=ctx.agent_id,
        )
        stored = await self._store.put_task_plan(stored)
        # Refresh the task index for span-to-task binding lookups on the
        # hot path. A re-emitted plan with the same id replaces previous
        # index entries so stale task ids are pruned.
        idx = self._task_index.setdefault(ctx.session_id, {})
        # Drop prior mappings that pointed at this plan id.
        for task_id in [tid for tid, pid in idx.items() if pid == stored.id]:
            idx.pop(task_id, None)
        for task in stored.tasks:
            idx[task.id] = stored.id
        self._bus.publish_task_plan(stored)
        return stored

    async def _on_task_started(
        self, ctx: StreamContext, payload: Any, run_id: str
    ) -> None:
        await self._apply_goldfive_task_status(
            ctx, payload.task_id, TaskStatus.RUNNING, run_id=run_id
        )

    def _on_task_progress(
        self, ctx: StreamContext, payload: Any, run_id: str
    ) -> None:
        # Progress is a high-frequency, non-terminal signal — fan out on
        # the bus so the frontend can render a progress bar, but do not
        # persist. Matches the design note in §5.2 of the migration plan.
        self._bus.publish_task_progress(
            ctx.session_id,
            run_id,
            task_id=payload.task_id,
            fraction=float(payload.fraction),
            detail=payload.detail,
        )

    async def _on_task_completed(
        self, ctx: StreamContext, payload: Any, run_id: str
    ) -> None:
        await self._apply_goldfive_task_status(
            ctx, payload.task_id, TaskStatus.COMPLETED, run_id=run_id
        )

    async def _on_task_failed(
        self, ctx: StreamContext, payload: Any, run_id: str
    ) -> None:
        await self._apply_goldfive_task_status(
            ctx, payload.task_id, TaskStatus.FAILED, run_id=run_id
        )

    async def _on_task_blocked(
        self, ctx: StreamContext, payload: Any, run_id: str
    ) -> None:
        await self._apply_goldfive_task_status(
            ctx, payload.task_id, TaskStatus.BLOCKED, run_id=run_id
        )

    async def _on_task_cancelled(
        self, ctx: StreamContext, payload: Any, run_id: str
    ) -> None:
        await self._apply_goldfive_task_status(
            ctx, payload.task_id, TaskStatus.CANCELLED, run_id=run_id
        )

    async def _apply_goldfive_task_status(
        self,
        ctx: StreamContext,
        task_id: str,
        status: TaskStatus,
        *,
        run_id: str,
    ) -> None:
        if not task_id:
            logger.debug(
                "goldfive task status event missing task_id on session_id=%s run_id=%s",
                ctx.session_id,
                run_id,
            )
            return
        plan_id = self._task_index.get(ctx.session_id, {}).get(task_id)
        if plan_id is None:
            # Fall back to a storage scan: handles pipeline restarts
            # where the in-memory index has not been populated yet.
            plans = await self._store.list_task_plans_for_session(ctx.session_id)
            for p in plans:
                for t in p.tasks:
                    if t.id == task_id:
                        plan_id = p.id
                        self._task_index.setdefault(ctx.session_id, {})[
                            task_id
                        ] = plan_id
                        break
                if plan_id is not None:
                    break
        if plan_id is None:
            logger.debug(
                "goldfive task_id=%s has no matching plan in session_id=%s",
                task_id,
                ctx.session_id,
            )
            return
        updated = await self._store.update_task_status(plan_id, task_id, status)
        if updated is not None:
            self._bus.publish_task_status(ctx.session_id, plan_id, updated)

    def _on_drift_detected(
        self, ctx: StreamContext, payload: Any, run_id: str
    ) -> None:
        # ``annotation_id`` is populated by goldfive for USER_STEER /
        # USER_CANCEL drifts minted from a ControlMessage that carried a
        # bridge-supplied annotation id (goldfive#176). Empty string for
        # drifts goldfive produced itself (loop detection, tool error,
        # etc). Used by the intervention aggregator (#75) to dedup the
        # drift row against the source annotation so a single user STEER
        # no longer surfaces as three separate cards.
        # harmonograf#99 / goldfive#199: ``id`` is the goldfive-minted
        # drift id (UUID4), always non-empty. Used as the strict join
        # key when a follow-up PlanRevised was triggered by an autonomous
        # drift — the aggregator merges plan rows whose trigger_event_id
        # matches this drift's id.
        record: dict[str, Any] = {
            "run_id": run_id,
            "kind": _drift_kind_pb_to_string(payload.kind),
            "severity": _drift_severity_pb_to_string(payload.severity),
            "detail": payload.detail,
            "current_task_id": payload.current_task_id,
            "current_agent_id": payload.current_agent_id,
            "annotation_id": getattr(payload, "annotation_id", "") or "",
            "id": getattr(payload, "id", "") or "",
            "recorded_at": self._now(),
        }
        ring = self._drifts_by_session.setdefault(ctx.session_id, [])
        ring.append(record)
        if len(ring) > self._drift_ring_max:
            del ring[: len(ring) - self._drift_ring_max]
        self._bus.publish_drift(
            ctx.session_id,
            run_id,
            kind=record["kind"],
            severity=record["severity"],
            detail=record["detail"],
            current_task_id=record["current_task_id"],
            current_agent_id=record["current_agent_id"],
            annotation_id=record["annotation_id"],
            drift_id=record["id"],
            recorded_at=record["recorded_at"],
        )

    def drifts_for_session(self, session_id: str) -> list[dict[str, Any]]:
        """Return the in-memory drift ring for ``session_id`` (oldest first).

        Called by the frontend RPC during WatchSession initial burst to
        replay drifts that have been seen this process. Returns an empty
        list when nothing has drifted yet or the session is unknown.
        """
        return list(self._drifts_by_session.get(session_id, []))

    async def _on_run_completed(
        self, ctx: StreamContext, payload: Any, run_id: str
    ) -> None:
        self._bus.publish_run_completed(
            ctx.session_id, run_id, outcome_summary=payload.outcome_summary
        )
        await self._finalize_session(
            ctx.session_id, final_status=SessionStatus.COMPLETED
        )

    async def _on_run_aborted(
        self, ctx: StreamContext, payload: Any, run_id: str
    ) -> None:
        self._bus.publish_run_aborted(
            ctx.session_id, run_id, reason=payload.reason
        )
        await self._finalize_session(
            ctx.session_id, final_status=SessionStatus.ABORTED
        )

    async def _finalize_session(
        self, session_id: str, *, final_status: SessionStatus
    ) -> None:
        """Flip ``session_id`` terminal and close any orphan INVOCATION spans.

        Previously the ingest pipeline published ``run_completed`` /
        ``run_aborted`` onto the bus for trajectory fan-out but never
        mutated the session row itself — ``sessions.status`` stayed LIVE
        and ``ended_at`` stayed NULL forever. The frontend's
        ``sessionIsInactive`` check reads ``sessionStatus`` so the LIVE
        ACTIVITY panel's "N RUNNING" header never cleared after a run
        completed. See harmonograf#96.

        Belt-and-suspenders: the harmonograf telemetry plugin sometimes
        leaks INVOCATION spans keyed by sub-Runner invocation_ids (the
        ADK ``after_run_callback`` is not in a ``finally`` block so a
        cancelled sub-Runner leaves its span open, and goldfive's
        ``on_cancellation`` hook is scoped to the outer invocation_id
        only — see goldfive#196). Close any still-open INVOCATION spans
        on the finalizing session so the Gantt reflects truth.

        Idempotent: a duplicate ``run_completed`` (out-of-order replay,
        reconnect) is a no-op because the store's ``update_session``
        only shifts ``status`` / ``ended_at`` when they change.
        """
        now = time.time()
        try:
            sess = await self._store.update_session(
                session_id,
                status=final_status,
                ended_at=now,
            )
        except Exception as exc:  # noqa: BLE001 — defensive: ingest must not raise
            logger.debug(
                "_finalize_session: update_session raised for %s: %s",
                session_id,
                exc,
            )
            sess = None
        if sess is None:
            # Session unknown (e.g. goldfive event arrived before the
            # Hello established the row). The broadcast below is still
            # useful: subscribers that later join will see the terminal
            # transition via the replayed SessionEnded variant on their
            # initial burst.
            pass
        # Close orphan INVOCATION spans. Filters to ``end_time IS NULL``
        # via storage's span list (we re-check kind and end_time here
        # because list_spans may not expose a direct "open INVOCATION"
        # filter across backends).
        try:
            open_spans = await self._store.get_spans(
                session_id, time_start=None, time_end=None, limit=None
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "_finalize_session: get_spans raised for %s: %s",
                session_id,
                exc,
            )
            open_spans = []
        for span in open_spans:
            if span.end_time is not None:
                continue
            if span.kind is not SpanKind.INVOCATION:
                # Only INVOCATION spans are swept — wrapper spans from
                # an interrupted run. LLM_CALL / TOOL_CALL leaks have
                # their own cleanup paths (after_model_callback,
                # after_tool_callback, on_cancellation) and usually
                # close correctly. Leaving them to those paths keeps
                # this sweeper scope-bounded and lowers the risk of
                # racing with a late-arriving legitimate end_span.
                continue
            try:
                await self._store.end_span(
                    span.id,
                    end_time=now,
                    status=(
                        SpanStatus.COMPLETED
                        if final_status is SessionStatus.COMPLETED
                        else SpanStatus.CANCELLED
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "_finalize_session: end_span raised for %s: %s",
                    span.id,
                    exc,
                )
                continue
            # Publish span-end so the frontend store updates in place
            # (without this the UI still shows "RUNNING" until refresh).
            refreshed = await self._store.get_span(span.id)
            if refreshed is not None:
                self._bus.publish_span_end(refreshed)
        # Fan out the session-level terminal signal last so subscribers
        # that listen for session lifecycle (``sessionIsInactive``, UI
        # banners) see it only after spans are finalized.
        self._bus.publish_session_ended(
            session_id,
            ended_at=now,
            final_status=final_status,
        )

    # Registry-dispatch observability events (goldfive 2986775+). These are
    # forward-only: no state machine side effects, just fan out on the bus
    # so the frontend can render delegation edges / per-invocation rows
    # that the telemetry-plugin INVOCATION spans alone don't capture.
    def _on_agent_invocation_started(
        self, ctx: StreamContext, payload: Any, run_id: str
    ) -> None:
        started_at: Optional[float] = None
        if payload.HasField("started_at"):
            started_at = ts_to_float(payload.started_at)
        self._bus.publish_agent_invocation_started(
            ctx.session_id,
            run_id,
            agent_name=payload.agent_name,
            task_id=payload.task_id,
            invocation_id=payload.invocation_id,
            parent_invocation_id=payload.parent_invocation_id,
            started_at=started_at,
        )

    def _on_agent_invocation_completed(
        self, ctx: StreamContext, payload: Any, run_id: str
    ) -> None:
        completed_at: Optional[float] = None
        if payload.HasField("completed_at"):
            completed_at = ts_to_float(payload.completed_at)
        self._bus.publish_agent_invocation_completed(
            ctx.session_id,
            run_id,
            agent_name=payload.agent_name,
            task_id=payload.task_id,
            invocation_id=payload.invocation_id,
            summary=payload.summary,
            completed_at=completed_at,
        )

    def _on_delegation_observed(
        self, ctx: StreamContext, payload: Any, run_id: str
    ) -> None:
        observed_at: Optional[float] = None
        if payload.HasField("observed_at"):
            observed_at = ts_to_float(payload.observed_at)
        self._bus.publish_delegation_observed(
            ctx.session_id,
            run_id,
            from_agent=payload.from_agent,
            to_agent=payload.to_agent,
            task_id=payload.task_id,
            invocation_id=payload.invocation_id,
            observed_at=observed_at,
        )

    async def _bind_task_to_span(
        self,
        session_id: str,
        task_id: str,
        span_id: str,
        status: TaskStatus,
    ) -> None:
        """Resolve which plan owns `task_id` in `session_id`, update its
        status + bound_span_id, and publish a task_status delta."""
        plan_id = self._task_index.get(session_id, {}).get(task_id)
        if plan_id is None:
            # Fall back to a storage scan in case the plan was persisted
            # before this pipeline instance started (e.g. after a restart).
            plans = await self._store.list_task_plans_for_session(session_id)
            for p in plans:
                for t in p.tasks:
                    if t.id == task_id:
                        plan_id = p.id
                        self._task_index.setdefault(session_id, {})[task_id] = plan_id
                        break
                if plan_id is not None:
                    break
        if plan_id is None:
            logger.debug(
                "hgraf.task_id=%s on span=%s has no matching plan in session=%s",
                task_id,
                span_id,
                session_id,
            )
            return
        updated = await self._store.update_task_status(
            plan_id, task_id, status, bound_span_id=span_id
        )
        if updated is not None:
            self._bus.publish_task_status(session_id, plan_id, updated)

    async def _handle_goodbye(
        self, ctx: StreamContext, msg: telemetry_pb2.Goodbye
    ) -> None:
        await self.close_stream(ctx, reason=msg.reason or "goodbye")


def _span_status_to_task_status(status: SpanStatus) -> Optional[TaskStatus]:
    """Map a terminal span status to the equivalent task status.
    Returns None for non-terminal states (RUNNING, PENDING, AWAITING_HUMAN)
    so that intermediate span updates don't flip a task to a terminal state.
    """
    if status == SpanStatus.COMPLETED:
        return TaskStatus.COMPLETED
    if status == SpanStatus.FAILED:
        return TaskStatus.FAILED
    if status == SpanStatus.CANCELLED:
        return TaskStatus.CANCELLED
    return None


_AGENT_HINT_KEYS = (
    "hgraf.agent.name",
    "hgraf.agent.parent_id",
    "hgraf.agent.kind",
    "hgraf.agent.branch",
)


def _extract_agent_hints(attributes: Any) -> dict[str, str]:
    """Pull ``hgraf.agent.*`` string-valued attributes from a span's attribute map.

    Used by ``_ensure_route`` to populate an auto-registered Agent row's
    ``name`` / ``metadata`` fields without a dedicated wire event. The
    plugin stamps these on the FIRST span emitted by each per-ADK-agent
    id; ``_ensure_route``'s ``seen_routes`` short-circuit means later
    spans from the same agent don't re-pay the attribute scan cost.

    Returns an empty dict when no hints are present, so the caller
    sees the same shape as older clients that don't stamp them —
    preserving back-compat for observe-mode agents that still ship
    spans without ``hgraf.agent.*``.
    """
    out: dict[str, str] = {}
    for key in _AGENT_HINT_KEYS:
        av = attributes.get(key)
        if av is None:
            continue
        if av.HasField("string_value"):
            val = av.string_value
            if val:
                out[key] = val
    return out


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
