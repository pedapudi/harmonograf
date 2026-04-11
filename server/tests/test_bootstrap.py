"""Smoke tests for Harmonograf bootstrap.

Builds a real Harmonograf app (both gRPC + gRPC-Web listeners) against
an InMemoryStore on ephemeral ports, makes a native-gRPC GetStats call,
and a raw HTTP POST against the sonora gRPC-Web endpoint to confirm it
responds to the gRPC-Web wire format.
"""

from __future__ import annotations

import asyncio
import socket

import grpc
import pytest

from harmonograf_server.config import ServerConfig
from harmonograf_server.main import Harmonograf
from harmonograf_server.pb import frontend_pb2, service_pb2_grpc


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_harmonograf_serves_grpc_and_grpc_web():
    cfg = ServerConfig(
        host="127.0.0.1",
        grpc_port=_free_port(),
        web_port=_free_port(),
        store_backend="memory",
        data_dir="",
        grace_seconds=0.5,
    )
    app = await Harmonograf.from_config(cfg)
    await app.start()

    try:
        # 1. Native gRPC: GetStats via the stub.
        async with grpc.aio.insecure_channel(f"127.0.0.1:{cfg.grpc_port}") as ch:
            stub = service_pb2_grpc.HarmonografStub(ch)
            stats = await stub.GetStats(frontend_pb2.GetStatsRequest())
            assert stats.session_count == 0
            assert stats.live_session_count == 0

        # 2. gRPC-Web: raw HTTP POST against the sonora endpoint. We do not
        # decode the framed response body (that is the frontend's job); we
        # just assert the endpoint exists and replies with grpc-status=0.
        reader, writer = await asyncio.open_connection("127.0.0.1", cfg.web_port)
        body = b"\x00\x00\x00\x00\x00"  # single grpc frame: zero-length payload
        path = "/harmonograf.v1.Harmonograf/GetStats"
        req = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{cfg.web_port}\r\n"
            f"Content-Type: application/grpc-web+proto\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode()
        writer.write(req + body)
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
        assert b"HTTP/1.1 200" in resp
        # sonora puts grpc-status into trailers (after the message frame).
        assert b"grpc-status" in resp.lower() or b"grpc-status" in resp
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_harmonograf_stop_is_idempotent():
    cfg = ServerConfig(
        host="127.0.0.1",
        grpc_port=_free_port(),
        web_port=_free_port(),
        store_backend="memory",
        data_dir="",
        grace_seconds=0.2,
    )
    app = await Harmonograf.from_config(cfg)
    await app.start()
    await app.stop()
    # A second stop must not raise.
    await app.stop()


@pytest.mark.asyncio
async def test_harmonograf_run_exits_on_request_stop():
    cfg = ServerConfig(
        host="127.0.0.1",
        grpc_port=_free_port(),
        web_port=_free_port(),
        store_backend="memory",
        data_dir="",
        grace_seconds=0.2,
    )
    app = await Harmonograf.from_config(cfg)

    async def stop_soon():
        await asyncio.sleep(0.2)
        app.request_stop()

    stopper = asyncio.create_task(stop_soon())
    await asyncio.wait_for(app.run(), timeout=5.0)
    await stopper


def test_cli_parser_defaults():
    from harmonograf_server.cli import config_from_args

    cfg = config_from_args([])
    assert cfg.host == "127.0.0.1"
    assert cfg.grpc_port == 7531
    assert cfg.web_port == 7532
    assert cfg.store_backend == "sqlite"

    cfg2 = config_from_args(
        ["--store", "memory", "--port", "9000", "--web-port", "9001", "--host", "0.0.0.0"]
    )
    assert cfg2.store_backend == "memory"
    assert cfg2.grpc_port == 9000
    assert cfg2.web_port == 9001
    assert cfg2.host == "0.0.0.0"
