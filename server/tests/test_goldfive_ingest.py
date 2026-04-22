"""Tests for the goldfive-event ingest path (issue #3, Phase B).

Harmonograf's ingest pipeline dispatches ``TelemetryUp.goldfive_event``
envelopes to per-kind handlers that (a) persist plan/task state to the
store and (b) fan out bus deltas to WatchSession subscribers. These
tests drive the pipeline in-process with synthetic ``goldfive.v1.Event``
payloads and verify both side-effects.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from goldfive.pb.goldfive.v1 import events_pb2 as ge
from goldfive.pb.goldfive.v1 import types_pb2 as gt

from harmonograf_server.bus import (
    DELTA_AGENT_INVOCATION_COMPLETED,
    DELTA_AGENT_INVOCATION_STARTED,
    DELTA_DELEGATION_OBSERVED,
    DELTA_DRIFT,
    DELTA_GOAL_DERIVED,
    DELTA_RUN_ABORTED,
    DELTA_RUN_COMPLETED,
    DELTA_RUN_STARTED,
    DELTA_SESSION_ENDED,
    DELTA_SPAN_END,
    DELTA_TASK_PLAN,
    DELTA_TASK_PROGRESS,
    DELTA_TASK_STATUS,
    SessionBus,
)
from harmonograf_server.ingest import IngestPipeline, StreamContext
from harmonograf_server.pb import telemetry_pb2
from harmonograf_server.storage import (
    Session,
    SessionStatus,
    TaskStatus,
    make_store,
)


# ---- fixtures ------------------------------------------------------------


@pytest_asyncio.fixture
async def store():
    s = make_store("memory")
    await s.start()
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
async def pipeline(store):
    bus = SessionBus()
    pipe = IngestPipeline(store, bus, now_fn=lambda: 1_000_000.0)
    yield pipe, bus, store


def _stream_ctx(session_id: str = "sess_gf", agent_id: str = "agent_gf") -> StreamContext:
    return StreamContext(
        stream_id="str_test",
        agent_id=agent_id,
        session_id=session_id,
        connected_at=1000.0,
        last_heartbeat=1000.0,
        seen_routes={(session_id, agent_id)},
    )


def _wrap(event: ge.Event) -> telemetry_pb2.TelemetryUp:
    return telemetry_pb2.TelemetryUp(goldfive_event=event)


async def _ensure_session(store, session_id: str = "sess_gf") -> None:
    await store.create_session(
        Session(
            id=session_id,
            title=session_id,
            created_at=1.0,
            status=SessionStatus.LIVE,
        )
    )


def _subscribe(bus: SessionBus, session_id: str = "sess_gf"):
    import asyncio

    loop = asyncio.get_event_loop()
    return loop.run_until_complete(bus.subscribe(session_id))


async def _drain(sub, kinds: set[str] | None = None) -> list:
    out = []
    while True:
        try:
            delta = sub.queue.get_nowait()
        except Exception:
            break
        if kinds is None or delta.kind in kinds:
            out.append(delta)
    return out


def _make_event(**kwargs) -> ge.Event:
    evt = ge.Event()
    evt.event_id = kwargs.get("event_id", "e1")
    evt.run_id = kwargs.get("run_id", "run-1")
    evt.sequence = kwargs.get("sequence", 0)
    return evt


def _plan_pb(
    plan_id: str = "plan-1",
    run_id: str = "run-1",
    task_ids: list[str] | None = None,
) -> gt.Plan:
    task_ids = task_ids or ["t1", "t2"]
    plan = gt.Plan()
    plan.id = plan_id
    plan.run_id = run_id
    plan.summary = "test plan"
    for tid in task_ids:
        t = plan.tasks.add()
        t.id = tid
        t.title = f"task {tid}"
        t.status = gt.TASK_STATUS_PENDING
    for a, b in zip(task_ids, task_ids[1:]):
        e = plan.edges.add()
        e.from_task_id = a
        e.to_task_id = b
    return plan


# ---- run lifecycle -------------------------------------------------------


@pytest.mark.asyncio
async def test_run_started_publishes_delta(pipeline):
    pipe, bus, _ = pipeline
    sub = await bus.subscribe("sess_gf")
    evt = _make_event()
    evt.run_started.run_id = "run-1"
    evt.run_started.goal_summary = "collect reports"
    await pipe.handle_message(_stream_ctx(), _wrap(evt))
    delta = sub.queue.get_nowait()
    assert delta.kind == DELTA_RUN_STARTED
    assert delta.payload["run_id"] == "run-1"
    assert delta.payload["goal_summary"] == "collect reports"


@pytest.mark.asyncio
async def test_run_completed_and_aborted_publish_deltas(pipeline, store):
    pipe, bus, _ = pipeline
    # Session must exist so the finalize path's ``update_session`` flips
    # it terminal — harmonograf#96 asserts the session row drives the
    # frontend's ``sessionIsInactive`` header-gate.
    await _ensure_session(store)
    sub = await bus.subscribe("sess_gf")
    completed = _make_event(event_id="e-c")
    completed.run_completed.outcome_summary = "done"
    aborted = _make_event(event_id="e-a", sequence=1)
    aborted.run_aborted.reason = "user cancel"
    await pipe.handle_message(_stream_ctx(), _wrap(completed))
    await pipe.handle_message(_stream_ctx(), _wrap(aborted))
    deltas = await _drain(sub)
    kinds = [d.kind for d in deltas]
    assert DELTA_RUN_COMPLETED in kinds and DELTA_RUN_ABORTED in kinds
    # harmonograf#96: both terminal events now trigger a SessionEnded
    # broadcast so the frontend's LIVE ACTIVITY header clears.
    session_ended_deltas = [d for d in deltas if d.kind == DELTA_SESSION_ENDED]
    assert len(session_ended_deltas) == 2
    assert session_ended_deltas[0].payload["final_status"] == SessionStatus.COMPLETED
    assert session_ended_deltas[1].payload["final_status"] == SessionStatus.ABORTED


@pytest.mark.asyncio
async def test_run_completed_flips_session_status_and_ended_at(pipeline, store):
    """harmonograf#96: a goldfive ``run_completed`` must drive
    ``sessions.status`` LIVE → COMPLETED and stamp ``ended_at``.

    Previously the ingest pipeline fanned out a bus delta for trajectory
    listeners but never mutated the session row, so the frontend's
    ``sessionIsInactive`` check stayed false forever and the LIVE
    ACTIVITY "N RUNNING" header never cleared.
    """
    pipe, _bus, _ = pipeline
    await _ensure_session(store)
    before = await store.get_session("sess_gf")
    assert before.status is SessionStatus.LIVE
    assert before.ended_at is None

    completed = _make_event(event_id="e-c")
    completed.run_completed.outcome_summary = "done"
    await pipe.handle_message(_stream_ctx(), _wrap(completed))

    after = await store.get_session("sess_gf")
    assert after.status is SessionStatus.COMPLETED
    assert after.ended_at is not None
    assert after.ended_at > 0


@pytest.mark.asyncio
async def test_run_aborted_flips_session_status_to_aborted(pipeline, store):
    """harmonograf#96: ``run_aborted`` flips session to ABORTED (not COMPLETED)."""
    pipe, _bus, _ = pipeline
    await _ensure_session(store)
    aborted = _make_event(event_id="e-a")
    aborted.run_aborted.reason = "user cancel"
    await pipe.handle_message(_stream_ctx(), _wrap(aborted))
    after = await store.get_session("sess_gf")
    assert after.status is SessionStatus.ABORTED
    assert after.ended_at is not None


