"""Tests for Store.gc_payloads() and the post-delete sweep wiring."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import grpc
import pytest
import pytest_asyncio

from harmonograf_server.bus import SessionBus
from harmonograf_server.control_router import ControlRouter
from harmonograf_server.ingest import IngestPipeline
from harmonograf_server.pb import frontend_pb2, service_pb2_grpc
from harmonograf_server.rpc.telemetry import TelemetryServicer
from harmonograf_server.storage import (
    Agent,
    AgentStatus,
    Capability,
    Framework,
    Session,
    SessionStatus,
    Span,
    SpanKind,
    SpanStatus,
    make_store,
)


@pytest_asyncio.fixture
async def mem_store():
    s = make_store("memory")
    await s.start()
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
async def sqlite_store(tmp_path):
    s = make_store(
        "sqlite",
        db_path=str(tmp_path / "harmonograf.db"),
        payload_dir=str(tmp_path / "payloads"),
    )
    await s.start()
    try:
        yield s
    finally:
        await s.close()


async def _seed_span_with_payload(store, *, session_id, agent_id, span_id, payload):
    digest = hashlib.sha256(payload).hexdigest()
    await store.create_session(
        Session(id=session_id, title=session_id, created_at=time.time())
    )
    await store.register_agent(
        Agent(
            id=agent_id,
            session_id=session_id,
            name=agent_id,
            framework=Framework.CUSTOM,
            capabilities=[Capability.CANCEL],
            connected_at=time.time(),
            last_heartbeat=time.time(),
            status=AgentStatus.CONNECTED,
        )
    )
    await store.put_payload(digest, payload, "application/json")
    await store.append_span(
        Span(
            id=span_id,
            session_id=session_id,
            agent_id=agent_id,
            kind=SpanKind.TOOL_CALL,
            name="t",
            start_time=time.time(),
            end_time=time.time() + 0.1,
            status=SpanStatus.COMPLETED,
            payload_digest=digest,
        )
    )
    return digest


# ---- core gc_payloads() ---------------------------------------------------


@pytest.mark.asyncio
async def test_gc_payloads_memory_no_orphans(mem_store):
    await _seed_span_with_payload(
        mem_store,
        session_id="sess_a",
        agent_id="ag",
        span_id="sp1",
        payload=b'{"kept": true}',
    )
    removed = await mem_store.gc_payloads()
    assert removed == 0
    stats = await mem_store.stats()
    assert stats.payload_count == 1


@pytest.mark.asyncio
async def test_gc_payloads_memory_removes_orphan(mem_store):
    orphan = b'{"orphaned": true}'
    orphan_digest = hashlib.sha256(orphan).hexdigest()
    await mem_store.put_payload(orphan_digest, orphan, "application/json")
    # No span referencing it -> orphan.
    pre = await mem_store.stats()
    assert pre.payload_count == 1

    removed = await mem_store.gc_payloads()
    assert removed == 1
    post = await mem_store.stats()
    assert post.payload_count == 0


@pytest.mark.asyncio
async def test_gc_payloads_sqlite_removes_orphan_and_file(sqlite_store, tmp_path):
    orphan = b"orphan bytes"
    orphan_digest = hashlib.sha256(orphan).hexdigest()
    await sqlite_store.put_payload(orphan_digest, orphan, "application/octet-stream")
    blob_path = tmp_path / "payloads" / orphan_digest[:2] / orphan_digest
    assert blob_path.exists()

    removed = await sqlite_store.gc_payloads()
    assert removed == 1
    assert not blob_path.exists()
    assert await sqlite_store.has_payload(orphan_digest) is False


@pytest.mark.asyncio
async def test_gc_payloads_sqlite_keeps_referenced(sqlite_store):
    await _seed_span_with_payload(
        sqlite_store,
        session_id="sess_b",
        agent_id="ag",
        span_id="sp_ref",
        payload=b'{"ref": 1}',
    )
    removed = await sqlite_store.gc_payloads()
    assert removed == 0
    stats = await sqlite_store.stats()
    assert stats.payload_count == 1


# ---- post-delete sweep through DeleteSession RPC --------------------------


@pytest_asyncio.fixture
async def harness(mem_store):
    bus = SessionBus()
    router = ControlRouter()
    ingest = IngestPipeline(mem_store, bus, control_sink=router)
    servicer = TelemetryServicer(ingest, router=router, data_dir="")

    server = grpc.aio.server()
    service_pb2_grpc.add_HarmonografServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield {"port": port, "store": mem_store}
    finally:
        await server.stop(grace=0.5)


@pytest.mark.asyncio
async def test_delete_session_sweeps_orphan_payloads(harness):
    store = harness["store"]
    # Seed a session with a payload-bearing span (COMPLETED so delete w/o force).
    digest = await _seed_span_with_payload(
        store,
        session_id="sess_gc",
        agent_id="ag1",
        span_id="sp_del",
        payload=b'{"gone": true}',
    )
    await store.update_session("sess_gc", status=SessionStatus.COMPLETED)

    # Add an *unrelated* orphan payload that predates the session delete —
    # the post-delete sweep should catch it too.
    orphan = b"unrelated orphan"
    orphan_digest = hashlib.sha256(orphan).hexdigest()
    await store.put_payload(orphan_digest, orphan, "text/plain")

    async with grpc.aio.insecure_channel(f"127.0.0.1:{harness['port']}") as ch:
        stub = service_pb2_grpc.HarmonografStub(ch)
        resp = await stub.DeleteSession(
            frontend_pb2.DeleteSessionRequest(session_id="sess_gc")
        )
    assert resp.deleted is True

    # The referenced payload goes away via refcount; the pre-existing orphan
    # goes away via the explicit gc_payloads() sweep.
    assert await store.has_payload(digest) is False
    assert await store.has_payload(orphan_digest) is False
    stats = await store.stats()
    assert stats.payload_count == 0
