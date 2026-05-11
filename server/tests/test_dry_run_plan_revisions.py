"""Dry-run ``PlanRevised`` ingest gate (harmonograf#257 / goldfive#379).

When goldfive runs in ``SteeringConfig.observation_only`` mode it emits
``PlanRevised`` envelopes with ``dry_run=True`` to surface a
"would-have-applied" preview of the refine the planner produced. The
three injection points on the goldfive side (``session.plan`` mutation,
``GOLDFIVE_STEER`` enqueue, ``request_invocation_cancel``) are SKIPPED
for those revisions — they're advisory annotations, not authoritative
plan state.

The harmonograf ingest path must mirror that intent: dry-run revisions
must NOT upsert into ``task_plans`` (the latest-snapshot, by-id table)
or into the per-revision ``task_plan_revisions`` sibling. The wire
envelope still lands in ``goldfive_events`` for observability /
replay — operators can reconstruct what goldfive would have suggested
by replaying the event stream.

These tests run against both the memory and sqlite backends to guard
against per-backend regressions (the gate lives at the ingest layer, so
every store inherits it; the parametrize asserts that).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from goldfive.pb.goldfive.v1 import events_pb2 as ge
from goldfive.pb.goldfive.v1 import types_pb2 as gt

from harmonograf_server.bus import SessionBus
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
    """Memory + sqlite parametrize so the gate is verified at the ingest
    layer for every store backend (mirrors ``test_storage_extensive.py``).
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
    session_id: str = "sess_dry", agent_id: str = "agent_dry"
) -> StreamContext:
    return StreamContext(
        stream_id="str_dry",
        agent_id=agent_id,
        session_id=session_id,
        connected_at=1000.0,
        last_heartbeat=1000.0,
        seen_routes={(session_id, agent_id)},
    )


def _wrap(event: ge.Event) -> telemetry_pb2.TelemetryUp:
    return telemetry_pb2.TelemetryUp(goldfive_event=event)


async def _ensure_session(store, session_id: str = "sess_dry") -> None:
    await store.create_session(
        Session(
            id=session_id,
            title=session_id,
            created_at=1.0,
            status=SessionStatus.LIVE,
        )
    )


def _make_event(*, run_id: str = "run-1", sequence: int = 0, event_id: str | None = None) -> ge.Event:
    """Mirror the helper in ``test_goldfive_ingest.py``: synthesize a
    unique ``event_id`` per envelope so the goldfive_events UNIQUE index
    doesn't collapse otherwise-distinct events.
    """
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
    plan_id: str = "plan-DR",
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


# ---- regression guard: real (non-dry-run) PlanRevised still upserts -------


@pytest.mark.asyncio
async def test_real_plan_revised_upserts_into_task_plans(pipeline, store):
    """Baseline: ``dry_run=False`` (or unset) PlanRevised continues to
    upsert ``task_plans`` and write to ``task_plan_revisions``. Guards
    against a too-eager gate that drops every revision.
    """
    pipe, _, _ = pipeline
    await _ensure_session(store)

    # Initial install.
    initial = _make_event()
    initial.plan_submitted.plan.CopyFrom(_plan_pb(plan_id="plan-real"))
    await pipe.handle_message(_stream_ctx(), _wrap(initial))

    # Real revision — dry_run left at the proto default (False).
    revised = _make_event(sequence=1)
    revised_plan = _plan_pb(plan_id="plan-real", task_ids=["t1", "t2", "t3"])
    revised_plan.revision_index = 1
    revised.plan_revised.plan.CopyFrom(revised_plan)
    revised.plan_revised.drift_kind = gt.DRIFT_KIND_TOOL_ERROR
    revised.plan_revised.severity = gt.DRIFT_SEVERITY_WARNING
    revised.plan_revised.reason = "real revision"
    revised.plan_revised.revision_index = 1
    assert revised.plan_revised.dry_run is False
    await pipe.handle_message(_stream_ctx(), _wrap(revised))

    stored = await store.get_task_plan("plan-real")
    assert stored is not None
    assert stored.revision_index == 1
    assert [t.id for t in stored.tasks] == ["t1", "t2", "t3"]
    assert stored.revision_reason == "real revision"
    assert stored.revision_kind == "tool_error"

    rows = await store.list_task_plan_revisions_for_session("sess_dry")
    assert [r.revision_index for r in rows] == [0, 1]


