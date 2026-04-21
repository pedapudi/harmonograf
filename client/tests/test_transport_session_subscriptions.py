"""Integration tests for per-ADK-session control subscriptions.

These tests drive the real :class:`Transport` (not :class:`FakeTransport`)
against an in-process gRPC server to prove that
:meth:`Client.register_session` actually opens a second SubscribeControl
RPC and that the subscription is re-established after reconnect.

The server fixture is a trimmed variant of the one in
``test_transport_mock.py`` that remembers every ``SubscribeControlRequest``
it received so tests can assert on session_id / stream_id / agent_id.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import grpc
import pytest

from harmonograf_client import Client
from harmonograf_client.pb import service_pb2_grpc, telemetry_pb2


ADK_SESSION = "adk-session-integration-abc"
OTHER_SESSION = "adk-session-integration-def"


class _RecordingServicer(service_pb2_grpc.HarmonografServicer):
    """Records every SubscribeControl request so tests can assert on them."""

    def __init__(self) -> None:
        self.subscribe_requests: list[Any] = []
        self.subscribe_lock = threading.Lock()
        self.welcome_sent = threading.Event()
        self.stream_counter = 0
        # Event that fires each time a distinct session_id is observed on
        # a SubscribeControl request.
        self.subscribed_session_ids: set[str] = set()

    async def StreamTelemetry(self, request_iterator, context):
        async for msg in request_iterator:
            which = msg.WhichOneof("msg")
            if which == "hello":
                self.stream_counter += 1
                session_id = msg.hello.session_id or "sess_srv_0001"
                yield telemetry_pb2.TelemetryDown(
                    welcome=telemetry_pb2.Welcome(
                        accepted=True,
                        assigned_session_id=session_id,
                        assigned_stream_id=f"srv-stream-{self.stream_counter}",
                    )
                )
                self.welcome_sent.set()
            elif which == "goodbye":
                return

    async def SubscribeControl(self, request, context):
        with self.subscribe_lock:
            self.subscribe_requests.append(request)
            self.subscribed_session_ids.add(request.session_id)
        # Hold the stream open until the client tears it down; no events are
        # pushed — these tests only care about subscribe REQUEST bookkeeping.
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return

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


class _ServerHarness:
    def __init__(self) -> None:
        self.servicer = _RecordingServicer()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: grpc.aio.Server | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_event: asyncio.Event | None = None
        self.port: int = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except RuntimeError:
            pass
        finally:
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
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
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        if self._loop is None or self._stop_event is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        except Exception:  # noqa: BLE001
            pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)


@pytest.fixture()
def server():
    h = _ServerHarness()
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


def test_register_session_opens_second_subscribe_control_rpc(server, isolated_identity):
    """End-to-end: registering an ADK session with the Client results in
    a second SubscribeControl request hitting the server with the right
    session_id. The home subscription also survives."""
    client = Client(
        name="real-transport-test",
        server_addr=f"127.0.0.1:{server.port}",
    )
    try:
        assert _wait(lambda: server.servicer.welcome_sent.is_set())
        # Home subscription should have landed already.
        assert _wait(lambda: len(server.servicer.subscribe_requests) >= 1, timeout=3.0)
        home_req = server.servicer.subscribe_requests[0]
        assert home_req.agent_id == client.agent_id
        # Now register an ADK session; a second SubscribeControl should fire.
        client.register_session(ADK_SESSION)
        assert _wait(
            lambda: ADK_SESSION in server.servicer.subscribed_session_ids, timeout=3.0
        )
        # And we still see the home session — the new sub is additive.
        assert home_req.session_id in server.servicer.subscribed_session_ids
    finally:
        client.shutdown(flush_timeout=1.0)


def test_register_session_is_idempotent_over_transport(server, isolated_identity):
    """The transport suppresses duplicate SubscribeControl for the same
    session_id — the plugin may call register_session on every span."""
    client = Client(
        name="real-transport-idem",
        server_addr=f"127.0.0.1:{server.port}",
    )
    try:
        assert _wait(lambda: server.servicer.welcome_sent.is_set())
        client.register_session(ADK_SESSION)
        client.register_session(ADK_SESSION)
        client.register_session(ADK_SESSION)
        # Let any stragglers land.
        _wait(
            lambda: sum(
                1
                for r in server.servicer.subscribe_requests
                if r.session_id == ADK_SESSION
            )
            >= 1,
            timeout=1.0,
        )
        # Exactly one SubscribeControl for ADK_SESSION.
        count = sum(
            1
            for r in server.servicer.subscribe_requests
            if r.session_id == ADK_SESSION
        )
        assert count == 1, f"expected 1 ADK_SESSION sub, got {count}"
    finally:
        client.shutdown(flush_timeout=1.0)


def test_register_multiple_sessions_produces_multiple_subs(server, isolated_identity):
    client = Client(
        name="real-transport-multi",
        server_addr=f"127.0.0.1:{server.port}",
    )
    try:
        assert _wait(lambda: server.servicer.welcome_sent.is_set())
        client.register_session(ADK_SESSION)
        client.register_session(OTHER_SESSION)
        assert _wait(
            lambda: {ADK_SESSION, OTHER_SESSION}.issubset(
                server.servicer.subscribed_session_ids
            ),
            timeout=3.0,
        )
    finally:
        client.shutdown(flush_timeout=1.0)


def test_register_session_before_connect_opens_after_welcome(isolated_identity, tmp_path):
    """register_session may be called before the transport has its first
    stub. The sub must open as soon as the connect+welcome completes."""
    # Boot a fresh server harness so we can register BEFORE welcome.
    h = _ServerHarness()
    h.start()
    try:
        client = Client(
            name="pre-connect",
            server_addr=f"127.0.0.1:{h.port}",
            autostart=False,
        )
        # Register BEFORE transport.start(). The sub is only recorded; no
        # SubscribeControl RPC yet.
        client.register_session(ADK_SESSION)
        client._transport.start()
        try:
            assert _wait(lambda: h.servicer.welcome_sent.is_set(), timeout=3.0)
            assert _wait(
                lambda: ADK_SESSION in h.servicer.subscribed_session_ids,
                timeout=3.0,
            )
        finally:
            client.shutdown(flush_timeout=1.0)
    finally:
        h.stop()
