"""gRPC bidi transport for the harmonograf client.

The :class:`Transport` runs on a daemon thread that owns its own asyncio
event loop. Agent code calls :meth:`Transport.notify` (a thread-safe,
non-blocking poke) whenever new envelopes are pushed into the event
ring buffer; the loop drains the buffer onto the ``StreamTelemetry``
bidi RPC. A second coroutine subscribes to ``SubscribeControl`` and
dispatches ``ControlEvent``\\s to user-registered handlers, with acks
fed back onto the telemetry upstream.

Reconnect is exponential backoff with jitter (100ms → 30s). On
reconnect the resume token = last server-acked span id is sent in the
new Hello so the server can dedup replays.

The public surface is deliberately small:

* ``start()`` — spin up the thread + loop
* ``notify()`` — wake the send loop (called under the buffer lock)
* ``enqueue_payload(digest, data, mime)`` — add bytes to the payload
  staging buffer and schedule chunked upload
* ``register_control_handler(kind, cb)``
* ``shutdown(timeout)`` — flush + send Goodbye + join

No grpc import happens at module load: importing ``transport`` in a
test that never starts it (e.g., a unit test for :class:`Client`) does
not pull grpc in.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import random
import threading
import time
from typing import Any, Callable, Optional

from .buffer import (
    BufferStats,
    EnvelopeKind,
    EventRingBuffer,
    PayloadBuffer,
    SpanEnvelope,
)

log = logging.getLogger("harmonograf_client.transport")


HEARTBEAT_INTERVAL_S = 5.0
RECONNECT_INITIAL_MS = 100
RECONNECT_MAX_MS = 30_000
SEND_BATCH_MAX = 64
PAYLOAD_CHUNK_INTERLEAVE = 10  # one chunk per N span messages
# Upper bound on the shutdown-time wait for the server to close its
# response stream after we sent Goodbye + EOF. Keeps a flush_timeout=5s
# Client.shutdown from stalling indefinitely on a hung server.
_SHUTDOWN_DRAIN_TIMEOUT_S = 2.0
BREAKER_FAILURE_THRESHOLD = 10
BREAKER_COOLDOWN_MS = 60_000


BREAKER_CLOSED = "closed"
BREAKER_OPEN = "open"
BREAKER_HALF_OPEN = "half_open"


@dataclasses.dataclass
class TransportConfig:
    server_addr: str = "127.0.0.1:7531"
    heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S
    reconnect_initial_ms: int = RECONNECT_INITIAL_MS
    reconnect_max_ms: int = RECONNECT_MAX_MS
    payload_chunk_bytes: int = 256 * 1024
    breaker_failure_threshold: int = BREAKER_FAILURE_THRESHOLD
    breaker_cooldown_ms: int = BREAKER_COOLDOWN_MS


ControlHandler = Callable[[Any], "ControlAckSpec"]


@dataclasses.dataclass
class ControlAckSpec:
    """What a control handler returns. Translated to a pb ControlAck by
    the transport before it goes upstream.
    """

    result: str = "success"  # "success" | "failure" | "unsupported"
    detail: str = ""


@dataclasses.dataclass
class _SessionSubscription:
    """Bookkeeping for one per-ADK-session control subscription.

    ``task`` is the asyncio task that owns the ``SubscribeControl`` RPC
    for this session. ``stream_id`` is a freshly-minted id used on the
    SubscribeControl request so the server can attribute this secondary
    sub independently of the home subscription. ``cancelled`` is set
    when :meth:`Transport.close_session_subscription` removes the entry
    so the loop can skip restart on the next reconnect.
    """

    session_id: str
    stream_id: str
    task: Optional[asyncio.Task] = None
    cancelled: bool = False


class Transport:
    def __init__(
        self,
        *,
        events: EventRingBuffer,
        payloads: PayloadBuffer,
        agent_id: str,
        session_id: str,
        name: str,
        framework: str,
        framework_version: str,
        capabilities: list[str],
        metadata: dict[str, str] | None = None,
        session_title: str = "",
        config: TransportConfig | None = None,
        channel_factory: Optional[Callable[[str], Any]] = None,
        auth_token: str | None = None,
        progress_fn: Optional[Callable[[], tuple[int, str]]] = None,
        context_window_fn: Optional[Callable[[], tuple[int, int]]] = None,
    ) -> None:
        self._events = events
        self._payloads = payloads
        self._agent_id = agent_id
        self._session_id = session_id
        self._name = name
        self._framework = framework
        self._framework_version = framework_version
        self._capabilities = list(capabilities)
        self._metadata = dict(metadata or {})
        self._auth_token = auth_token or None
        self._session_title = session_title
        self._config = config or TransportConfig()
        self._channel_factory = channel_factory
        self._progress_fn = progress_fn
        self._context_window_fn = context_window_fn

        self._handlers: dict[str, ControlHandler] = {}
        self._handlers_lock = threading.Lock()

        # Optional raw-event forwarder. When set, _control_loop routes every
        # ControlEvent to this callback instead of _dispatch_control — the
        # forwarder is then responsible for producing acks via
        # :meth:`send_control_ack`. Used by the goldfive ControlChannel
        # bridge (see ``harmonograf_client._control_bridge``).
        self._control_forward: Optional[Callable[[Any], None]] = None
        # Cached reference to the live bidi send queue, set at the start of
        # each ``_serve`` call so ``send_control_ack`` can thread-safely
        # push acks from the bridge's loop onto the transport's loop.
        self._send_queue: Optional[asyncio.Queue] = None

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_ready = threading.Event()
        self._stop = threading.Event()
        self._wake: Optional[asyncio.Event] = None
        self._connected = threading.Event()

        # Last span id the server acked (via any downstream message or
        # resume tracking). Used as the resume token on reconnect.
        self._resume_token: str = ""

        # Pending PayloadUpload chunks queued for interleave. Each item
        # is a tuple (digest, mime, total_size, offset, last).
        self._chunk_queue: list[tuple[str, str, int, int, bool]] = []
        self._chunk_lock = threading.Lock()

        # Heartbeat drop counters that the buffer itself does not track.
        self._payloads_evicted = 0

        self._assigned_session_id: str = session_id
        self._assigned_stream_id: str = ""

        # Per-ADK-session secondary control subscriptions. Keyed by the ADK
        # ``session.id`` — NOT the harmonograf-assigned home session. The
        # home subscription (opened from ``_control_loop``) is always the
        # baseline identity of this Client to the server; these additional
        # subscriptions are additive and let STEER targets carrying an
        # ADK ``session_id`` land on a sub whose ``session_id`` matches,
        # which the router prefers over the home sub (see server-side
        # ``ControlRouter.deliver``).
        self._session_subs: dict[str, _SessionSubscription] = {}
        self._session_subs_lock = threading.Lock()
        # Snapshot of the currently-live stub so newly-registered
        # subscriptions can bind without waiting for the next connect.
        # Cleared on every ``_serve`` teardown; replayed after reconnect.
        self._live_stub: Any = None

        # Reconnect hardening state.
        # _healthy is set when the current connection has produced at
        # least one successful upstream span/heartbeat send after
        # Welcome. It is what drives the backoff reset — not just
        # "connect succeeded", which can happen repeatedly against a
        # server that accepts then immediately drops.
        self._healthy = False
        self._breaker_state: str = BREAKER_CLOSED
        self._consecutive_failures = 0
        self._breaker_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public (called from the agent thread)
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._thread_main, name="harmonograf-transport", daemon=True
        )
        self._thread.start()
        self._loop_ready.wait(timeout=2.0)

    def notify(self) -> None:
        """Non-blocking wake. Safe to call from any thread, even before
        the loop is ready (a later tick will pick up the buffer state).
        """
        loop = self._loop
        wake = self._wake
        if loop is None or wake is None:
            return
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:
            pass

    def enqueue_payload(self, digest: str, data: bytes, mime: str) -> bool:
        """Stage payload bytes for chunked upload. Returns False if the
        blob was too large or evicted immediately; the caller should
        mark its ``payload_ref.evicted`` accordingly.
        """
        if not self._payloads.put(digest, data):
            self._payloads_evicted += 1
            return False
        total = len(data)
        chunks = max(1, (total + self._config.payload_chunk_bytes - 1) // self._config.payload_chunk_bytes)
        with self._chunk_lock:
            for i in range(chunks):
                offset = i * self._config.payload_chunk_bytes
                last = i == chunks - 1
                self._chunk_queue.append((digest, mime, total, offset, last))
        self.notify()
        return True

    def register_control_handler(self, kind: str, cb: ControlHandler) -> None:
        with self._handlers_lock:
            self._handlers[kind] = cb

    def set_control_forward(self, fn: Optional[Callable[[Any], None]]) -> None:
        """Install a raw-event forwarder. Pass ``None`` to uninstall.

        When set, every incoming ``ControlEvent`` is handed to ``fn``
        (on the transport's own loop) instead of being dispatched to the
        per-kind handlers registered via :meth:`register_control_handler`.
        The forwarder is responsible for eventually producing a
        :class:`ControlAck` via :meth:`send_control_ack` — the transport
        no longer acks synchronously on its behalf.
        """
        self._control_forward = fn

    def send_control_ack(
        self, control_id: str, result: str, detail: str = ""
    ) -> None:
        """Thread-safe ack push onto the current bidi send queue.

        Safe to call from any thread. If no stream is currently live
        (e.g. during a reconnect) the ack is dropped — the server will
        re-deliver the ``ControlEvent`` on the next subscribe.
        """
        from goldfive.pb.goldfive.v1 import control_pb2 as gf_control_pb2

        from .pb import telemetry_pb2

        result_enum = {
            "success": gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS,
            "failure": gf_control_pb2.CONTROL_ACK_RESULT_FAILURE,
            "unsupported": gf_control_pb2.CONTROL_ACK_RESULT_UNSUPPORTED,
        }.get(result.lower(), gf_control_pb2.CONTROL_ACK_RESULT_UNSUPPORTED)
        ack = gf_control_pb2.ControlAck(
            control_id=control_id, result=result_enum, detail=detail
        )
        up = telemetry_pb2.TelemetryUp(control_ack=ack)
        loop = self._loop
        sq = self._send_queue
        if loop is None or sq is None:
            return
        try:
            loop.call_soon_threadsafe(sq.put_nowait, up)
        except RuntimeError:
            pass

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self.notify()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def assigned_session_id(self) -> str:
        return self._assigned_session_id

    @property
    def assigned_stream_id(self) -> str:
        return self._assigned_stream_id

    @property
    def breaker_state(self) -> str:
        """Current circuit-breaker state: "closed", "open", or "half_open".

        Closed is the normal operating state. Open means too many
        consecutive connection attempts have failed and the worker is
        sleeping out a cooldown window before trying again. Half-open
        means a single trial attempt is in flight after a cooldown.
        """
        with self._breaker_lock:
            return self._breaker_state

    @property
    def consecutive_failures(self) -> int:
        with self._breaker_lock:
            return self._consecutive_failures

    # ------------------------------------------------------------------
    # Thread + loop plumbing
    # ------------------------------------------------------------------

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._wake = asyncio.Event()
        self._loop_ready.set()
        try:
            loop.run_until_complete(self._run())
        except Exception:
            log.exception("transport loop crashed")
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _run(self) -> None:
        backoff_ms = self._config.reconnect_initial_ms
        while not self._stop.is_set():
            # If the breaker is open, sleep the cooldown window before
            # even attempting a connection. Half-open on wake.
            if self._breaker_is_open():
                cooldown_s = self._config.breaker_cooldown_ms / 1000.0
                try:
                    await asyncio.sleep(cooldown_s)
                except asyncio.CancelledError:
                    return
                if self._stop.is_set():
                    break
                self._breaker_half_open()

            self._healthy = False
            try:
                await self._connect_and_serve()
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("transport disconnected: %s", e)
                self._connected.clear()
                if self._healthy:
                    # The connection proved itself before dying.
                    # Reset backoff and breaker; next loop iteration
                    # reconnects immediately with the initial delay.
                    self._on_healthy_disconnect()
                    backoff_ms = self._config.reconnect_initial_ms
                else:
                    self._on_failed_attempt()
                if self._stop.is_set():
                    break
                # If the breaker just opened, skip the exponential
                # sleep — the top-of-loop cooldown handles it.
                if not self._breaker_is_open():
                    jitter = random.uniform(0, backoff_ms / 2)
                    delay = (backoff_ms + jitter) / 1000.0
                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        return
                    backoff_ms = min(backoff_ms * 2, self._config.reconnect_max_ms)
                continue
            # Clean return (e.g., server_goodbye). Treat as healthy end.
            self._connected.clear()
            if self._healthy:
                self._on_healthy_disconnect()
            backoff_ms = self._config.reconnect_initial_ms

    # ------------------------------------------------------------------
    # Breaker helpers
    # ------------------------------------------------------------------

    def _breaker_is_open(self) -> bool:
        with self._breaker_lock:
            return self._breaker_state == BREAKER_OPEN

    def _breaker_half_open(self) -> None:
        with self._breaker_lock:
            self._breaker_state = BREAKER_HALF_OPEN

    def _on_failed_attempt(self) -> None:
        with self._breaker_lock:
            self._consecutive_failures += 1
            if (
                self._breaker_state != BREAKER_OPEN
                and self._consecutive_failures >= self._config.breaker_failure_threshold
            ):
                self._breaker_state = BREAKER_OPEN
                log.warning(
                    "transport circuit breaker OPEN after %d consecutive failures; "
                    "cooling down for %dms",
                    self._consecutive_failures,
                    self._config.breaker_cooldown_ms,
                )
            elif self._breaker_state == BREAKER_HALF_OPEN:
                # Trial attempt failed — re-open for another cooldown.
                self._breaker_state = BREAKER_OPEN
                log.warning("transport circuit breaker re-OPEN after half-open trial failed")

    def _on_healthy_disconnect(self) -> None:
        """Called when a connection that was proven healthy has dropped.

        Resets the breaker — the network is demonstrably working and we
        should reconnect promptly, not treat the drop as another strike.
        """
        with self._breaker_lock:
            self._breaker_state = BREAKER_CLOSED
            self._consecutive_failures = 0

    def _mark_healthy(self) -> None:
        """Called by the send loop after a message has been handed off
        to gRPC following the Welcome. Resets the breaker and backoff.
        """
        if self._healthy:
            return
        self._healthy = True
        with self._breaker_lock:
            self._breaker_state = BREAKER_CLOSED
            self._consecutive_failures = 0

    async def _connect_and_serve(self) -> None:
        # Lazy import of grpc + pb so the module is cheap to import in
        # unit tests that don't start a transport.
        import grpc  # noqa: F401
        from .pb import service_pb2_grpc  # noqa: F401

        channel = self._open_channel()
        try:
            stub = self._make_stub(channel)
            await self._serve(stub)
        finally:
            close = getattr(channel, "close", None)
            if close is not None:
                try:
                    res = close()
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass

    def _open_channel(self) -> Any:
        if self._channel_factory is not None:
            return self._channel_factory(self._config.server_addr)
        import grpc

        return grpc.aio.insecure_channel(self._config.server_addr)

    def _make_stub(self, channel: Any) -> Any:
        from .pb import service_pb2_grpc

        return service_pb2_grpc.HarmonografStub(channel)

    async def _serve(self, stub: Any) -> None:
        from .pb import control_pb2, telemetry_pb2

        hello = self._build_hello(telemetry_pb2)
        send_queue: asyncio.Queue = asyncio.Queue()
        self._send_queue = send_queue
        self._live_stub = stub
        await send_queue.put(telemetry_pb2.TelemetryUp(hello=hello))

        async def request_iter():
            while True:
                item = await send_queue.get()
                if item is None:
                    return
                yield item

        call_kwargs = {}
        if self._auth_token:
            call_kwargs["metadata"] = (
                ("authorization", f"bearer {self._auth_token}"),
            )
        call = stub.StreamTelemetry(request_iter(), **call_kwargs)

        welcome_received = asyncio.Event()

        async def recv_loop():
            async for msg in call:
                which = msg.WhichOneof("msg")
                if which == "welcome":
                    self._assigned_session_id = msg.welcome.assigned_session_id
                    self._assigned_stream_id = msg.welcome.assigned_stream_id
                    welcome_received.set()
                    self._connected.set()
                elif which == "payload_request":
                    self._handle_payload_request(msg.payload_request.digest)
                elif which == "flow_control":
                    pass  # v0: ignore
                elif which == "server_goodbye":
                    return

        recv_task = asyncio.create_task(recv_loop())

        # Wait briefly for welcome so session/stream ids are set before
        # the control subscriber opens.
        try:
            await asyncio.wait_for(welcome_received.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            recv_task.cancel()
            raise RuntimeError("no welcome from server")

        control_task = asyncio.create_task(
            self._control_loop(
                stub,
                send_queue,
                session_id=self._assigned_session_id,
                stream_id=self._assigned_stream_id,
            )
        )
        send_task = asyncio.create_task(self._send_loop(send_queue))

        # Replay any per-ADK-session subscriptions registered before or
        # between connects so STEER targets carrying an ADK session id
        # land on a sub whose session_id matches, which the server
        # prefers over the home sub.
        self._start_registered_session_subs(stub, send_queue)

        try:
            done, pending = await asyncio.wait(
                {recv_task, control_task, send_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # On clean shutdown — send_task exited because ``_stop`` was set —
            # the send_loop has already drained the ring buffer and put Goodbye
            # + EOF on the queue. gRPC is still yielding those items to the
            # server; cancelling recv_task now would abort the bidi call and
            # drop pending events in transit. Wait for the server to close
            # its side (recv_task returns naturally) before tearing down.
            if (
                self._stop.is_set()
                and send_task in done
                and not send_task.cancelled()
                and recv_task not in done
            ):
                try:
                    await asyncio.wait_for(
                        asyncio.shield(recv_task), timeout=_SHUTDOWN_DRAIN_TIMEOUT_S
                    )
                except (asyncio.TimeoutError, Exception):
                    pass
            for t in pending:
                t.cancel()
            for t in pending:
                try:
                    await t
                except Exception:
                    pass
            for t in done:
                exc = t.exception()
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    raise exc
        finally:
            # Always tear down per-session subscription tasks tied to this
            # stub, even on exception — otherwise the task wraps a stale
            # gRPC call and leaks across reconnect. The registered subs
            # themselves stay in ``_session_subs`` so the next ``_serve``
            # can re-bind them.
            await self._cancel_session_sub_tasks()
            # Clear send_queue so late bridge acks are not posted onto a
            # stream that is already dead.
            self._send_queue = None
            self._live_stub = None

    # ------------------------------------------------------------------
    # Send loop
    # ------------------------------------------------------------------

    async def _send_loop(self, send_queue: asyncio.Queue) -> None:
        from .pb import telemetry_pb2

        last_hb = time.monotonic()
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self._config.heartbeat_interval_s)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()

                # Drain envelopes in small batches, interleaving payload chunks.
                sent_since_chunk = 0
                batch = self._events.pop_batch(SEND_BATCH_MAX)
                for env in batch:
                    up = self._envelope_to_up(env, telemetry_pb2)
                    if up is not None:
                        # Track resume token: server will ack via normal
                        # downstream, but we also keep the last span id we
                        # sent so reconnect can resume from there.
                        self._resume_token = env.span_id or self._resume_token
                        await send_queue.put(up)
                        self._mark_healthy()
                        sent_since_chunk += 1
                    if sent_since_chunk >= PAYLOAD_CHUNK_INTERLEAVE:
                        await self._maybe_send_chunk(send_queue, telemetry_pb2)
                        sent_since_chunk = 0

                # Flush any leftover payload chunks if we weren't busy.
                while True:
                    pushed = await self._maybe_send_chunk(send_queue, telemetry_pb2)
                    if not pushed:
                        break

                # Heartbeat tick.
                now = time.monotonic()
                if now - last_hb >= self._config.heartbeat_interval_s:
                    last_hb = now
                    hb = self._build_heartbeat(telemetry_pb2)
                    await send_queue.put(telemetry_pb2.TelemetryUp(heartbeat=hb))
                    self._mark_healthy()
        finally:
            # Shutdown path. Drain any envelopes still in the ring buffer —
            # Client.shutdown spins until the buffer is empty, but a race
            # can leave a trailing batch that the while-loop never observed
            # (stop was set between the last pop_batch and the next check).
            # Without this flush, events the caller *successfully emitted*
            # get silently dropped. Then send Goodbye + EOF so gRPC closes
            # the request side cleanly after yielding every queued item.
            try:
                remaining = self._events.pop_batch(self._events.capacity)
                for env in remaining:
                    up = self._envelope_to_up(env, telemetry_pb2)
                    if up is not None:
                        self._resume_token = env.span_id or self._resume_token
                        await send_queue.put(up)
                try:
                    await send_queue.put(
                        telemetry_pb2.TelemetryUp(
                            goodbye=telemetry_pb2.Goodbye(reason="shutdown")
                        )
                    )
                except Exception:
                    pass
                await send_queue.put(None)
            except Exception:
                # Best-effort: if the loop is tearing down for any reason,
                # don't mask the original error.
                pass

    async def _maybe_send_chunk(self, send_queue: asyncio.Queue, telemetry_pb2: Any) -> bool:
        with self._chunk_lock:
            if not self._chunk_queue:
                return False
            digest, mime, total, offset, last = self._chunk_queue.pop(0)
        data = self._payloads.take(digest) if last else self._peek_payload(digest)
        if data is None:
            up = telemetry_pb2.TelemetryUp(
                payload=telemetry_pb2.PayloadUpload(
                    digest=digest,
                    total_size=total,
                    mime=mime,
                    chunk=b"",
                    last=True,
                    evicted=True,
                )
            )
            await send_queue.put(up)
            return True
        end = min(offset + self._config.payload_chunk_bytes, total)
        chunk = data[offset:end]
        up = telemetry_pb2.TelemetryUp(
            payload=telemetry_pb2.PayloadUpload(
                digest=digest,
                total_size=total,
                mime=mime,
                chunk=chunk,
                last=last,
            )
        )
        await send_queue.put(up)
        return True

    def _peek_payload(self, digest: str) -> Optional[bytes]:
        # PayloadBuffer.take removes; we need non-destructive access for
        # mid-stream chunking. Re-put after peek is safe since dedup key
        # is the digest.
        data = self._payloads.take(digest)
        if data is not None:
            self._payloads.put(digest, data)
        return data

    def _envelope_to_up(self, env: SpanEnvelope, telemetry_pb2: Any) -> Any:
        payload = env.payload
        if env.kind is EnvelopeKind.SPAN_START:
            return telemetry_pb2.TelemetryUp(span_start=payload)
        if env.kind is EnvelopeKind.SPAN_UPDATE:
            return telemetry_pb2.TelemetryUp(span_update=payload)
        if env.kind is EnvelopeKind.SPAN_END:
            return telemetry_pb2.TelemetryUp(span_end=payload)
        if env.kind is EnvelopeKind.GOLDFIVE_EVENT:
            return telemetry_pb2.TelemetryUp(goldfive_event=payload)
        return None

    def _build_hello(self, telemetry_pb2: Any) -> Any:
        from .pb import types_pb2

        framework_enum = getattr(
            types_pb2, f"FRAMEWORK_{self._framework.upper()}", types_pb2.FRAMEWORK_CUSTOM
        )
        caps = []
        for c in self._capabilities:
            val = getattr(types_pb2, f"CAPABILITY_{c.upper()}", None)
            if val is not None:
                caps.append(val)
        return telemetry_pb2.Hello(
            agent_id=self._agent_id,
            session_id=self._session_id,
            name=self._name,
            framework=framework_enum,
            framework_version=self._framework_version,
            capabilities=caps,
            metadata=self._metadata,
            resume_token=self._resume_token,
            session_title=self._session_title,
        )

    def _build_heartbeat(self, telemetry_pb2: Any) -> Any:
        stats: BufferStats = self._events.stats_snapshot()
        progress_counter, current_activity = (
            self._progress_fn() if self._progress_fn is not None else (0, "")
        )
        ctx_tokens, ctx_limit = (
            self._context_window_fn() if self._context_window_fn is not None else (0, 0)
        )
        return telemetry_pb2.Heartbeat(
            buffered_events=stats.buffered_events,
            dropped_events=stats.dropped_total,
            dropped_spans_critical=stats.dropped_spans,
            buffered_payload_bytes=self._payloads.buffered_bytes(),
            payloads_evicted=self._payloads_evicted,
            cpu_self_pct=0.0,
            progress_counter=progress_counter,
            current_activity=current_activity,
            context_window_tokens=ctx_tokens,
            context_window_limit_tokens=ctx_limit,
        )

    # ------------------------------------------------------------------
    # Control subscribe loop
    # ------------------------------------------------------------------

    async def _control_loop(
        self,
        stub: Any,
        send_queue: asyncio.Queue,
        *,
        session_id: str,
        stream_id: str,
    ) -> None:
        """Open one ``SubscribeControl`` stream and route its events.

        Used for both the home subscription (called once from
        :meth:`_serve`) and for each per-ADK-session subscription
        registered via :meth:`open_session_subscription`. All subs share
        the same ``_control_forward`` / ``_dispatch_control`` path so a
        goldfive bridge receives events from any sub indistinguishably —
        session scoping is achieved by the server's router preferring
        session-matching subs over the home fallback.
        """
        from goldfive.pb.goldfive.v1 import control_pb2 as gf_control_pb2

        from .pb import control_pb2, telemetry_pb2

        req = control_pb2.SubscribeControlRequest(
            session_id=session_id,
            agent_id=self._agent_id,
            stream_id=stream_id,
        )
        sub_kwargs = {}
        if self._auth_token:
            sub_kwargs["metadata"] = (
                ("authorization", f"bearer {self._auth_token}"),
            )
        try:
            call = stub.SubscribeControl(req, **sub_kwargs)
            async for event in call:
                fwd = self._control_forward
                if fwd is not None:
                    try:
                        fwd(event)
                    except Exception:
                        log.exception("control forward raised")
                    continue
                ack = self._dispatch_control(event, gf_control_pb2)
                await send_queue.put(
                    telemetry_pb2.TelemetryUp(control_ack=ack)
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(
                "control subscription ended session_id=%s: %s", session_id, e
            )

    # ------------------------------------------------------------------
    # Per-ADK-session control subscriptions
    # ------------------------------------------------------------------

    def open_session_subscription(self, adk_session_id: str) -> None:
        """Open an additional ``SubscribeControl`` stream keyed on ``adk_session_id``.

        Thread-safe. Idempotent: calling twice with the same session_id
        is a no-op. Safe to call before the transport has connected —
        the subscription is recorded and opened automatically once the
        loop reaches the control-loop phase of the next ``_serve`` call.
        """
        if not adk_session_id:
            return
        with self._session_subs_lock:
            if adk_session_id in self._session_subs:
                return
            stream_id = f"{self._assigned_stream_id or 'str'}.{adk_session_id}"
            sub = _SessionSubscription(
                session_id=adk_session_id, stream_id=stream_id
            )
            self._session_subs[adk_session_id] = sub
        loop = self._loop
        stub = self._live_stub
        sq = self._send_queue
        if loop is None or stub is None or sq is None:
            # Not currently connected. The sub is registered; it will be
            # started on the next successful ``_serve``.
            return
        try:
            loop.call_soon_threadsafe(
                self._spawn_session_sub_task, stub, sq, sub
            )
        except RuntimeError:
            # Loop is shutting down — next reconnect (if any) replays.
            pass

    def close_session_subscription(self, adk_session_id: str) -> None:
        """Cancel and remove a previously-registered per-ADK-session sub.

        Safe to call from any thread. A no-op if no matching sub exists.
        """
        if not adk_session_id:
            return
        with self._session_subs_lock:
            sub = self._session_subs.pop(adk_session_id, None)
        if sub is None:
            return
        sub.cancelled = True
        task = sub.task
        loop = self._loop
        if task is not None and loop is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

    @property
    def registered_session_ids(self) -> tuple[str, ...]:
        """Snapshot of currently-registered per-ADK-session ids.

        Exposed for tests and observability; not part of the daily API.
        """
        with self._session_subs_lock:
            return tuple(self._session_subs.keys())

    def _start_registered_session_subs(
        self, stub: Any, send_queue: asyncio.Queue
    ) -> None:
        """Called from :meth:`_serve` after the home control task is live.

        Launches one ``_control_loop`` task per already-registered
        per-ADK-session subscription so they are re-bound to the fresh
        stub after every reconnect.
        """
        with self._session_subs_lock:
            snapshot = list(self._session_subs.values())
        for sub in snapshot:
            if sub.cancelled:
                continue
            self._spawn_session_sub_task(stub, send_queue, sub)

    def _spawn_session_sub_task(
        self, stub: Any, send_queue: asyncio.Queue, sub: _SessionSubscription
    ) -> None:
        # Runs on the transport loop.
        if sub.cancelled:
            return
        if sub.task is not None and not sub.task.done():
            return
        sub.task = asyncio.create_task(
            self._control_loop(
                stub,
                send_queue,
                session_id=sub.session_id,
                stream_id=sub.stream_id,
            )
        )

    async def _cancel_session_sub_tasks(self) -> None:
        with self._session_subs_lock:
            tasks = [
                s.task for s in self._session_subs.values() if s.task is not None
            ]
            for s in self._session_subs.values():
                s.task = None
        for t in tasks:
            if not t.done():
                t.cancel()
        for t in tasks:
            try:
                await t
            except Exception:
                pass

    def _dispatch_control(self, event: Any, gf_control_pb2: Any) -> Any:
        kind_name = _control_kind_name(event.kind, gf_control_pb2)
        with self._handlers_lock:
            handler = self._handlers.get(kind_name)
        result = "unsupported"
        detail = ""
        if handler is not None:
            try:
                spec = handler(event)
                if spec is None:
                    result = "success"
                else:
                    result = spec.result
                    detail = spec.detail
            except Exception as e:
                result = "failure"
                detail = repr(e)
        result_enum = {
            "success": gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS,
            "failure": gf_control_pb2.CONTROL_ACK_RESULT_FAILURE,
            "unsupported": gf_control_pb2.CONTROL_ACK_RESULT_UNSUPPORTED,
        }.get(result, gf_control_pb2.CONTROL_ACK_RESULT_UNSUPPORTED)
        return gf_control_pb2.ControlAck(
            control_id=event.id,
            result=result_enum,
            detail=detail,
        )

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def _handle_payload_request(self, digest: str) -> None:
        data = self._payloads.take(digest)
        if data is None:
            with self._chunk_lock:
                self._chunk_queue.append((digest, "application/octet-stream", 0, 0, True))
            self.notify()
            return
        # Put back and schedule re-upload.
        self._payloads.put(digest, data)
        total = len(data)
        chunks = max(1, (total + self._config.payload_chunk_bytes - 1) // self._config.payload_chunk_bytes)
        with self._chunk_lock:
            for i in range(chunks):
                offset = i * self._config.payload_chunk_bytes
                last = i == chunks - 1
                self._chunk_queue.append((digest, "application/octet-stream", total, offset, last))
        self.notify()


def _control_kind_name(kind_value: int, gf_control_pb2: Any) -> str:
    try:
        raw = gf_control_pb2.ControlKind.Name(kind_value)
    except Exception:
        return "UNSPECIFIED"
    return raw.removeprefix("CONTROL_KIND_")