# ---- core fix: dry-run PlanRevised does NOT upsert task_plans -------------


@pytest.mark.asyncio
async def test_dry_run_plan_revised_does_not_upsert_task_plans(
    pipeline, store
):
    """The headline contract: a PlanRevised with ``dry_run=True`` must
    not clobber the authoritative ``task_plans`` row. The prior plan
    state (the install) remains exactly as written.
    """
    pipe, _, _ = pipeline
    await _ensure_session(store)

    # Real install — the authoritative snapshot we must preserve.
    initial = _make_event()
    initial.plan_submitted.plan.CopyFrom(
        _plan_pb(plan_id="plan-dr", task_ids=["t1", "t2"])
    )
    await pipe.handle_message(_stream_ctx(), _wrap(initial))

    baseline = await store.get_task_plan("plan-dr")
    assert baseline is not None
    assert [t.id for t in baseline.tasks] == ["t1", "t2"]
    assert int(baseline.revision_index or 0) == 0

    # Dry-run revision — goldfive ran the planner in observation_only mode
    # and produced this preview, but did NOT mutate session.plan.
    revised = _make_event(sequence=1)
    revised_plan = _plan_pb(
        plan_id="plan-dr", task_ids=["t1", "t2", "t3", "t4"]
    )
    revised_plan.revision_index = 1
    revised.plan_revised.plan.CopyFrom(revised_plan)
    revised.plan_revised.drift_kind = gt.DRIFT_KIND_LOOPING_REASONING
    revised.plan_revised.severity = gt.DRIFT_SEVERITY_WARNING
    revised.plan_revised.reason = "would have added t3,t4"
    revised.plan_revised.revision_index = 1
    revised.plan_revised.dry_run = True
    await pipe.handle_message(_stream_ctx(), _wrap(revised))

    # task_plans must still hold the install (t1, t2 / revision 0), not
    # the dry-run preview.
    after = await store.get_task_plan("plan-dr")
    assert after is not None
    assert [t.id for t in after.tasks] == ["t1", "t2"]
    assert int(after.revision_index or 0) == 0
    assert (after.revision_reason or "") == ""
    assert (after.revision_kind or "") == ""


@pytest.mark.asyncio
async def test_dry_run_plan_revised_does_not_write_revision_sibling(
    pipeline, store
):
    """The sibling ``task_plan_revisions`` table must also stay clean —
    a future plan-history view should not surface dry-run previews as
    if they were real revisions. Operators can still read the raw
    envelope from ``goldfive_events`` (covered by the next test).
    """
    pipe, _, _ = pipeline
    await _ensure_session(store)

    initial = _make_event()
    initial.plan_submitted.plan.CopyFrom(_plan_pb(plan_id="plan-sib"))
    await pipe.handle_message(_stream_ctx(), _wrap(initial))

    revised = _make_event(sequence=1)
    rp = _plan_pb(plan_id="plan-sib", task_ids=["t1", "t2", "t3"])
    rp.revision_index = 1
    revised.plan_revised.plan.CopyFrom(rp)
    revised.plan_revised.drift_kind = gt.DRIFT_KIND_TOOL_ERROR
    revised.plan_revised.revision_index = 1
    revised.plan_revised.dry_run = True
    await pipe.handle_message(_stream_ctx(), _wrap(revised))

    rows = await store.list_task_plan_revisions_for_session("sess_dry")
    # Only the install's revision-zero row should be present.
    assert [r.revision_index for r in rows] == [0]