@pytest.mark.asyncio
async def test_run_completed_closes_orphan_invocation_spans(pipeline, store):
    """harmonograf#96 belt-and-suspenders: orphan INVOCATION spans whose
    ``after_run_callback`` never fired (ADK cancel race on AgentTool
    sub-Runners) must be swept closed on ``run_completed`` so the Gantt
    reflects truth. Non-INVOCATION leaks (LLM_CALL / TOOL_CALL) are left
    alone because they have their own ADK cleanup paths and could race
    with a late-arriving legitimate end_span.
    """
    from harmonograf_server.storage import Agent, AgentStatus, Framework, Span, SpanKind, SpanStatus

    pipe, bus, _ = pipeline
    await _ensure_session(store)
    await store.register_agent(
        Agent(
            id="agent_gf",
            session_id="sess_gf",
            name="coordinator",
            framework=Framework.UNKNOWN,
            status=AgentStatus.CONNECTED,
            connected_at=1.0,
            last_heartbeat=1.0,
        )
    )
    # Two INVOCATION spans left open (simulates a sub-Runner whose
    # after_run_callback never fired) plus one LLM_CALL that should be
    # left alone (covered by model-after cleanup).
    await store.append_span(
        Span(
            id="inv_open_1",
            session_id="sess_gf",
            agent_id="agent_gf",
            name="web_developer_agent",
            kind=SpanKind.INVOCATION,
            status=SpanStatus.RUNNING,
            start_time=100.0,
            end_time=None,
        )
    )
    await store.append_span(
        Span(
            id="inv_open_2",
            session_id="sess_gf",
            agent_id="agent_gf",
            name="coordinator_agent",
            kind=SpanKind.INVOCATION,
            status=SpanStatus.RUNNING,
            start_time=101.0,
            end_time=None,
        )
    )
    await store.append_span(
        Span(
            id="llm_open",
            session_id="sess_gf",
            agent_id="agent_gf",
            name="openai/Qwen",
            kind=SpanKind.LLM_CALL,
            status=SpanStatus.RUNNING,
            start_time=102.0,
            end_time=None,
        )
    )

    completed = _make_event(event_id="e-c")
    completed.run_completed.outcome_summary = "done"
    await pipe.handle_message(_stream_ctx(), _wrap(completed))

    # INVOCATION spans must now be closed with end_time set. Sqlite
    # storage doesn't expose an "open only" filter so we re-fetch.
    inv1 = await store.get_span("inv_open_1")
    inv2 = await store.get_span("inv_open_2")
    llm = await store.get_span("llm_open")
    assert inv1.end_time is not None
    assert inv1.status is SpanStatus.COMPLETED
    assert inv2.end_time is not None
    assert inv2.status is SpanStatus.COMPLETED
    # LLM_CALL left alone — not the ingest path's responsibility.
    assert llm.end_time is None


