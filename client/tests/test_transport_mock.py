"""Integration tests for Transport + Client against an in-process gRPC
server.

The fake server implements just enough of the Harmonograf service to
let us assert:

* Hello/Welcome handshake and assigned_stream_id propagation
* Span flow upstream (start/update/end) in order
* Heartbeat emission
* Control event delivery + ack round-trip
* Reconnect + Hello.resume_token carries the last emitted span id

We drive it with asyncio but the Client surface we call is pure sync.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent import futures
from pathlib import Path

import grpc
import pytest

from goldfive.pb.goldfive.v1 import control_pb2 as gf_control_pb2

from harmonograf_client import Client
from harmonograf_client.pb import (
    control_pb2,
    service_pb2_grpc,
    telemetry_pb2,
    types_pb2,
)


class FakeHarmonografServicer(service_pb2_grpc.HarmonografServicer):
    def __init__(self) -> None:
        self.received: list[telemetry_pb2.TelemetryUp] = []
        self.received_lock = threading.Lock()
        self.hellos: list[telemetry_pb2.Hello] = []
        self.stream_counter = 0
        self.control_queues: dict[str, asyncio.Queue] = {}
        self.welcome_sent = threading.Event()
        self.first_span_seen = threading.Event()
        self.heartbeat_seen = threading.Event()
        self.ack_seen = threading.Event()

    async def StreamTelemetry(self, request_iterator, context):
        welcome_sent = False
        stream_id = ""
        async for msg in request_iterator:
            which = msg.WhichOneof("msg")
            with self.received_lock:
                self.received.append(msg)
            if which == "hello":
                self.hellos.append(msg.hello)
                self.stream_counter += 1
                stream_id = f"stream-{self.stream_counter}"
                session_id = msg.hello.session_id or "sess_test_0001"
                yield telemetry_pb2.TelemetryDown(
                    welcome=telemetry_pb2.Welcome(
                        accepted=True,
                        assigned_session_id=session_id,
                        assigned_stream_id=stream_id,
                    )
                )
                welcome_sent = True
                self.welcome_sent.set()
            elif which in ("span_start", "span_update", "span_end"):
                self.first_span_seen.set()
            elif which == "heartbeat":
                self.heartbeat_seen.set()
            elif which == "control_ack":
                self.ack_seen.set()
            elif which == "goodbye":
                return

    async def SubscribeControl(self, request, context):
        key = f"{request.agent_id}:{request.stream_id}"
        q: asyncio.Queue = asyncio.Queue()
        self.control_queues[key] = q
        try:
            while True:
                event = await q.get()
                if event is None:
                    return
                yield event
        finally:
            self.control_queues.pop(key, None)

    # Frontend RPCs — unimplemented for these tests.
    async def ListSessions(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        return

    async def WatchSession(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        return

    async def GetPayload(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        return

    async def GetSpanTree(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        return

    async def PostAnnotation(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        return

    async def SendControl(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        return

    async def DeleteSession(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        return

    async def GetStats(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        return


class FakeServerHarness:
    """Runs a grpc.aio server in a dedicated thread with its own loop."""

    def __init__(self) -> None:
        self.servicer = FakeHarmonografServicer()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: grpc.aio.Server | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.port: int = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _bootstrap():
            await self._serve()

        try:
            self._loop.run_until_complete(_bootstrap())
        except RuntimeError:
            pass
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _serve(self) -> None:
        self._server = grpc.aio.server()
        service_pb2_grpc.add_HarmonografServicer_to_server(self.servicer, self._server)
        self.port = self._server.add_insecure_port("127.0.0.1:0")
        await self._server.start()
        self._ready.set()
        self._stop_event = asyncio.Event()
        await self._stop_event.wait()
        try:
            await self._server.stop(grace=0)
        except Exception:
            pass

    def push_control(self, agent_id: str, kind: int) -> None:
        assert self._loop is not None
        event = gf_control_pb2.ControlEvent(
            id=f"ctrl-{int(time.time()*1e6)}",
            kind=kind,
        )

        async def _deliver():
            for key, q in list(self.servicer.control_queues.items()):
                if key.startswith(agent_id + ":"):
                    await q.put(event)

        fut = asyncio.run_coroutine_threadsafe(_deliver(), self._loop)
        fut.result(timeout=2.0)

    def stop(self) -> None:
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)


@pytest.fixture()
def server():
    h = FakeServerHarness()
    h.start()
    yield h
    h.stop()


@pytest.fixture()
def isolated_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("HARMONOGRAF_HOME", str(tmp_path))
    yield tmp_path


def _wait(cond, timeout=3.0):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if cond():
            return True
        time.sleep(0.01)
    return False


class TestTransportHandshake:
    def test_hello_and_welcome(self, server, isolated_identity):
        client = Client(
            name="test-agent",
            server_addr=f"127.0.0.1:{server.port}",
        )
        try:
            assert _wait(lambda: server.servicer.welcome_sent.is_set())
            assert len(server.servicer.hellos) == 1
            hello = server.servicer.hellos[0]
            assert hello.name == "test-agent"
            assert hello.agent_id == client.agent_id
            assert _wait(lambda: client.session_id == "sess_test_0001")
        finally:
            client.shutdown(flush_timeout=1.0)

    def test_span_flow_upstream(self, server, isolated_identity):
        client = Client(
            name="test-agent",
            server_addr=f"127.0.0.1:{server.port}",
        )
        try:
            _wait(lambda: server.servicer.welcome_sent.is_set())
            sid = client.emit_span_start(kind="LLM_CALL", name="gpt-4o")
            client.emit_span_update(sid, attributes={"tokens_in": 42})
            client.emit_span_end(sid, status="COMPLETED")
            assert _wait(lambda: server.servicer.first_span_seen.is_set())
            # Let the send loop flush.
            assert _wait(
                lambda: sum(
                    1
                    for m in server.servicer.received
                    if m.WhichOneof("msg") in ("span_start", "span_update", "span_end")
                )
                >= 3,
                timeout=3.0,
            )
            kinds = [
                m.WhichOneof("msg")
                for m in server.servicer.received
                if m.WhichOneof("msg") in ("span_start", "span_update", "span_end")
            ]
            assert kinds == ["span_start", "span_update", "span_end"]
        finally:
            client.shutdown(flush_timeout=1.0)


class TestHeartbeat:
    def test_heartbeat_carries_context_window_fields(self, isolated_identity):
        # Direct unit coverage for the transport's heartbeat builder: the
        # context_window_tokens / limit fields must flow from a Client's
        # set_context_window() through _build_heartbeat without needing a
        # live server. This is the client-side half of task #2 plumbing.
        from harmonograf_client.pb import telemetry_pb2

        client = Client(
            name="ctxwin-agent",
            server_addr="127.0.0.1:1",  # unused; transport never starts
            autostart=False,
        )
        try:
            client.set_context_window(tokens=12345, limit_tokens=128000)
            hb = client._transport._build_heartbeat(telemetry_pb2)
            assert hb.context_window_tokens == 12345
            assert hb.context_window_limit_tokens == 128000
            # Subsequent call overwrites the previous sample.
            client.set_context_window(tokens=42, limit_tokens=200000)
            hb2 = client._transport._build_heartbeat(telemetry_pb2)
            assert hb2.context_window_tokens == 42
            assert hb2.context_window_limit_tokens == 200000
        finally:
            client.shutdown(flush_timeout=0.1)

    def test_heartbeat_emitted(self, server, isolated_identity):
        from harmonograf_client.transport import TransportConfig, Transport
        from harmonograf_client.buffer import EventRingBuffer, PayloadBuffer

        # Faster heartbeat interval so the test is quick.
        def factory(**kwargs):
            cfg = kwargs.get("config") or TransportConfig()
            cfg = TransportConfig(
                server_addr=cfg.server_addr,
                heartbeat_interval_s=0.2,
                reconnect_initial_ms=cfg.reconnect_initial_ms,
                reconnect_max_ms=cfg.reconnect_max_ms,
                payload_chunk_bytes=cfg.payload_chunk_bytes,
            )
            kwargs["config"] = cfg
            return Transport(**kwargs)

        client = Client(
            name="test-agent",
            server_addr=f"127.0.0.1:{server.port}",
            _transport_factory=factory,
        )
        try:
            assert _wait(lambda: server.servicer.heartbeat_seen.is_set(), timeout=3.0)
        finally:
            client.shutdown(flush_timeout=1.0)


class TestControl:
    def test_control_event_ack_round_trip(self, server, isolated_identity):
        client = Client(
            name="test-agent",
            server_addr=f"127.0.0.1:{server.port}",
            capabilities=["PAUSE_RESUME"],
        )
        handler_calls: list[str] = []

        def on_pause(event):
            handler_calls.append(event.id)
            return None  # default = success

        client.on_control("PAUSE", on_pause)
        try:
            _wait(lambda: server.servicer.welcome_sent.is_set())
            # Wait for control subscription to be in place.
            assert _wait(lambda: any(
                k.startswith(client.agent_id + ":") for k in server.servicer.control_queues
            ), timeout=3.0)
            server.push_control(client.agent_id, gf_control_pb2.CONTROL_KIND_PAUSE)
            assert _wait(lambda: len(handler_calls) == 1, timeout=3.0)
            assert _wait(lambda: server.servicer.ack_seen.is_set(), timeout=3.0)
            acks = [
                m.control_ack
                for m in server.servicer.received
                if m.WhichOneof("msg") == "control_ack"
            ]
            assert len(acks) >= 1
            assert acks[0].result == gf_control_pb2.CONTROL_ACK_RESULT_SUCCESS
        finally:
            client.shutdown(flush_timeout=1.0)


class TestNonBlocking:
    def test_emit_is_non_blocking_when_buffer_full(self, server, isolated_identity):
        # No connection — point at dead port; agent code still must not block.
        client = Client(
            name="offline-agent",
            server_addr="127.0.0.1:1",  # refused
            buffer_size=10,
        )
        try:
            start = time.monotonic()
            for i in range(2000):
                client.emit_span_start(kind="TOOL_CALL", name=f"t{i}")
            elapsed = time.monotonic() - start
            # 2000 non-blocking emits must finish well under a second.
            assert elapsed < 1.0, f"emit took {elapsed:.3f}s — too slow"
            stats = client._events.stats_snapshot()
            assert stats.dropped_total > 0
        finally:
            client.shutdown(flush_timeout=0.5)
