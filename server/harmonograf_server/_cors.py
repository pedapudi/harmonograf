"""ASGI CORS middleware for the gRPC-Web app.

Why this exists: sonora's built-in CORS support echoes the request's
``host`` header back as ``Access-Control-Allow-Origin`` (and also sets
``Access-Control-Allow-Credentials: true``). Browsers reject that
because the host string ("127.0.0.1:7532") is not the same as the
page Origin ("http://127.0.0.1:5173"), and the credentialed mode
requires an exact echo. ``_sonora_shim`` patches the upstream
bytes/str crash but cannot fix the policy choice without invasive
forks. This middleware sits in front of the sonora app, handles
OPTIONS preflights itself, and rewrites ``Access-Control-Allow-*``
headers on outgoing responses so the frontend's gRPC-Web fetches
actually pass the browser CORS check.
"""

from __future__ import annotations

from typing import Awaitable, Callable

ASGIApp = Callable[..., Awaitable[None]]
Scope = dict
Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]


_ALLOW_HEADERS = (
    b"content-type, x-grpc-web, x-user-agent, grpc-timeout, authorization, "
    b"x-grpc-web-text"
)
_EXPOSE_HEADERS = b"grpc-status, grpc-message, grpc-status-details-bin"


def _origin(scope: Scope) -> bytes:
    for header, value in scope.get("headers") or ():
        if header == b"origin":
            return value
    return b"*"


def asgi_cors(inner: ASGIApp) -> ASGIApp:
    """Wrap ``inner`` with permissive, browser-friendly CORS handling."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await inner(scope, receive, send)
            return

        origin = _origin(scope)
        # Use credentials only when echoing a real origin. With "*" we
        # cannot legally set credentials true.
        allow_credentials = origin != b"*"

        cors_headers = [
            (b"access-control-allow-origin", origin),
            (b"access-control-allow-methods", b"POST, OPTIONS"),
            (b"access-control-allow-headers", _ALLOW_HEADERS),
            (b"access-control-expose-headers", _EXPOSE_HEADERS),
            (b"access-control-max-age", b"86400"),
            (b"vary", b"origin"),
        ]
        if allow_credentials:
            cors_headers.append((b"access-control-allow-credentials", b"true"))

        if scope.get("method") == "OPTIONS":
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": cors_headers
                    + [(b"content-length", b"0")],
                }
            )
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                # Drop any ACAO/ACAC headers the inner app set so we
                # don't end up with conflicting duplicates, then append
                # our own. Also normalize Content-Type: sonora echoes
                # whatever Accept the request sent, but browsers default
                # to "*/*" which Connect-Web rejects as a non-gRPC-Web
                # response. Force it to application/grpc-web+proto.
                existing: list[tuple[bytes, bytes]] = []
                for k, v in message.get("headers") or ():
                    kl = k.lower()
                    if kl.startswith(b"access-control-"):
                        continue
                    if kl == b"content-type" and (v == b"*/*" or v.startswith(b"*/")):
                        v = b"application/grpc-web+proto"
                    existing.append((k, v))
                message = dict(message)
                message["headers"] = existing + cors_headers
            await send(message)

        await inner(scope, receive, send_wrapper)

    return app