@pytest.mark.asyncio
async def test_run_aborted_closes_orphan_invocations_with_cancelled(pipeline, store):
    """A run that aborts should close orphan INVOCATION spans with
    ``status=CANCELLED`` (not COMPLETED) so the Gantt renders the
    terminal state correctly.
    """
    from harmonograf_server.storage import Agent, AgentStatus, Framework, Span, SpanKind, SpanStatus

    pipe, _bus, _ = pipeline
    await _ensure_session(store)
    await store.register_agent(
        Agent(
            id="agent_gf",
            session_id="sess_gf",
            name="coordinator",
            framework=Framework.UNKNOWN,
            status=AgentStatus.CONNECTED,
            connected_at=1.0,
            last_heartbeat=1.0,
        )
    )
    await store.append_span(
        Span(
            id="inv_open",
            session_id="sess_gf",
            agent_id="agent_gf",
            name="web_developer_agent",
            kind=SpanKind.INVOCATION,
            status=SpanStatus.RUNNING,
            start_time=100.0,
            end_time=None,
        )
    )

    aborted = _make_event(event_id="e-a")
    aborted.run_aborted.reason = "user cancel"
    await pipe.handle_message(_stream_ctx(), _wrap(aborted))

    inv = await store.get_span("inv_open")
    assert inv.end_time is not None
    assert inv.status is SpanStatus.CANCELLED


@pytest.mark.asyncio
async def test_run_completed_session_unknown_is_noop_but_still_broadcasts(pipeline):
    """When goldfive sends ``run_completed`` for a session the server
    has never seen (e.g. event arrives before the Hello established the
    row), the finalize path must NOT raise. The broadcast still fires
    so a subscriber that later joins and lists the session sees the
    terminal state via its initial burst.
    """
    pipe, bus, _ = pipeline
    sub = await bus.subscribe("sess_never_created")
    completed = _make_event(event_id="e-c")
    completed.run_completed.outcome_summary = "orphaned"
    # NOTE: different session_id in ctx so the finalize path tries the
    # update against an unknown row.
    await pipe.handle_message(
        _stream_ctx(session_id="sess_never_created"), _wrap(completed)
    )
    deltas = await _drain(sub)
    kinds = [d.kind for d in deltas]
    assert DELTA_RUN_COMPLETED in kinds
    assert DELTA_SESSION_ENDED in kinds


# ---- goal derivation -----------------------------------------------------


@pytest.mark.asyncio
async def test_goal_derived_fans_out_goals(pipeline):
    pipe, bus, _ = pipeline
    sub = await bus.subscribe("sess_gf")
    evt = _make_event()
    g = evt.goal_derived.goals.add()
    g.id = "goal-1"
    g.summary = "ship release"
    g.metadata["owner"] = "team"
    await pipe.handle_message(_stream_ctx(), _wrap(evt))
    delta = sub.queue.get_nowait()
    assert delta.kind == DELTA_GOAL_DERIVED
    assert len(delta.payload["goals"]) == 1
    assert delta.payload["goals"][0]["id"] == "goal-1"
    assert delta.payload["goals"][0]["metadata"] == {"owner": "team"}


# ---- plan submission / revision ------------------------------------------


@pytest.mark.asyncio
async def test_plan_submitted_persists_and_fans_out(pipeline, store):
    pipe, bus, _ = pipeline
    await _ensure_session(store)
    sub = await bus.subscribe("sess_gf")
    evt = _make_event()
    evt.plan_submitted.plan.CopyFrom(_plan_pb())
    await pipe.handle_message(_stream_ctx(), _wrap(evt))

    plans = await store.list_task_plans_for_session("sess_gf")
    assert len(plans) == 1
    plan = plans[0]
    assert plan.id == "plan-1"
    assert plan.session_id == "sess_gf"
    assert plan.planner_agent_id == "agent_gf"
    assert [t.id for t in plan.tasks] == ["t1", "t2"]
    assert all(t.status == TaskStatus.PENDING for t in plan.tasks)
    assert len(plan.edges) == 1

    delta = sub.queue.get_nowait()
    assert delta.kind == DELTA_TASK_PLAN
    assert delta.payload.id == "plan-1"

    # Index populated for fast span-to-task binding lookups.
    assert pipe._task_index["sess_gf"]["t1"] == "plan-1"
    assert pipe._task_index["sess_gf"]["t2"] == "plan-1"


