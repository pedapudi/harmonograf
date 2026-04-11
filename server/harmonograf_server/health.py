"""Tiny ASGI router that mounts /healthz and /readyz alongside sonora.

Both endpoints return text/plain. /healthz is always 200 as long as the
process is serving requests. /readyz additionally calls store.ping()
and returns 503 if that raises or returns falsy. Both endpoints are
intentionally exempt from the bearer-token guard so orchestrators can
probe without credentials.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from harmonograf_server.storage.base import Store


logger = logging.getLogger("harmonograf_server.health")


ASGIApp = Callable[[dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]]


def _plain(status: int, body: bytes):
    return (
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        },
        {"type": "http.response.body", "body": body},
    )


def build_health_router(store: Store, inner: ASGIApp) -> ASGIApp:
    """Return an ASGI app that answers /healthz and /readyz, forwarding
    every other request to ``inner`` (the sonora gRPC-Web app)."""

    async def app(scope, receive, send):
        if scope.get("type") != "http":
            await inner(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/healthz":
            start, body = _plain(200, b"ok\n")
            await send(start)
            await send(body)
            return
        if path == "/readyz":
            try:
                ready = await store.ping()
            except Exception:
                logger.exception("readyz: store.ping() raised")
                ready = False
            if ready:
                start, body = _plain(200, b"ready\n")
            else:
                start, body = _plain(503, b"not ready\n")
            await send(start)
            await send(body)
            return
        await inner(scope, receive, send)

    return app
