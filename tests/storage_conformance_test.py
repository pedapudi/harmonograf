"""Cross-backend conformance suite for the ``Store`` ABC.

Every backend in ``harmonograf_server.storage`` must round-trip the same
operations with the same observable semantics. This module is the
canonical contract: if a new backend passes every test here, the rest of
the server (ingest, retention, frontend RPCs) will work against it
unchanged.

Adding a third backend is intentionally a one-line change — append a new
entry to ``BACKENDS`` describing how to construct a started Store
instance for the test. Everything else flows from the parametrized
``store`` fixture.

See ``docs/dev-guide/storage-backends.md`` for the full prose contract.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import pytest
import pytest_asyncio

from harmonograf_server.storage import (
    Agent,
    AgentStatus,
    Annotation,
    AnnotationKind,
    AnnotationTarget,
    Capability,
    ContextWindowSample,
    Framework,
    LinkRelation,
    Session,
    SessionStatus,
    Span,
    SpanKind,
    SpanLink,
    SpanStatus,
    Store,
    Task,
    TaskEdge,
    TaskPlan,
    TaskStatus,
    make_store,
)


# --- backend registry ------------------------------------------------------
#
# To add a third backend, append one entry. ``build`` receives a pytest
# ``tmp_path`` so backends that need an on-disk location can scope it to
# the test. The fixture does ``await store.start()`` / ``await
# store.close()`` for you.

@dataclass
class BackendSpec:
    name: str
    build: Callable[[Path], Store]


BACKENDS: list[BackendSpec] = [
    BackendSpec(name="memory", build=lambda _tmp: make_store("memory")),
    BackendSpec(
        name="sqlite",
        build=lambda tmp: make_store(
            "sqlite",
            db_path=str(tmp / "harmonograf.db"),
            payload_dir=str(tmp / "payloads"),
        ),
    ),
]


@pytest_asyncio.fixture(params=BACKENDS, ids=lambda b: b.name)
async def store(request, tmp_path: Path):
    spec: BackendSpec = request.param
    s = spec.build(tmp_path)
    await s.start()
    try:
        yield s
    finally:
        await s.close()


# --- fixture builders ------------------------------------------------------


SESSION_ID = "sess_2026-04-14_0001"
AGENT_ID = "researcher"
PEER_AGENT_ID = "writer"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _session(sid: str = SESSION_ID, *, status: SessionStatus = SessionStatus.LIVE) -> Session:
    return Session(
        id=sid,
        title="Conformance Run",
        created_at=1_700_000_000.0,
        status=status,
        metadata={"suite": "conformance"},
    )


def _agent(session_id: str = SESSION_ID, agent_id: str = AGENT_ID) -> Agent:
    return Agent(
        id=agent_id,
        session_id=session_id,
        name=agent_id,
        framework=Framework.ADK,
        framework_version="1.0",
        capabilities=[Capability.PAUSE_RESUME, Capability.STEERING],
        metadata={"region": "local"},
        connected_at=1_700_000_001.0,
        last_heartbeat=1_700_000_001.0,
        status=AgentStatus.CONNECTED,
    )


def _span(
    span_id: str,
    *,
    session_id: str = SESSION_ID,
    agent_id: str = AGENT_ID,
    start: float = 1_700_000_010.0,
    end: float | None = 1_700_000_020.0,
    kind: SpanKind = SpanKind.TOOL_CALL,
    status: SpanStatus | None = None,
    parent_span_id: str | None = None,
    payload_digest: str | None = None,
    links: list[SpanLink] | None = None,
) -> Span:
    if status is None:
        status = SpanStatus.COMPLETED if end is not None else SpanStatus.RUNNING
    return Span(
        id=span_id,
        session_id=session_id,
        agent_id=agent_id,
        kind=kind,
        name=span_id,
        start_time=start,
        end_time=end,
        status=status,
        parent_span_id=parent_span_id,
        attributes={"q": "harmonograph"},
        payload_digest=payload_digest,
        links=links or [],
    )


async def _seed(store: Store) -> tuple[Session, Agent]:
    session = await store.create_session(_session())
    agent = await store.register_agent(_agent())
    return session, agent


# === sessions ==============================================================


async def test_session_create_and_get(store: Store):
    sess = await store.create_session(_session())
    assert sess.id == SESSION_ID
    fetched = await store.get_session(SESSION_ID)
    assert fetched is not None
    assert fetched.title == "Conformance Run"
    assert fetched.metadata == {"suite": "conformance"}
    assert fetched.status == SessionStatus.LIVE


async def test_session_create_is_idempotent(store: Store):
    await store.create_session(_session())
    again = await store.create_session(_session())
    assert again.id == SESSION_ID
    listed = await store.list_sessions()
    assert len([s for s in listed if s.id == SESSION_ID]) == 1


async def test_get_session_missing_returns_none(store: Store):
    assert await store.get_session("nope") is None


async def test_list_sessions_filter_by_status(store: Store):
    await store.create_session(_session("sess_live"))
    completed = _session("sess_done", status=SessionStatus.COMPLETED)
    await store.create_session(completed)
    only_live = await store.list_sessions(status=SessionStatus.LIVE)
    only_done = await store.list_sessions(status=SessionStatus.COMPLETED)
    assert {s.id for s in only_live} == {"sess_live"}
    assert {s.id for s in only_done} == {"sess_done"}


async def test_list_sessions_limit(store: Store):
    for i in range(5):
        s = _session(f"sess_{i:02d}")
        s.created_at = 1_700_000_000.0 + i
        await store.create_session(s)
    capped = await store.list_sessions(limit=2)
    assert len(capped) == 2
    # newest-first ordering
    assert capped[0].id == "sess_04"
    assert capped[1].id == "sess_03"


async def test_update_session(store: Store):
    await store.create_session(_session())
    updated = await store.update_session(
        SESSION_ID,
        title="renamed",
        status=SessionStatus.COMPLETED,
        ended_at=1_700_000_999.0,
        metadata={"extra": "value"},
    )
    assert updated is not None
    assert updated.title == "renamed"
    assert updated.status == SessionStatus.COMPLETED
    assert updated.ended_at == 1_700_000_999.0
    # metadata is merged, not replaced
    assert updated.metadata.get("suite") == "conformance"
    assert updated.metadata.get("extra") == "value"


async def test_update_session_missing_returns_none(store: Store):
    assert await store.update_session("ghost", title="x") is None


async def test_delete_session_cascades(store: Store):
    session, _agent_obj = await _seed(store)
    span = _span("span_1")
    await store.append_span(span)
    annotation = Annotation(
        id="ann_1",
        session_id=SESSION_ID,
        target=AnnotationTarget(span_id="span_1"),
        author="alice",
        created_at=1_700_000_030.0,
        kind=AnnotationKind.COMMENT,
        body="ok",
    )
    await store.put_annotation(annotation)
    plan = TaskPlan(
        id="plan_1",
        session_id=SESSION_ID,
        created_at=1_700_000_040.0,
        tasks=[Task(id="t1", title="t1", status=TaskStatus.PENDING)],
    )
    await store.put_task_plan(plan)
    sample = ContextWindowSample(
        session_id=SESSION_ID,
        agent_id=AGENT_ID,
        recorded_at=1_700_000_050.0,
        tokens=100,
        limit_tokens=4096,
    )
    await store.append_context_window_sample(sample)

    deleted = await store.delete_session(SESSION_ID)
    assert deleted is True

    assert await store.get_session(SESSION_ID) is None
    assert await store.get_span("span_1") is None
    assert await store.list_agents_for_session(SESSION_ID) == []
    assert await store.list_annotations(session_id=SESSION_ID) == []
    assert await store.list_task_plans_for_session(SESSION_ID) == []
    assert await store.list_context_window_samples(SESSION_ID) == []


async def test_delete_session_missing_returns_false(store: Store):
    assert await store.delete_session("ghost") is False


# === agents ================================================================


async def test_register_agent_and_get(store: Store):
    await store.create_session(_session())
    agent = await store.register_agent(_agent())
    assert agent.id == AGENT_ID
    fetched = await store.get_agent(SESSION_ID, AGENT_ID)
    assert fetched is not None
    assert fetched.framework == Framework.ADK
    assert Capability.STEERING in fetched.capabilities


async def test_register_agent_upserts(store: Store):
    await store.create_session(_session())
    await store.register_agent(_agent())
    bumped = _agent()
    bumped.last_heartbeat = 1_700_000_500.0
    bumped.status = AgentStatus.CONNECTED
    await store.register_agent(bumped)
    agents = await store.list_agents_for_session(SESSION_ID)
    assert len(agents) == 1
    assert agents[0].last_heartbeat == 1_700_000_500.0


async def test_list_agents_for_session(store: Store):
    await store.create_session(_session())
    await store.register_agent(_agent(agent_id=AGENT_ID))
    await store.register_agent(_agent(agent_id=PEER_AGENT_ID))
    agents = await store.list_agents_for_session(SESSION_ID)
    assert {a.id for a in agents} == {AGENT_ID, PEER_AGENT_ID}


async def test_update_agent_status(store: Store):
    await _seed(store)
    await store.update_agent_status(
        SESSION_ID, AGENT_ID, AgentStatus.DISCONNECTED, last_heartbeat=1_700_001_000.0
    )
    agent = await store.get_agent(SESSION_ID, AGENT_ID)
    assert agent is not None
    assert agent.status == AgentStatus.DISCONNECTED
    assert agent.last_heartbeat == 1_700_001_000.0


async def test_update_agent_status_missing_is_noop(store: Store):
    await store.create_session(_session())
    # No raise even though the agent doesn't exist.
    await store.update_agent_status(SESSION_ID, "ghost", AgentStatus.CRASHED)
    assert await store.get_agent(SESSION_ID, "ghost") is None


# === spans =================================================================


async def test_append_span_round_trip(store: Store):
    await _seed(store)
    span = _span("span_a", end=None, status=SpanStatus.RUNNING)
    span.links = [
        SpanLink(target_span_id="other", target_agent_id=PEER_AGENT_ID, relation=LinkRelation.INVOKED)
    ]
    appended = await store.append_span(span)
    assert appended.id == "span_a"
    fetched = await store.get_span("span_a")
    assert fetched is not None
    assert fetched.status == SpanStatus.RUNNING
    assert fetched.attributes == {"q": "harmonograph"}
    assert len(fetched.links) == 1
    assert fetched.links[0].relation == LinkRelation.INVOKED


async def test_append_span_is_idempotent(store: Store):
    await _seed(store)
    await store.append_span(_span("span_a"))
    await store.append_span(_span("span_a"))  # second insert is a no-op
    spans = await store.get_spans(SESSION_ID)
    assert [s.id for s in spans] == ["span_a"]


async def test_update_span(store: Store):
    await _seed(store)
    await store.append_span(_span("span_a", end=None, status=SpanStatus.RUNNING))
    updated = await store.update_span(
        "span_a",
        status=SpanStatus.AWAITING_HUMAN,
        attributes={"new": "attr"},
    )
    assert updated is not None
    assert updated.status == SpanStatus.AWAITING_HUMAN
    assert updated.attributes.get("q") == "harmonograph"  # merged, not replaced
    assert updated.attributes.get("new") == "attr"


async def test_update_span_missing_returns_none(store: Store):
    await _seed(store)
    assert await store.update_span("nope", status=SpanStatus.COMPLETED) is None


async def test_end_span_sets_end_time_and_status(store: Store):
    await _seed(store)
    await store.append_span(_span("span_a", end=None, status=SpanStatus.RUNNING))
    ended = await store.end_span(
        "span_a",
        end_time=1_700_000_055.0,
        status=SpanStatus.FAILED,
        error={"type": "Boom", "message": "kapow", "stack": ""},
    )
    assert ended is not None
    assert ended.end_time == 1_700_000_055.0
    assert ended.status == SpanStatus.FAILED
    assert ended.error == {"type": "Boom", "message": "kapow", "stack": ""}


async def test_end_span_missing_returns_none(store: Store):
    await _seed(store)
    assert await store.end_span("nope", end_time=1.0, status=SpanStatus.COMPLETED) is None


async def test_get_spans_filters_by_agent(store: Store):
    await _seed(store)
    await store.register_agent(_agent(agent_id=PEER_AGENT_ID))
    await store.append_span(_span("a1", agent_id=AGENT_ID, start=10.0, end=20.0))
    await store.append_span(_span("b1", agent_id=PEER_AGENT_ID, start=15.0, end=25.0))
    only_a = await store.get_spans(SESSION_ID, agent_id=AGENT_ID)
    assert [s.id for s in only_a] == ["a1"]


async def test_get_spans_filters_by_time_window(store: Store):
    await _seed(store)
    await store.append_span(_span("early", start=10.0, end=20.0))
    await store.append_span(_span("mid", start=30.0, end=40.0))
    await store.append_span(_span("late", start=100.0, end=110.0))
    window = await store.get_spans(SESSION_ID, time_start=25.0, time_end=50.0)
    assert {s.id for s in window} == {"mid"}
    overlap = await store.get_spans(SESSION_ID, time_start=15.0, time_end=35.0)
    assert {s.id for s in overlap} == {"early", "mid"}


async def test_get_spans_orders_by_start_time(store: Store):
    await _seed(store)
    await store.append_span(_span("c", start=30.0, end=35.0))
    await store.append_span(_span("a", start=10.0, end=15.0))
    await store.append_span(_span("b", start=20.0, end=25.0))
    spans = await store.get_spans(SESSION_ID)
    assert [s.id for s in spans] == ["a", "b", "c"]


# === annotations ===========================================================


async def test_annotation_round_trip(store: Store):
    await _seed(store)
    ann = Annotation(
        id="ann_1",
        session_id=SESSION_ID,
        target=AnnotationTarget(span_id=None, agent_id=AGENT_ID, time_start=10.0, time_end=20.0),
        author="alice",
        created_at=1_700_000_100.0,
        kind=AnnotationKind.STEERING,
        body="please refocus",
        delivered_at=1_700_000_101.0,
    )
    await store.put_annotation(ann)
    listed = await store.list_annotations(session_id=SESSION_ID)
    assert len(listed) == 1
    assert listed[0].body == "please refocus"
    assert listed[0].kind == AnnotationKind.STEERING
    assert listed[0].target.agent_id == AGENT_ID


async def test_list_annotations_filter_by_span(store: Store):
    await _seed(store)
    await store.append_span(_span("span_a"))
    await store.put_annotation(
        Annotation(
            id="ann_a",
            session_id=SESSION_ID,
            target=AnnotationTarget(span_id="span_a"),
            author="alice",
            created_at=1.0,
            kind=AnnotationKind.COMMENT,
            body="on span a",
        )
    )
    await store.put_annotation(
        Annotation(
            id="ann_b",
            session_id=SESSION_ID,
            target=AnnotationTarget(agent_id=AGENT_ID),
            author="alice",
            created_at=2.0,
            kind=AnnotationKind.COMMENT,
            body="on agent",
        )
    )
    only_span = await store.list_annotations(span_id="span_a")
    assert [a.id for a in only_span] == ["ann_a"]


# === payloads ==============================================================


async def test_payload_round_trip(store: Store):
    data = b"hello world"
    digest = _sha(data)
    meta = await store.put_payload(digest, data, "text/plain", summary="hi")
    assert meta.size == len(data)
    assert meta.digest == digest
    assert await store.has_payload(digest) is True
    record = await store.get_payload(digest)
    assert record is not None
    assert record.bytes_ == data
    assert record.meta.mime == "text/plain"


async def test_get_payload_missing_returns_none(store: Store):
    assert await store.get_payload("deadbeef") is None
    assert await store.has_payload("deadbeef") is False


async def test_payload_dedup_on_same_digest(store: Store):
    data = b"dup"
    digest = _sha(data)
    await store.put_payload(digest, data, "text/plain")
    await store.put_payload(digest, data, "text/plain")
    stats = await store.stats()
    assert stats.payload_count == 1


async def test_gc_payloads_removes_orphans(store: Store):
    data = b"orphan"
    digest = _sha(data)
    await store.put_payload(digest, data, "text/plain")
    # No span references this digest, so it must be evictable.
    evicted = await store.gc_payloads()
    assert evicted >= 1
    assert await store.has_payload(digest) is False


# === task plans ============================================================


async def test_task_plan_round_trip(store: Store):
    await _seed(store)
    plan = TaskPlan(
        id="plan_1",
        session_id=SESSION_ID,
        created_at=1_700_000_200.0,
        invocation_span_id="inv_1",
        planner_agent_id=AGENT_ID,
        summary="do the thing",
        tasks=[
            Task(id="t1", title="step one", status=TaskStatus.PENDING),
            Task(id="t2", title="step two", status=TaskStatus.PENDING),
        ],
        edges=[TaskEdge(from_task_id="t1", to_task_id="t2")],
    )
    stored = await store.put_task_plan(plan)
    assert stored.id == "plan_1"
    fetched = await store.get_task_plan("plan_1")
    assert fetched is not None
    assert [t.id for t in fetched.tasks] == ["t1", "t2"]
    assert fetched.edges == [TaskEdge(from_task_id="t1", to_task_id="t2")]
    assert fetched.summary == "do the thing"


async def test_put_task_plan_replaces_tasks(store: Store):
    await _seed(store)
    plan = TaskPlan(
        id="plan_r",
        session_id=SESSION_ID,
        created_at=1_700_000_200.0,
        tasks=[Task(id="orig", title="orig", status=TaskStatus.PENDING)],
    )
    await store.put_task_plan(plan)
    revised = TaskPlan(
        id="plan_r",
        session_id=SESSION_ID,
        created_at=1_700_000_201.0,
        revision_index=1,
        revision_reason="replanned",
        tasks=[
            Task(id="new1", title="new1", status=TaskStatus.PENDING),
            Task(id="new2", title="new2", status=TaskStatus.PENDING),
        ],
    )
    await store.put_task_plan(revised)
    fetched = await store.get_task_plan("plan_r")
    assert fetched is not None
    assert {t.id for t in fetched.tasks} == {"new1", "new2"}
    assert fetched.revision_index == 1
    assert fetched.revision_reason == "replanned"


async def test_put_task_plan_round_trips_revision_annotation_id(store: Store):
    """harmonograf#95 / goldfive#196: source annotation id survives round-trip.

    The id is stamped on the PlanRevised wire envelope by goldfive for
    user-control refines and carried through every storage backend so
    the intervention aggregator can strict-join plan-revision rows
    against the source annotation without a time-window fallback.
    """
    await _seed(store)
    plan = TaskPlan(
        id="plan_ann",
        session_id=SESSION_ID,
        created_at=1_700_000_250.0,
        revision_index=1,
        revision_kind="user_steer",
        revision_severity="warning",
        revision_reason="by alice: pivot",
        revision_annotation_id="ann_store_123",
        tasks=[Task(id="t", title="t", status=TaskStatus.PENDING)],
    )
    await store.put_task_plan(plan)
    fetched = await store.get_task_plan("plan_ann")
    assert fetched is not None
    assert fetched.revision_annotation_id == "ann_store_123"
    # Autonomous refines (no id) continue to round-trip as "".
    plan2 = TaskPlan(
        id="plan_no_ann",
        session_id=SESSION_ID,
        created_at=1_700_000_260.0,
        revision_index=2,
        revision_kind="looping_reasoning",
        revision_annotation_id="",
        tasks=[Task(id="t2", title="t2", status=TaskStatus.PENDING)],
    )
    await store.put_task_plan(plan2)
    fetched2 = await store.get_task_plan("plan_no_ann")
    assert fetched2 is not None
    assert fetched2.revision_annotation_id == ""


async def test_list_task_plans_for_session(store: Store):
    await _seed(store)
    for i, ts in enumerate([1_700_000_300.0, 1_700_000_400.0]):
        await store.put_task_plan(
            TaskPlan(
                id=f"plan_{i}",
                session_id=SESSION_ID,
                created_at=ts,
                tasks=[Task(id="t", title="t", status=TaskStatus.PENDING)],
            )
        )
    plans = await store.list_task_plans_for_session(SESSION_ID)
    assert [p.id for p in plans] == ["plan_0", "plan_1"]


async def test_update_task_status(store: Store):
    await _seed(store)
    await store.put_task_plan(
        TaskPlan(
            id="plan_u",
            session_id=SESSION_ID,
            created_at=1.0,
            tasks=[Task(id="t1", title="t1", status=TaskStatus.PENDING)],
        )
    )
    updated = await store.update_task_status(
        "plan_u", "t1", TaskStatus.RUNNING, bound_span_id="span_x"
    )
    assert updated is not None
    assert updated.status == TaskStatus.RUNNING
    assert updated.bound_span_id == "span_x"
    refetched = await store.get_task_plan("plan_u")
    assert refetched is not None
    assert refetched.tasks[0].status == TaskStatus.RUNNING
    assert refetched.tasks[0].bound_span_id == "span_x"


async def test_update_task_status_missing_returns_none(store: Store):
    await _seed(store)
    assert await store.update_task_status("ghost", "t", TaskStatus.RUNNING) is None


# === context window samples ===============================================


async def test_context_window_sample_round_trip(store: Store):
    await _seed(store)
    for i in range(3):
        await store.append_context_window_sample(
            ContextWindowSample(
                session_id=SESSION_ID,
                agent_id=AGENT_ID,
                recorded_at=1_700_000_500.0 + i,
                tokens=100 + i,
                limit_tokens=4096,
            )
        )
    samples = await store.list_context_window_samples(SESSION_ID, agent_id=AGENT_ID)
    assert len(samples) == 3
    assert [s.tokens for s in samples] == [100, 101, 102]


async def test_context_window_sample_per_agent_cap(store: Store):
    await _seed(store)
    await store.register_agent(_agent(agent_id=PEER_AGENT_ID))
    for i in range(5):
        await store.append_context_window_sample(
            ContextWindowSample(
                session_id=SESSION_ID,
                agent_id=AGENT_ID,
                recorded_at=float(i),
                tokens=i,
                limit_tokens=4096,
            )
        )
        await store.append_context_window_sample(
            ContextWindowSample(
                session_id=SESSION_ID,
                agent_id=PEER_AGENT_ID,
                recorded_at=float(i),
                tokens=i * 10,
                limit_tokens=4096,
            )
        )
    capped = await store.list_context_window_samples(SESSION_ID, limit_per_agent=2)
    by_agent: dict[str, list[int]] = {}
    for s in capped:
        by_agent.setdefault(s.agent_id, []).append(s.tokens)
    assert sorted(by_agent.keys()) == [AGENT_ID, PEER_AGENT_ID]
    # Both agents present, each capped at 2 newest samples.
    assert len(by_agent[AGENT_ID]) == 2
    assert len(by_agent[PEER_AGENT_ID]) == 2


# === stats =================================================================


async def test_stats_counts(store: Store):
    await _seed(store)
    await store.append_span(_span("span_a"))
    data = b"payload"
    await store.put_payload(_sha(data), data, "text/plain")
    stats = await store.stats()
    assert stats.session_count == 1
    assert stats.agent_count == 1
    assert stats.span_count == 1
    assert stats.payload_count == 1
    assert stats.payload_bytes == len(data)


# === readiness =============================================================


async def test_ping_returns_true_after_start(store: Store):
    assert await store.ping() is True