@pytest.mark.asyncio
async def test_plan_submitted_with_missing_id_is_dropped(pipeline, store):
    pipe, bus, _ = pipeline
    await _ensure_session(store)
    sub = await bus.subscribe("sess_gf")
    evt = _make_event()
    # No plan.id set; harmonograf refuses to persist to avoid corrupt rows.
    evt.plan_submitted.plan.summary = "nameless"
    await pipe.handle_message(_stream_ctx(), _wrap(evt))
    assert await store.list_task_plans_for_session("sess_gf") == []
    # No delta should have been published either.
    import asyncio

    assert sub.queue.empty()


@pytest.mark.asyncio
async def test_plan_revised_increments_revision_metadata(pipeline, store):
    pipe, bus, _ = pipeline
    await _ensure_session(store)
    sub = await bus.subscribe("sess_gf")

    # Initial plan.
    initial = _make_event()
    initial.plan_submitted.plan.CopyFrom(_plan_pb(plan_id="plan-A"))
    await pipe.handle_message(_stream_ctx(), _wrap(initial))

    # Revised plan on the same id — drift metadata carried on the event.
    revised = _make_event(sequence=1, event_id="e-rev")
    revised_plan = _plan_pb(plan_id="plan-A", task_ids=["t1", "t2", "t3"])
    revised_plan.revision_index = 1
    revised_plan.revision_reason = "tool flaky"
    revised.plan_revised.plan.CopyFrom(revised_plan)
    revised.plan_revised.drift_kind = gt.DRIFT_KIND_TOOL_ERROR
    revised.plan_revised.severity = gt.DRIFT_SEVERITY_WARNING
    revised.plan_revised.reason = "tool flaky"
    revised.plan_revised.revision_index = 1
    await pipe.handle_message(_stream_ctx(), _wrap(revised))

    stored = await store.get_task_plan("plan-A")
    assert stored is not None
    assert stored.revision_index == 1
    assert stored.revision_reason == "tool flaky"
    assert stored.revision_kind == "tool_error"
    assert stored.revision_severity == "warning"
    assert [t.id for t in stored.tasks] == ["t1", "t2", "t3"]

    # The bus should have received at least the two plan fan-outs (initial + revised).
    plan_deltas = []
    while not sub.queue.empty():
        d = sub.queue.get_nowait()
        if d.kind == DELTA_TASK_PLAN:
            plan_deltas.append(d)
    assert len(plan_deltas) >= 2


# ---- task status transitions --------------------------------------------


@pytest.mark.asyncio
async def test_task_started_completed_flow(pipeline, store):
    pipe, bus, _ = pipeline
    await _ensure_session(store)
    sub = await bus.subscribe("sess_gf")

    plan_evt = _make_event()
    plan_evt.plan_submitted.plan.CopyFrom(_plan_pb(plan_id="p-flow"))
    await pipe.handle_message(_stream_ctx(), _wrap(plan_evt))

    # RUNNING
    started = _make_event(sequence=1, event_id="e-s")
    started.task_started.task_id = "t1"
    await pipe.handle_message(_stream_ctx(), _wrap(started))

    # COMPLETED
    completed = _make_event(sequence=2, event_id="e-c")
    completed.task_completed.task_id = "t1"
    completed.task_completed.summary = "ok"
    await pipe.handle_message(_stream_ctx(), _wrap(completed))

    plan = await store.get_task_plan("p-flow")
    assert plan is not None
    t1 = next(t for t in plan.tasks if t.id == "t1")
    assert t1.status == TaskStatus.COMPLETED

    status_deltas = []
    while not sub.queue.empty():
        d = sub.queue.get_nowait()
        if d.kind == DELTA_TASK_STATUS:
            status_deltas.append(d)
    statuses = [d.payload["task"].status for d in status_deltas]
    assert TaskStatus.RUNNING in statuses
    assert TaskStatus.COMPLETED in statuses


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,expected_status",
    [
        ("task_failed", TaskStatus.FAILED),
        ("task_blocked", TaskStatus.BLOCKED),
        ("task_cancelled", TaskStatus.CANCELLED),
    ],
)
async def test_task_terminal_statuses(pipeline, store, field: str, expected_status):
    pipe, bus, _ = pipeline
    await _ensure_session(store)
    plan_evt = _make_event()
    plan_evt.plan_submitted.plan.CopyFrom(_plan_pb(plan_id=f"p-{field}"))
    await pipe.handle_message(_stream_ctx(), _wrap(plan_evt))

    evt = _make_event(sequence=1)
    sub_msg = getattr(evt, field)
    sub_msg.task_id = "t1"
    await pipe.handle_message(_stream_ctx(), _wrap(evt))

    plan = await store.get_task_plan(f"p-{field}")
    assert plan is not None
    t1 = next(t for t in plan.tasks if t.id == "t1")
    assert t1.status == expected_status


