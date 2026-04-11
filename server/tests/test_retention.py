"""Tests for the retention sweeper."""

from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio

from harmonograf_server.retention import retention_loop, sweep_once
from harmonograf_server.storage import (
    Session,
    SessionStatus,
    make_store,
)


@pytest_asyncio.fixture
async def store():
    s = make_store("memory")
    await s.start()
    try:
        yield s
    finally:
        await s.close()


async def _mk(store, sid, *, status, created_at, ended_at=None):
    await store.create_session(
        Session(id=sid, title=sid, created_at=created_at)
    )
    await store.update_session(sid, status=status, ended_at=ended_at)


@pytest.mark.asyncio
async def test_sweep_once_deletes_old_terminal_sessions(store):
    now = 10_000.0
    window = 3600.0  # 1 hour
    await _mk(store, "old_done", status=SessionStatus.COMPLETED,
              created_at=now - 9000, ended_at=now - 7200)
    await _mk(store, "old_abort", status=SessionStatus.ABORTED,
              created_at=now - 9000, ended_at=now - 7200)
    await _mk(store, "recent_done", status=SessionStatus.COMPLETED,
              created_at=now - 60, ended_at=now - 30)
    await _mk(store, "live", status=SessionStatus.LIVE,
              created_at=now - 9000)

    deleted = await sweep_once(store, window, now=now)
    assert deleted == 2
    remaining = {s.id for s in await store.list_sessions()}
    assert remaining == {"recent_done", "live"}


@pytest.mark.asyncio
async def test_sweep_once_zero_window_is_noop(store):
    await _mk(
        store, "s1",
        status=SessionStatus.COMPLETED,
        created_at=time.time() - 99999,
        ended_at=time.time() - 99999,
    )
    deleted = await sweep_once(store, 0.0)
    assert deleted == 0
    assert len(await store.list_sessions()) == 1


@pytest.mark.asyncio
async def test_sweep_once_respects_ended_at_not_created_at(store):
    now = 10_000.0
    window = 3600.0
    # Created long ago, but only just ended — should NOT be swept.
    await _mk(store, "long_lived", status=SessionStatus.COMPLETED,
              created_at=now - 100_000, ended_at=now - 10)
    deleted = await sweep_once(store, window, now=now)
    assert deleted == 0


@pytest.mark.asyncio
async def test_sweep_once_falls_back_to_created_at_when_ended_at_missing(store):
    now = 10_000.0
    window = 3600.0
    # COMPLETED but ended_at unset — use created_at.
    await store.create_session(Session(id="weird", title="w", created_at=now - 9999))
    await store.update_session("weird", status=SessionStatus.COMPLETED)
    deleted = await sweep_once(store, window, now=now)
    assert deleted == 1


@pytest.mark.asyncio
async def test_retention_loop_runs_and_exits_on_cancel(store):
    now = time.time()
    await _mk(store, "victim", status=SessionStatus.COMPLETED,
              created_at=now - 9999, ended_at=now - 9999)

    task = asyncio.create_task(retention_loop(store, 3600.0, 0.05))
    # Give it a tick or two to run.
    for _ in range(20):
        await asyncio.sleep(0.02)
        if not await store.list_sessions():
            break
    assert await store.list_sessions() == []

    task.cancel()
    await task  # should not raise


@pytest.mark.asyncio
async def test_retention_loop_zero_window_returns_immediately(store):
    # Should return without looping.
    await asyncio.wait_for(retention_loop(store, 0.0, 60.0), timeout=0.5)
