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
import time

import grpc
from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig
from sonora.asgi import grpcASGI

from harmonograf_server import _sonora_shim  # noqa: F401  # patches sonora.asgi
from harmonograf_server._cors import asgi_cors
from harmonograf_server.auth import BearerTokenInterceptor, asgi_bearer_guard
from harmonograf_server.bus import SessionBus
from harmonograf_server.config import ServerConfig
from harmonograf_server.control_router import ControlRouter
from harmonograf_server.health import build_health_router
from harmonograf_server.ingest import IngestPipeline
from harmonograf_server.metrics import metrics_loop
from harmonograf_server.pb import service_pb2_grpc
from harmonograf_server.retention import retention_loop
from harmonograf_server.rpc.telemetry import TelemetryServicer, heartbeat_sweeper
from harmonograf_server.static_site import build_static_router
from harmonograf_server.storage import make_store

logger = logging.getLogger("harmonograf_server")

_WEB_STARTUP_TIMEOUT_S = 10.0


async def _web_is_healthy(host: str, port: int) -> bool:
    """Return once the HTTP listener can serve Harmonograf health checks."""
    connect_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(connect_host, port), timeout=0.5
        )
        writer.write(
            f"GET /healthz HTTP/1.1\r\nHost: {connect_host}:{port}\r\n"
            "Connection: close\r\n\r\n".encode()
        )
        await writer.drain()
        status = await asyncio.wait_for(reader.readline(), timeout=0.5)
        return status.startswith(b"HTTP/1.1 200")
    except (TimeoutError, OSError):
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def _wait_for_web_ready(
    task: asyncio.Task, host: str, port: int, timeout_s: float = _WEB_STARTUP_TIMEOUT_S
) -> None:
    """Wait for the web service, surfacing task failure and bounding startup."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if task.done():
            break
        if await _web_is_healthy(host, port):
            return
        await asyncio.sleep(0.05)
    # A dead server task is the more informative failure, even when it died
    # in the final poll window; `await task` re-raises its real exception.
    if task.done():
        await task
        raise RuntimeError("web server exited during startup")
    raise TimeoutError(f"web listener did not become healthy within {timeout_s:g}s")


async def _cancel_task(task: asyncio.Task | None, name: str) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("%s failed during shutdown", name)


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
        self._grpc_server: grpc.aio.Server | None = None
        self._web_shutdown: asyncio.Event | None = None
        self._web_task: asyncio.Task | None = None
        self._sweeper_task: asyncio.Task | None = None
        self._retention_task: asyncio.Task | None = None
        self._metrics_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    @classmethod
    async def from_config(cls, cfg: ServerConfig) -> Harmonograf:
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

        async def _on_status_query(
            session_id: str, agent_id: str, span_id: str, report: str
        ) -> None:
            bus.publish_task_report(
                session_id, agent_id, report, invocation_span_id=span_id
            )

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
        """Start both listeners, or roll back every acquired resource.

        Returning from this method is the public readiness boundary: native
        clients can connect and the HTTP service answers ``/healthz``.
        """
        try:
            await self._start()
        except BaseException:
            await self.stop()
            raise

    async def _start(self) -> None:
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
        # add_insecure_port raises RuntimeError when the bind fails (grpcio
        # validates the core's port result); test_bootstrap relies on that to
        # keep "started" implying a live native listener.
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
        service_pb2_grpc.add_HarmonografServicer_to_server(self.servicer, grpc_web_app)
        # Bearer-token guard wraps only the gRPC-Web app; /healthz and
        # /readyz remain unauthenticated so orchestrators can probe.
        if self.cfg.auth_token:
            grpc_web_app = asgi_bearer_guard(grpc_web_app, self.cfg.auth_token)
        # CORS middleware sits outside auth so preflights succeed even
        # before the browser attaches the bearer token.
        grpc_web_app = asgi_cors(grpc_web_app)
        # Health router answers /healthz + /readyz and forwards everything
        # else to gRPC-Web. The static layer wraps that: it serves the
        # built console SPA (with runtime endpoint injection + SPA fallback)
        # for browser GET/HEAD paths and forwards gRPC-Web + health through.
        health_app = build_health_router(self.store, grpc_web_app)
        self._web_app = build_static_router(
            health_app,
            web_root=self.cfg.web_root,
            web_port=self.cfg.web_port,
            public_base_url=self.cfg.public_base_url,
        )
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
        await _wait_for_web_ready(self._web_task, self.cfg.host, self.cfg.web_port)
        logger.info("gRPC-Web listening on %s:%d", self.cfg.host, self.cfg.web_port)

    async def stop(self) -> None:
        logger.info("shutting down (grace=%.1fs)", self.cfg.grace_seconds)
        await _cancel_task(self._sweeper_task, "heartbeat sweeper")
        await _cancel_task(self._retention_task, "retention sweeper")
        await _cancel_task(self._metrics_task, "metrics loop")
        if self._grpc_server is not None:
            try:
                await self._grpc_server.stop(grace=self.cfg.grace_seconds)
            except Exception:
                logger.exception("native gRPC listener failed during shutdown")
        if self._web_shutdown is not None:
            self._web_shutdown.set()
        if self._web_task is not None:
            try:
                await asyncio.wait_for(
                    self._web_task, timeout=self.cfg.grace_seconds + 1
                )
            except TimeoutError:
                logger.error("web listener exceeded its shutdown deadline")
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("web listener failed during shutdown")
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