@pytest.mark.asyncio
async def test_task_cancelled_reason_persists_on_task_row(pipeline, store):
    """harmonograf#110 / goldfive#205.

    The structured cancel reason on ``TaskCancelled.reason`` must round-
    trip onto the stored task's ``cancel_reason`` column so the
    Trajectory view task-delta list and the Drawer overview can render
    it without a second fetch.
    """
    pipe, _bus, _ = pipeline
    await _ensure_session(store)
    plan_evt = _make_event()
    plan_evt.plan_submitted.plan.CopyFrom(_plan_pb(plan_id="p-cancel-reason"))
    await pipe.handle_message(_stream_ctx(), _wrap(plan_evt))

    evt = _make_event(sequence=1)
    evt.task_cancelled.task_id = "t1"
    evt.task_cancelled.reason = "upstream_failed:root_task"
    await pipe.handle_message(_stream_ctx(), _wrap(evt))

    plan = await store.get_task_plan("p-cancel-reason")
    assert plan is not None
    t1 = next(t for t in plan.tasks if t.id == "t1")
    assert t1.status == TaskStatus.CANCELLED
    assert t1.cancel_reason == "upstream_failed:root_task"


@pytest.mark.asyncio
async def test_task_failed_reason_persists_on_task_row(pipeline, store):
    """harmonograf#110 / goldfive#205: TaskFailed.reason rides through too."""
    pipe, _bus, _ = pipeline
    await _ensure_session(store)
    plan_evt = _make_event()
    plan_evt.plan_submitted.plan.CopyFrom(_plan_pb(plan_id="p-fail-reason"))
    await pipe.handle_message(_stream_ctx(), _wrap(plan_evt))

    evt = _make_event(sequence=1)
    evt.task_failed.task_id = "t1"
    evt.task_failed.reason = "refine_validation_failed"
    await pipe.handle_message(_stream_ctx(), _wrap(evt))

    plan = await store.get_task_plan("p-fail-reason")
    assert plan is not None
    t1 = next(t for t in plan.tasks if t.id == "t1")
    assert t1.status == TaskStatus.FAILED
    assert t1.cancel_reason == "refine_validation_failed"


@pytest.mark.asyncio
async def test_cancel_reason_preserved_across_later_non_cancel_transitions(
    pipeline, store
):
    """A later BLOCKED / RUNNING ping must not wipe a stamped reason.

    Regression guard: update_task_status is called for every transition;
    without preserve semantics a later BLOCKED event with an empty
    cancel_reason arg would blank the prior CANCELLED / FAILED reason.
    """
    pipe, _bus, _ = pipeline
    await _ensure_session(store)
    plan_evt = _make_event()
    plan_evt.plan_submitted.plan.CopyFrom(_plan_pb(plan_id="p-preserve"))
    await pipe.handle_message(_stream_ctx(), _wrap(plan_evt))

    # Stamp a cancel reason.
    cancel = _make_event(sequence=1)
    cancel.task_cancelled.task_id = "t1"
    cancel.task_cancelled.reason = "user_cancel:ann-abc"
    await pipe.handle_message(_stream_ctx(), _wrap(cancel))

    # Later transition without a reason (simulates a late BLOCKED ping).
    later = _make_event(sequence=2)
    later.task_blocked.task_id = "t1"
    await pipe.handle_message(_stream_ctx(), _wrap(later))

    plan = await store.get_task_plan("p-preserve")
    assert plan is not None
    t1 = next(t for t in plan.tasks if t.id == "t1")
    # Status rolls forward; reason preserved.
    assert t1.status == TaskStatus.BLOCKED
    assert t1.cancel_reason == "user_cancel:ann-abc"


@pytest.mark.asyncio
async def test_task_status_for_unknown_task_is_logged_not_crash(pipeline, store):
    pipe, _bus, _ = pipeline
    # No plan submitted — task_started for an unknown id should be a no-op.
    evt = _make_event()
    evt.task_started.task_id = "ghost-task"
    await pipe.handle_message(_stream_ctx(), _wrap(evt))
    # Pipeline stayed alive.
    assert pipe._task_index == {}


@pytest.mark.asyncio
async def test_task_progress_fans_out_without_persisting(pipeline, store):
    pipe, bus, _ = pipeline
    await _ensure_session(store)
    sub = await bus.subscribe("sess_gf")
    plan_evt = _make_event()
    plan_evt.plan_submitted.plan.CopyFrom(_plan_pb(plan_id="p-prog"))
    await pipe.handle_message(_stream_ctx(), _wrap(plan_evt))
    # Drain plan delta so the progress delta is first.
    while not sub.queue.empty():
        sub.queue.get_nowait()

    prog = _make_event(sequence=1)
    prog.task_progress.task_id = "t1"
    prog.task_progress.fraction = 0.4
    prog.task_progress.detail = "40% done"
    await pipe.handle_message(_stream_ctx(), _wrap(prog))

    delta = sub.queue.get_nowait()
    assert delta.kind == DELTA_TASK_PROGRESS
    assert delta.payload["task_id"] == "t1"
    assert delta.payload["fraction"] == pytest.approx(0.4)
    assert delta.payload["detail"] == "40% done"

    # Progress does NOT flip persisted task status.
    plan = await store.get_task_plan("p-prog")
    assert plan is not None
    assert all(t.status == TaskStatus.PENDING for t in plan.tasks)


