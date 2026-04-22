"""Smoke tests for Harmonograf bootstrap.

Builds a real Harmonograf app (both gRPC + gRPC-Web listeners) against
an InMemoryStore on ephemeral ports, makes a native-gRPC GetStats call,
and a raw HTTP POST against the sonora gRPC-Web endpoint to confirm it
responds to the gRPC-Web wire format.
"""

from __future__ import annotations

import asyncio
import socket

import grpc
import pytest

from harmonograf_server.config import ServerConfig
from harmonograf_server.main import Harmonograf
from harmonograf_server.pb import frontend_pb2, service_pb2_grpc


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_harmonograf_serves_grpc_and_grpc_web():
    cfg = ServerConfig(
        host="127.0.0.1",
        grpc_port=_free_port(),
        web_port=_free_port(),
        store_backend="memory",
        data_dir="",
        grace_seconds=0.5,
    )
    app = await Harmonograf.from_config(cfg)
    await app.start()

    try:
        # 1. Native gRPC: GetStats via the stub.
        async with grpc.aio.insecure_channel(f"127.0.0.1:{cfg.grpc_port}") as ch:
            stub = service_pb2_grpc.HarmonografStub(ch)
            stats = await stub.GetStats(frontend_pb2.GetStatsRequest())
            assert stats.session_count == 0
            assert stats.live_session_count == 0

        # 2. gRPC-Web: raw HTTP POST against the sonora endpoint. We do not
        # decode the framed response body (that is the frontend's job); we
        # just assert the endpoint exists and replies with grpc-status=0.
        reader, writer = await asyncio.open_connection("127.0.0.1", cfg.web_port)
        body = b"\x00\x00\x00\x00\x00"  # single grpc frame: zero-length payload
        path = "/harmonograf.v1.Harmonograf/GetStats"
        req = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{cfg.web_port}\r\n"
            f"Content-Type: application/grpc-web+proto\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode()
        writer.write(req + body)
        await writer.drain()
        resp = b""
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            resp += chunk
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        assert b"HTTP/1.1 200" in resp
        # sonora puts grpc-status into trailers (after the message frame).
        assert b"grpc-status" in resp.lower() or b"grpc-status" in resp
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_harmonograf_stop_is_idempotent():
    cfg = ServerConfig(
        host="127.0.0.1",
        grpc_port=_free_port(),
        web_port=_free_port(),
        store_backend="memory",
        data_dir="",
        grace_seconds=0.2,
    )
    app = await Harmonograf.from_config(cfg)
    await app.start()
    await app.stop()
    # A second stop must not raise.
    await app.stop()


@pytest.mark.asyncio
async def test_harmonograf_run_exits_on_request_stop():
    cfg = ServerConfig(
        host="127.0.0.1",
        grpc_port=_free_port(),
        web_port=_free_port(),
        store_backend="memory",
        data_dir="",
        grace_seconds=0.2,
    )
    app = await Harmonograf.from_config(cfg)

    async def stop_soon():
        await asyncio.sleep(0.2)
        app.request_stop()

    stopper = asyncio.create_task(stop_soon())
    await asyncio.wait_for(app.run(), timeout=5.0)
    await stopper


def test_cli_parser_defaults():
    from harmonograf_server.cli import config_from_args

    cfg = config_from_args([])
    assert cfg.host == "127.0.0.1"
    assert cfg.grpc_port == 7531
    assert cfg.web_port == 7532
    assert cfg.store_backend == "sqlite"

    cfg2 = config_from_args(
        ["--store", "memory", "--port", "9000", "--web-port", "9001", "--host", "0.0.0.0"]
    )
    assert cfg2.store_backend == "memory"
    assert cfg2.grpc_port == 9000
    assert cfg2.web_port == 9001
    assert cfg2.host == "0.0.0.0"


# ---- harmonograf#102: server tunables on ServerConfig + CLI ----------

