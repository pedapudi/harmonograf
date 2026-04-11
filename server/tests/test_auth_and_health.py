"""Tests for bearer-token auth and /healthz, /readyz endpoints."""

from __future__ import annotations

import asyncio
import socket

import grpc
import pytest

from harmonograf_server.auth import (
    _extract_token_from_metadata,
    asgi_bearer_guard,
)
from harmonograf_server.config import ServerConfig
from harmonograf_server.health import build_health_router
from harmonograf_server.main import Harmonograf
from harmonograf_server.pb import frontend_pb2, service_pb2_grpc


TOKEN = "s3cr3t"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- metadata parsing ----------------------------------------------------


def test_extract_token_from_metadata_bearer_case_insensitive():
    assert _extract_token_from_metadata([("authorization", "Bearer abc")]) == "abc"
    assert _extract_token_from_metadata([("Authorization", "bearer abc")]) == "abc"
    assert _extract_token_from_metadata([("authorization", "abc")]) == "abc"


def test_extract_token_from_metadata_missing():
    assert _extract_token_from_metadata([]) is None
    assert _extract_token_from_metadata(None) is None
    assert _extract_token_from_metadata([("other", "v")]) is None


# ---- end-to-end: grpc server with auth enabled ---------------------------


async def _build(token: str, **overrides) -> Harmonograf:
    cfg = ServerConfig(
        host="127.0.0.1",
        grpc_port=_free_port(),
        web_port=_free_port(),
        store_backend="memory",
        data_dir="",
        grace_seconds=0.5,
        auth_token=token,
        metrics_interval_seconds=0.0,
        **overrides,
    )
    app = await Harmonograf.from_config(cfg)
    await app.start()
    return app


@pytest.mark.asyncio
async def test_grpc_rejects_missing_token():
    app = await _build(TOKEN)
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{app.cfg.grpc_port}") as ch:
            stub = service_pb2_grpc.HarmonografStub(ch)
            with pytest.raises(grpc.aio.AioRpcError) as excinfo:
                await stub.GetStats(frontend_pb2.GetStatsRequest())
            assert excinfo.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_grpc_rejects_wrong_token():
    app = await _build(TOKEN)
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{app.cfg.grpc_port}") as ch:
            stub = service_pb2_grpc.HarmonografStub(ch)
            md = (("authorization", "bearer nope"),)
            with pytest.raises(grpc.aio.AioRpcError) as excinfo:
                await stub.GetStats(frontend_pb2.GetStatsRequest(), metadata=md)
            assert excinfo.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_grpc_accepts_valid_token():
    app = await _build(TOKEN)
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{app.cfg.grpc_port}") as ch:
            stub = service_pb2_grpc.HarmonografStub(ch)
            md = (("authorization", f"bearer {TOKEN}"),)
            stats = await stub.GetStats(
                frontend_pb2.GetStatsRequest(), metadata=md
            )
            assert stats.session_count == 0
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_grpc_no_auth_when_token_unset():
    # Back-compat: empty token means the interceptor is not installed at all.
    app = await _build("")
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{app.cfg.grpc_port}") as ch:
            stub = service_pb2_grpc.HarmonografStub(ch)
            stats = await stub.GetStats(frontend_pb2.GetStatsRequest())
            assert stats.session_count == 0
    finally:
        await app.stop()


# ---- healthz / readyz ----------------------------------------------------


