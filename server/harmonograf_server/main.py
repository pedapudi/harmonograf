"""Server bootstrap: wire components and run until signal.

One process hosts two listeners so a single `harmonograf-server` does
everything the frontend and agents need:

  * grpc.aio server on cfg.grpc_port  — native gRPC for Python clients
    (the agent client library) and for test tooling.
  * sonora grpcASGI on cfg.web_port    — gRPC-Web for the browser
    frontend, served by hypercorn.

Both listeners share the same `TelemetryServicer` instance so telemetry
flowing over grpc.aio and control/watch traffic flowing over grpc-web
see the same SessionBus and ControlRouter.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Optional

import grpc
from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig

from harmonograf_server import _sonora_shim  # noqa: F401  # patches sonora.asgi
from harmonograf_server._cors import asgi_cors
from sonora.asgi import grpcASGI

from harmonograf_server.auth import BearerTokenInterceptor, asgi_bearer_guard
from harmonograf_server.bus import SessionBus
from harmonograf_server.config import ServerConfig
from harmonograf_server.control_router import ControlRouter
from harmonograf_server.health import build_health_router
from harmonograf_server.ingest import IngestPipeline
from harmonograf_server.pb import service_pb2_grpc
from harmonograf_server.metrics import metrics_loop
from harmonograf_server.retention import retention_loop
from harmonograf_server.rpc.telemetry import TelemetryServicer, heartbeat_sweeper
from harmonograf_server.storage import make_store


logger = logging.getLogger("harmonograf_server")


class Harmonograf:
    """Composition root. Build with from_config(), then await run()."""

    def __init__(
        self,
        cfg: ServerConfig,
        *,
        store,
        bus: SessionBus,
        router: ControlRouter,
        ingest: IngestPipeline,
        servicer: TelemetryServicer,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.bus = bus
        self.router = router
        self.ingest = ingest
        self.servicer = servicer
        self._grpc_server: Optional[grpc.aio.Server] = None
        self._web_shutdown: Optional[asyncio.Event] = None
        self._sweeper_task: Optional[asyncio.Task] = None
        self._retention_task: Optional[asyncio.Task] = None
        self._metrics_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    @classmethod
    async def from_config(cls, cfg: ServerConfig) -> "Harmonograf":
        data_dir = os.path.expanduser(cfg.data_dir)
        if cfg.store_backend == "sqlite":
            os.makedirs(data_dir, exist_ok=True)
            store = make_store(
                "sqlite",
                db_path=os.path.join(data_dir, "harmonograf.db"),
                payload_dir=os.path.join(data_dir, "payloads"),
            )
        else:
            store = make_store(cfg.store_backend)
        await store.start()

        bus = SessionBus()
        router = ControlRouter()
        ingest = IngestPipeline(
            store,
            bus,
            control_sink=router,
            heartbeat_timeout_s=cfg.heartbeat_timeout_seconds,
            stuck_threshold_beats=cfg.stuck_threshold_beats,
            payload_max_bytes=cfg.payload_max_bytes,
        )

        async def _on_status_query(session_id: str, agent_id: str, span_id: str, report: str) -> None:
            bus.publish_task_report(session_id, agent_id, report, invocation_span_id=span_id)

        router.on_status_query_response(_on_status_query)

        servicer = TelemetryServicer(
            ingest, router=router, data_dir=data_dir, config=cfg
        )
        return cls(
            cfg,
            store=store,
            bus=bus,
            router=router,
            ingest=ingest,
            servicer=servicer,
        )

    async def start(self) -> None:
        # Native gRPC server. Install bearer-token interceptor iff configured.
        interceptors = []
        if self.cfg.auth_token:
            interceptors.append(BearerTokenInterceptor(self.cfg.auth_token))
            logger.info("bearer-token auth enabled on gRPC + gRPC-Web")
        self._grpc_server = grpc.aio.server(interceptors=interceptors)
        service_pb2_grpc.add_HarmonografServicer_to_server(
            self.servicer, self._grpc_server
        )
        grpc_bind = f"{self.cfg.host}:{self.cfg.grpc_port}"
        self._grpc_server.add_insecure_port(grpc_bind)
        await self._grpc_server.start()
        logger.info("gRPC listening on %s", grpc_bind)

        # Background sweep for heartbeat timeouts.
        self._sweeper_task = asyncio.create_task(
            heartbeat_sweeper(
                self.ingest,
                interval_s=self.cfg.heartbeat_check_interval_seconds,
            ),
            name="heartbeat_sweeper",
        )

        # Retention sweeper (no-op when retention_hours == 0).
        if self.cfg.retention_hours > 0:
            self._retention_task = asyncio.create_task(
                retention_loop(
                    self.store,
                    self.cfg.retention_hours * 3600.0,
                    self.cfg.retention_interval_seconds,
                ),
                name="retention_sweeper",
            )
            logger.info(
                "retention sweeper active: %.1fh window, %.0fs interval",
                self.cfg.retention_hours,
                self.cfg.retention_interval_seconds,
            )

        # Periodic metrics snapshot.
        if self.cfg.metrics_interval_seconds > 0:
            self._metrics_task = asyncio.create_task(
                metrics_loop(
                    self.ingest,
                    self.store,
                    self.cfg.metrics_interval_seconds,
                ),
                name="metrics_loop",
            )

        # gRPC-Web ASGI app. Reuses the same servicer instance so state is
        # shared with native gRPC.
        grpc_web_app = grpcASGI()
        service_pb2_grpc.add_HarmonografServicer_to_server(
            self.servicer, grpc_web_app
        )
        # Bearer-token guard wraps only the gRPC-Web app; /healthz and
        # /readyz remain unauthenticated so orchestrators can probe.
        if self.cfg.auth_token:
            grpc_web_app = asgi_bearer_guard(grpc_web_app, self.cfg.auth_token)
        # CORS middleware sits outside auth so preflights succeed even
        # before the browser attaches the bearer token.
        grpc_web_app = asgi_cors(grpc_web_app)
        self._web_app = build_health_router(self.store, grpc_web_app)
        self._web_shutdown = asyncio.Event()
        hc = HypercornConfig()
        hc.bind = [f"{self.cfg.host}:{self.cfg.web_port}"]
        hc.graceful_timeout = self.cfg.grace_seconds
        hc.accesslog = None
        hc.errorlog = "-"
        hc.loglevel = self.cfg.log_level.lower()
        self._web_task = asyncio.create_task(
            serve(
                self._web_app,
                hc,
                shutdown_trigger=self._web_shutdown.wait,
            ),
            name="hypercorn_serve",
        )
        logger.info("gRPC-Web listening on %s:%d", self.cfg.host, self.cfg.web_port)

    async def stop(self) -> None:
        logger.info("shutting down (grace=%.1fs)", self.cfg.grace_seconds)
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            try:
                await self._sweeper_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._retention_task is not None:
            self._retention_task.cancel()
            try:
                await self._retention_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._metrics_task is not None:
            self._metrics_task.cancel()
            try:
                await self._metrics_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._grpc_server is not None:
            await self._grpc_server.stop(grace=self.cfg.grace_seconds)
        if self._web_shutdown is not None:
            self._web_shutdown.set()
        if getattr(self, "_web_task", None) is not None:
            try:
                await asyncio.wait_for(self._web_task, timeout=self.cfg.grace_seconds + 1)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        try:
            await self.store.close()
        except Exception:
            logger.exception("error closing store")
        logger.info("shutdown complete")

    async def run(self) -> None:
        await self.start()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except NotImplementedError:  # pragma: no cover - non-unix
                pass
        await self._stop_event.wait()
        await self.stop()

    def request_stop(self) -> None:
        self._stop_event.set()


async def run(cfg: ServerConfig) -> None:
    app = await Harmonograf.from_config(cfg)
    await app.run()
