"""gRPC surface for StreamTelemetry.

Owns the bidi stream lifecycle and delegates message semantics to
IngestPipeline. This keeps the ingest logic transport-agnostic and easy
to unit-test without standing up a real gRPC server.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Optional

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from harmonograf_server.control_router import ControlRouter
from harmonograf_server.ingest import IngestPipeline
from harmonograf_server.pb import service_pb2_grpc, telemetry_pb2
from harmonograf_server.rpc.control import ControlServicerMixin


logger = logging.getLogger(__name__)


class TelemetryServicer(ControlServicerMixin, service_pb2_grpc.HarmonografServicer):
    """Implements StreamTelemetry + SubscribeControl.

    Frontend-facing RPCs (ListSessions, WatchSession, etc.) land in task #3
    as additional mixins composed into this same class.
    """

    def __init__(
        self, ingest: IngestPipeline, router: Optional[ControlRouter] = None
    ) -> None:
        self._ingest = ingest
        self._router = router or ControlRouter()

    async def StreamTelemetry(
        self,
        request_iterator: AsyncIterator[telemetry_pb2.TelemetryUp],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[telemetry_pb2.TelemetryDown]:
        ctx = None
        try:
            # First message MUST be Hello.
            first: Optional[telemetry_pb2.TelemetryUp] = None
            async for msg in request_iterator:
                first = msg
                break
            if first is None:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "empty telemetry stream; expected Hello",
                )
                return
            if first.WhichOneof("msg") != "hello":
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "first TelemetryUp must be Hello",
                )
                return

            try:
                ctx, _session = await self._ingest.handle_hello(first.hello)
            except ValueError as e:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(e))
                return

            # Send Welcome.
            welcome = telemetry_pb2.Welcome(
                accepted=True,
                assigned_session_id=ctx.session_id,
                assigned_stream_id=ctx.stream_id,
            )
            server_ts = Timestamp()
            server_ts.GetCurrentTime()
            welcome.server_time.CopyFrom(server_ts)
            yield telemetry_pb2.TelemetryDown(welcome=welcome)

            # Drain the rest of the stream.
            async for msg in request_iterator:
                try:
                    await self._ingest.handle_message(ctx, msg)
                except ValueError as e:
                    logger.warning("ingest error agent_id=%s: %s", ctx.agent_id, e)
                    await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(e))
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("StreamTelemetry crashed")
            raise
        finally:
            if ctx is not None:
                await self._ingest.close_stream(ctx)


async def heartbeat_sweeper(
    ingest: IngestPipeline, *, interval_s: float = 5.0
) -> None:
    """Background task: periodically mark agents whose streams have gone
    quiet for too long as DISCONNECTED. The RPC coroutine for the actual
    stream will also eventually be terminated by gRPC timeouts; this is a
    belt-and-suspenders check for the agent row in the UI.
    """
    while True:
        try:
            await asyncio.sleep(interval_s)
            expired = await ingest.sweep_heartbeats()
            for ctx in expired:
                logger.info(
                    "heartbeat timeout session_id=%s agent_id=%s stream_id=%s",
                    ctx.session_id,
                    ctx.agent_id,
                    ctx.stream_id,
                )
                await ingest.close_stream(ctx, reason="heartbeat_timeout")
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("heartbeat_sweeper iteration failed")
