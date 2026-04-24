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
from harmonograf_server.convert import plan_to_snapshot_json
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
    TaskPlanRevision,
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


async def _seed_plan(store, plan: TaskPlan) -> None:
    """Seed both ``task_plans`` (latest snapshot) and ``task_plan_revisions``
    (per-revision history) the way ``ingest._upsert_plan`` /
    ``_on_plan_revised`` would. ``GetSessionPlanHistory`` reads from the
    revisions table now (post-Option-B fix); tests must populate it
    explicitly because the test fixtures don't go through ingest."""

    await store.put_task_plan(plan)
    await store.put_task_plan_revision(
        TaskPlanRevision(
            plan_id=plan.id,
            revision_index=int(plan.revision_index or 0),
            session_id=plan.session_id,
            revision_reason=plan.revision_reason or "",
            revision_kind=plan.revision_kind or "",
            revision_severity=plan.revision_severity or "",
            trigger_event_id=plan.trigger_event_id or "",
            emitted_at=float(plan.created_at),
            snapshot_json=plan_to_snapshot_json(plan),
        )
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
    await _seed_plan(store, 
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
    await _seed_plan(store, 
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
    await _seed_plan(store, 
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
    await _seed_plan(store, 
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
    await _seed_plan(store, 
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
    await _seed_plan(store, 
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
    await _seed_plan(store, 
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
    await _seed_plan(store, 
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


async def test_lazy_migration_backfills_from_goldfive_events(rpc_stack):
    """Pre-Option-B sessions have ``plan_submitted`` / ``plan_revised``
    rows in ``goldfive_events`` but no ``task_plan_revisions``. The
    handler must replay them into the new table on first query.

    Drives the real ingest path so ``goldfive_events`` is populated with
    real wire bytes; then wipes ``task_plan_revisions`` and re-queries
    to confirm the lazy backfill rebuilds the table from the persisted
    envelopes.
    """

    from goldfive.v1 import events_pb2 as ge
    from goldfive.v1 import types_pb2 as gt
    from harmonograf_server.ingest import StreamContext
    from harmonograf_server.pb import telemetry_pb2

    store = rpc_stack["store"]
    sid = "sess_lazy"
    await _seed_session(store, sid, created_at=0.0)
    pipe = rpc_stack["servicer"]._ingest

    def _wrap(evt: ge.Event) -> telemetry_pb2.TelemetryUp:
        return telemetry_pb2.TelemetryUp(goldfive_event=evt)

    def _stream_ctx() -> StreamContext:
        return StreamContext(
            stream_id="s1",
            agent_id="a1",
            session_id=sid,
            connected_at=0.0,
            last_heartbeat=0.0,
            seen_routes={(sid, "a1")},
        )

    # Drive PlanSubmitted + 2 PlanRevised through ingest. This populates
    # both ``task_plan_revisions`` (the new path) and ``goldfive_events``
    # (the audit log we'll lazily migrate from below).
    base = ge.Event(event_id="e0", run_id="run-1", sequence=0)
    plan = gt.Plan(id="plan-L")
    plan.tasks.add(id="t1")
    base.plan_submitted.plan.CopyFrom(plan)
    await pipe.handle_message(_stream_ctx(), _wrap(base))

    for i in (1, 2):
        evt = ge.Event(event_id=f"e{i}", run_id="run-1", sequence=i)
        rp = gt.Plan(id="plan-L", revision_index=i)
        for j in range(1, 2 + i):
            rp.tasks.add(id=f"t{j}")
        evt.plan_revised.plan.CopyFrom(rp)
        evt.plan_revised.drift_kind = gt.DRIFT_KIND_TOOL_ERROR
        evt.plan_revised.severity = gt.DRIFT_SEVERITY_WARNING
        evt.plan_revised.reason = f"reason-r{i}"
        evt.plan_revised.revision_index = i
        evt.plan_revised.trigger_event_id = f"trig-{i}"
        await pipe.handle_message(_stream_ctx(), _wrap(evt))

    # Sanity: live-ingest path already populated the sibling table.
    pre = await store.list_task_plan_revisions_for_session(sid)
    assert [r.revision_index for r in pre] == [0, 1, 2]

    # Simulate a pre-Option-B DB by wiping the sibling table while
    # leaving ``goldfive_events`` (and ``task_plans``) intact.
    store._task_plan_revisions.clear()  # type: ignore[attr-defined]
    assert await store.list_task_plan_revisions_for_session(sid) == []

    # The RPC should detect (events > revisions), replay the events,
    # and return all 3 revisions on the first query.
    resp = await _call(rpc_stack, sid)
    assert [r.revision_number for r in resp.revisions] == [0, 1, 2]
    assert [r.plan.id for r in resp.revisions] == ["plan-L", "plan-L", "plan-L"]
    assert resp.revisions[1].revision_kind == "TOOL_ERROR"
    assert resp.revisions[1].revision_trigger_event_id == "trig-1"
    assert resp.revisions[2].revision_trigger_event_id == "trig-2"

    # Second query is now a plain table read — backfill should not run again.
    post = await store.list_task_plan_revisions_for_session(sid)
    assert [r.revision_index for r in post] == [0, 1, 2]


async def test_rpc_reflects_ingested_plan_revisions(rpc_stack):
    """Seed the store the way ingest._on_plan_submitted/revised would."""

    store = rpc_stack["store"]
    sid = "sess_ingested"
    await _seed_session(store, sid, created_at=0.0)
    # Simulate what ingest does: PlanSubmitted is a r0 plan; PlanRevised is
    # a subsequent plan with revision metadata copied from the envelope.
    await _seed_plan(store, 
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
    await _seed_plan(store, 
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