def test_server_config_tunable_defaults_match_pre_refactor_module_constants():
    """Ship the same defaults the module-level constants had before the
    refactor — ingest/RPC behaviour must not change when nothing is
    overridden. Guards against silent regressions in default tuning.
    """
    cfg = ServerConfig()
    # ingest.py
    assert cfg.heartbeat_timeout_seconds == 15.0
    assert cfg.heartbeat_check_interval_seconds == 5.0
    assert cfg.stuck_threshold_beats == 3
    assert cfg.payload_max_bytes == 64 * 1024 * 1024
    # rpc/frontend.py
    assert cfg.rpc_payload_chunk_bytes == 256 * 1024
    assert cfg.rpc_watch_window_seconds == 3600.0
    assert cfg.rpc_span_tree_limit == 10_000
    assert cfg.rpc_send_control_timeout_seconds == 5.0


def test_cli_heartbeat_timeout_flag():
    from harmonograf_server.cli import config_from_args

    # Default is surfaced.
    assert config_from_args([]).heartbeat_timeout_seconds == 15.0
    # Override flows through to ServerConfig.
    cfg = config_from_args(["--heartbeat-timeout-seconds", "45"])
    assert cfg.heartbeat_timeout_seconds == 45.0


def test_cli_payload_max_bytes_flag():
    from harmonograf_server.cli import config_from_args

    assert config_from_args([]).payload_max_bytes == 64 * 1024 * 1024
    cfg = config_from_args(["--payload-max-bytes", "131072"])
    assert cfg.payload_max_bytes == 131072


def test_cli_rpc_watch_window_flag():
    from harmonograf_server.cli import config_from_args

    assert config_from_args([]).rpc_watch_window_seconds == 3600.0
    cfg = config_from_args(["--rpc-watch-window-seconds", "7200"])
    assert cfg.rpc_watch_window_seconds == 7200.0


def test_cli_rpc_span_tree_limit_flag():
    from harmonograf_server.cli import config_from_args

    assert config_from_args([]).rpc_span_tree_limit == 10_000
    cfg = config_from_args(["--rpc-span-tree-limit", "500"])
    assert cfg.rpc_span_tree_limit == 500


def test_server_config_heartbeat_check_interval_default_and_override():
    # Default matches the pre-refactor module constant.
    assert ServerConfig().heartbeat_check_interval_seconds == 5.0
    # No CLI flag for this one — constructor-only.
    cfg = ServerConfig(heartbeat_check_interval_seconds=1.0)
    assert cfg.heartbeat_check_interval_seconds == 1.0


def test_server_config_stuck_threshold_beats_default_and_override():
    assert ServerConfig().stuck_threshold_beats == 3
    cfg = ServerConfig(stuck_threshold_beats=5)
    assert cfg.stuck_threshold_beats == 5


def test_server_config_rpc_payload_chunk_bytes_default_and_override():
    assert ServerConfig().rpc_payload_chunk_bytes == 256 * 1024
    cfg = ServerConfig(rpc_payload_chunk_bytes=65536)
    assert cfg.rpc_payload_chunk_bytes == 65536


def test_server_config_rpc_send_control_timeout_default_and_override():
    assert ServerConfig().rpc_send_control_timeout_seconds == 5.0
    cfg = ServerConfig(rpc_send_control_timeout_seconds=10.0)
    assert cfg.rpc_send_control_timeout_seconds == 10.0