# ---- drift ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_drift_detected_publishes_delta(pipeline):
    pipe, bus, _ = pipeline
    sub = await bus.subscribe("sess_gf")
    evt = _make_event()
    evt.drift_detected.kind = gt.DRIFT_KIND_TOOL_ERROR
    evt.drift_detected.severity = gt.DRIFT_SEVERITY_CRITICAL
    evt.drift_detected.detail = "backend flaky"
    evt.drift_detected.current_task_id = "t1"
    evt.drift_detected.current_agent_id = "agent_gf"
    await pipe.handle_message(_stream_ctx(), _wrap(evt))
    delta = sub.queue.get_nowait()
    assert delta.kind == DELTA_DRIFT
    assert delta.payload["kind"] == "tool_error"
    assert delta.payload["severity"] == "critical"
    assert delta.payload["detail"] == "backend flaky"
    assert delta.payload["current_task_id"] == "t1"


# ---- dispatch sanity -----------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_payload_is_ignored(pipeline):
    pipe, bus, _ = pipeline
    sub = await bus.subscribe("sess_gf")
    # An Event with no payload variant set — goldfive's forward-compat path.
    evt = _make_event()
    await pipe.handle_message(_stream_ctx(), _wrap(evt))
    assert sub.queue.empty()


# ---- per-event session_id routing (goldfive#155 / harmonograf#63) -------
#
# ``goldfive.v1.Event`` gained a ``session_id`` field in goldfive#155 / PR
# #157, and the Runner / Steerer / Executors stamp it on every emitted
# event so one StreamTelemetry RPC can multiplex events across multiple
# sessions (e.g. ADK's AgentTool mints a sub-Runner session inside an
# adk-web run). Harmonograf#63 switches server-side routing from the
# stream's Hello session to the per-event ``session_id`` with a
# back-compat fallback to the Hello session when the field is empty.


@pytest.mark.asyncio
async def test_goldfive_event_routes_to_per_event_session_id(pipeline, store):
    """Event with ``session_id=X`` on a stream Hello'd as ``Y`` lands on
    session X, not Y. The session row is auto-created on first sighting
    the same way span ingest auto-creates unseen ``(session, agent)``
    routes.
    """
    pipe, bus, _ = pipeline
    hello_sid = "sess_home"
    per_event_sid = "sess_per_event"
    ctx = _stream_ctx(session_id=hello_sid)

    # Subscribe to both sessions so we can tell which one the fan-out
    # lands on without relying on storage side-effects.
    sub_home = await bus.subscribe(hello_sid)
    sub_target = await bus.subscribe(per_event_sid)

    evt = _make_event()
    evt.session_id = per_event_sid
    evt.run_started.run_id = "run-1"
    evt.run_started.goal_summary = "check routing"
    await pipe.handle_message(ctx, _wrap(evt))

    # The per-event session got a run_started delta (alongside the
    # auto-register agent_upsert / session-create deltas that fire on
    # first sighting of an unseen route).
    deltas = await _drain(sub_target)
    run_started = [d for d in deltas if d.kind == DELTA_RUN_STARTED]
    assert len(run_started) == 1
    assert run_started[0].payload["run_id"] == "run-1"

    # ...and the Hello session did NOT see a run_started delta.
    home_deltas = await _drain(sub_home)
    assert not [d for d in home_deltas if d.kind == DELTA_RUN_STARTED]

    # Session row was auto-created so downstream RPCs can find it.
    assert await store.get_session(per_event_sid) is not None


@pytest.mark.asyncio
async def test_goldfive_event_empty_session_id_falls_back_to_hello(
    pipeline, store
):
    """Empty ``session_id`` preserves pre-goldfive#155 behavior: events
    route to the stream's Hello session. Back-compat guard for older
    goldfive clients and for events emitted before stamping was
    threaded through every executor path.
    """
    pipe, bus, _ = pipeline
    hello_sid = "sess_home"
    ctx = _stream_ctx(session_id=hello_sid)

    sub = await bus.subscribe(hello_sid)

    evt = _make_event()
    # session_id left empty (the pre-#155 shape).
    assert evt.session_id == ""
    evt.run_started.run_id = "run-1"
    evt.run_started.goal_summary = "fallback check"
    await pipe.handle_message(ctx, _wrap(evt))

    delta = sub.queue.get_nowait()
    assert delta.kind == DELTA_RUN_STARTED
    assert delta.payload["run_id"] == "run-1"


