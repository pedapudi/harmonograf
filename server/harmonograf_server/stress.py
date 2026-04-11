"""Standalone stress harness.

Spins up an in-process server (memory backend, ephemeral ports), attaches
N client-library agents, drives span traffic at a configurable rate for a
fixed duration, and prints a report with ingest latency percentiles,
memory growth, and SessionBus delta lag.

NOT a pytest. Run as:

    python -m harmonograf_server.stress --agents 50 --spans-per-sec 2000 --duration 60

Useful for regression detection as the ingest hot path evolves.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import resource
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from harmonograf_server.bus import (
    DELTA_BACKPRESSURE,
    DELTA_SPAN_END,
    DELTA_SPAN_START,
    Subscription,
)
from harmonograf_server.config import ServerConfig
from harmonograf_server.main import Harmonograf


# ---- helpers -------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _rss_bytes() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    # Linux reports ru_maxrss in KiB; macOS in bytes. Assume Linux here
    # since the server's deploy target is Linux.
    return usage.ru_maxrss * 1024


def _percentile(sorted_samples: list[float], q: float) -> float:
    if not sorted_samples:
        return 0.0
    if q <= 0:
        return sorted_samples[0]
    if q >= 100:
        return sorted_samples[-1]
    k = (len(sorted_samples) - 1) * (q / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_samples) - 1)
    frac = k - lo
    return sorted_samples[lo] * (1 - frac) + sorted_samples[hi] * frac


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TiB"


# ---- config --------------------------------------------------------------


@dataclass
class StressConfig:
    agents: int = 10
    spans_per_sec: int = 500
    duration: float = 15.0
    session_id: str = "stress"
    # fraction of spans that should carry a small payload (0..1)
    payload_fraction: float = 0.25
    payload_bytes: int = 512


@dataclass
class StressReport:
    config: StressConfig
    spans_emitted: int = 0
    spans_observed: int = 0
    latencies_us: list[float] = field(default_factory=list)
    backpressure_drops: int = 0
    rss_start: int = 0
    rss_end: int = 0
    wall_seconds: float = 0.0
    first_observed_ts: Optional[float] = None
    last_observed_ts: Optional[float] = None

    def summary(self) -> str:
        n = len(self.latencies_us)
        self.latencies_us.sort()
        p50 = _percentile(self.latencies_us, 50)
        p95 = _percentile(self.latencies_us, 95)
        p99 = _percentile(self.latencies_us, 99)
        lmax = self.latencies_us[-1] if self.latencies_us else 0.0
        emitted_rate = self.spans_emitted / self.wall_seconds if self.wall_seconds > 0 else 0.0
        observed_rate = self.spans_observed / self.wall_seconds if self.wall_seconds > 0 else 0.0
        rss_delta = self.rss_end - self.rss_start
        lines = [
            "==== harmonograf stress report ====",
            f"agents:          {self.config.agents}",
            f"target rate:     {self.config.spans_per_sec} spans/s",
            f"duration:        {self.config.duration:.1f}s (wall {self.wall_seconds:.2f}s)",
            f"payload mix:     {int(self.config.payload_fraction * 100)}% x {self.config.payload_bytes}B",
            "",
            f"emitted spans:   {self.spans_emitted} ({emitted_rate:.0f}/s)",
            f"observed spans:  {self.spans_observed} ({observed_rate:.0f}/s)",
            f"latency samples: {n}",
            "",
            "ingest latency (emit -> bus observe):",
            f"  p50:  {p50:9.1f} us",
            f"  p95:  {p95:9.1f} us",
            f"  p99:  {p99:9.1f} us",
            f"  max:  {lmax:9.1f} us",
            "",
            f"RSS start:       {_fmt_bytes(self.rss_start)}",
            f"RSS end:         {_fmt_bytes(self.rss_end)}",
            f"RSS delta:       {_fmt_bytes(max(rss_delta, 0))}",
            f"backpressure:    {self.backpressure_drops} dropped",
        ]
        if self.spans_emitted and self.spans_observed < self.spans_emitted:
            missed = self.spans_emitted - self.spans_observed
            lines.append(
                f"warning:         {missed} spans emitted but not observed "
                f"({missed / self.spans_emitted:.1%})"
            )
        return "\n".join(lines)


# ---- agent worker --------------------------------------------------------


def _agent_worker(
    *,
    idx: int,
    cfg: StressConfig,
    server_addr: str,
    stop_event: threading.Event,
    emitted_counter: list[int],
):
    """Runs in its own thread. Emits spans at cfg.spans_per_sec / cfg.agents."""
    # Import lazily so the server can start before we pay client import cost.
    from harmonograf_client.client import Client

    per_agent_rate = max(1.0, cfg.spans_per_sec / cfg.agents)
    period = 1.0 / per_agent_rate
    payload_every = (
        int(round(1 / cfg.payload_fraction)) if cfg.payload_fraction > 0 else 0
    )

    client = Client(
        name=f"stress_agent_{idx}",
        session_id=cfg.session_id,
        session_title="harmonograf stress run",
        server_addr=server_addr,
        buffer_size=4096,
        # Disable identity-on-disk for clean runs.
        identity_root=os.path.join("/tmp", f"harmonograf_stress_{os.getpid()}_{idx}"),
    )
    try:
        n = 0
        next_tick = time.perf_counter()
        payload_blob = b"x" * cfg.payload_bytes if cfg.payload_bytes > 0 else b""
        while not stop_event.is_set():
            now_perf = time.perf_counter()
            if now_perf < next_tick:
                sleep_for = min(next_tick - now_perf, 0.05)
                if stop_event.wait(sleep_for):
                    break
                continue
            next_tick += period

            t_emit_ns = time.time_ns()
            payload = (
                payload_blob if payload_every and (n % payload_every == 0) else None
            )
            sid = client.emit_span_start(
                kind="TOOL_CALL",
                name=f"op_{n}",
                attributes={
                    "t_emit_ns": t_emit_ns,
                    "agent_idx": idx,
                },
                payload=payload,
            )
            client.emit_span_end(sid, status="COMPLETED")
            n += 1
        emitted_counter[idx] = n
    finally:
        try:
            client.shutdown(flush_timeout=5.0)
        except Exception:
            pass


# ---- observer ------------------------------------------------------------


async def _observer(
    sub: Subscription,
    stop_event: asyncio.Event,
    report: StressReport,
):
    """Drain the bus subscription and record latency for spans that carry
    our t_emit_ns attribute. Exits on stop_event."""
    while not stop_event.is_set():
        try:
            delta = await asyncio.wait_for(sub.queue.get(), timeout=0.2)
        except asyncio.TimeoutError:
            continue

        if delta.kind == DELTA_BACKPRESSURE:
            report.backpressure_drops += int(delta.payload.get("dropped", 0))
            continue
        if delta.kind != DELTA_SPAN_START:
            continue

        span = delta.payload
        attrs = getattr(span, "attributes", None) or {}
        t_emit = attrs.get("t_emit_ns")
        if not isinstance(t_emit, (int, float)):
            continue
        now_ns = time.time_ns()
        lat_us = (now_ns - int(t_emit)) / 1000.0
        if lat_us < 0:
            continue
        report.latencies_us.append(lat_us)
        report.spans_observed += 1
        now_wall = time.time()
        if report.first_observed_ts is None:
            report.first_observed_ts = now_wall
        report.last_observed_ts = now_wall


# ---- main ----------------------------------------------------------------


async def _run(cfg: StressConfig) -> StressReport:
    report = StressReport(config=cfg)

    grpc_port = _free_port()
    web_port = _free_port()
    server_cfg = ServerConfig(
        host="127.0.0.1",
        grpc_port=grpc_port,
        web_port=web_port,
        store_backend="memory",
        log_level="WARNING",
        log_format="text",
        metrics_interval_seconds=0.0,
    )
    app = await Harmonograf.from_config(server_cfg)
    await app.start()

    sub = await app.bus.subscribe(cfg.session_id)
    stop_observer = asyncio.Event()
    observer_task = asyncio.create_task(_observer(sub, stop_observer, report))

    report.rss_start = _rss_bytes()
    stop_event = threading.Event()
    emitted_counter = [0] * cfg.agents
    threads = [
        threading.Thread(
            target=_agent_worker,
            name=f"stress-agent-{i}",
            kwargs={
                "idx": i,
                "cfg": cfg,
                "server_addr": f"127.0.0.1:{grpc_port}",
                "stop_event": stop_event,
                "emitted_counter": emitted_counter,
            },
            daemon=True,
        )
        for i in range(cfg.agents)
    ]

    t0 = time.perf_counter()
    for th in threads:
        th.start()

    # Let the load run for the requested duration.
    await asyncio.sleep(cfg.duration)
    stop_event.set()

    # Wait for client threads to drain.
    for th in threads:
        th.join(timeout=10.0)
    report.wall_seconds = time.perf_counter() - t0
    report.spans_emitted = sum(emitted_counter)

    # Drain the bus for a bit so late spans land in the observer.
    drain_deadline = time.perf_counter() + 2.0
    while time.perf_counter() < drain_deadline:
        await asyncio.sleep(0.1)
        if report.spans_observed >= report.spans_emitted:
            break

    stop_observer.set()
    try:
        await asyncio.wait_for(observer_task, timeout=2.0)
    except asyncio.TimeoutError:
        observer_task.cancel()

    await app.bus.unsubscribe(sub)
    report.rss_end = _rss_bytes()
    await app.stop()
    return report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m harmonograf_server.stress",
        description="Synthetic N-agent load generator for the harmonograf server.",
    )
    p.add_argument("--agents", type=int, default=10)
    p.add_argument("--spans-per-sec", type=int, default=500)
    p.add_argument("--duration", type=float, default=15.0, help="seconds")
    p.add_argument("--session-id", default="stress")
    p.add_argument(
        "--payload-fraction",
        type=float,
        default=0.25,
        help="fraction of spans that should carry a small payload (0..1)",
    )
    p.add_argument(
        "--payload-bytes",
        type=int,
        default=512,
        help="size of synthetic payload per flagged span",
    )
    return p


def _cfg_from_args(argv: list[str] | None) -> StressConfig:
    args = build_parser().parse_args(argv)
    return StressConfig(
        agents=args.agents,
        spans_per_sec=args.spans_per_sec,
        duration=args.duration,
        session_id=args.session_id,
        payload_fraction=max(0.0, min(1.0, args.payload_fraction)),
        payload_bytes=max(0, args.payload_bytes),
    )


def main(argv: list[str] | None = None) -> int:
    cfg = _cfg_from_args(argv)
    report = asyncio.run(_run(cfg))
    print(report.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