def test_ingest_pipeline_honors_payload_max_bytes_override():
    """Constructor kwarg threads through to the payload assembler so
    operators tuning ``--payload-max-bytes`` get the DoS guard they
    asked for.
    """
    import asyncio

    from harmonograf_server.bus import SessionBus
    from harmonograf_server.ingest import IngestPipeline
    from harmonograf_server.pb import telemetry_pb2
    from harmonograf_server.storage import make_store

    async def _run():
        store = make_store("memory")
        await store.start()
        bus = SessionBus()
        pipe = IngestPipeline(store, bus, payload_max_bytes=16)
        hello = telemetry_pb2.Hello(agent_id="a1", session_id="sess_cap")
        ctx, _ = await pipe.handle_hello(hello)
        # First chunk fits; the second pushes us over the 16-byte cap.
        up1 = telemetry_pb2.PayloadUpload(
            digest="deadbeef", mime="text/plain", total_size=32, chunk=b"x" * 12
        )
        await pipe._handle_payload(ctx, up1)
        up2 = telemetry_pb2.PayloadUpload(
            digest="deadbeef", mime="text/plain", total_size=32, chunk=b"x" * 8
        )
        try:
            await pipe._handle_payload(ctx, up2)
        except ValueError as e:
            assert "16 bytes" in str(e)
        else:
            raise AssertionError("expected ValueError for exceeded payload cap")

    asyncio.run(_run())


def test_ingest_pipeline_honors_stuck_threshold_override():
    """A looser ``stuck_threshold_beats`` means the stuck flag flips
    only after the configured number of repeats.
    """
    import asyncio

    from harmonograf_server.bus import SessionBus
    from harmonograf_server.ingest import IngestPipeline
    from harmonograf_server.pb import telemetry_pb2
    from harmonograf_server.storage import make_store

    async def _run():
        store = make_store("memory")
        await store.start()
        bus = SessionBus()
        # Five identical beats required before ``is_stuck`` flips.
        pipe = IngestPipeline(store, bus, stuck_threshold_beats=5)
        hello = telemetry_pb2.Hello(agent_id="a1", session_id="sess_stuck")
        ctx, _ = await pipe.handle_hello(hello)
        # First beat seeds the baseline; ``stuck_heartbeat_count`` only
        # increments on the SECOND identical beat. With threshold=5 we
        # therefore need 5 identical repeats after the baseline to trip,
        # i.e. 5 identical beats in total (baseline + 4 repeats keeps
        # the count at 4, below 5).
        for _ in range(5):
            hb = telemetry_pb2.Heartbeat(progress_counter=7, current_activity="loop")
            await pipe._handle_heartbeat(ctx, hb)
        assert ctx.is_stuck is False  # count == 4, still < 5
        # Sixth identical beat hits the override threshold.
        await pipe._handle_heartbeat(
            ctx, telemetry_pb2.Heartbeat(progress_counter=7, current_activity="loop")
        )
        assert ctx.is_stuck is True

    asyncio.run(_run())


def test_telemetry_servicer_uses_configured_rpc_tunables():
    """The frontend mixin reads ``self._config.rpc_*`` — verify that
    a ``TelemetryServicer`` constructed with a ``ServerConfig`` exposes
    the config instance so the RPC tunables take effect.
    """
    from harmonograf_server.bus import SessionBus
    from harmonograf_server.ingest import IngestPipeline
    from harmonograf_server.rpc.telemetry import TelemetryServicer
    from harmonograf_server.storage import make_store

    store = make_store("memory")
    bus = SessionBus()
    ingest = IngestPipeline(store, bus)
    cfg = ServerConfig(
        rpc_watch_window_seconds=120.0,
        rpc_span_tree_limit=42,
        rpc_payload_chunk_bytes=1024,
        rpc_send_control_timeout_seconds=9.0,
    )
    servicer = TelemetryServicer(ingest, data_dir="", config=cfg)
    assert servicer._config.rpc_watch_window_seconds == 120.0
    assert servicer._config.rpc_span_tree_limit == 42
    assert servicer._config.rpc_payload_chunk_bytes == 1024
    assert servicer._config.rpc_send_control_timeout_seconds == 9.0

    # Back-compat: omitting ``config`` falls back to ServerConfig defaults
    # (preserves behaviour for tests and ad-hoc callers).
    default_servicer = TelemetryServicer(ingest, data_dir="")
    assert default_servicer._config.rpc_watch_window_seconds == 3600.0
    assert default_servicer._config.rpc_span_tree_limit == 10_000