@pytest.mark.asyncio
async def test_goldfive_plan_persisted_under_per_event_session(pipeline, store):
    """Storage writes also follow the per-event ``session_id``. A plan
    emitted with ``session_id=X`` on a stream Hello'd as ``Y`` is
    persisted as a child of X (via ``session_id`` on :class:`TaskPlan`),
    so cross-session lookups resolve the plan through the correct
    session aggregate.
    """
    pipe, _, _ = pipeline
    hello_sid = "sess_home"
    per_event_sid = "sess_per_event"
    ctx = _stream_ctx(session_id=hello_sid)

    evt = _make_event()
    evt.session_id = per_event_sid
    evt.plan_submitted.plan.CopyFrom(_plan_pb(plan_id="p-routed"))
    await pipe.handle_message(ctx, _wrap(evt))

    # Plan lists under the per-event session.
    plans_target = await store.list_task_plans_for_session(per_event_sid)
    assert [p.id for p in plans_target] == ["p-routed"]
    assert plans_target[0].session_id == per_event_sid

    # Hello session stays empty — nothing leaked onto it.
    plans_home = await store.list_task_plans_for_session(hello_sid)
    assert plans_home == []

    # Task index keyed by per-event session (the hot-path index used by
    # span-to-task binding lookups).
    assert pipe._task_index.get(per_event_sid, {}).get("t1") == "p-routed"
    assert pipe._task_index.get(hello_sid, {}).get("t1") is None


@pytest.mark.asyncio
async def test_span_and_goldfive_event_co_locate_on_adk_session(pipeline, store):
    """End-to-end: a client emits a span with ``session_id=X`` and a
    goldfive ``plan_submitted`` event with ``session_id=X`` over the
    same stream Hello'd as ``Y``. Both MUST land on session X so the
    frontend's one-session-per-adk-web-run rollup has everything.
    Regression guard against a future change that routes spans and
    goldfive events via different codepaths.
    """
    from harmonograf_server.pb import types_pb2

    pipe, bus, _ = pipeline
    hello_sid = "sess_home"
    adk_sid = "sess_adk_run"
    ctx = _stream_ctx(session_id=hello_sid)

    # 1. Span with per-span session_id=X (the telemetry_plugin's
    #    ``_stamp_session_id`` path after harmonograf#63).
    span_msg = telemetry_pb2.SpanStart(
        span=types_pb2.Span(
            id="span-1",
            session_id=adk_sid,
            agent_id=ctx.agent_id,
            kind=types_pb2.SPAN_KIND_INVOCATION,
            status=types_pb2.SPAN_STATUS_RUNNING,
            name="invocation",
        )
    )
    span_msg.span.start_time.seconds = 100
    await pipe.handle_message(ctx, telemetry_pb2.TelemetryUp(span_start=span_msg))

    # 2. Goldfive event with per-event session_id=X (goldfive#155 wire
    #    stamping from Runner / Steerer / Executors).
    gf_evt = _make_event()
    gf_evt.session_id = adk_sid
    gf_evt.plan_submitted.plan.CopyFrom(_plan_pb(plan_id="p-shared"))
    await pipe.handle_message(ctx, _wrap(gf_evt))

    # Span lands on the ADK session.
    spans = await store.get_spans(adk_sid)
    assert [s.id for s in spans] == ["span-1"]

    # Plan lands on the ADK session.
    plans = await store.list_task_plans_for_session(adk_sid)
    assert [p.id for p in plans] == ["p-shared"]
    assert plans[0].session_id == adk_sid

    # Nothing leaked onto the Hello session.
    assert await store.get_spans(hello_sid) == []
    assert await store.list_task_plans_for_session(hello_sid) == []


@pytest.mark.asyncio
async def test_goldfive_event_same_session_as_hello_uses_hello_ctx(
    pipeline, store
):
    """``session_id`` equal to the Hello session is a no-op on the routing
    path — we must not spuriously auto-create or duplicate state. Regression
    guard against a future edit that always spawns a _SessionView even when
    it would add no value.
    """
    pipe, bus, _ = pipeline
    hello_sid = "sess_home"
    ctx = _stream_ctx(session_id=hello_sid)
    await _ensure_session(store, session_id=hello_sid)

    sub = await bus.subscribe(hello_sid)

    evt = _make_event()
    evt.session_id = hello_sid  # stamped to the same session the stream is on
    evt.plan_submitted.plan.CopyFrom(_plan_pb(plan_id="p-same"))
    await pipe.handle_message(ctx, _wrap(evt))

    # Plan landed on the Hello session exactly as if session_id had been empty.
    plans = await store.list_task_plans_for_session(hello_sid)
    assert [p.id for p in plans] == ["p-same"]
    delta = sub.queue.get_nowait()
    assert delta.kind == DELTA_TASK_PLAN


@pytest.mark.asyncio
async def test_task_index_repopulates_from_store_after_restart(pipeline, store):
    """A pipeline restart drops ``_task_index``; a subsequent task event
    must still resolve the plan via a store scan."""

    pipe, bus, _ = pipeline
    await _ensure_session(store)

    plan_evt = _make_event()
    plan_evt.plan_submitted.plan.CopyFrom(_plan_pb(plan_id="p-restart"))
    await pipe.handle_message(_stream_ctx(), _wrap(plan_evt))

    # Simulate restart: wipe the in-memory index.
    pipe._task_index.clear()

    started = _make_event(sequence=1)
    started.task_started.task_id = "t1"
    await pipe.handle_message(_stream_ctx(), _wrap(started))

    plan = await store.get_task_plan("p-restart")
    t1 = next(t for t in plan.tasks if t.id == "t1")
    assert t1.status == TaskStatus.RUNNING
    # And the index got rebuilt from the store scan.
    assert pipe._task_index["sess_gf"]["t1"] == "p-restart"


