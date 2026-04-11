"""Retention sweeper for terminal sessions.

Periodically walks sessions whose status is COMPLETED or ABORTED and
whose `ended_at` (falling back to `created_at` if unset) is older than
the configured retention window, then asks the store to delete them.
Live sessions are never touched.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable

from harmonograf_server.storage.base import Session, SessionStatus, Store


logger = logging.getLogger("harmonograf_server.retention")


_TERMINAL = (SessionStatus.COMPLETED, SessionStatus.ABORTED)


def _expired(sessions: Iterable[Session], cutoff: float) -> list[str]:
    out: list[str] = []
    for s in sessions:
        if s.status not in _TERMINAL:
            continue
        ts = s.ended_at if s.ended_at is not None else s.created_at
        if ts <= cutoff:
            out.append(s.id)
    return out


async def sweep_once(store: Store, retention_seconds: float, *, now: float | None = None) -> int:
    """Single retention pass. Returns count of sessions deleted."""
    if retention_seconds <= 0:
        return 0
    current = now if now is not None else time.time()
    cutoff = current - retention_seconds
    sessions = await store.list_sessions()
    victims = _expired(sessions, cutoff)
    deleted = 0
    for sid in victims:
        try:
            if await store.delete_session(sid):
                deleted += 1
        except Exception:
            logger.exception("retention delete failed session=%s", sid)
    if deleted:
        logger.info("retention swept %d session(s)", deleted)
    return deleted


async def retention_loop(
    store: Store,
    retention_seconds: float,
    interval_seconds: float,
) -> None:
    """Background loop. Exits cleanly on cancellation."""
    if retention_seconds <= 0 or interval_seconds <= 0:
        return
    try:
        while True:
            try:
                await sweep_once(store, retention_seconds)
            except Exception:
                logger.exception("retention sweep crashed; continuing")
            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        return
