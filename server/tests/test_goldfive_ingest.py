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
async def test_run_completed_and_aborted_publish_deltas(pipeline):
    pipe, bus, _ = pipeline
    sub = await bus.subscribe("sess_gf")
    completed = _make_event(event_id="e-c")
    completed.run_completed.outcome_summary = "done"
    aborted = _make_event(event_id="e-a", sequence=1)
    aborted.run_aborted.reason = "user cancel"
    await pipe.handle_message(_stream_ctx(), _wrap(completed))
    await pipe.handle_message(_stream_ctx(), _wrap(aborted))
    deltas = [sub.queue.get_nowait(), sub.queue.get_nowait()]
    kinds = [d.kind for d in deltas]
    assert DELTA_RUN_COMPLETED in kinds and DELTA_RUN_ABORTED in kinds
    assert deltas[0].payload["outcome_summary"] == "done"
    assert deltas[1].payload["reason"] == "user cancel"


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
