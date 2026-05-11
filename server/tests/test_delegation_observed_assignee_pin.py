"""Observational assignee pin via ``DelegationObserved`` (harmonograf#261).

goldfive#259's observational pin updates ``session.plan`` so
``report_task_*`` resolves at runtime but explicitly does NOT emit a
``PlanRevised`` (pinning is not a revision). That left the persisted
``tasks.assignee_agent_id`` empty for every row whose plan was installed
at ``plan_submitted`` time — the live e2e on 2026-05-11 showed every
pothos task in the DB with ``assignee=-`` even though the agent
bindings had succeeded and the lifecycle transitions all landed.

The fix (harmonograf-side, no goldfive proto changes needed because
``DelegationObserved`` already carries ``task_id`` + ``to_agent`` on
the wire) is to stamp ``tasks.assignee_agent_id`` when a
``delegation_observed`` event lands.

These tests run against both the memory and sqlite backends — the
ingest-layer stamp is shared, so every store backend inherits it, and
the parametrize asserts that contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from goldfive.pb.goldfive.v1 import events_pb2 as ge
from goldfive.pb.goldfive.v1 import types_pb2 as gt

from harmonograf_server.bus import (
    DELTA_TASK_STATUS,
    SessionBus,
)
from harmonograf_server.ingest import IngestPipeline, StreamContext
from harmonograf_server.pb import telemetry_pb2
from harmonograf_server.storage import (
    Session,
    SessionStatus,
    make_store,
)


# ---- fixtures -------------------------------------------------------------


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def store(request, tmp_path: Path):
    """Memory + sqlite parametrize so the pin path is verified at the
    storage layer for every backend (mirrors ``test_dry_run_plan_revisions``).
    """
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


@pytest_asyncio.fixture
async def pipeline(store):
    bus = SessionBus()
    pipe = IngestPipeline(store, bus, now_fn=lambda: 1_000_000.0)
    yield pipe, bus, store


def _stream_ctx(
    session_id: str = "sess_pin", agent_id: str = "agent_pin"
) -> StreamContext:
    return StreamContext(
        stream_id="str_pin",
        agent_id=agent_id,
        session_id=session_id,
        connected_at=1000.0,
        last_heartbeat=1000.0,
        seen_routes={(session_id, agent_id)},
    )


def _wrap(event: ge.Event) -> telemetry_pb2.TelemetryUp:
    return telemetry_pb2.TelemetryUp(goldfive_event=event)


async def _ensure_session(store, session_id: str = "sess_pin") -> None:
    await store.create_session(
        Session(
            id=session_id,
            title=session_id,
            created_at=1.0,
            status=SessionStatus.LIVE,
        )
    )


def _make_event(
    *, run_id: str = "run-1", sequence: int = 0, event_id: str | None = None
) -> ge.Event:
    evt = ge.Event()
    if event_id is None:
        import uuid as _uuid

        evt.event_id = f"{run_id}:{sequence}:{_uuid.uuid4().hex[:8]}"
    else:
        evt.event_id = event_id
    evt.run_id = run_id
    evt.sequence = sequence
    return evt


def _plan_pb(
    plan_id: str = "plan-PIN",
    run_id: str = "run-1",
    task_ids: list[str] | None = None,
) -> gt.Plan:
    """Mimic the wire shape goldfive emits at plan_submitted time: an
    install with empty ``assignee_agent_id`` on each task. The pin's
    job is to fill that in later from a ``DelegationObserved`` event.
    """
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
        # Empty assignee_agent_id at install — this is the gap the pin
        # closes. Don't pre-stamp.
    return plan


# ---- storage helper unit tests --------------------------------------------


@pytest.mark.asyncio
async def test_update_task_assignee_stamps_row(pipeline, store):
    """Direct unit test on the new storage helper: install a plan with
    empty assignees, call ``update_task_assignee`` for one task, read
    the plan back, confirm only that task's assignee mutated.
    """
    pipe, _, _ = pipeline
    await _ensure_session(store)

    e = _make_event()
    e.plan_submitted.plan.CopyFrom(
        _plan_pb(plan_id="plan-stamp", task_ids=["t1", "t2"])
    )
    await pipe.handle_message(_stream_ctx(), _wrap(e))

    baseline = await store.get_task_plan("plan-stamp")
    assert baseline is not None
    assert all(t.assignee_agent_id == "" for t in baseline.tasks)

    updated = await store.update_task_assignee(
        "plan-stamp", "t1", "client-x:researcher"
    )
    assert updated is not None
    assert updated.id == "t1"
    assert updated.assignee_agent_id == "client-x:researcher"

    refetched = await store.get_task_plan("plan-stamp")
    assert refetched is not None
    by_id = {t.id: t for t in refetched.tasks}
    assert by_id["t1"].assignee_agent_id == "client-x:researcher"
    # t2 untouched — the stamp is per-task, not per-plan.
    assert by_id["t2"].assignee_agent_id == ""


@pytest.mark.asyncio
async def test_update_task_assignee_idempotent(pipeline, store):
    """Re-applying the same assignee is a no-op that still returns the
    current row. Guards against duplicate ``DelegationObserved`` events
    (retries / replay) clobbering anything.
    """
    pipe, _, _ = pipeline
    await _ensure_session(store)

    e = _make_event()
    e.plan_submitted.plan.CopyFrom(_plan_pb(plan_id="plan-idem"))
    await pipe.handle_message(_stream_ctx(), _wrap(e))

    first = await store.update_task_assignee("plan-idem", "t1", "agent_a")
    assert first is not None
    assert first.assignee_agent_id == "agent_a"

    second = await store.update_task_assignee("plan-idem", "t1", "agent_a")
    assert second is not None
    assert second.assignee_agent_id == "agent_a"


@pytest.mark.asyncio
async def test_update_task_assignee_unknown_returns_none(store):
    """Unknown plan or unknown task → return None (caller logs + bails).
    Critical for the ingest path: a delegation event for a task we
    haven't indexed yet (cold start / racing plan_submitted) must not
    raise.
    """
    res = await store.update_task_assignee("does-not-exist", "t1", "agent")
    assert res is None


@pytest.mark.asyncio
async def test_update_task_assignee_preserves_status_and_bind(
    pipeline, store
):
    """The new helper must NOT touch ``status`` / ``bound_span_id`` /
    ``cancel_reason`` — those are owned by ``update_task_status``. Race
    safety: a delegation event landing AFTER ``task_started`` (or after
    ``task_completed``) must not regress the lifecycle.
    """
    from harmonograf_server.storage import TaskStatus

    pipe, _, _ = pipeline
    await _ensure_session(store)

    e = _make_event()
    e.plan_submitted.plan.CopyFrom(_plan_pb(plan_id="plan-pres"))
    await pipe.handle_message(_stream_ctx(), _wrap(e))

    # Walk the task forward to COMPLETED with a bound span first.
    await store.update_task_status(
        "plan-pres", "t1", TaskStatus.RUNNING, bound_span_id="span-A"
    )
    await store.update_task_status(
        "plan-pres", "t1", TaskStatus.COMPLETED
    )

    # Now stamp the assignee (late-arriving delegation event).
    updated = await store.update_task_assignee(
        "plan-pres", "t1", "client-x:agent"
    )
    assert updated is not None
    assert updated.assignee_agent_id == "client-x:agent"
    assert updated.status == TaskStatus.COMPLETED
    assert updated.bound_span_id == "span-A"


# ---- ingest-layer integration: DelegationObserved → DB stamp -------------


@pytest.mark.asyncio
async def test_delegation_observed_stamps_assignee_into_tasks_row(
    pipeline, store
):
    """Headline contract: a ``DelegationObserved`` event with a
    ``(task_id, to_agent)`` pair mutates ``tasks.assignee_agent_id``
    for the matching ``(plan_id, task_id)`` row. The DB-backed SELECT
    that the harmonograf UI runs against now matches the actual
    runtime binding goldfive's observational pin made.
    """
    pipe, _, _ = pipeline
    await _ensure_session(store)

    # 1. plan_submitted — install with empty assignees (mirrors the live
    #    bug: planner emits a tree-shaped plan; pin happens later).
    e1 = _make_event(sequence=0)
    e1.plan_submitted.plan.CopyFrom(
        _plan_pb(plan_id="plan-D", task_ids=["t1", "t2"])
    )
    await pipe.handle_message(_stream_ctx(), _wrap(e1))

    pre = await store.get_task_plan("plan-D")
    assert pre is not None
    assert {t.id: t.assignee_agent_id for t in pre.tasks} == {
        "t1": "",
        "t2": "",
    }

    # 2. delegation_observed — coordinator delegates t1 to the researcher.
    e2 = _make_event(sequence=1)
    e2.delegation_observed.from_agent = "client-x:coordinator"
    e2.delegation_observed.to_agent = "client-x:researcher"
    e2.delegation_observed.task_id = "t1"
    e2.delegation_observed.invocation_id = "inv-r1"
    e2.delegation_observed.observed_at.seconds = 1_000_010
    await pipe.handle_message(_stream_ctx(), _wrap(e2))

    post = await store.get_task_plan("plan-D")
    assert post is not None
    by_id = {t.id: t.assignee_agent_id for t in post.tasks}
    assert by_id["t1"] == "client-x:researcher"
    # t2 is untouched — only the delegated task gets stamped.
    assert by_id["t2"] == ""


@pytest.mark.asyncio
async def test_delegation_observed_without_task_id_does_not_stamp(
    pipeline, store
):
    """Some delegations are orphan (cross-agent A2A handoff with no
    specific bound task). They carry empty ``task_id`` and must not
    raise / scan / stamp anything — the existing publish-on-bus path
    still runs unchanged.
    """
    pipe, _, _ = pipeline
    await _ensure_session(store)

    e1 = _make_event(sequence=0)
    e1.plan_submitted.plan.CopyFrom(
        _plan_pb(plan_id="plan-orphan", task_ids=["t1"])
    )
    await pipe.handle_message(_stream_ctx(), _wrap(e1))

    e2 = _make_event(sequence=1)
    e2.delegation_observed.from_agent = "client-x:coordinator"
    e2.delegation_observed.to_agent = "client-x:researcher"
    # task_id intentionally left empty
    e2.delegation_observed.invocation_id = "inv-orphan"
    await pipe.handle_message(_stream_ctx(), _wrap(e2))

    post = await store.get_task_plan("plan-orphan")
    assert post is not None
    assert post.tasks[0].assignee_agent_id == ""


@pytest.mark.asyncio
async def test_delegation_observed_unknown_task_is_silent(pipeline, store):
    """Race: a delegation event references a ``task_id`` we never saw
    on this session (no matching ``plan_submitted`` yet). The handler
    logs at DEBUG and bails — it MUST NOT raise into the dispatch loop
    (one bad event would tear down the stream).
    """
    pipe, _, _ = pipeline
    await _ensure_session(store)

    e = _make_event(sequence=0)
    e.delegation_observed.from_agent = "client-x:a"
    e.delegation_observed.to_agent = "client-x:b"
    e.delegation_observed.task_id = "unknown-task"
    # No prior plan — this is the cold-start race.
    await pipe.handle_message(_stream_ctx(), _wrap(e))
    # Reaching here at all is the assertion: no exception, no panic.


@pytest.mark.asyncio
async def test_delegation_observed_publishes_task_status_on_stamp(
    pipeline, store
):
    """Live observability: after stamping the DB, the pipeline must
    republish the task on the bus so live subscribers (Gantt /
    Trajectory) refresh the task card with the new assignee. Without
    this, the only way to see the fresh assignee on a connected
    frontend is a full reconnect / reload.
    """
    pipe, bus, _ = pipeline
    await _ensure_session(store)

    e1 = _make_event(sequence=0)
    e1.plan_submitted.plan.CopyFrom(
        _plan_pb(plan_id="plan-pub", task_ids=["t1"])
    )
    await pipe.handle_message(_stream_ctx(), _wrap(e1))

    # Subscribe AFTER install so we only see the post-stamp deltas.
    import asyncio

    sub = await bus.subscribe("sess_pin")

    e2 = _make_event(sequence=1)
    e2.delegation_observed.from_agent = "client-x:coord"
    e2.delegation_observed.to_agent = "client-x:worker"
    e2.delegation_observed.task_id = "t1"
    e2.delegation_observed.invocation_id = "inv-pub"
    await pipe.handle_message(_stream_ctx(), _wrap(e2))

    # Drain the queue, looking for a TASK_STATUS delta on plan-pub.
    task_status_deltas = []
    deadline = asyncio.get_event_loop().time() + 2.0
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        try:
            delta = await asyncio.wait_for(sub.queue.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if (
            delta.kind == DELTA_TASK_STATUS
            and isinstance(delta.payload, dict)
            and delta.payload.get("plan_id") == "plan-pub"
        ):
            task_status_deltas.append(delta)
            break

    await bus.unsubscribe(sub)
    assert task_status_deltas, "expected a publish_task_status after stamp"
    stamped = task_status_deltas[-1].payload["task"]
    assert stamped.assignee_agent_id == "client-x:worker"


# ---- end-to-end: replay the live-bug sequence ----------------------------


@pytest.mark.asyncio
async def test_install_then_delegation_then_lifecycle_propagates_assignee(
    pipeline, store
):
    """Replay the 2026-05-11 pothos session shape:

        1. plan_submitted with empty assignees
        2. delegation_observed binds t1 → researcher
        3. task_started → task_completed for t1
        4. delegation_observed binds t2 → drafter
        5. task_started → task_failed for t2 (mirrors create_slides FAIL)

    Final state in tasks: t1 has researcher + COMPLETED, t2 has
    drafter + FAILED. Both rows reflect the bind, not the empty
    assignee from plan_submitted.
    """
    from harmonograf_server.storage import TaskStatus

    pipe, _, _ = pipeline
    await _ensure_session(store)

    # 1. install
    e = _make_event(sequence=0)
    e.plan_submitted.plan.CopyFrom(
        _plan_pb(plan_id="plan-e2e", task_ids=["t1", "t2"])
    )
    await pipe.handle_message(_stream_ctx(), _wrap(e))

    # 2. delegation → t1 ↦ researcher
    e = _make_event(sequence=1)
    e.delegation_observed.from_agent = "client-x:coord"
    e.delegation_observed.to_agent = "client-x:researcher"
    e.delegation_observed.task_id = "t1"
    e.delegation_observed.invocation_id = "inv-r"
    await pipe.handle_message(_stream_ctx(), _wrap(e))

    # 3. task_started / task_completed for t1
    e = _make_event(sequence=2)
    e.task_started.task_id = "t1"
    await pipe.handle_message(_stream_ctx(), _wrap(e))
    e = _make_event(sequence=3)
    e.task_completed.task_id = "t1"
    e.task_completed.summary = "research done"
    await pipe.handle_message(_stream_ctx(), _wrap(e))

    # 4. delegation → t2 ↦ drafter
    e = _make_event(sequence=4)
    e.delegation_observed.from_agent = "client-x:coord"
    e.delegation_observed.to_agent = "client-x:drafter"
    e.delegation_observed.task_id = "t2"
    e.delegation_observed.invocation_id = "inv-d"
    await pipe.handle_message(_stream_ctx(), _wrap(e))

    # 5. task_started / task_failed for t2
    e = _make_event(sequence=5)
    e.task_started.task_id = "t2"
    await pipe.handle_message(_stream_ctx(), _wrap(e))
    e = _make_event(sequence=6)
    e.task_failed.task_id = "t2"
    e.task_failed.reason = "slide_render_error"
    await pipe.handle_message(_stream_ctx(), _wrap(e))

    final = await store.get_task_plan("plan-e2e")
    assert final is not None
    by_id = {t.id: t for t in final.tasks}
    assert by_id["t1"].assignee_agent_id == "client-x:researcher"
    assert by_id["t1"].status == TaskStatus.COMPLETED
    assert by_id["t2"].assignee_agent_id == "client-x:drafter"
    assert by_id["t2"].status == TaskStatus.FAILED
    assert by_id["t2"].cancel_reason == "slide_render_error"


@pytest.mark.asyncio
async def test_delegation_after_task_completed_still_stamps(
    pipeline, store
):
    """Stream-ordering edge case: a delegation event lands AFTER the
    task has already transitioned to a terminal status. The stamp
    must still apply (so the SELECT shows the correct binding); the
    status / cancel_reason must NOT regress.
    """
    from harmonograf_server.storage import TaskStatus

    pipe, _, _ = pipeline
    await _ensure_session(store)

    e = _make_event(sequence=0)
    e.plan_submitted.plan.CopyFrom(
        _plan_pb(plan_id="plan-late", task_ids=["t1"])
    )
    await pipe.handle_message(_stream_ctx(), _wrap(e))

    e = _make_event(sequence=1)
    e.task_started.task_id = "t1"
    await pipe.handle_message(_stream_ctx(), _wrap(e))
    e = _make_event(sequence=2)
    e.task_completed.task_id = "t1"
    e.task_completed.summary = "done"
    await pipe.handle_message(_stream_ctx(), _wrap(e))

    # Late delegation — arrives after COMPLETED.
    e = _make_event(sequence=3)
    e.delegation_observed.from_agent = "client-x:coord"
    e.delegation_observed.to_agent = "client-x:worker"
    e.delegation_observed.task_id = "t1"
    e.delegation_observed.invocation_id = "inv-late"
    await pipe.handle_message(_stream_ctx(), _wrap(e))

    final = await store.get_task_plan("plan-late")
    assert final is not None
    t1 = final.tasks[0]
    assert t1.assignee_agent_id == "client-x:worker"
    assert t1.status == TaskStatus.COMPLETED