# ---- registry-dispatch observability events (goldfive 2986775+) ----------
#
# These three events are pure observability: the server forwards them to
# subscribed frontends and does NOT mutate persisted state. The frontend
# uses delegation_observed to render cross-agent edges on the Gantt and
# the agent_invocation_* pair as an optional per-invocation timeline.
# A prior gap caused the server to drop them as "unknown payload"; the
# tests below lock in that the dispatch wires them through to the bus.


@pytest.mark.asyncio
async def test_agent_invocation_started_forwards_to_bus(pipeline):
    pipe, bus, _ = pipeline
    sub = await bus.subscribe("sess_gf")
    evt = _make_event()
    evt.agent_invocation_started.agent_name = "researcher"
    evt.agent_invocation_started.task_id = "t1"
    evt.agent_invocation_started.invocation_id = "inv-1"
    evt.agent_invocation_started.parent_invocation_id = ""
    await pipe.handle_message(_stream_ctx(), _wrap(evt))
    delta = sub.queue.get_nowait()
    assert delta.kind == DELTA_AGENT_INVOCATION_STARTED
    assert delta.payload["agent_name"] == "researcher"
    assert delta.payload["task_id"] == "t1"
    assert delta.payload["invocation_id"] == "inv-1"
    assert delta.payload["parent_invocation_id"] == ""


@pytest.mark.asyncio
async def test_agent_invocation_completed_forwards_to_bus(pipeline):
    pipe, bus, _ = pipeline
    sub = await bus.subscribe("sess_gf")
    evt = _make_event(sequence=1)
    evt.agent_invocation_completed.agent_name = "researcher"
    evt.agent_invocation_completed.task_id = "t1"
    evt.agent_invocation_completed.invocation_id = "inv-1"
    evt.agent_invocation_completed.summary = "found 3 reports"
    await pipe.handle_message(_stream_ctx(), _wrap(evt))
    delta = sub.queue.get_nowait()
    assert delta.kind == DELTA_AGENT_INVOCATION_COMPLETED
    assert delta.payload["agent_name"] == "researcher"
    assert delta.payload["invocation_id"] == "inv-1"
    assert delta.payload["summary"] == "found 3 reports"


@pytest.mark.asyncio
async def test_delegation_observed_forwards_to_bus(pipeline):
    pipe, bus, _ = pipeline
    sub = await bus.subscribe("sess_gf")
    evt = _make_event()
    evt.delegation_observed.from_agent = "coordinator"
    evt.delegation_observed.to_agent = "researcher"
    evt.delegation_observed.task_id = "t1"
    evt.delegation_observed.invocation_id = "inv-1"
    await pipe.handle_message(_stream_ctx(), _wrap(evt))
    delta = sub.queue.get_nowait()
    assert delta.kind == DELTA_DELEGATION_OBSERVED
    assert delta.payload["from_agent"] == "coordinator"
    assert delta.payload["to_agent"] == "researcher"
    assert delta.payload["task_id"] == "t1"
    assert delta.payload["invocation_id"] == "inv-1"


@pytest.mark.asyncio
async def test_registry_events_do_not_mutate_storage(pipeline, store):
    """The three observability events must not touch persisted plan/task
    state — they're forward-only. Regression guard against a future edit
    that accidentally binds them to _apply_goldfive_task_status or similar.
    """
    pipe, bus, _ = pipeline
    await _ensure_session(store)
    plan_evt = _make_event()
    plan_evt.plan_submitted.plan.CopyFrom(_plan_pb(plan_id="p-obs"))
    await pipe.handle_message(_stream_ctx(), _wrap(plan_evt))

    # Baseline plan row.
    before = await store.get_task_plan("p-obs")
    assert before is not None
    status_before = [(t.id, t.status) for t in before.tasks]

    # Fire all three observability events.
    started = _make_event(sequence=1)
    started.agent_invocation_started.agent_name = "agent_gf"
    started.agent_invocation_started.task_id = "t1"
    started.agent_invocation_started.invocation_id = "inv-x"
    await pipe.handle_message(_stream_ctx(), _wrap(started))

    completed = _make_event(sequence=2)
    completed.agent_invocation_completed.agent_name = "agent_gf"
    completed.agent_invocation_completed.task_id = "t1"
    completed.agent_invocation_completed.invocation_id = "inv-x"
    await pipe.handle_message(_stream_ctx(), _wrap(completed))

    delegation = _make_event(sequence=3)
    delegation.delegation_observed.from_agent = "agent_gf"
    delegation.delegation_observed.to_agent = "sub_agent"
    delegation.delegation_observed.task_id = "t1"
    await pipe.handle_message(_stream_ctx(), _wrap(delegation))

    after = await store.get_task_plan("p-obs")
    assert after is not None
    status_after = [(t.id, t.status) for t in after.tasks]
    assert status_before == status_after
