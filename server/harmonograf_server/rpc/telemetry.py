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

from harmonograf_server.bus import SessionBus
from harmonograf_server.config import ServerConfig
from harmonograf_server.control_router import ControlRouter
from harmonograf_server.ingest import IngestPipeline
from harmonograf_server.pb import service_pb2_grpc, telemetry_pb2
from harmonograf_server.rpc.control import ControlServicerMixin
from harmonograf_server.rpc.frontend import FrontendServicerMixin
from harmonograf_server.storage import Store


logger = logging.getLogger(__name__)


class TelemetryServicer(
    FrontendServicerMixin,
    ControlServicerMixin,
    service_pb2_grpc.HarmonografServicer,
):
    """Implements StreamTelemetry + SubscribeControl + frontend RPCs."""

    def __init__(
        self,
        ingest: IngestPipeline,
        router: Optional[ControlRouter] = None,
        *,
        store: Optional[Store] = None,
        bus: Optional[SessionBus] = None,
        data_dir: str = "",
        config: Optional[ServerConfig] = None,
    ) -> None:
        self._ingest = ingest
        self._router = router or ControlRouter()
        self._store = store if store is not None else ingest.store
        self._bus = bus if bus is not None else ingest.bus
        self._data_dir = data_dir
        # Held by reference so RPC handlers can read opt-in config fields
        # (e.g. legacy_plan_attribution_window_ms) without a separate
        # plumb through each call site. Defaults keep test construction
        # zero-arg friendly.
        self._config = config if config is not None else ServerConfig()

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
