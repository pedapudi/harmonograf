"""CLI entrypoint.

Both `python -m harmonograf_server` and the `harmonograf-server` console
script land here. Parses argparse, configures logging, constructs the
ServerConfig, and delegates to main.run().
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from harmonograf_server.config import ServerConfig
from harmonograf_server.main import run


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="harmonograf-server",
        description="Harmonograf coordination console server.",
    )
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=7531, help="gRPC port (default: 7531)")
    p.add_argument(
        "--web-port",
        type=int,
        default=7532,
        help="gRPC-Web (sonora) port (default: 7532)",
    )
    p.add_argument(
        "--store",
        choices=("memory", "sqlite"),
        default="sqlite",
        help="storage backend (default: sqlite)",
    )
    p.add_argument(
        "--data-dir",
        default="~/.harmonograf/data",
        help="data directory for sqlite + payloads (default: ~/.harmonograf/data)",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    p.add_argument(
        "--log-format",
        default="text",
        choices=("text", "json"),
        help="log formatter: text for humans, json for log shippers (default: text)",
    )
    p.add_argument(
        "--metrics-interval-seconds",
        type=float,
        default=30.0,
        help=(
            "how often to emit a metrics snapshot line (sessions/spans/ingest "
            "rate/active streams); 0 disables (default: 30)"
        ),
    )
    p.add_argument(
        "--grace",
        type=float,
        default=5.0,
        help="shutdown grace period in seconds (default: 5.0)",
    )
    p.add_argument(
        "--retention-hours",
        type=float,
        default=0.0,
        help=(
            "delete terminal (COMPLETED/ABORTED) sessions older than this many "
            "hours; 0 disables retention sweeping (default: 0)"
        ),
    )
    p.add_argument(
        "--retention-interval-seconds",
        type=float,
        default=300.0,
        help="retention sweeper interval in seconds (default: 300)",
    )
    p.add_argument(
        "--auth-token",
        default="",
        help=(
            "require this shared secret in the 'authorization: bearer <token>' "
            "metadata header on every RPC (and HTTP header on gRPC-Web); empty "
            "disables auth. /healthz and /readyz are always unauthenticated."
        ),
    )
    p.add_argument(
        "--legacy-plan-attribution-window-ms",
        type=float,
        default=0.0,
        help=(
            "Opt-in fallback for plan-revision attribution; 0 (default) "
            "disables. See docs/runbooks/plan-revision-dedup.md."
        ),
    )
    return p


def config_from_args(argv: list[str] | None = None) -> ServerConfig:
    args = build_parser().parse_args(argv)
    return ServerConfig(
        host=args.host,
        grpc_port=args.port,
        web_port=args.web_port,
        store_backend=args.store,
        data_dir=args.data_dir,
        log_level=args.log_level,
        grace_seconds=args.grace,
        retention_hours=args.retention_hours,
        retention_interval_seconds=args.retention_interval_seconds,
        log_format=args.log_format,
        metrics_interval_seconds=args.metrics_interval_seconds,
        auth_token=args.auth_token,
        legacy_plan_attribution_window_ms=args.legacy_plan_attribution_window_ms,
    )


def main(argv: list[str] | None = None) -> int:
    cfg = config_from_args(argv)
    from harmonograf_server.logging_setup import configure_logging
    configure_logging(cfg.log_level, cfg.log_format)
    if cfg.host != "127.0.0.1":
        logging.warning(
            "binding to non-loopback %s: v0 has no auth; any host on this "
            "network can connect",
            cfg.host,
        )
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
