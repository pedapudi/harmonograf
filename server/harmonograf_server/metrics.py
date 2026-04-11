"""Periodic INFO-level metrics snapshot.

Emits one line every `interval_seconds` with: ingest rate (spans per
second since the last snapshot), active telemetry streams, and total
session count. Structured logging mode renders these as a JSON record
with distinct fields; text mode still gets them via the log message.
"""

from __future__ import annotations

import asyncio
import logging
import time

from harmonograf_server.ingest import IngestPipeline
from harmonograf_server.storage.base import Store


logger = logging.getLogger("harmonograf_server.metrics")


async def metrics_loop(
    ingest: IngestPipeline,
    store: Store,
    interval_seconds: float,
) -> None:
    """Background loop. Exits cleanly on cancellation."""
    if interval_seconds <= 0:
        return
    last_ts = time.monotonic()
    last_span_count = (await store.stats()).span_count
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                stats = await store.stats()
                now = time.monotonic()
                dt = max(now - last_ts, 1e-6)
                delta_spans = max(stats.span_count - last_span_count, 0)
                rate = delta_spans / dt
                last_ts = now
                last_span_count = stats.span_count
                logger.info(
                    "metrics sessions=%d spans=%d ingest_rate=%.2f active_streams=%d",
                    stats.session_count,
                    stats.span_count,
                    rate,
                    ingest.active_stream_count(),
                    extra={
                        "metric": True,
                        "sessions": stats.session_count,
                        "spans": stats.span_count,
                        "ingest_rate": round(rate, 3),
                        "active_streams": ingest.active_stream_count(),
                    },
                )
            except Exception:
                logger.exception("metrics snapshot failed")
    except asyncio.CancelledError:
        return