@pytest.mark.asyncio
async def test_dry_run_plan_revised_still_persists_goldfive_event(
    pipeline, store
):
    """Observability invariant: even though the dry-run revision skips
    derived-state writes, the raw envelope STILL lands in
    ``goldfive_events``. Replay / audit must be able to reconstruct
    what goldfive would have suggested.
    """
    pipe, _, _ = pipeline
    await _ensure_session(store)

    initial = _make_event()
    initial.plan_submitted.plan.CopyFrom(_plan_pb(plan_id="plan-obs"))
    await pipe.handle_message(_stream_ctx(), _wrap(initial))

    revised = _make_event(sequence=1, event_id="evt-dry-1")
    rp = _plan_pb(plan_id="plan-obs", task_ids=["t1", "t2", "t3"])
    rp.revision_index = 1
    revised.plan_revised.plan.CopyFrom(rp)
    revised.plan_revised.drift_kind = gt.DRIFT_KIND_LOOPING_REASONING
    revised.plan_revised.revision_index = 1
    revised.plan_revised.reason = "would have refined"
    revised.plan_revised.dry_run = True
    await pipe.handle_message(_stream_ctx(), _wrap(revised))

    events = await store.list_goldfive_events("sess_dry", kind="plan_revised")
    assert len(events) == 1
    parsed = ge.Event()
    parsed.ParseFromString(events[0].payload_bytes)
    assert parsed.plan_revised.dry_run is True
    assert parsed.plan_revised.reason == "would have refined"
    assert parsed.plan_revised.plan.id == "plan-obs"


# ---- the full e2e sequence the bug-report describes ----------------------


@pytest.mark.asyncio
async def test_install_dryrun_real_sequence_skips_only_middle(
    pipeline, store
):
    """The live observation_only scenario:

        1. real install        (plan_submitted)
        2. dry-run refine      (plan_revised, dry_run=True)
        3. real refine         (plan_revised, dry_run=False)

    After all three: ``task_plans`` reflects the install + real refine
    (revision 0 → revision 2). The dry-run row is absent from
    ``task_plan_revisions`` so the plan-history view doesn't render
    a fake revision between them.
    """
    pipe, _, _ = pipeline
    await _ensure_session(store)

    # 1. install — revision 0, tasks [t1, t2]
    e1 = _make_event(sequence=0)
    e1.plan_submitted.plan.CopyFrom(
        _plan_pb(plan_id="plan-seq", task_ids=["t1", "t2"])
    )
    await pipe.handle_message(_stream_ctx(), _wrap(e1))

    # 2. dry-run preview — would have added t3
    e2 = _make_event(sequence=1)
    p2 = _plan_pb(plan_id="plan-seq", task_ids=["t1", "t2", "t3"])
    p2.revision_index = 1
    e2.plan_revised.plan.CopyFrom(p2)
    e2.plan_revised.drift_kind = gt.DRIFT_KIND_LOOPING_REASONING
    e2.plan_revised.severity = gt.DRIFT_SEVERITY_WARNING
    e2.plan_revised.reason = "dry-run preview"
    e2.plan_revised.revision_index = 1
    e2.plan_revised.dry_run = True
    await pipe.handle_message(_stream_ctx(), _wrap(e2))

    # 3. real refine — observation_only later disabled; tasks now t1..t4
    e3 = _make_event(sequence=2)
    p3 = _plan_pb(plan_id="plan-seq", task_ids=["t1", "t2", "t3", "t4"])
    p3.revision_index = 2
    e3.plan_revised.plan.CopyFrom(p3)
    e3.plan_revised.drift_kind = gt.DRIFT_KIND_TOOL_ERROR
    e3.plan_revised.severity = gt.DRIFT_SEVERITY_CRITICAL
    e3.plan_revised.reason = "actual revision"
    e3.plan_revised.revision_index = 2
    e3.plan_revised.dry_run = False
    await pipe.handle_message(_stream_ctx(), _wrap(e3))

    # task_plans should hold the real refine (revision 2), not the dry-run.
    final = await store.get_task_plan("plan-seq")
    assert final is not None
    assert int(final.revision_index or 0) == 2
    assert [t.id for t in final.tasks] == ["t1", "t2", "t3", "t4"]
    assert final.revision_reason == "actual revision"
    assert final.revision_kind == "tool_error"

    # task_plan_revisions: install (0) and real refine (2) — NO row at
    # index 1 from the dry-run preview.
    rows = await store.list_task_plan_revisions_for_session("sess_dry")
    indices = sorted(r.revision_index for r in rows)
    assert indices == [0, 2]

    # And all three envelopes are still on disk in goldfive_events for
    # observability / replay.
    submitted = await store.list_goldfive_events(
        "sess_dry", kind="plan_submitted"
    )
    revised = await store.list_goldfive_events(
        "sess_dry", kind="plan_revised"
    )
    assert len(submitted) == 1
    assert len(revised) == 2
