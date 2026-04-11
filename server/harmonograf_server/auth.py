"""Optional shared-secret auth for gRPC and gRPC-Web.

When a non-empty token is configured, every RPC must carry an
``authorization: bearer <token>`` metadata header (or an equivalent
HTTP header on the sonora side). Missing or mismatched tokens are
rejected with UNAUTHENTICATED.

This is explicitly *not* a real auth system — there is no rotation,
no TLS, no multi-tenant scoping. It exists to prevent accidental
cross-machine leakage in shared dev environments.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

import grpc
from grpc.aio import ServerInterceptor


logger = logging.getLogger("harmonograf_server.auth")

_BEARER_PREFIX = "bearer "


def _extract_token_from_metadata(metadata) -> Optional[str]:
    if metadata is None:
        return None
    for key, value in metadata:
        if key.lower() != "authorization":
            continue
        if isinstance(value, bytes):
            try:
                value = value.decode("ascii")
            except UnicodeDecodeError:
                return None
        if value.lower().startswith(_BEARER_PREFIX):
            return value[len(_BEARER_PREFIX):]
        return value
    return None


class BearerTokenInterceptor(ServerInterceptor):
    """grpc.aio interceptor that enforces a bearer token on every RPC."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("BearerTokenInterceptor requires a non-empty token")
        self._token = token

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler]],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        supplied = _extract_token_from_metadata(handler_call_details.invocation_metadata)
        if supplied != self._token:
            logger.warning(
                "rejecting unauthenticated call method=%s",
                handler_call_details.method,
            )
            return _unauthenticated_handler()
        return await continuation(handler_call_details)


def _unauthenticated_handler() -> grpc.RpcMethodHandler:
    async def abort(ignored_request, context):
        await context.abort(
            grpc.StatusCode.UNAUTHENTICATED,
            "missing or invalid bearer token",
        )

    return grpc.unary_unary_rpc_method_handler(abort)


# ---- sonora / ASGI side --------------------------------------------------


def asgi_bearer_guard(inner, token: str):
    """Wrap an ASGI app to require ``authorization: bearer <token>``.

    Applied to the sonora grpcASGI app *and* the healthz/readyz app (healthz
    is intentionally exempt — see main.py for how we mount).
    """
    if not token:
        raise ValueError("asgi_bearer_guard requires a non-empty token")
    expected = token

    async def app(scope, receive, send):
        if scope.get("type") != "http":
            await inner(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        raw = headers.get(b"authorization")
        supplied: Optional[str] = None
        if raw is not None:
            try:
                decoded = raw.decode("ascii")
            except UnicodeDecodeError:
                decoded = ""
            if decoded.lower().startswith(_BEARER_PREFIX):
                supplied = decoded[len(_BEARER_PREFIX):]
            else:
                supplied = decoded
        if supplied != expected:
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            })
            await send({
                "type": "http.response.body",
                "body": b"unauthorized",
            })
            return
        await inner(scope, receive, send)

    return app
