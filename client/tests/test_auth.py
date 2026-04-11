"""End-to-end test: Client(token=...) attaches bearer metadata."""

from __future__ import annotations

import asyncio
import threading
import time

import grpc
import pytest

from harmonograf_client.client import Client
from harmonograf_client.pb import (
    service_pb2_grpc,
    telemetry_pb2,
)


class _RecordingServicer(service_pb2_grpc.HarmonografServicer):
    """Minimal bidi servicer that records the initial metadata observed on
    StreamTelemetry, then issues a Welcome and drains the rest."""

    def __init__(self) -> None:
        self.stream_metadata: list[tuple] = []
        self.control_metadata: list[tuple] = []
        self.first_metadata = threading.Event()

    async def StreamTelemetry(self, request_iterator, context):
        self.stream_metadata.append(tuple(context.invocation_metadata()))
        self.first_metadata.set()
        welcome_sent = False
        async for msg in request_iterator:
            which = msg.WhichOneof("msg")
            if which == "hello" and not welcome_sent:
                yield telemetry_pb2.TelemetryDown(
                    welcome=telemetry_pb2.Welcome(
                        accepted=True,
                        assigned_session_id=msg.hello.session_id or "sess",
                        assigned_stream_id="stream-1",
                    )
                )
                welcome_sent = True
            elif which == "goodbye":
                return

    async def SubscribeControl(self, request, context):
        self.control_metadata.append(tuple(context.invocation_metadata()))
        # immediately exit so the client loop moves on
        return

    # Unused RPCs -------------------------------------------------------
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
        self._stop_event = asyncio.Event()
        self._ready.set()
        await self._stop_event.wait()
        try:
            await self._server.stop(grace=0)
        except Exception:
            pass

    def stop(self) -> None:
        if self._loop is None or self._stop_event is None:
            return
        self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread is not None:
            self._thread.join(timeout=3.0)


@pytest.fixture
def server():
    h = _ServerHarness()
    h.start()
    yield h
    h.stop()


def _find_auth_value(md: tuple) -> str | None:
    for k, v in md:
        if k.lower() == "authorization":
            return v
    return None


def test_client_without_token_sends_no_authorization_header(server, tmp_path):
    c = Client(
        name="no_token",
        server_addr=f"127.0.0.1:{server.port}",
        identity_root=str(tmp_path),
    )
    try:
        assert server.servicer.first_metadata.wait(timeout=5.0)
        c.emit_span_start(kind="TOOL_CALL", name="op")
    finally:
        c.shutdown(flush_timeout=3.0)

    assert server.servicer.stream_metadata, "server saw no StreamTelemetry calls"
    md = server.servicer.stream_metadata[0]
    assert _find_auth_value(md) is None


def test_client_with_token_sends_bearer_authorization_header(server, tmp_path):
    c = Client(
        name="with_token",
        server_addr=f"127.0.0.1:{server.port}",
        identity_root=str(tmp_path),
        token="s3cr3t",
    )
    try:
        assert server.servicer.first_metadata.wait(timeout=5.0)
        c.emit_span_start(kind="TOOL_CALL", name="op")
        time.sleep(0.1)
    finally:
        c.shutdown(flush_timeout=3.0)

    assert server.servicer.stream_metadata
    md = server.servicer.stream_metadata[0]
    auth = _find_auth_value(md)
    assert auth is not None
    assert auth.lower() == "bearer s3cr3t"
