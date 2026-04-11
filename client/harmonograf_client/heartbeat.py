"""Heartbeat assembly.

The transport layer sends a ``Heartbeat`` message every ~5 seconds
(§4.4). It carries buffer health so the server/frontend can visibly
mark agents that are struggling.

This module is proto-free: it builds a plain dataclass from the
current :class:`BufferStats` plus optional self-reported CPU. The
transport converts the dataclass to pb at send time.
"""

from __future__ import annotations

import dataclasses
import os
import time

from .buffer import BufferStats, EventRingBuffer, PayloadBuffer


DEFAULT_INTERVAL_SECONDS = 5.0


@dataclasses.dataclass
class Heartbeat:
    buffered_events: int
    dropped_events: int
    buffered_payload_bytes: int
    cpu_self_pct: float
    sent_at_unix: float


def build_heartbeat(
    events: EventRingBuffer,
    payloads: PayloadBuffer,
    cpu_self_pct: float = 0.0,
    now: float | None = None,
) -> Heartbeat:
    stats: BufferStats = events.stats_snapshot()
    return Heartbeat(
        buffered_events=stats.buffered_events,
        dropped_events=stats.dropped_total,
        buffered_payload_bytes=payloads.buffered_bytes(),
        cpu_self_pct=cpu_self_pct,
        sent_at_unix=now if now is not None else time.time(),
    )


def read_self_cpu_pct() -> float:
    """Best-effort instantaneous CPU-pct estimate, never raises.

    Returns 0.0 when the value isn't cheaply available. The field is
    optional per §4.4 so a zero is a valid stand-in.
    """
    try:
        import resource  # type: ignore
    except ImportError:
        return 0.0
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # Approximate: user+sys time over wall time since process start.
        # Wall-time base cached on the function object to keep stateless
        # callers working correctly.
        now = time.time()
        base = getattr(read_self_cpu_pct, "_base", None)
        last_cpu = getattr(read_self_cpu_pct, "_last_cpu", 0.0)
        last_wall = getattr(read_self_cpu_pct, "_last_wall", now)
        if base is None:
            read_self_cpu_pct._base = now  # type: ignore[attr-defined]
            read_self_cpu_pct._last_cpu = usage.ru_utime + usage.ru_stime  # type: ignore[attr-defined]
            read_self_cpu_pct._last_wall = now  # type: ignore[attr-defined]
            return 0.0
        cur_cpu = usage.ru_utime + usage.ru_stime
        dt = max(now - last_wall, 1e-6)
        pct = max(0.0, (cur_cpu - last_cpu) / dt) * 100.0 / max(os.cpu_count() or 1, 1)
        read_self_cpu_pct._last_cpu = cur_cpu  # type: ignore[attr-defined]
        read_self_cpu_pct._last_wall = now  # type: ignore[attr-defined]
        return round(pct, 2)
    except Exception:
        return 0.0
