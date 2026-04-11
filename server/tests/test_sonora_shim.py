"""Unit test for the sonora CORS preflight shim.

Without the shim, sonora's grpcASGI._do_cors_preflight emits a
`str` value for the `Access-Control-Allow-Origin` header, which
violates the ASGI spec (header values must be bytes) and crashes
hypercorn on every browser preflight. The shim patches the method
to compare header names as bytes and encode the fallback origin.
"""

from __future__ import annotations

import asyncio

from harmonograf_server import _sonora_shim  # noqa: F401  # apply patch
from sonora.asgi import grpcASGI


def _options_scope(host_header: bytes | None = b"example.test"):
    headers = []
    if host_header is not None:
        headers.append((b"host", host_header))
    headers.append((b"access-control-request-method", b"POST"))
    return {
        "type": "http",
        "method": "OPTIONS",
        "path": "/harmonograf.Harmonograf/StreamTelemetry",
        "headers": headers,
        "server": ("127.0.0.1", 8080),
        "scheme": "http",
        "query_string": b"",
    }


async def _invoke(scope):
    app = grpcASGI()
    messages: list[dict] = []

    async def receive():  # pragma: no cover - preflight does not read body
        return {"type": "http.disconnect"}

    async def send(msg):
        messages.append(msg)

    await app._do_cors_preflight(scope, receive, send)
    return messages


def _header_map(messages):
    start = next(m for m in messages if m["type"] == "http.response.start")
    return dict(start["headers"]), start["status"]


def test_preflight_returns_200_with_bytes_headers():
    msgs = asyncio.run(_invoke(_options_scope()))
    headers, status = _header_map(msgs)
    assert status == 200
    for k, v in headers.items():
        assert isinstance(k, bytes), f"header name {k!r} is not bytes"
        assert isinstance(v, bytes), f"header value {v!r} is not bytes"
    assert headers[b"Access-Control-Allow-Origin"] == b"example.test"
    assert headers[b"Access-Control-Allow-Methods"] == b"POST, OPTIONS"


def test_preflight_falls_back_to_scope_server_when_no_host_header():
    msgs = asyncio.run(_invoke(_options_scope(host_header=None)))
    headers, status = _header_map(msgs)
    assert status == 200
    # Fallback: scope["server"][0] = "127.0.0.1", encoded to bytes.
    assert headers[b"Access-Control-Allow-Origin"] == b"127.0.0.1"
    assert isinstance(headers[b"Access-Control-Allow-Origin"], bytes)
