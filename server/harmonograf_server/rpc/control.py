"""gRPC surface for SubscribeControl.

The server-streaming RPC that the agent opens once after its Welcome
lands. Each yielded ControlEvent is drained from the per-subscription
queue owned by ControlRouter. Acks travel back on the telemetry
upstream (not on this stream).
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

import grpc

from harmonograf_server.control_router import ControlRouter
from harmonograf_server.pb import control_pb2, types_pb2


logger = logging.getLogger(__name__)


class ControlServicerMixin:
    """Mixes into the main HarmonografServicer. Expects `self._router` to
    exist; the composing class wires it in __init__."""

    _router: ControlRouter

    async def SubscribeControl(
        self,
        request: control_pb2.SubscribeControlRequest,
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[types_pb2.ControlEvent]:
        if not request.agent_id:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "agent_id is required"
            )
            return
        if not request.stream_id:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "stream_id is required"
            )
            return

        sub = await self._router.subscribe(
            request.session_id, request.agent_id, request.stream_id
        )
        try:
            while True:
                event = await sub.queue.get()
                if sub.closed:
                    return
                yield event
        except asyncio.CancelledError:
            raise
        finally:
            await self._router.unsubscribe(sub)
