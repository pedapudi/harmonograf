"""Top-level Client — public API for emitting harmonograf spans.

The :class:`Client` is a pure, synchronous, non-blocking handle. It owns
an :class:`EventRingBuffer`, a :class:`PayloadBuffer`, and a
:class:`Transport` running on a daemon thread. Every ``emit_*`` method
builds a protobuf message, pushes it onto the ring buffer, and wakes
the transport — all under O(1) lock hold times. If the buffer is full,
the drop policy (updates → payload refs → whole spans) runs under the
same lock and a counter is incremented. Agent code never awaits IO.

See ``docs/design/02`` §3 for the API surface and ``§4.5`` of doc 01
for the backpressure contract.
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from typing import Any, Callable, Iterable, Mapping, Optional

from google.protobuf import timestamp_pb2

from .buffer import EnvelopeKind, EventRingBuffer, PayloadBuffer, SpanEnvelope
from .enums import Capability, SpanKind, SpanStatus
from .identity import AgentIdentity, load_or_create
from .transport import ControlAckSpec, Transport, TransportConfig


ControlCallback = Callable[[Any], Optional[ControlAckSpec]]


def _uuid7_hex() -> str:
    """Return a UUIDv7-ish hex id — 48-bit unix-ms time prefix + random.
    Good enough for within-process monotonic sorting without needing a
    third-party uuid7 lib.
    """
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rnd = os.urandom(10)
    return f"{ms:012x}{rnd.hex()}"


def _now_ts() -> timestamp_pb2.Timestamp:
    t = timestamp_pb2.Timestamp()
    t.GetCurrentTime()
    return t


def _to_ts(val: Any) -> timestamp_pb2.Timestamp:
    if val is None:
        return _now_ts()
    if isinstance(val, timestamp_pb2.Timestamp):
        return val
    if isinstance(val, (int, float)):
        t = timestamp_pb2.Timestamp()
        t.FromSeconds(int(val))
        t.nanos = int((val - int(val)) * 1_000_000_000)
        return t
    raise TypeError(f"unsupported timestamp: {type(val).__name__}")


class Client:
    """Non-blocking handle for emitting spans to a harmonograf server.

    >>> client = Client(name="research-agent")
    >>> sid = client.emit_span_start(kind="LLM_CALL", name="gpt-4o")
    >>> client.emit_span_end(sid, status="COMPLETED")
    >>> client.shutdown()
    """

    def __init__(
        self,
        *,
        name: str,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        framework: str = "CUSTOM",
        framework_version: str = "",
        capabilities: Iterable[str | Capability] = (),
        server_addr: str = "127.0.0.1:7531",
        buffer_size: int = 2000,
        payload_buffer_bytes: int = 16 * 1024 * 1024,
        payload_chunk_bytes: int = 256 * 1024,
        session_title: str = "",
        metadata: Optional[Mapping[str, str]] = None,
        identity_root: Optional[str] = None,
        autostart: bool = True,
        token: Optional[str] = None,
        _transport_factory: Optional[Callable[..., Transport]] = None,
    ) -> None:
        from .pb import telemetry_pb2, types_pb2

        self._telemetry_pb2 = telemetry_pb2
        self._types_pb2 = types_pb2

        self._name = name
        if agent_id is None:
            from pathlib import Path

            ident: AgentIdentity = load_or_create(
                name,
                framework=framework,
                framework_version=framework_version,
                metadata=dict(metadata or {}),
                root=Path(identity_root) if identity_root else None,
            )
            self._agent_id = ident.agent_id
        else:
            self._agent_id = agent_id

        self._session_id = session_id or ""
        self._framework = framework

        self._events = EventRingBuffer(capacity=buffer_size)
        self._payloads = PayloadBuffer(capacity_bytes=payload_buffer_bytes)

        caps: list[str] = []
        for c in capabilities:
            caps.append(c.value if isinstance(c, Capability) else str(c))

        self._progress_counter: int = 0
        self._current_activity: str = ""
        self._context_window_tokens: int = 0
        self._context_window_limit_tokens: int = 0

        cfg = TransportConfig(
            server_addr=server_addr,
            payload_chunk_bytes=payload_chunk_bytes,
        )
        factory = _transport_factory or Transport
        self._transport = factory(
            events=self._events,
            payloads=self._payloads,
            agent_id=self._agent_id,
            session_id=self._session_id,
            name=name,
            framework=framework,
            framework_version=framework_version,
            capabilities=caps,
            metadata=dict(metadata or {}),
            session_title=session_title,
            config=cfg,
            auth_token=token,
            progress_fn=lambda: self._progress_snapshot,
            context_window_fn=lambda: self._context_window_snapshot,
        )
        self._shutdown_called = False
        if autostart:
            self._transport.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def session_id(self) -> str:
        return self._transport.assigned_session_id or self._session_id

    def set_current_activity(self, text: str) -> None:
        """Set the current activity description, reported in the next heartbeat."""
        self._current_activity = text

    def set_context_window(self, tokens: int, limit_tokens: int) -> None:
        """Set the current LLM context window snapshot for this agent.

        ``tokens`` is the count (or best-effort estimate) of tokens the
        model saw in the most recent request. ``limit_tokens`` is the
        model's max context, if known. Both are carried on every
        subsequent heartbeat until set again. Passing zeros clears the
        sample (heartbeats will then be ignored server-side).
        """
        self._context_window_tokens = max(0, int(tokens))
        self._context_window_limit_tokens = max(0, int(limit_tokens))

    def set_metadata(self, key: str, value: str) -> None:
        """Set a key in the agent metadata dict carried on the Hello frame.

        Mutates the transport's metadata snapshot directly. Intended to be
        called *before* the transport connects for the first time; later
        connects and reconnects will carry the update. A no-op if the key
        and value already match.
        """
        existing = self._transport._metadata.get(key)
        if existing == value:
            return
        self._transport._metadata[key] = value

    @property
    def _progress_snapshot(self) -> tuple[int, str]:
        """Returns (progress_counter, current_activity) for heartbeat assembly."""
        return self._progress_counter, self._current_activity

    @property
    def _context_window_snapshot(self) -> tuple[int, int]:
        """Returns (tokens, limit_tokens) for heartbeat assembly."""
        return self._context_window_tokens, self._context_window_limit_tokens

    def emit_span_start(
        self,
        *,
        kind: str | SpanKind,
        name: str,
        span_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        attributes: Optional[Mapping[str, Any]] = None,
        payload: Optional[bytes] = None,
        payload_mime: str = "application/json",
        payload_role: str = "input",
        start_time: Any = None,
        links: Optional[Iterable[Mapping[str, Any]]] = None,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> str:
        sid = span_id or _uuid7_hex()
        kind_enum, kind_string = self._resolve_kind(kind)
        span = self._types_pb2.Span(
            id=sid,
            session_id=session_id or self._session_id,
            agent_id=agent_id or self._agent_id,
            parent_span_id=parent_span_id or "",
            kind=kind_enum,
            kind_string=kind_string,
            status=self._types_pb2.SPAN_STATUS_RUNNING,
            name=name,
            start_time=_to_ts(start_time),
        )
        self._apply_attributes(span.attributes, attributes)
        self._apply_links(span.links, links)
        has_payload = self._attach_payload(
            span.payload_refs, payload, payload_mime, payload_role
        )
        msg = self._telemetry_pb2.SpanStart(span=span)
        env = SpanEnvelope(
            kind=EnvelopeKind.SPAN_START,
            span_id=sid,
            payload=msg,
            has_payload_ref=has_payload,
        )
        self._events.push(env)
        self._transport.notify()
        self._progress_counter += 1
        return sid

    def emit_span_update(
        self,
        span_id: str,
        *,
        attributes: Optional[Mapping[str, Any]] = None,
        status: Optional[str | SpanStatus] = None,
        payload: Optional[bytes] = None,
        payload_mime: str = "application/json",
        payload_role: str = "output",
    ) -> None:
        msg = self._telemetry_pb2.SpanUpdate(span_id=span_id)
        self._apply_attributes(msg.attributes, attributes)
        if status is not None:
            msg.status = self._resolve_status(status)
        has_payload = self._attach_payload(
            msg.payload_refs, payload, payload_mime, payload_role
        )
        env = SpanEnvelope(
            kind=EnvelopeKind.SPAN_UPDATE,
            span_id=span_id,
            payload=msg,
            has_payload_ref=has_payload,
        )
        self._events.push(env)
        self._transport.notify()
        self._progress_counter += 1

    def emit_span_end(
        self,
        span_id: str,
        *,
        status: str | SpanStatus = "COMPLETED",
        end_time: Any = None,
        payload: Optional[bytes] = None,
        payload_mime: str = "application/json",
        payload_role: str = "output",
        error: Optional[Mapping[str, str]] = None,
        attributes: Optional[Mapping[str, Any]] = None,
    ) -> None:
        msg = self._telemetry_pb2.SpanEnd(
            span_id=span_id,
            end_time=_to_ts(end_time),
            status=self._resolve_status(status),
        )
        self._apply_attributes(msg.attributes, attributes)
        has_payload = self._attach_payload(
            msg.payload_refs, payload, payload_mime, payload_role
        )
        if error:
            msg.error.type = error.get("type", "")
            msg.error.message = error.get("message", "")
            msg.error.stack = error.get("stack", "")
        env = SpanEnvelope(
            kind=EnvelopeKind.SPAN_END,
            span_id=span_id,
            payload=msg,
            has_payload_ref=has_payload,
        )
        self._events.push(env)
        self._transport.notify()
        self._progress_counter += 1

    def on_control(self, kind: str, callback: ControlCallback) -> None:
        self._transport.register_control_handler(kind.upper(), callback)

    def set_control_forward(self, fn: Optional[Callable[[Any], None]]) -> None:
        """Install a raw ``ControlEvent`` forwarder. ``None`` uninstalls.

        While a forwarder is active, events bypass the per-kind callbacks
        registered via :meth:`on_control` — ack responsibility moves to
        the forwarder, which must call :meth:`send_control_ack` for every
        event. This is the hook the goldfive bridge in
        ``harmonograf_client._control_bridge`` uses.
        """
        self._transport.set_control_forward(fn)

    def send_control_ack(
        self, control_id: str, result: str, detail: str = ""
    ) -> None:
        """Send a ``ControlAck`` upstream. Thread-safe and non-blocking."""
        self._transport.send_control_ack(control_id, result, detail)

    def emit_goldfive_event(self, event_pb: Any) -> None:
        """Ship a ``goldfive.v1.Event`` to the server.

        Goldfive owns orchestration post-migration (issue #2); plans and
        task-status deltas that used to travel as dedicated
        ``TaskPlan`` / ``UpdatedTaskStatus`` envelopes now ride inside a
        ``TelemetryUp.goldfive_event`` variant. The envelope is pushed
        onto the same buffer that spans use, so it shares backpressure
        and reconnect semantics — this call never blocks.
        """
        env = SpanEnvelope(
            kind=EnvelopeKind.GOLDFIVE_EVENT,
            span_id="",
            payload=event_pb,
        )
        self._events.push(env)
        self._transport.notify()

    def shutdown(self, flush_timeout: float = 5.0) -> None:
        if self._shutdown_called:
            return
        self._shutdown_called = True
        deadline = time.monotonic() + max(0.0, flush_timeout)
        while time.monotonic() < deadline and len(self._events) > 0:
            self._transport.notify()
            time.sleep(0.02)
        self._transport.shutdown(timeout=max(0.1, deadline - time.monotonic()))

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_kind(self, kind: str | SpanKind) -> tuple[int, str]:
        types_pb2 = self._types_pb2
        name = kind.value if isinstance(kind, SpanKind) else str(kind)
        enum_val = getattr(types_pb2, f"SPAN_KIND_{name}", None)
        if enum_val is None:
            return types_pb2.SPAN_KIND_CUSTOM, name
        return enum_val, ""

    def _resolve_status(self, status: str | SpanStatus) -> int:
        types_pb2 = self._types_pb2
        name = status.value if isinstance(status, SpanStatus) else str(status)
        return getattr(types_pb2, f"SPAN_STATUS_{name}", types_pb2.SPAN_STATUS_COMPLETED)

    def _apply_attributes(self, target: Any, attributes: Optional[Mapping[str, Any]]) -> None:
        if not attributes:
            return
        types_pb2 = self._types_pb2
        for k, v in attributes.items():
            av = types_pb2.AttributeValue()
            if isinstance(v, bool):
                av.bool_value = v
            elif isinstance(v, int):
                av.int_value = v
            elif isinstance(v, float):
                av.double_value = v
            elif isinstance(v, bytes):
                av.bytes_value = v
            else:
                av.string_value = str(v)
            target[k].CopyFrom(av)

    def _apply_links(self, target: Any, links: Optional[Iterable[Mapping[str, Any]]]) -> None:
        if not links:
            return
        types_pb2 = self._types_pb2
        for link in links:
            relation_name = str(link.get("relation", "UNSPECIFIED"))
            relation_val = getattr(
                types_pb2,
                f"LINK_RELATION_{relation_name}",
                types_pb2.LINK_RELATION_UNSPECIFIED,
            )
            sl = types_pb2.SpanLink(
                target_span_id=str(link.get("target_span_id", "") or ""),
                target_agent_id=str(link.get("target_agent_id", "") or ""),
                relation=relation_val,
            )
            target.append(sl)

    def _attach_payload(
        self,
        refs_target: Any,
        data: Optional[bytes],
        mime: str,
        role: str,
    ) -> bool:
        if data is None:
            return False
        digest = hashlib.sha256(data).hexdigest()
        summary = self._derive_summary(data, mime)
        ref = self._types_pb2.PayloadRef(
            digest=digest,
            size=len(data),
            mime=mime,
            summary=summary,
            role=role,
        )
        ok = self._transport.enqueue_payload(digest, data, mime)
        if not ok:
            ref.evicted = True
        refs_target.append(ref)
        return ok

    def _derive_summary(self, data: bytes, mime: str) -> str:
        try:
            if "json" in mime or mime.startswith("text/"):
                return data[:200].decode("utf-8", errors="replace")
        except Exception:
            pass
        return f"<{mime}, {len(data)} bytes>"
