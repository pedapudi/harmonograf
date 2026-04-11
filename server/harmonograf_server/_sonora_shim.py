"""Monkey-patch for sonora.asgi CORS preflight bytes/str bug.

Upstream sonora/asgi.py:_do_cors_preflight compares an ASGI header name
(bytes) against the literal "host" (str), so the comparison is always
False. It then falls through to scope["server"][0] (a str) and passes
that as a header value to hypercorn, which explodes with
`TypeError: string argument without an encoding` on every browser
gRPC-Web preflight.

This module patches the method in place. Import it before any other
code imports sonora.asgi so the patched method is live by the time a
grpcASGI instance handles a request.
"""

from __future__ import annotations

import logging

import sonora.asgi as _sonora_asgi
import sonora.protocol as _sonora_protocol

logger = logging.getLogger("harmonograf_server")


async def _do_cors_preflight(self, scope, receive, send):
    origin = next(
        (value for header, value in scope["headers"] if header == b"host"),
        scope["server"][0].encode("ascii"),
    )
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"Content-Type", b"text/plain"),
                (b"Content-Length", b"0"),
                (b"Access-Control-Allow-Methods", b"POST, OPTIONS"),
                (b"Access-Control-Allow-Headers", b"*"),
                (b"Access-Control-Allow-Origin", origin),
                (b"Access-Control-Allow-Credentials", b"true"),
                (b"Access-Control-Expose-Headers", b"*"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": b"", "more_body": False})


_sonora_asgi.grpcASGI._do_cors_preflight = _do_cors_preflight


def _pack_trailers(trailers):
    """Replacement for sonora.protocol.pack_trailers that handles bytes.

    Upstream uses ``f"{k}: {v}"`` which calls ``repr()`` on bytes inputs,
    producing literal ``b'grpc-status': b'0'`` text in the trailer
    frame. asgi._do_unary_response builds trailers as bytes pairs, so
    every unary gRPC-Web response from sonora ships malformed trailers
    that Connect-Web rejects as "server unreachable". Decode bytes
    inputs to ascii before formatting.
    """

    def _to_str(x):
        return x.decode("ascii") if isinstance(x, (bytes, bytearray)) else x

    parts = []
    for k, v in trailers:
        parts.append(f"{_to_str(k).lower()}: {_to_str(v)}\r\n".encode("ascii"))
    return b"".join(parts)


_sonora_protocol.pack_trailers = _pack_trailers
_sonora_asgi.protocol.pack_trailers = _pack_trailers

logger.info(
    "sonora shim active: CORS preflight bytes/str fix + trailer encoding fix"
)
