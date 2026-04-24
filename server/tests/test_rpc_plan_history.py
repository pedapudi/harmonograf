"""Coverage for the ``GetSessionPlanHistory`` RPC (harmonograf#158).

The RPC returns every persisted ``task_plans`` row for one session in
``created_at`` order, packaged as ``PlanRevision`` records carrying the
full ``goldfive.v1.Plan`` snapshot plus denormalized revision metadata
(``revision_number`` / ``revision_reason`` / ``revision_kind`` /
``revision_trigger_event_id`` / ``emitted_at``).

These tests assert:
  * empty session → zero revisions
  * single ``plan_submitted`` → one revision, revision_number=0, no trigger
  * ``plan_submitted`` + one ``plan_revised`` → two revisions, the second
    carries the drift trigger
  * multi-revision → all, in chronological order
  * corrupt row (revision_index>0 but empty revision_kind) → returned
    gracefully with empty trigger fields and a WARNING in the logs
  * end-to-end proto round-trip through the real gRPC server
  * error paths: empty session_id → INVALID_ARGUMENT; unknown session →
    NOT_FOUND
"""

from __future__ import annotations

import logging

import grpc
import pytest
import pytest_asyncio

from harmonograf_server.bus import SessionBus
from harmonograf_server.control_router import ControlRouter
from harmonograf_server.ingest import IngestPipeline
from harmonograf_server.pb import (
    frontend_pb2,
    service_pb2_grpc,
)
from harmonograf_server.rpc.telemetry import TelemetryServicer
from harmonograf_server.storage import (
    Session,
    SessionStatus,
    Task,
    TaskPlan,
    TaskStatus,
    make_store,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def store():
    s = make_store("memory")
    await s.start()
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
async def rpc_stack(store):
    bus = SessionBus()
    router = ControlRouter()
    ingest = IngestPipeline(store, bus, control_sink=router)
    servicer = TelemetryServicer(ingest, router=router, data_dir="/var/harmonograf")
    server = grpc.aio.server()
    service_pb2_grpc.add_HarmonografServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield {"port": port, "store": store, "servicer": servicer}
    finally:
        await server.stop(grace=0.3)


async def _seed_session(store, sid: str, created_at: float = 1_000.0) -> None:
    await store.create_session(
        Session(id=sid, title=sid, created_at=created_at, status=SessionStatus.LIVE)
    )


def _make_task(tid: str, status: TaskStatus = TaskStatus.PENDING) -> Task:
    return Task(
        id=tid,
        title=tid,
        description="",
        assignee_agent_id="a",
        status=status,
    )


async def _call(stack, session_id: str):
    ch = grpc.aio.insecure_channel(f"127.0.0.1:{stack['port']}")
    try:
        stub = service_pb2_grpc.HarmonografStub(ch)
        return await stub.GetSessionPlanHistory(
            frontend_pb2.GetSessionPlanHistoryRequest(session_id=session_id)
        )
    finally:
        await ch.close()


# ---------------------------------------------------------------------------
# Happy paths — everything exercises the real gRPC stack via _call().
# ---------------------------------------------------------------------------


async def test_empty_session_returns_no_revisions(rpc_stack):
    store = rpc_stack["store"]
    sid = "sess_empty"
    await _seed_session(store, sid)
    resp = await _call(rpc_stack, sid)
    assert list(resp.revisions) == []


async def test_single_plan_submitted_one_revision_no_trigger(rpc_stack):
    store = rpc_stack["store"]
    sid = "sess_single"
    await _seed_session(store, sid, created_at=100.0)
    await store.put_task_plan(
        TaskPlan(
            id="p0",
            session_id=sid,
            created_at=110.0,
            summary="initial plan",
            tasks=[_make_task("t1"), _make_task("t2")],
            edges=[],
            revision_index=0,
        )
    )
    resp = await _call(rpc_stack, sid)
    assert len(resp.revisions) == 1
    rev = resp.revisions[0]
    assert rev.revision_number == 0
    assert rev.revision_reason == ""
    assert rev.revision_kind == ""
    assert rev.revision_trigger_event_id == ""
    # Plan payload is a faithful goldfive.v1.Plan snapshot.
    assert rev.plan.id == "p0"
    assert rev.plan.summary == "initial plan"
    assert [t.id for t in rev.plan.tasks] == ["t1", "t2"]
    assert rev.plan.revision_index == 0
    assert rev.emitted_at.seconds == 110


async def test_plan_submitted_plus_plan_revised_two_revisions(rpc_stack):
    store = rpc_stack["store"]
    sid = "sess_revised"
    await _seed_session(store, sid, created_at=100.0)
    await store.put_task_plan(
        TaskPlan(
            id="p0",
            session_id=sid,
            created_at=110.0,
            summary="initial",
            tasks=[_make_task("t1")],
            edges=[],
            revision_index=0,
        )
    )
    await store.put_task_plan(
        TaskPlan(
            id="p1",
            session_id=sid,
            created_at=120.0,
            summary="revised",
            tasks=[_make_task("t1"), _make_task("t2")],
            edges=[],
            revision_reason="agent re-reading same doc",
            revision_kind="looping_reasoning",
            revision_severity="warning",
            revision_index=1,
            trigger_event_id="drift_abc123",
        )
    )
    resp = await _call(rpc_stack, sid)
    assert len(resp.revisions) == 2
    # Chronological order: r0 then r1.
    assert [r.revision_number for r in resp.revisions] == [0, 1]
    # r0: no trigger fields.
    assert resp.revisions[0].revision_kind == ""
    assert resp.revisions[0].revision_trigger_event_id == ""
    # r1: trigger populated from the plan's denormalized fields.
    r1 = resp.revisions[1]
    assert r1.revision_number == 1
    assert r1.revision_reason == "agent re-reading same doc"
    assert r1.revision_kind == "LOOPING_REASONING"
    assert r1.revision_trigger_event_id == "drift_abc123"
    assert r1.emitted_at.seconds == 120
    assert [t.id for t in r1.plan.tasks] == ["t1", "t2"]


async def test_multi_revision_chronological_order(rpc_stack):
    store = rpc_stack["store"]
    sid = "sess_multi"
    await _seed_session(store, sid, created_at=0.0)
    # Insert out of order to prove the handler sorts by created_at.
    await store.put_task_plan(
        TaskPlan(
            id="p2",
            session_id=sid,
            created_at=300.0,
            summary="third",
            tasks=[],
            edges=[],
            revision_reason="cascade downstream",
            revision_kind="cascade_cancel",
            revision_index=2,
            trigger_event_id="drift_cascade",
        )
    )
    await store.put_task_plan(
        TaskPlan(
            id="p0",
            session_id=sid,
            created_at=100.0,
            summary="first",
            tasks=[],
            edges=[],
            revision_index=0,
        )
    )
    await store.put_task_plan(
        TaskPlan(
            id="p1",
            session_id=sid,
            created_at=200.0,
            summary="second",
            tasks=[],
            edges=[],
            revision_reason="user steered",
            revision_kind="user_steer",
            revision_index=1,
            trigger_event_id="ann_xyz",
        )
    )
    resp = await _call(rpc_stack, sid)
    assert [r.revision_number for r in resp.revisions] == [0, 1, 2]
    assert [r.plan.id for r in resp.revisions] == ["p0", "p1", "p2"]
    assert [r.plan.summary for r in resp.revisions] == ["first", "second", "third"]
    assert [r.emitted_at.seconds for r in resp.revisions] == [100, 200, 300]
    # Trigger ids propagate on non-zero revisions only.
    assert resp.revisions[0].revision_trigger_event_id == ""
    assert resp.revisions[1].revision_trigger_event_id == "ann_xyz"
    assert resp.revisions[2].revision_trigger_event_id == "drift_cascade"
    assert resp.revisions[1].revision_kind == "USER_STEER"
    assert resp.revisions[2].revision_kind == "CASCADE_CANCEL"


async def test_corrupt_revision_missing_kind_logs_and_returns(rpc_stack, caplog):
    """revision_index>0 but empty revision_kind → WARNING + empty fields."""

    store = rpc_stack["store"]
    sid = "sess_corrupt"
    await _seed_session(store, sid)
    await store.put_task_plan(
        TaskPlan(
            id="p0",
            session_id=sid,
            created_at=100.0,
            summary="initial",
            tasks=[],
            edges=[],
            revision_index=0,
        )
    )
    await store.put_task_plan(
        TaskPlan(
            id="p1",
            session_id=sid,
            created_at=110.0,
            summary="corrupt revision",
            tasks=[],
            edges=[],
            # revision_index > 0 but no revision_kind — legacy / corrupt row.
            revision_index=1,
            revision_kind="",
            revision_reason="",
            trigger_event_id="",
        )
    )
    with caplog.at_level(logging.WARNING, logger="harmonograf_server.rpc.frontend"):
        resp = await _call(rpc_stack, sid)
    assert len(resp.revisions) == 2
    bad = resp.revisions[1]
    assert bad.revision_number == 1
    assert bad.revision_kind == ""
    assert bad.revision_reason == ""
    assert bad.revision_trigger_event_id == ""
    # WARNING fired once for the corrupt row.
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("empty revision_kind" in m for m in msgs), msgs


# ---------------------------------------------------------------------------
# RPC error paths
# ---------------------------------------------------------------------------


async def test_rpc_rejects_missing_session_id(rpc_stack):
    ch = grpc.aio.insecure_channel(f"127.0.0.1:{rpc_stack['port']}")
    try:
        stub = service_pb2_grpc.HarmonografStub(ch)
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await stub.GetSessionPlanHistory(
                frontend_pb2.GetSessionPlanHistoryRequest(session_id="")
            )
        assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    finally:
        await ch.close()


async def test_rpc_unknown_session_is_not_found(rpc_stack):
    ch = grpc.aio.insecure_channel(f"127.0.0.1:{rpc_stack['port']}")
    try:
        stub = service_pb2_grpc.HarmonografStub(ch)
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await stub.GetSessionPlanHistory(
                frontend_pb2.GetSessionPlanHistoryRequest(session_id="nope")
            )
        assert exc.value.code() == grpc.StatusCode.NOT_FOUND
    finally:
        await ch.close()


# ---------------------------------------------------------------------------
# End-to-end: ingested PlanSubmitted + PlanRevised via the real ingest path.
# Proves the RPC reflects what WatchSession would see on reconnect.
# ---------------------------------------------------------------------------


async def test_rpc_reflects_ingested_plan_revisions(rpc_stack):
    """Seed the store the way ingest._on_plan_submitted/revised would."""

    store = rpc_stack["store"]
    sid = "sess_ingested"
    await _seed_session(store, sid, created_at=0.0)
    # Simulate what ingest does: PlanSubmitted is a r0 plan; PlanRevised is
    # a subsequent plan with revision metadata copied from the envelope.
    await store.put_task_plan(
        TaskPlan(
            id="plan_r0",
            session_id=sid,
            created_at=10.0,
            planner_agent_id="planner",
            summary="initial plan",
            tasks=[_make_task("t1"), _make_task("t2")],
            edges=[],
            revision_index=0,
        )
    )
    await store.put_task_plan(
        TaskPlan(
            id="plan_r1",
            session_id=sid,
            created_at=20.0,
            planner_agent_id="planner",
            summary="after user steer",
            tasks=[_make_task("t1"), _make_task("t3")],
            edges=[],
            revision_reason="please try a different approach",
            revision_kind="user_steer",
            revision_severity="info",
            revision_index=1,
            trigger_event_id="ann_user_1",
        )
    )
    resp = await _call(rpc_stack, sid)
    assert len(resp.revisions) == 2
    # Generations in order; plan ids preserved.
    assert [r.plan.id for r in resp.revisions] == ["plan_r0", "plan_r1"]
    # r1's trigger is the source annotation id (user-control refine).
    assert resp.revisions[1].revision_trigger_event_id == "ann_user_1"
    assert resp.revisions[1].revision_kind == "USER_STEER"
    # Task evolution across revisions is visible — the downstream frontend
    # can diff r0 vs r1 to discover t2 was dropped / t3 was added.
    r0_task_ids = {t.id for t in resp.revisions[0].plan.tasks}
    r1_task_ids = {t.id for t in resp.revisions[1].plan.tasks}
    assert r0_task_ids == {"t1", "t2"}
    assert r1_task_ids == {"t1", "t3"}
