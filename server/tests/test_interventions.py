"""Aggregator + RPC coverage for ``list_interventions`` (issue #69).

The aggregator merges three sources already on the wire:
  1. ``annotations`` — user STEER / HUMAN_RESPONSE rows.
  2. Ingest drift ring — goldfive ``drift_detected`` events.
  3. ``task_plans`` with a non-empty ``revision_kind`` — autonomous
     goldfive revisions (cascade_cancel, refine_retry, …) plus user
     drift kinds.

These tests assert:
  * chronological ordering across all three sources
  * correct source attribution (user / drift / goldfive)
  * outcome attribution — drift followed by plan revision becomes
    ``plan_revised:rN``; drift with trailing cancelled tasks becomes
    ``cascade_cancel:N_tasks``; otherwise ``recorded``
  * annotation_id is populated for user-authored rows
  * the RPC returns the same sequence end-to-end (proto round-trip)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import grpc
import pytest
import pytest_asyncio

from harmonograf_server.bus import SessionBus
from harmonograf_server.control_router import ControlRouter
from harmonograf_server.ingest import IngestPipeline
from harmonograf_server.interventions import (
    InterventionRecord,
    list_interventions,
)
from harmonograf_server.pb import (
    frontend_pb2,
    service_pb2_grpc,
)
from harmonograf_server.rpc.telemetry import TelemetryServicer
from harmonograf_server.storage import (
    Agent,
    AgentStatus,
    Annotation,
    AnnotationKind,
    AnnotationTarget,
    Framework,
    Session,
    SessionStatus,
    Task,
    TaskPlan,
    TaskStatus,
    make_store,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _StubDrifts:
    """Duck-type of ``IngestPipeline.drifts_for_session``."""

    def __init__(self, drifts: dict[str, list[dict]]) -> None:
        self._drifts = drifts

    def drifts_for_session(self, session_id: str) -> list[dict]:
        return list(self._drifts.get(session_id, []))


@pytest_asyncio.fixture
async def store():
    s = make_store("memory")
    await s.start()
    try:
        yield s
    finally:
        await s.close()


async def _seed_session(store, sid: str, created_at: float = 1_000.0) -> None:
    await store.create_session(
        Session(id=sid, title=sid, created_at=created_at, status=SessionStatus.LIVE)
    )


# ---------------------------------------------------------------------------
# Pure aggregator tests
# ---------------------------------------------------------------------------


async def test_list_interventions_merges_annotations_drifts_and_refines(store):
    """The three sources interleave into one chronological list."""

    sid = "sess_merge"
    await _seed_session(store, sid, created_at=100.0)

    # User STEER at t=110 — annotation source.
    await store.put_annotation(
        Annotation(
            id="ann_steer_1",
            session_id=sid,
            target=AnnotationTarget(agent_id="a", time_start=110.0),
            author="alice",
            created_at=110.0,
            kind=AnnotationKind.STEERING,
            body="try a different approach",
        )
    )
    # Drift at t=120 — drift source.
    drifts = _StubDrifts(
        {
            sid: [
                {
                    "run_id": "r1",
                    "kind": "looping_reasoning",
                    "severity": "warning",
                    "detail": "agent re-reading same doc",
                    "current_task_id": "t1",
                    "current_agent_id": "a",
                    "recorded_at": 120.0,
                }
            ]
        }
    )
    # Plan revision at t=122 (within the 5s window) — should attach as
    # outcome of the drift above, not a separate intervention row.
    await store.put_task_plan(
        TaskPlan(
            id="p1",
            session_id=sid,
            created_at=122.0,
            summary="revised plan",
            tasks=[
                Task(
                    id="t1",
                    title="research",
                    description="",
                    assignee_agent_id="a",
                    status=TaskStatus.RUNNING,
                )
            ],
            edges=[],
            revision_reason="drift refine",
            revision_kind="looping_reasoning",
            revision_severity="warning",
            revision_index=2,
        )
    )
    # Autonomous cascade_cancel revision at t=130 — no preceding drift
    # with that kind, so shows as goldfive-sourced intervention.
    await store.put_task_plan(
        TaskPlan(
            id="p2",
            session_id=sid,
            created_at=130.0,
            summary="cascade cancel",
            tasks=[],
            edges=[],
            revision_reason="downstream tasks invalidated",
            revision_kind="cascade_cancel",
            revision_index=3,
        )
    )

    records = await list_interventions(sid, store=store, drifts_provider=drifts)

    assert [r.at for r in records] == sorted(r.at for r in records)
    sources = [r.source for r in records]
    assert sources == ["user", "drift", "goldfive"]
    # User row keeps its annotation id for deep-linking.
    assert records[0].kind == "STEER"
    assert records[0].annotation_id == "ann_steer_1"
    assert records[0].author == "alice"
    # Drift row was attributed to the plan revision — not a bare "recorded".
    assert records[1].kind == "LOOPING_REASONING"
    assert records[1].outcome == "plan_revised:r2"
    assert records[1].plan_revision_index == 2
    # Goldfive row carries its own outcome.
    assert records[2].kind == "CASCADE_CANCEL"
    assert records[2].outcome == "plan_revised:r3"


async def test_user_drift_kind_is_attributed_to_user_source(store):
    """``user_steer`` drift kind → source=user, with STEER label."""

    sid = "sess_user_drift"
    await _seed_session(store, sid)
    drifts = _StubDrifts(
        {
            sid: [
                {
                    "run_id": "r1",
                    "kind": "user_steer",
                    "severity": "info",
                    "detail": "operator steered",
                    "recorded_at": 200.0,
                }
            ]
        }
    )
    records = await list_interventions(sid, store=store, drifts_provider=drifts)
    assert len(records) == 1
    assert records[0].source == "user"
    assert records[0].kind == "STEER"
    assert records[0].drift_kind == "user_steer"


async def test_ordering_across_sources_is_by_timestamp(store):
    """Rows interleave strictly by timestamp, regardless of source."""

    sid = "sess_order"
    await _seed_session(store, sid)
    # Annotation at 300, drift at 100 (in the past), goldfive at 200.
    await store.put_annotation(
        Annotation(
            id="ann_x",
            session_id=sid,
            target=AnnotationTarget(agent_id="a", time_start=300.0),
            author="u",
            created_at=300.0,
            kind=AnnotationKind.STEERING,
            body="b",
        )
    )
    drifts = _StubDrifts(
        {
            sid: [
                {
                    "kind": "tool_error",
                    "severity": "info",
                    "detail": "d",
                    "recorded_at": 100.0,
                }
            ]
        }
    )
    await store.put_task_plan(
        TaskPlan(
            id="pg",
            session_id=sid,
            created_at=200.0,
            summary="",
            tasks=[],
            edges=[],
            revision_reason="",
            revision_kind="refine_retry",
            revision_index=1,
        )
    )
    records = await list_interventions(sid, store=store, drifts_provider=drifts)
    assert [r.source for r in records] == ["drift", "goldfive", "user"]


async def test_drift_without_matching_revision_is_recorded(store):
    """A drift with no follow-on revision is surfaced with outcome=recorded."""

    sid = "sess_recorded"
    await _seed_session(store, sid)
    drifts = _StubDrifts(
        {
            sid: [
                {
                    "kind": "confabulation_risk",
                    "severity": "info",
                    "detail": "",
                    "recorded_at": 400.0,
                }
            ]
        }
    )
    records = await list_interventions(sid, store=store, drifts_provider=drifts)
    assert len(records) == 1
    assert records[0].outcome == "recorded"


async def test_cascade_cancel_counts_cancelled_tasks(store):
    """Drift followed by a plan with CANCELLED tasks → cascade_cancel:N."""

    sid = "sess_cascade"
    await _seed_session(store, sid)
    # Drift at 500; plan at 510 (just outside the 5s revision window) that
    # has cancelled tasks — attribution falls through to cascade_cancel.
    drifts = _StubDrifts(
        {
            sid: [
                {
                    "kind": "runaway_delegation",
                    "severity": "critical",
                    "detail": "",
                    "recorded_at": 500.0,
                }
            ]
        }
    )
    await store.put_task_plan(
        TaskPlan(
            id="p_after",
            session_id=sid,
            created_at=520.0,
            summary="",
            tasks=[
                Task(
                    id=f"t{i}",
                    title="",
                    description="",
                    assignee_agent_id="a",
                    status=TaskStatus.CANCELLED,
                )
                for i in range(3)
            ],
            edges=[],
            # No revision_kind — not a refine, so the drift cannot claim
            # plan_revised and falls through to cascade_cancel.
            revision_reason="",
            revision_kind="",
            revision_index=0,
        )
    )
    records = await list_interventions(sid, store=store, drifts_provider=drifts)
    assert len(records) == 1
    assert records[0].outcome == "cascade_cancel:3_tasks"


async def test_plan_revision_with_matching_drift_is_not_double_counted(store):
    """The drift row owns the refine — no separate plan row emitted."""

    sid = "sess_dedup"
    await _seed_session(store, sid)
    drifts = _StubDrifts(
        {
            sid: [
                {
                    "kind": "agent_refusal",
                    "severity": "warning",
                    "detail": "refused",
                    "recorded_at": 600.0,
                }
            ]
        }
    )
    await store.put_task_plan(
        TaskPlan(
            id="p_ref",
            session_id=sid,
            created_at=601.0,
            summary="",
            tasks=[],
            edges=[],
            revision_reason="",
            revision_kind="agent_refusal",
            revision_index=2,
        )
    )
    records = await list_interventions(sid, store=store, drifts_provider=drifts)
    # Exactly one row — the drift, enriched with plan_revised:r2.
    assert len(records) == 1
    assert records[0].source == "drift"
    assert records[0].outcome == "plan_revised:r2"
    assert records[0].plan_revision_index == 2


async def test_user_steer_annotation_plus_drift_plus_plan_collapse_to_one_card(store):
    """harmonograf#75: one annotation + one USER_STEER drift + one refine → 1 card.

    Before the dedup work, a single user STEER surfaced as three cards
    in the Trajectory view — the annotation row, the USER_STEER drift
    row (tagged with the same annotation_id by goldfive#176), and the
    plan_revised row. All three trace back to the same user event; the
    aggregator must collapse them into one intervention card whose base
    row is the user's annotation (keeping author + body) with the
    drift's severity and the plan_revised outcome folded in.
    """

    sid = "sess_dedup_user"
    await _seed_session(store, sid)

    # 1. User STEER annotation at t=100.
    await store.put_annotation(
        Annotation(
            id="ann_steer_42",
            session_id=sid,
            target=AnnotationTarget(agent_id="a", time_start=100.0),
            author="alice",
            created_at=100.0,
            kind=AnnotationKind.STEERING,
            body="focus on the intro",
        )
    )

    # 2. USER_STEER drift minted by goldfive from the ControlMessage, carrying
    #    the bridge-supplied annotation_id (goldfive#176). Drift time is
    #    after the annotation because the steerer adds a bit of processing
    #    latency before emitting.
    drifts = _StubDrifts(
        {
            sid: [
                {
                    "run_id": "r1",
                    "kind": "user_steer",
                    "severity": "warning",
                    "detail": "by alice: focus on the intro",
                    "annotation_id": "ann_steer_42",
                    "recorded_at": 100.2,
                }
            ]
        }
    )

    # 3. Plan revision produced by the refine path, revision_kind=user_steer,
    #    landing inside the 5s attribution window.
    await store.put_task_plan(
        TaskPlan(
            id="p_rev",
            session_id=sid,
            created_at=100.8,
            summary="revised after steer",
            tasks=[],
            edges=[],
            revision_reason="by alice: focus on the intro",
            revision_kind="user_steer",
            revision_severity="warning",
            revision_index=1,
        )
    )

    records = await list_interventions(sid, store=store, drifts_provider=drifts)

    # Exactly one card — not three.
    assert len(records) == 1, f"expected 1 card, got {len(records)}: {records}"
    card = records[0]
    # Base row is the user's annotation.
    assert card.source == "user"
    assert card.kind == "STEER"
    assert card.annotation_id == "ann_steer_42"
    assert card.author == "alice"
    assert card.body_or_reason == "focus on the intro"
    # Drift + plan outcomes were folded in.
    assert card.severity == "warning"
    assert card.outcome == "plan_revised:r1"
    assert card.plan_revision_index == 1


async def test_autonomous_drift_keeps_own_card_alongside_user_annotation(store):
    """Dedup key is annotation_id — autonomous drifts must NOT merge.

    Per the user directive ("steering due to drift IS a steering"),
    drifts goldfive auto-detected (LOOPING_REASONING, etc.) keep their
    own cards even when the session also contains user annotations.
    The deduper only collapses rows that share an annotation_id.
    """

    sid = "sess_mixed"
    await _seed_session(store, sid)
    # User STEER annotation at t=100.
    await store.put_annotation(
        Annotation(
            id="ann_user_1",
            session_id=sid,
            target=AnnotationTarget(agent_id="a", time_start=100.0),
            author="alice",
            created_at=100.0,
            kind=AnnotationKind.STEERING,
            body="pivot",
        )
    )
    # Autonomous drift at t=200 (no annotation_id).
    drifts = _StubDrifts(
        {
            sid: [
                {
                    "kind": "looping_reasoning",
                    "severity": "warning",
                    "detail": "loop",
                    "recorded_at": 200.0,
                },
                # User-control drift with annotation_id — MUST merge into the
                # annotation row above.
                {
                    "kind": "user_steer",
                    "severity": "warning",
                    "detail": "by alice: pivot",
                    "annotation_id": "ann_user_1",
                    "recorded_at": 100.1,
                },
            ]
        }
    )
    records = await list_interventions(sid, store=store, drifts_provider=drifts)

    # 2 cards: the merged user STEER + the autonomous looping_reasoning drift.
    assert len(records) == 2
    user_card = next(r for r in records if r.source == "user")
    drift_card = next(r for r in records if r.source == "drift")
    assert user_card.annotation_id == "ann_user_1"
    assert drift_card.kind == "LOOPING_REASONING"
    assert drift_card.annotation_id == ""  # autonomous — owns its card


async def test_user_steer_drift_without_annotation_id_keeps_separate_card(store):
    """Back-compat: goldfive pre-#176 emits drifts without annotation_id.

    In that case the deduper can't join, so the drift surfaces as its
    own card. Existing behavior — verified to keep the fallback working
    even after the dedup pass is added.
    """

    sid = "sess_nojoin"
    await _seed_session(store, sid)
    await store.put_annotation(
        Annotation(
            id="ann_x",
            session_id=sid,
            target=AnnotationTarget(agent_id="a", time_start=100.0),
            author="alice",
            created_at=100.0,
            kind=AnnotationKind.STEERING,
            body="x",
        )
    )
    drifts = _StubDrifts(
        {
            sid: [
                {
                    "kind": "user_steer",
                    "severity": "warning",
                    "detail": "x",
                    "recorded_at": 101.0,
                    # No annotation_id — pre-#176 goldfive.
                }
            ]
        }
    )
    records = await list_interventions(sid, store=store, drifts_provider=drifts)
    assert len(records) == 2  # annotation + orphan drift
    assert all(r.source == "user" for r in records)


async def test_slow_user_steer_refine_still_dedups_to_one_card(store):
    """harmonograf#86: drift→plan gap > 5s must still collapse for user STEERs.

    The user's STEER routes through the planner's LLM, which on large
    local models (e.g. Qwen3.5-35B) can take tens of seconds to return
    a refined plan. The narrow 5s attribution window used for
    autonomous drifts strand-suppresses the drift row in that case —
    leaving the annotation merged with the drift AND a separate
    plan-sourced STEER card. After #86 the user-control kinds use an
    extended window so both paths still collapse onto the same card.
    """

    sid = "sess_slow_refine"
    await _seed_session(store, sid)

    # 1. User STEER annotation at t=100.
    await store.put_annotation(
        Annotation(
            id="ann_steer_slow",
            session_id=sid,
            target=AnnotationTarget(agent_id="a", time_start=100.0),
            author="alice",
            created_at=100.0,
            kind=AnnotationKind.STEERING,
            body="pivot to solar flares",
        )
    )

    # 2. USER_STEER drift with annotation_id at t=100.2 (near-immediate
    #    relay from the bridge).
    drifts = _StubDrifts(
        {
            sid: [
                {
                    "run_id": "r1",
                    "kind": "user_steer",
                    "severity": "warning",
                    "detail": "by alice: pivot to solar flares",
                    "annotation_id": "ann_steer_slow",
                    "recorded_at": 100.2,
                }
            ]
        }
    )

    # 3. Plan revision lands 70s later — the refine LLM took a while.
    #    Inside _USER_OUTCOME_WINDOW_S but well outside the default 5s.
    await store.put_task_plan(
        TaskPlan(
            id="p_slow",
            session_id=sid,
            created_at=170.5,
            summary="pivoted plan",
            tasks=[],
            edges=[],
            revision_reason="by alice: pivot to solar flares",
            revision_kind="user_steer",
            revision_severity="warning",
            revision_index=1,
        )
    )

    records = await list_interventions(sid, store=store, drifts_provider=drifts)

    # Exactly one card — the slow refine must not leak a second STEER row.
    assert len(records) == 1, f"expected 1 card, got {len(records)}: {records}"
    card = records[0]
    assert card.source == "user"
    assert card.kind == "STEER"
    assert card.annotation_id == "ann_steer_slow"
    assert card.author == "alice"
    assert card.body_or_reason == "pivot to solar flares"
    # The plan_revised outcome is attributed even across the wider gap.
    assert card.outcome == "plan_revised:r1"
    assert card.plan_revision_index == 1
    assert card.severity == "warning"


async def test_slow_autonomous_refine_is_still_bounded_by_5s(store):
    """Autonomous (non-user) drifts keep the tight 5s window.

    Widening only the user-control kinds means a slow goldfive refine
    (which doesn't happen in practice — autonomous plan revisions fire
    fast) followed by a model-kind drift doesn't claim-steal an
    unrelated later revision. Covered here so a future relaxation
    doesn't accidentally apply across the board.
    """

    sid = "sess_slow_autonomous"
    await _seed_session(store, sid)

    drifts = _StubDrifts(
        {
            sid: [
                {
                    "kind": "looping_reasoning",
                    "severity": "warning",
                    "detail": "loop",
                    "recorded_at": 100.0,
                }
            ]
        }
    )
    # Plan revision 60s later — way outside 5s, inside 300s. Autonomous
    # kinds must still emit a separate card.
    await store.put_task_plan(
        TaskPlan(
            id="p_later",
            session_id=sid,
            created_at=160.0,
            summary="",
            tasks=[],
            edges=[],
            revision_reason="",
            revision_kind="looping_reasoning",
            revision_index=2,
        )
    )

    records = await list_interventions(sid, store=store, drifts_provider=drifts)
    # Two cards: the drift (outcome=recorded, since too far to attribute)
    # and the plan revision as its own entry.
    assert len(records) == 2
    drift_row = next(r for r in records if r.source == "drift" and not r.outcome.startswith("plan_revised:"))
    plan_row = next(r for r in records if r.outcome.startswith("plan_revised:"))
    assert drift_row.outcome == "recorded"
    assert plan_row.plan_revision_index == 2


async def test_empty_session_returns_empty_list(store):
    sid = "sess_empty"
    await _seed_session(store, sid)
    drifts = _StubDrifts({})
    records = await list_interventions(sid, store=store, drifts_provider=drifts)
    assert records == []


# ---------------------------------------------------------------------------
# RPC end-to-end
# ---------------------------------------------------------------------------


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
        yield {"port": port, "store": store, "ingest": ingest}
    finally:
        await server.stop(grace=0.3)


async def test_list_interventions_rpc_round_trips(rpc_stack):
    store = rpc_stack["store"]
    ingest = rpc_stack["ingest"]
    sid = "sess_rpc"
    await _seed_session(store, sid)
    await store.put_annotation(
        Annotation(
            id="ann_u",
            session_id=sid,
            target=AnnotationTarget(agent_id="a", time_start=100.0),
            author="bob",
            created_at=100.0,
            kind=AnnotationKind.STEERING,
            body="please slow down",
        )
    )
    # Inject a drift into the ingest ring so the RPC's drifts_provider
    # (the real IngestPipeline) surfaces it without depending on a
    # goldfive event stream.
    ingest._drifts_by_session.setdefault(sid, []).append(
        {
            "kind": "looping_reasoning",
            "severity": "warning",
            "detail": "ring",
            "recorded_at": 110.0,
        }
    )

    ch = grpc.aio.insecure_channel(f"127.0.0.1:{rpc_stack['port']}")
    try:
        stub = service_pb2_grpc.HarmonografStub(ch)
        resp = await stub.ListInterventions(
            frontend_pb2.ListInterventionsRequest(session_id=sid)
        )
    finally:
        await ch.close()

    sources = [iv.source for iv in resp.interventions]
    assert sources == ["user", "drift"]
    assert resp.interventions[0].kind == "STEER"
    assert resp.interventions[0].annotation_id == "ann_u"
    assert resp.interventions[0].author == "bob"
    assert resp.interventions[1].kind == "LOOPING_REASONING"
    assert resp.interventions[1].severity == "warning"


async def test_list_interventions_rpc_rejects_missing_session(rpc_stack):
    ch = grpc.aio.insecure_channel(f"127.0.0.1:{rpc_stack['port']}")
    try:
        stub = service_pb2_grpc.HarmonografStub(ch)
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await stub.ListInterventions(
                frontend_pb2.ListInterventionsRequest(session_id="")
            )
        assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    finally:
        await ch.close()


async def test_list_interventions_rpc_unknown_session_is_not_found(rpc_stack):
    ch = grpc.aio.insecure_channel(f"127.0.0.1:{rpc_stack['port']}")
    try:
        stub = service_pb2_grpc.HarmonografStub(ch)
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await stub.ListInterventions(
                frontend_pb2.ListInterventionsRequest(session_id="nope")
            )
        assert exc.value.code() == grpc.StatusCode.NOT_FOUND
    finally:
        await ch.close()
