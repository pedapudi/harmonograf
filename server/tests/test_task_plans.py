"""Tests for the TaskPlan feature.

Covers:
  - Round-trip a TaskPlan through storage (sqlite + memory via parametrize).
  - Ingest receives a TaskPlan -> bus publishes a task_plan delta, and
    _delta_to_session_update produces a SessionUpdate with the plan.
  - Ingest receives a SpanStart carrying `hgraf.task_id` -> the matching
    task transitions to RUNNING and bound_span_id is set. SpanEnd with
    COMPLETED transitions it to COMPLETED.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from google.protobuf.timestamp_pb2 import Timestamp

from harmonograf_server.bus import (
    DELTA_TASK_PLAN,
    DELTA_TASK_STATUS,
    SessionBus,
)
from harmonograf_server.ingest import IngestPipeline, StreamContext
from harmonograf_server.pb import telemetry_pb2, types_pb2
from harmonograf_server.rpc.frontend import _delta_to_session_update
from harmonograf_server.storage import (
    Task,
    TaskEdge,
    TaskPlan,
    TaskStatus,
    make_store,
)


# ---- fixtures -------------------------------------------------------------


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def store(request, tmp_path: Path):
    if request.param == "memory":
        s = make_store("memory")
    else:
        s = make_store("sqlite", db_path=tmp_path / "hg.db")
    await s.start()
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
async def pipeline(store):
    bus = SessionBus()
    ingest = IngestPipeline(store, bus)
    return ingest, bus


def _ts(sec: float) -> Timestamp:
    t = Timestamp()
    t.seconds = int(sec)
    t.nanos = int((sec - int(sec)) * 1e9)
    return t


def _stream_ctx(session_id: str = "sess_tp", agent_id: str = "transport") -> StreamContext:
    return StreamContext(
        stream_id="str_test",
        agent_id=agent_id,
        session_id=session_id,
        connected_at=1000.0,
        last_heartbeat=1000.0,
        seen_routes={(session_id, agent_id)},
    )


# ---- storage round-trip ---------------------------------------------------


async def test_task_plan_round_trip(store):
    # Need a session so foreign references are coherent (sqlite has no FK
    # from task_plans -> sessions, but this mirrors real usage).
    from harmonograf_server.storage import Session, SessionStatus

    await store.create_session(
        Session(id="sess_tp", title="tp", created_at=1.0, status=SessionStatus.LIVE)
    )

    plan = TaskPlan(
        id="plan_1",
        session_id="sess_tp",
        created_at=1234.5,
        invocation_span_id="inv_span_1",
        planner_agent_id="planner",
        summary="do work",
        tasks=[
            Task(
                id="t1",
                title="step one",
                description="first",
                assignee_agent_id="worker_a",
                status=TaskStatus.PENDING,
                predicted_start_ms=0,
                predicted_duration_ms=1000,
            ),
            Task(
                id="t2",
                title="step two",
                assignee_agent_id="worker_b",
                status=TaskStatus.PENDING,
            ),
        ],
        edges=[TaskEdge(from_task_id="t1", to_task_id="t2")],
    )

    stored = await store.put_task_plan(plan)
    assert stored.id == "plan_1"
    assert len(stored.tasks) == 2
    assert stored.edges == [TaskEdge(from_task_id="t1", to_task_id="t2")]

    got = await store.get_task_plan("plan_1")
    assert got is not None
    assert got.summary == "do work"
    assert got.tasks[0].assignee_agent_id == "worker_a"

    plans = await store.list_task_plans_for_session("sess_tp")
    assert len(plans) == 1

    updated = await store.update_task_status(
        "plan_1", "t1", TaskStatus.RUNNING, bound_span_id="span_a"
    )
    assert updated is not None
    assert updated.status == TaskStatus.RUNNING
    assert updated.bound_span_id == "span_a"

    got = await store.get_task_plan("plan_1")
    assert got.tasks[0].status == TaskStatus.RUNNING
    assert got.tasks[0].bound_span_id == "span_a"

    # Cascade on delete_session.
    assert await store.delete_session("sess_tp") is True
    assert await store.get_task_plan("plan_1") is None


async def test_task_plan_revision_fields_round_trip(store):
    from harmonograf_server.storage import Session, SessionStatus

    await store.create_session(
        Session(id="sess_rev", title="rev", created_at=1.0, status=SessionStatus.LIVE)
    )
    plan = TaskPlan(
        id="plan_rev",
        session_id="sess_rev",
        created_at=1.0,
        invocation_span_id="inv",
        planner_agent_id="planner",
        summary="drift-tagged",
        tasks=[Task(id="t1", title="one", assignee_agent_id="worker_a")],
        edges=[],
        revision_reason="tool_error: search raised TimeoutError",
        revision_kind="tool_error",
        revision_severity="warning",
        revision_index=3,
    )
    await store.put_task_plan(plan)
    got = await store.get_task_plan("plan_rev")
    assert got is not None
    assert got.revision_reason == "tool_error: search raised TimeoutError"
    assert got.revision_kind == "tool_error"
    assert got.revision_severity == "warning"
    assert got.revision_index == 3


async def test_sqlite_task_plan_migration_from_legacy_schema(tmp_path: Path):
    """Simulate an older DB without the new revision_* columns; opening
    the store should ALTER TABLE to add them without dropping data."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE task_plans (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            invocation_span_id TEXT,
            planner_agent_id TEXT,
            created_at REAL NOT NULL,
            summary TEXT,
            edges TEXT
        );
        INSERT INTO task_plans VALUES (
            'plan_legacy', 'sess_legacy', 'inv', 'planner', 1.0, 'legacy', '[]'
        );
        CREATE TABLE tasks (
            plan_id TEXT NOT NULL,
            id TEXT NOT NULL,
            title TEXT,
            description TEXT,
            assignee_agent_id TEXT,
            status TEXT NOT NULL,
            predicted_start_ms INTEGER DEFAULT 0,
            predicted_duration_ms INTEGER DEFAULT 0,
            bound_span_id TEXT,
            PRIMARY KEY (plan_id, id)
        );
        """
    )
    con.commit()
    con.close()

    store = make_store("sqlite", db_path=db_path)
    await store.start()
    try:
        got = await store.get_task_plan("plan_legacy")
        assert got is not None
        assert got.revision_reason == ""
        assert got.revision_kind == ""
        assert got.revision_severity == ""
        assert got.revision_index == 0

        got.revision_reason = "tool_error: boom"
        got.revision_kind = "tool_error"
        got.revision_severity = "warning"
        got.revision_index = 1
        await store.put_task_plan(got)

        got2 = await store.get_task_plan("plan_legacy")
        assert got2.revision_kind == "tool_error"
        assert got2.revision_severity == "warning"
        assert got2.revision_index == 1
    finally:
        await store.close()


async def test_update_task_status_missing(store):
    assert (
        await store.update_task_status(
            "nope", "nope", TaskStatus.COMPLETED
        )
    ) is None


# ---- ingest: task_plan message --------------------------------------------


def _make_pb_plan(
    plan_id: str = "plan_1",
    session_id: str = "sess_tp",
    planner: str = "planner",
) -> types_pb2.TaskPlan:
    plan = types_pb2.TaskPlan(
        id=plan_id,
        session_id=session_id,
        invocation_span_id="inv_1",
        planner_agent_id=planner,
        summary="do work",
    )
    plan.created_at.CopyFrom(_ts(1234.5))
    plan.tasks.add(
        id="t1",
        title="one",
        assignee_agent_id="worker_a",
        status=types_pb2.TASK_STATUS_PENDING,
    )
    plan.tasks.add(
        id="t2",
        title="two",
        assignee_agent_id="worker_b",
        status=types_pb2.TASK_STATUS_PENDING,
    )
    plan.edges.add(from_task_id="t1", to_task_id="t2")
    return plan


async def test_ingest_task_plan_publishes_delta_and_converts(store, pipeline):
    ingest, bus = pipeline
    from harmonograf_server.storage import Session, SessionStatus

    await store.create_session(
        Session(id="sess_tp", title="tp", created_at=1.0, status=SessionStatus.LIVE)
    )

    sub = await bus.subscribe("sess_tp")
    ctx = _stream_ctx()

    msg = telemetry_pb2.TelemetryUp(task_plan=_make_pb_plan())
    await ingest.handle_message(ctx, msg)

    # Stored
    plans = await store.list_task_plans_for_session("sess_tp")
    assert len(plans) == 1
    assert plans[0].planner_agent_id == "planner"
    assert len(plans[0].tasks) == 2

    # Delta published — auto-registered agent upserts may precede the plan,
    # so drain until we see the task_plan delta.
    task_plan_delta = None
    while not sub.queue.empty():
        d = sub.queue.get_nowait()
        if d.kind == DELTA_TASK_PLAN:
            task_plan_delta = d
            break
    assert task_plan_delta is not None, "expected a task_plan delta on the bus"
    delta = task_plan_delta

    update = _delta_to_session_update(delta)
    assert update is not None
    assert update.WhichOneof("kind") == "task_plan"
    assert update.task_plan.id == "plan_1"
    assert len(update.task_plan.tasks) == 2

    # Agents for planner + assignees were auto-registered.
    agent_ids = {a.id for a in await store.list_agents_for_session("sess_tp")}
    assert {"planner", "worker_a", "worker_b"}.issubset(agent_ids)


# ---- ingest: span binding via hgraf.task_id -------------------------------


async def test_span_start_binds_task_to_running(store, pipeline):
    ingest, bus = pipeline
    from harmonograf_server.storage import Session, SessionStatus

    await store.create_session(
        Session(id="sess_tp", title="tp", created_at=1.0, status=SessionStatus.LIVE)
    )
    ctx = _stream_ctx()

    # First, load a plan via ingest (populates the in-memory task index).
    await ingest.handle_message(
        ctx, telemetry_pb2.TelemetryUp(task_plan=_make_pb_plan())
    )

    sub = await bus.subscribe("sess_tp")

    # Now a SpanStart carrying hgraf.task_id=t1.
    span = types_pb2.Span(
        id="span_x",
        session_id="sess_tp",
        agent_id="worker_a",
        kind=types_pb2.SPAN_KIND_TOOL_CALL,
        status=types_pb2.SPAN_STATUS_RUNNING,
        name="run_t1",
    )
    span.start_time.CopyFrom(_ts(100.0))
    span.attributes["hgraf.task_id"].string_value = "t1"
    await ingest.handle_message(
        ctx, telemetry_pb2.TelemetryUp(span_start=telemetry_pb2.SpanStart(span=span))
    )

    # Drain deltas; expect at least a span_start + a task_status delta.
    kinds_seen = []
    while not sub.queue.empty():
        d = sub.queue.get_nowait()
        kinds_seen.append(d.kind)
    assert DELTA_TASK_STATUS in kinds_seen

    # Task is RUNNING with bound span.
    plan = await store.get_task_plan("plan_1")
    t1 = next(t for t in plan.tasks if t.id == "t1")
    assert t1.status == TaskStatus.RUNNING
    assert t1.bound_span_id == "span_x"

    # Now end the span successfully — task MUST NOT flip to COMPLETED.
    # Task completion is a semantic event driven by the walker via
    # explicit task_status_update messages, not by span_end scanning.
    # See iter13 task #6.
    se = telemetry_pb2.SpanEnd(span_id="span_x", status=types_pb2.SPAN_STATUS_COMPLETED)
    se.end_time.CopyFrom(_ts(101.0))
    await ingest.handle_message(ctx, telemetry_pb2.TelemetryUp(span_end=se))

    plan = await store.get_task_plan("plan_1")
    t1 = next(t for t in plan.tasks if t.id == "t1")
    assert t1.status == TaskStatus.RUNNING

    # Completion must come from an explicit task_status_update message.
    uts = types_pb2.UpdatedTaskStatus(
        plan_id="plan_1", task_id="t1", status=types_pb2.TASK_STATUS_COMPLETED
    )
    await ingest.handle_message(
        ctx, telemetry_pb2.TelemetryUp(task_status_update=uts)
    )
    plan = await store.get_task_plan("plan_1")
    t1 = next(t for t in plan.tasks if t.id == "t1")
    assert t1.status == TaskStatus.COMPLETED


async def test_invocation_span_does_not_bind_task(store, pipeline):
    """Wrapper spans (INVOCATION, TRANSFER) must NOT bind planned
    tasks, even when stamped with ``hgraf.task_id``. Their lifecycles
    don't correspond to task execution, so allowing them to flip task
    status would let the outermost invocation end prematurely mark the
    task COMPLETED before any leaf work had run — the bug that
    motivated this guard.
    """
    ingest, _bus = pipeline
    from harmonograf_server.storage import Session, SessionStatus

    await store.create_session(
        Session(id="sess_tp", title="tp", created_at=1.0, status=SessionStatus.LIVE)
    )
    ctx = _stream_ctx()
    await ingest.handle_message(
        ctx, telemetry_pb2.TelemetryUp(task_plan=_make_pb_plan())
    )

    # Stamp an INVOCATION span with hgraf.task_id=t1.
    inv_span = types_pb2.Span(
        id="span_inv",
        session_id="sess_tp",
        agent_id="worker_a",
        kind=types_pb2.SPAN_KIND_INVOCATION,
        status=types_pb2.SPAN_STATUS_RUNNING,
        name="invocation",
    )
    inv_span.start_time.CopyFrom(_ts(100.0))
    inv_span.attributes["hgraf.task_id"].string_value = "t1"
    await ingest.handle_message(
        ctx,
        telemetry_pb2.TelemetryUp(
            span_start=telemetry_pb2.SpanStart(span=inv_span)
        ),
    )

    plan = await store.get_task_plan("plan_1")
    t1 = next(t for t in plan.tasks if t.id == "t1")
    assert t1.status == TaskStatus.PENDING, (
        "INVOCATION span must not flip task to RUNNING"
    )
    assert not t1.bound_span_id

    # And ending it with COMPLETED must not flip the task either.
    se = telemetry_pb2.SpanEnd(
        span_id="span_inv", status=types_pb2.SPAN_STATUS_COMPLETED
    )
    se.end_time.CopyFrom(_ts(101.0))
    await ingest.handle_message(ctx, telemetry_pb2.TelemetryUp(span_end=se))

    plan = await store.get_task_plan("plan_1")
    t1 = next(t for t in plan.tasks if t.id == "t1")
    assert t1.status == TaskStatus.PENDING, (
        "INVOCATION span end must not flip task to COMPLETED"
    )

    # Same guard for TRANSFER spans.
    xfer_span = types_pb2.Span(
        id="span_xfer",
        session_id="sess_tp",
        agent_id="worker_a",
        kind=types_pb2.SPAN_KIND_TRANSFER,
        status=types_pb2.SPAN_STATUS_RUNNING,
        name="transfer",
    )
    xfer_span.start_time.CopyFrom(_ts(102.0))
    xfer_span.attributes["hgraf.task_id"].string_value = "t1"
    await ingest.handle_message(
        ctx,
        telemetry_pb2.TelemetryUp(
            span_start=telemetry_pb2.SpanStart(span=xfer_span)
        ),
    )
    plan = await store.get_task_plan("plan_1")
    t1 = next(t for t in plan.tasks if t.id == "t1")
    assert t1.status == TaskStatus.PENDING
    assert not t1.bound_span_id
