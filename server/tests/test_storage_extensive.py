"""Extensive storage tests — parametrized across memory + sqlite backends.

Covers session/agent/span/annotation/payload/task-plan CRUD and key
invariants: idempotent append, first-write-wins, cascading delete,
payload gc, time-range queries, task status updates, stats accounting.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import pytest_asyncio

from harmonograf_server.storage import (
    Agent,
    AgentStatus,
    Annotation,
    AnnotationKind,
    AnnotationTarget,
    Capability,
    Framework,
    LinkRelation,
    Session,
    SessionStatus,
    Span,
    SpanKind,
    SpanLink,
    SpanStatus,
    Task,
    TaskEdge,
    TaskPlan,
    TaskPlanRevision,
    TaskStatus,
    make_store,
)


# ---- fixtures -------------------------------------------------------------


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def store(request, tmp_path: Path):
    if request.param == "memory":
        s = make_store("memory")
    else:
        s = make_store(
            "sqlite",
            db_path=tmp_path / "harmonograf.db",
            payload_dir=tmp_path / "payloads",
        )
    await s.start()
    try:
        yield s
    finally:
        await s.close()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sess(sid: str = "sess_1", created_at: float = 1000.0, status=SessionStatus.LIVE) -> Session:
    return Session(
        id=sid, title=sid, created_at=created_at, status=status, metadata={"k": "v"}
    )


def _agent(session_id: str, agent_id: str = "a") -> Agent:
    return Agent(
        id=agent_id,
        session_id=session_id,
        name=agent_id,
        framework=Framework.ADK,
        framework_version="0.1",
        capabilities=[Capability.PAUSE_RESUME],
        connected_at=1000.0,
        last_heartbeat=1000.0,
        status=AgentStatus.CONNECTED,
    )


def _span(
    session_id: str,
    agent_id: str,
    span_id: str,
    start: float = 100.0,
    end: float | None = 110.0,
    status: SpanStatus = SpanStatus.COMPLETED,
    **kwargs,
) -> Span:
    return Span(
        id=span_id,
        session_id=session_id,
        agent_id=agent_id,
        kind=SpanKind.TOOL_CALL,
        name="tool",
        start_time=start,
        end_time=end,
        status=status,
        **kwargs,
    )


# ---- sessions -------------------------------------------------------------


async def test_get_missing_session_returns_none(store):
    assert await store.get_session("nope") is None


async def test_list_sessions_status_filter(store):
    await store.create_session(_sess("s_live"))
    await store.create_session(_sess("s_done", status=SessionStatus.COMPLETED))
    live = await store.list_sessions(status=SessionStatus.LIVE)
    assert {s.id for s in live} == {"s_live"}
    done = await store.list_sessions(status=SessionStatus.COMPLETED)
    assert {s.id for s in done} == {"s_done"}


async def test_list_sessions_limit_and_recency_order(store):
    await store.create_session(_sess("s_old", created_at=100.0))
    await store.create_session(_sess("s_mid", created_at=200.0))
    await store.create_session(_sess("s_new", created_at=300.0))
    listed = await store.list_sessions(limit=2)
    assert [s.id for s in listed] == ["s_new", "s_mid"]


async def test_update_session_missing_returns_none(store):
    out = await store.update_session("nope", title="nothing")
    assert out is None


async def test_update_session_partial_fields(store):
    await store.create_session(_sess("s_part"))
    out = await store.update_session("s_part", title="new title")
    assert out.title == "new title"
    assert out.status == SessionStatus.LIVE  # unchanged
    out2 = await store.update_session("s_part", status=SessionStatus.ABORTED)
    assert out2.status == SessionStatus.ABORTED


async def test_delete_missing_session_returns_false(store):
    assert await store.delete_session("ghost") is False


# ---- agents ---------------------------------------------------------------


async def test_register_agent_is_upsert(store):
    await store.create_session(_sess())
    a = _agent("sess_1")
    await store.register_agent(a)
    # Second registration with mutated name merges in.
    a2 = _agent("sess_1")
    a2.name = "updated-name"
    await store.register_agent(a2)
    fetched = await store.get_agent("sess_1", "a")
    assert fetched.name == "updated-name"
    # Still one row total.
    listed = await store.list_agents_for_session("sess_1")
    assert len(listed) == 1


async def test_get_agent_missing_returns_none(store):
    assert await store.get_agent("no_sess", "no_agent") is None


async def test_update_agent_status_missing_is_noop(store):
    # Silently does nothing on unknown agent.
    await store.update_agent_status("no_sess", "no_agent", AgentStatus.DISCONNECTED)


async def test_list_agents_respects_session_scope(store):
    await store.create_session(_sess("s_one"))
    await store.create_session(_sess("s_two"))
    await store.register_agent(_agent("s_one", "a"))
    await store.register_agent(_agent("s_two", "b"))
    assert {a.id for a in await store.list_agents_for_session("s_one")} == {"a"}
    assert {a.id for a in await store.list_agents_for_session("s_two")} == {"b"}


# ---- spans ----------------------------------------------------------------


async def test_append_span_first_write_wins_on_mutation(store):
    await store.create_session(_sess())
    await store.register_agent(_agent("sess_1"))
    sp = _span("sess_1", "a", "sp1")
    await store.append_span(sp)
    sp2 = _span("sess_1", "a", "sp1")
    sp2.name = "different"
    await store.append_span(sp2)
    fetched = await store.get_span("sp1")
    assert fetched.name == "tool"  # first-write wins


async def test_update_span_missing_returns_none(store):
    out = await store.update_span("ghost", status=SpanStatus.FAILED)
    assert out is None


async def test_end_span_missing_returns_none(store):
    out = await store.end_span("ghost", end_time=1.0, status=SpanStatus.COMPLETED)
    assert out is None


async def test_end_span_terminal_sets_end_and_status(store):
    await store.create_session(_sess())
    await store.register_agent(_agent("sess_1"))
    await store.append_span(_span("sess_1", "a", "sp1", end=None, status=SpanStatus.RUNNING))
    ended = await store.end_span("sp1", end_time=150.0, status=SpanStatus.FAILED, error={"type": "E"})
    assert ended.status == SpanStatus.FAILED
    assert ended.end_time == 150.0
    assert ended.error == {"type": "E"}


async def test_span_time_window_boundary(store):
    await store.create_session(_sess())
    await store.register_agent(_agent("sess_1"))
    await store.append_span(_span("sess_1", "a", "sp1", 100.0, 200.0))
    await store.append_span(_span("sess_1", "a", "sp2", 300.0, 400.0))
    # Overlapping window picks sp1.
    in_window = await store.get_spans("sess_1", time_start=150.0, time_end=250.0)
    assert {s.id for s in in_window} == {"sp1"}
    # Fully-enclosed window.
    big = await store.get_spans("sess_1", time_start=50.0, time_end=500.0)
    assert {s.id for s in big} == {"sp1", "sp2"}


async def test_span_query_limit_truncates(store):
    await store.create_session(_sess())
    await store.register_agent(_agent("sess_1"))
    for i in range(5):
        await store.append_span(_span("sess_1", "a", f"sp{i}", 100.0 + i, 101.0 + i))
    out = await store.get_spans("sess_1", limit=3)
    assert len(out) == 3


async def test_update_span_sets_payload_ref_and_refcounts(store):
    await store.create_session(_sess())
    await store.register_agent(_agent("sess_1"))
    await store.append_span(_span("sess_1", "a", "sp1"))
    data = b"payload-bytes"
    digest = _sha(data)
    await store.put_payload(digest, data, "text/plain", summary="p")
    updated = await store.update_span(
        "sp1",
        payload_digest=digest,
        payload_mime="text/plain",
        payload_size=len(data),
        payload_summary="p",
        payload_role="input",
    )
    assert updated.payload_digest == digest
    assert updated.payload_role == "input"


async def test_zero_duration_span_indexed(store):
    await store.create_session(_sess())
    await store.register_agent(_agent("sess_1"))
    sp = _span("sess_1", "a", "sp0", start=500.0, end=500.0)
    await store.append_span(sp)
    out = await store.get_spans("sess_1", time_start=499.0, time_end=501.0)
    assert any(s.id == "sp0" for s in out)


# ---- payloads -------------------------------------------------------------


async def test_get_missing_payload_returns_none(store):
    assert await store.get_payload("deadbeef") is None
    assert await store.has_payload("deadbeef") is False


async def test_payload_dedup_keeps_single_row(store):
    data = b"abcdef" * 50
    d = _sha(data)
    await store.put_payload(d, data, "text/plain")
    await store.put_payload(d, data, "text/plain")
    stats = await store.stats()
    assert stats.payload_count == 1


async def test_gc_payloads_removes_orphans(store):
    # Orphan payload (no span references it).
    data = b"orphan-bytes"
    d = _sha(data)
    await store.put_payload(d, data, "text/plain")
    removed = await store.gc_payloads()
    assert removed >= 1
    assert await store.has_payload(d) is False


async def test_gc_payloads_keeps_referenced(store):
    await store.create_session(_sess())
    await store.register_agent(_agent("sess_1"))
    data = b"useful-bytes"
    d = _sha(data)
    await store.put_payload(d, data, "application/json", summary="s")
    sp = _span("sess_1", "a", "sp1")
    sp.payload_digest = d
    sp.payload_size = len(data)
    sp.payload_mime = "application/json"
    await store.append_span(sp)
    removed = await store.gc_payloads()
    assert removed == 0
    assert await store.has_payload(d) is True


# ---- annotations ----------------------------------------------------------


async def test_annotation_target_agent_time(store):
    await store.create_session(_sess())
    ann = Annotation(
        id="ann-t",
        session_id="sess_1",
        target=AnnotationTarget(agent_id="a", time_start=10.0, time_end=20.0),
        author="me",
        created_at=5.0,
        kind=AnnotationKind.STEERING,
        body="slow down",
    )
    await store.put_annotation(ann)
    listed = await store.list_annotations(session_id="sess_1")
    assert len(listed) == 1
    assert listed[0].target.agent_id == "a"
    assert listed[0].kind == AnnotationKind.STEERING


async def test_annotation_filter_by_span(store):
    await store.create_session(_sess())
    await store.register_agent(_agent("sess_1"))
    await store.append_span(_span("sess_1", "a", "sp1"))
    await store.append_span(_span("sess_1", "a", "sp2"))
    for sid in ("sp1", "sp2"):
        ann = Annotation(
            id=f"ann-{sid}",
            session_id="sess_1",
            target=AnnotationTarget(span_id=sid),
            author="me",
            created_at=1.0,
            kind=AnnotationKind.COMMENT,
            body="c",
        )
        await store.put_annotation(ann)
    by_span = await store.list_annotations(span_id="sp1")
    assert {a.id for a in by_span} == {"ann-sp1"}


# ---- task plans -----------------------------------------------------------


def _plan(session_id: str = "sess_1") -> TaskPlan:
    return TaskPlan(
        id="plan-1",
        session_id=session_id,
        created_at=10.0,
        planner_agent_id="planner",
        summary="multi-step",
        tasks=[
            Task(id="t1", title="draft", assignee_agent_id="writer"),
            Task(id="t2", title="review", assignee_agent_id="reviewer"),
        ],
        edges=[TaskEdge(from_task_id="t1", to_task_id="t2")],
    )


async def test_task_plan_round_trip(store):
    await store.create_session(_sess())
    stored = await store.put_task_plan(_plan())
    assert stored.id == "plan-1"
    fetched = await store.get_task_plan("plan-1")
    assert fetched is not None
    assert len(fetched.tasks) == 2
    assert fetched.edges[0].from_task_id == "t1"


# harmonograf#107 — edges must survive the full wire round-trip: storage
# TaskPlan → goldfive.v1.Plan (WatchSession replay on the initial burst) →
# back to storage via the ingest goldfive_pb_plan_to_storage convert. A
# regression in either direction silently drops dependency arrows in the
# Gantt pre-strip.
async def test_task_plan_edges_survive_wire_roundtrip(store):
    from harmonograf_server.convert import (
        goldfive_pb_plan_to_storage,
        storage_plan_to_goldfive_pb,
    )

    await store.create_session(_sess())
    plan = TaskPlan(
        id="plan-edges",
        session_id="sess_1",
        created_at=20.0,
        planner_agent_id="planner",
        summary="7-stage DAG",
        tasks=[
            Task(id=f"t{i}", title=f"step {i}", assignee_agent_id="worker")
            for i in range(1, 8)
        ],
        edges=[
            TaskEdge(from_task_id="t1", to_task_id="t2"),
            TaskEdge(from_task_id="t2", to_task_id="t3"),
            TaskEdge(from_task_id="t2", to_task_id="t4"),
            TaskEdge(from_task_id="t3", to_task_id="t5"),
            TaskEdge(from_task_id="t4", to_task_id="t5"),
            TaskEdge(from_task_id="t5", to_task_id="t6"),
            TaskEdge(from_task_id="t6", to_task_id="t7"),
        ],
    )
    await store.put_task_plan(plan)
    persisted = await store.get_task_plan("plan-edges")
    assert persisted is not None
    assert len(persisted.edges) == 7

    # Outbound — WatchSession initial-burst synthesises goldfive.v1.Event
    # frames from persisted plans. Every edge must ride.
    pb = storage_plan_to_goldfive_pb(persisted)
    assert len(pb.edges) == 7
    assert pb.edges[0].from_task_id == "t1"
    assert pb.edges[0].to_task_id == "t2"

    # Inbound — ingest pipeline converts the same proto back to storage when
    # a goldfive planner emits a fresh plan. Edges must also survive this
    # direction so live-tail plans and replayed plans both render.
    re_stored = goldfive_pb_plan_to_storage(
        pb, session_id="sess_1", created_at=20.0, planner_agent_id="planner"
    )
    assert len(re_stored.edges) == 7
    assert {(e.from_task_id, e.to_task_id) for e in re_stored.edges} == {
        ("t1", "t2"),
        ("t2", "t3"),
        ("t2", "t4"),
        ("t3", "t5"),
        ("t4", "t5"),
        ("t5", "t6"),
        ("t6", "t7"),
    }


async def test_get_task_plan_missing_returns_none(store):
    assert await store.get_task_plan("no-plan") is None


async def test_list_task_plans_for_session_scoped(store):
    await store.create_session(_sess("s_a"))
    await store.create_session(_sess("s_b"))
    p1 = _plan("s_a")
    p2 = _plan("s_b")
    p2.id = "plan-2"
    await store.put_task_plan(p1)
    await store.put_task_plan(p2)
    a_plans = await store.list_task_plans_for_session("s_a")
    assert {p.id for p in a_plans} == {"plan-1"}


async def test_update_task_status_binds_span(store):
    await store.create_session(_sess())
    await store.put_task_plan(_plan())
    updated = await store.update_task_status(
        "plan-1", "t1", TaskStatus.RUNNING, bound_span_id="sp-1"
    )
    assert updated is not None
    assert updated.status == TaskStatus.RUNNING
    assert updated.bound_span_id == "sp-1"
    # Reread the plan: both task and binding persisted.
    fetched = await store.get_task_plan("plan-1")
    t1 = next(t for t in fetched.tasks if t.id == "t1")
    assert t1.status == TaskStatus.RUNNING
    assert t1.bound_span_id == "sp-1"


async def test_update_task_status_unknown_plan(store):
    out = await store.update_task_status("ghost", "t1", TaskStatus.RUNNING)
    assert out is None


async def test_update_task_status_unknown_task(store):
    await store.create_session(_sess())
    await store.put_task_plan(_plan())
    out = await store.update_task_status("plan-1", "ghost", TaskStatus.RUNNING)
    assert out is None


# ---- stats + cascade ------------------------------------------------------


# ---- task plan revisions (Option B sibling table) -------------------------


def _rev(
    plan_id: str,
    revision_index: int,
    *,
    session_id: str = "sess_1",
    emitted_at: float = 100.0,
    snapshot_json: str = '{"v":1}',
    revision_kind: str = "",
    revision_reason: str = "",
    revision_severity: str = "",
    trigger_event_id: str = "",
) -> TaskPlanRevision:
    return TaskPlanRevision(
        plan_id=plan_id,
        revision_index=revision_index,
        session_id=session_id,
        emitted_at=emitted_at,
        snapshot_json=snapshot_json,
        revision_kind=revision_kind,
        revision_reason=revision_reason,
        revision_severity=revision_severity,
        trigger_event_id=trigger_event_id,
    )


async def test_task_plan_revision_round_trip(store):
    await store.create_session(_sess())
    rev = _rev("plan-A", 0, snapshot_json='{"v":1,"x":1}')
    stored = await store.put_task_plan_revision(rev)
    assert stored.plan_id == "plan-A"
    assert stored.revision_index == 0
    fetched = await store.get_task_plan_revision("plan-A", 0)
    assert fetched is not None
    assert fetched.snapshot_json == '{"v":1,"x":1}'


async def test_task_plan_revision_upsert_is_idempotent(store):
    """Re-emitting the same (plan_id, revision_index) replaces the row."""

    await store.create_session(_sess())
    await store.put_task_plan_revision(
        _rev("plan-A", 0, snapshot_json='{"first":true}', revision_reason="initial")
    )
    await store.put_task_plan_revision(
        _rev(
            "plan-A",
            0,
            snapshot_json='{"second":true}',
            revision_reason="enriched",
            trigger_event_id="trig-X",
        )
    )
    rows = await store.list_task_plan_revisions_for_session("sess_1")
    assert len(rows) == 1
    assert rows[0].snapshot_json == '{"second":true}'
    assert rows[0].revision_reason == "enriched"
    assert rows[0].trigger_event_id == "trig-X"


async def test_list_task_plan_revisions_session_scoped_and_ordered(store):
    await store.create_session(_sess("s_a"))
    await store.create_session(_sess("s_b"))
    # Insert out of emitted_at order to prove the list orders chronologically.
    await store.put_task_plan_revision(
        _rev("plan-A", 2, session_id="s_a", emitted_at=300.0)
    )
    await store.put_task_plan_revision(
        _rev("plan-A", 0, session_id="s_a", emitted_at=100.0)
    )
    await store.put_task_plan_revision(
        _rev("plan-A", 1, session_id="s_a", emitted_at=200.0)
    )
    await store.put_task_plan_revision(
        _rev("plan-B", 0, session_id="s_b", emitted_at=150.0)
    )
    a_rows = await store.list_task_plan_revisions_for_session("s_a")
    assert [r.revision_index for r in a_rows] == [0, 1, 2]
    b_rows = await store.list_task_plan_revisions_for_session("s_b")
    assert [r.plan_id for r in b_rows] == ["plan-B"]


async def test_task_plan_revision_distinct_revisions_share_plan_id(store):
    """The exact bug Option B fixes: same plan_id, different revision_index
    must NOT collide. ``put_task_plan`` upserts on plan_id alone and
    silently drops earlier revisions; this sibling table keeps them all."""

    await store.create_session(_sess())
    await store.put_task_plan_revision(
        _rev("plan-A", 0, snapshot_json='{"r":0}', emitted_at=100.0)
    )
    await store.put_task_plan_revision(
        _rev("plan-A", 1, snapshot_json='{"r":1}', emitted_at=110.0)
    )
    await store.put_task_plan_revision(
        _rev("plan-A", 2, snapshot_json='{"r":2}', emitted_at=120.0)
    )
    rows = await store.list_task_plan_revisions_for_session("sess_1")
    assert [r.revision_index for r in rows] == [0, 1, 2]
    assert [r.snapshot_json for r in rows] == ['{"r":0}', '{"r":1}', '{"r":2}']


async def test_task_plan_revision_get_missing_returns_none(store):
    assert await store.get_task_plan_revision("nope", 0) is None


async def test_delete_session_clears_task_plan_revisions(store):
    await store.create_session(_sess())
    await store.put_task_plan_revision(_rev("plan-A", 0))
    await store.put_task_plan_revision(_rev("plan-A", 1, emitted_at=110.0))
    assert await store.delete_session("sess_1") is True
    assert await store.list_task_plan_revisions_for_session("sess_1") == []


async def test_delete_session_cascade_full(store):
    await store.create_session(_sess())
    await store.register_agent(_agent("sess_1"))
    await store.append_span(_span("sess_1", "a", "sp1"))
    ann = Annotation(
        id="ann",
        session_id="sess_1",
        target=AnnotationTarget(span_id="sp1"),
        author="me",
        created_at=1.0,
        kind=AnnotationKind.COMMENT,
        body="x",
    )
    await store.put_annotation(ann)
    await store.put_task_plan(_plan())

    assert await store.delete_session("sess_1") is True
    assert await store.get_session("sess_1") is None
    assert await store.get_span("sp1") is None
    assert await store.list_annotations(session_id="sess_1") == []
    assert await store.get_task_plan("plan-1") is None


async def test_stats_zero_on_empty_store(store):
    stats = await store.stats()
    assert stats.session_count == 0
    assert stats.agent_count == 0
    assert stats.span_count == 0
    assert stats.payload_count == 0


async def test_ping_returns_true(store):
    assert await store.ping() is True