async def _wait_for_port(port: int, timeout: float = 3.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    last_err: Exception | None = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return
        except OSError as e:
            last_err = e
            await asyncio.sleep(0.05)
    raise TimeoutError(f"port {port} not ready after {timeout}s: {last_err}")


async def _http_get(port: int, path: str, *, headers: dict[bytes, bytes] | None = None) -> tuple[int, bytes]:
    await _wait_for_port(port)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    hdrs = [f"Host: 127.0.0.1:{port}", "Connection: close"]
    for k, v in (headers or {}).items():
        hdrs.append(f"{k.decode()}: {v.decode()}")
    req = f"GET {path} HTTP/1.1\r\n" + "\r\n".join(hdrs) + "\r\n\r\n"
    writer.write(req.encode())
    await writer.drain()
    resp = b""
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            break
        resp += chunk
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    status_line, _, rest = resp.partition(b"\r\n")
    # "HTTP/1.1 200 OK"
    status = int(status_line.split()[1])
    return status, rest


@pytest.mark.asyncio
async def test_healthz_is_200_without_auth_even_when_auth_enabled():
    app = await _build(TOKEN)
    try:
        status, body = await _http_get(app.cfg.web_port, "/healthz")
        assert status == 200
        assert b"ok" in body
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_readyz_is_200_when_store_ok():
    app = await _build("")
    try:
        status, body = await _http_get(app.cfg.web_port, "/readyz")
        assert status == 200
        assert b"ready" in body
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_grpc_web_rejects_missing_token_but_healthz_still_ok():
    app = await _build(TOKEN)
    try:
        # /healthz unauth: still 200.
        status, _ = await _http_get(app.cfg.web_port, "/healthz")
        assert status == 200
        # A non-health HTTP GET (we use /fake just to exercise the guard)
        # should be rejected with 401 when it traverses the auth wrapper.
        status, body = await _http_get(app.cfg.web_port, "/harmonograf.v1.Harmonograf/GetStats")
        assert status == 401
        assert b"unauthorized" in body
    finally:
        await app.stop()


# ---- health router unit test (no network) -------------------------------


class _FakeStore:
    def __init__(self, ok: bool):
        self._ok = ok

    async def ping(self):
        return self._ok


@pytest.mark.asyncio
async def test_health_router_readyz_503_when_store_not_ready():
    sent: list[dict] = []

    async def inner(scope, receive, send):  # pragma: no cover - unused here
        raise AssertionError("inner should not be called for /readyz")

    async def send(msg):
        sent.append(msg)

    async def receive():  # pragma: no cover - unused
        return {"type": "http.request", "body": b""}

    app = build_health_router(_FakeStore(ok=False), inner)
    await app({"type": "http", "path": "/readyz"}, receive, send)
    assert sent[0]["status"] == 503
    assert b"not ready" in sent[1]["body"]


@pytest.mark.asyncio
async def test_health_router_forwards_non_health_paths_to_inner():
    forwarded: list[str] = []

    async def inner(scope, receive, send):
        forwarded.append(scope["path"])

    async def send(_msg):  # pragma: no cover
        pass

    async def receive():  # pragma: no cover
        return {"type": "http.request", "body": b""}

    app = build_health_router(_FakeStore(ok=True), inner)
    await app({"type": "http", "path": "/something/else"}, receive, send)
    assert forwarded == ["/something/else"]


# ---- asgi bearer guard unit test ----------------------------------------


@pytest.mark.asyncio
async def test_asgi_bearer_guard_allows_valid_token():
    called: list[str] = []

    async def inner(scope, receive, send):
        called.append("inner")

    async def send(_msg):
        pass

    async def receive():
        return {"type": "http.request", "body": b""}

    app = asgi_bearer_guard(inner, TOKEN)
    scope = {
        "type": "http",
        "headers": [(b"authorization", f"bearer {TOKEN}".encode())],
    }
    await app(scope, receive, send)
    assert called == ["inner"]


@pytest.mark.asyncio
async def test_asgi_bearer_guard_rejects_wrong_token():
    sent: list[dict] = []

    async def inner(scope, receive, send):  # pragma: no cover
        raise AssertionError("inner should not be called")

    async def send(msg):
        sent.append(msg)

    async def receive():  # pragma: no cover
        return {"type": "http.request", "body": b""}

    app = asgi_bearer_guard(inner, TOKEN)
    scope = {
        "type": "http",
        "headers": [(b"authorization", b"bearer nope")],
    }
    await app(scope, receive, send)
    assert sent[0]["status"] == 401
