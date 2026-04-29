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
    # Drift at t=120 — drift source. goldfive#199: every drift carries a
    # stable ``id`` (UUID4) that harmonograf's aggregator uses as the
    # strict join key for a subsequent refine's trigger_event_id.
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
                    "id": "drift_loop_1",
                    "recorded_at": 120.0,
                }
            ]
        }
    )
    # Plan revision at t=122 — strict-id merges via trigger_event_id
    # matching the drift id above.
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
            trigger_event_id="drift_loop_1",
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


# goldfive#271 Option A: ``Runner._install_revision`` no longer
# fabricates a USER_STEER drift on every plan install. Turn-1 installs
# emit ``PlanRevised`` only (no ``DriftDetected``); turn N+1 installs
# emit ``NEW_WORK_DISCOVERED``. So the synthetic-filter fallbacks the
# v15 / v17 regressions needed are gone — the upstream stops producing
# the phantom rows in the first place. The tests below pin Option A's
# wire shape: a real operator STEER drift surfaces; an Option-A
# turn-1 install (no drift, just a PlanRevised) surfaces as a
# bare-plan card whose absence of upstream intervention is honest
# (the framework chose this plan; no operator pushed it).
async def test_real_operator_steer_drift_surfaces_in_intervention_list(store):
    """A genuine operator STEER drift (with annotation_id) surfaces
    as a USER STEER intervention — Option A doesn't change the
    real-steer path."""

    sid = "sess_real_steer"
    await _seed_session(store, sid)
    drifts = _StubDrifts(
        {
            sid: [
                {
                    "run_id": "r1",
                    "kind": "user_steer",
                    "severity": "warning",
                    "detail": "operator-Alice: refocus",
                    "recorded_at": 250.0,
                    "id": "drift_real_steer",
                    "annotation_id": "ann_real_x",
                },
            ]
        }
    )
    records = await list_interventions(sid, store=store, drifts_provider=drifts)
    assert len(records) == 1, (
        f"expected the operator STEER to surface; got "
        f"records={[(r.kind, r.body_or_reason) for r in records]!r}"
    )
    assert records[0].source == "user"
    assert records[0].kind == "STEER"
    assert records[0].body_or_reason == "operator-Alice: refocus"
    assert records[0].annotation_id == "ann_real_x"


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
                    "id": "drift_refusal_1",
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
            trigger_event_id="drift_refusal_1",
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
    #    carrying the source annotation_id as its trigger_event_id (goldfive#199).
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
            trigger_event_id="ann_steer_42",
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


async def test_user_steer_plan_revision_strict_id_merge(store):
    """harmonograf#99 (rescope): 20-min refine still merges via strict id.

    Replaces the pre-#99 time-window test. The strict-id contract means
    arbitrary refine latency no longer matters — the plan-revision row
    carries the source annotation_id as its ``trigger_event_id``, so the
    aggregator collapses annotation + drift + plan onto one card by
    exact id match regardless of the gap.
    """

    sid = "sess_slow_refine"
    await _seed_session(store, sid)

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

    drifts = _StubDrifts(
        {
            sid: [
                {
                    "run_id": "r1",
                    "kind": "user_steer",
                    "severity": "warning",
                    "detail": "by alice: pivot to solar flares",
                    "annotation_id": "ann_steer_slow",
                    "id": "drift_user_steer_slow",
                    "recorded_at": 100.2,
                }
            ]
        }
    )

    # Plan revision lands 20 minutes later. Strict-id doesn't care.
    await store.put_task_plan(
        TaskPlan(
            id="p_slow",
            session_id=sid,
            created_at=1300.5,  # +20 minutes
            summary="pivoted plan",
            tasks=[],
            edges=[],
            revision_reason="by alice: pivot to solar flares",
            revision_kind="user_steer",
            revision_severity="warning",
            revision_index=1,
            trigger_event_id="ann_steer_slow",
        )
    )

    records = await list_interventions(sid, store=store, drifts_provider=drifts)

    assert len(records) == 1, f"expected 1 card, got {len(records)}: {records}"
    card = records[0]
    assert card.source == "user"
    assert card.kind == "STEER"
    assert card.annotation_id == "ann_steer_slow"
    assert card.author == "alice"
    assert card.body_or_reason == "pivot to solar flares"
    assert card.outcome == "plan_revised:r1"
    assert card.plan_revision_index == 1
    assert card.severity == "warning"


async def test_autonomous_drift_plan_revision_strict_id_merge(store):
    """harmonograf#99: autonomous drift + its plan revision → 1 card (strict-id).

    Previously autonomous drift + subsequent plan revision were always
    separate cards (the pre-#99 code only merged via annotation_id, which
    autonomous drifts don't have). The rescope stamps a drift-id
    ``trigger_event_id`` on the plan so harmonograf's merge collapses
    the drift + plan onto one card.

    This also exercises the test case from the rescope spec:
    LOOPING_REASONING drift + its PlanRevised at t+10min → 1 card.
    """

    sid = "sess_auto_loop"
    await _seed_session(store, sid)
    drifts = _StubDrifts(
        {
            sid: [
                {
                    "run_id": "r1",
                    "kind": "looping_reasoning",
                    "severity": "warning",
                    "detail": "stuck on same doc",
                    "current_task_id": "t1",
                    "id": "drift_loop_AUTO",
                    "recorded_at": 100.0,
                }
            ]
        }
    )
    # Plan revision lands 10 minutes later — strict-id still merges.
    await store.put_task_plan(
        TaskPlan(
            id="p_loop",
            session_id=sid,
            created_at=700.0,
            summary="revised around loop",
            tasks=[],
            edges=[],
            revision_reason="loop workaround",
            revision_kind="looping_reasoning",
            revision_severity="warning",
            revision_index=1,
            trigger_event_id="drift_loop_AUTO",
        )
    )
    records = await list_interventions(sid, store=store, drifts_provider=drifts)

    assert len(records) == 1, f"expected 1 card, got {len(records)}: {records}"
    card = records[0]
    assert card.source == "drift"
    assert card.kind == "LOOPING_REASONING"
    assert card.outcome == "plan_revised:r1"
    assert card.plan_revision_index == 1


async def test_no_trigger_id_no_merge_by_default(store):
    """Empty trigger_event_id + legacy param UNSET → 2 cards (no silent merge).

    Documented behaviour: pre-#99 data, or a refine where goldfive didn't
    stamp trigger_event_id, surfaces as a standalone plan card. The
    aggregator does NOT silently merge on time-window heuristics by
    default.
    """

    sid = "sess_no_trigger"
    await _seed_session(store, sid)
    await store.put_annotation(
        Annotation(
            id="ann_no_trigger",
            session_id=sid,
            target=AnnotationTarget(agent_id="a", time_start=100.0),
            author="alice",
            created_at=100.0,
            kind=AnnotationKind.STEERING,
            body="pivot",
        )
    )
    drifts = _StubDrifts({sid: []})
    await store.put_task_plan(
        TaskPlan(
            id="p_nojoin",
            session_id=sid,
            created_at=105.0,
            summary="",
            tasks=[],
            edges=[],
            revision_reason="pivot",
            revision_kind="user_steer",
            revision_severity="warning",
            revision_index=1,
            # No trigger_event_id — pre-#99 row or bridge misconfigured.
        )
    )
    # Default window (0.0) → fallback disabled.
    records = await list_interventions(sid, store=store, drifts_provider=drifts)
    # 2 cards: annotation + orphan plan row.
    assert len(records) == 2
    kinds = sorted(r.kind for r in records)
    assert kinds == ["STEER", "STEER"]


async def test_legacy_flag_enabled_via_config(store, caplog):
    """Legacy window passed via param → fallback fires + WARNING logged."""

    caplog.set_level("WARNING", logger="harmonograf_server.interventions")
    sid = "sess_legacy_on"
    await _seed_session(store, sid)
    await store.put_annotation(
        Annotation(
            id="ann_legacy",
            session_id=sid,
            target=AnnotationTarget(agent_id="a", time_start=100.0),
            author="alice",
            created_at=100.0,
            kind=AnnotationKind.STEERING,
            body="pivot",
        )
    )
    drifts = _StubDrifts(
        {
            sid: [
                {
                    "run_id": "r1",
                    "kind": "user_steer",
                    "severity": "warning",
                    "detail": "by alice: pivot",
                    "annotation_id": "ann_legacy",
                    "id": "drift_legacy",
                    "recorded_at": 100.2,
                }
            ]
        }
    )
    # Plan with NO trigger_event_id — strict-id won't merge, so the
    # legacy time-window path must fire. Gap: 10 min (inside 15 min
    # window).
    await store.put_task_plan(
        TaskPlan(
            id="p_legacy",
            session_id=sid,
            created_at=700.0,
            summary="",
            tasks=[],
            edges=[],
            revision_reason="pivot",
            revision_kind="user_steer",
            revision_severity="warning",
            revision_index=1,
            # No trigger_event_id.
        )
    )
    records = await list_interventions(
        sid,
        store=store,
        drifts_provider=drifts,
        legacy_plan_attribution_window_ms=900_000.0,
    )
    # User annotation + drift merge via strict id (both have
    # "ann_legacy" on trigger_event_id). Plan row's outcome was
    # attributed via legacy fallback onto the drift row during
    # _attribute_outcomes — the plan row itself remains because it
    # lacks a trigger_event_id for strict merging.
    user_cards = [r for r in records if r.source == "user"]
    assert len(user_cards) == 1
    assert user_cards[0].outcome == "plan_revised:r1"
    # WARNING logged so operators notice when the fallback fires.
    warnings = [
        rec for rec in caplog.records
        if rec.levelname == "WARNING"
        and "legacy time-window fallback" in rec.getMessage()
    ]
    assert warnings, "expected a WARNING log on legacy fallback match"
    # WARNING references the new config/flag surface, not the old env var.
    assert any(
        "legacy_plan_attribution_window_ms" in rec.getMessage()
        for rec in warnings
    )
    assert not any(
        "HARMONOGRAF_LEGACY_PLAN_ATTRIBUTION_WINDOW_MS" in rec.getMessage()
        for rec in warnings
    )


async def test_legacy_flag_disabled_default(store):
    """Default window (0.0) → no time-window fallback fires (2 cards stay 2)."""

    sid = "sess_legacy_off"
    await _seed_session(store, sid)
    await store.put_annotation(
        Annotation(
            id="ann_no_legacy",
            session_id=sid,
            target=AnnotationTarget(agent_id="a", time_start=100.0),
            author="alice",
            created_at=100.0,
            kind=AnnotationKind.STEERING,
            body="pivot",
        )
    )
    drifts = _StubDrifts(
        {
            sid: [
                {
                    "run_id": "r1",
                    "kind": "user_steer",
                    "severity": "warning",
                    "detail": "by alice: pivot",
                    "annotation_id": "ann_no_legacy",
                    "id": "drift_no_legacy",
                    "recorded_at": 100.2,
                }
            ]
        }
    )
    await store.put_task_plan(
        TaskPlan(
            id="p_no_legacy",
            session_id=sid,
            created_at=700.0,
            summary="",
            tasks=[],
            edges=[],
            revision_reason="pivot",
            revision_kind="user_steer",
            revision_severity="warning",
            revision_index=1,
            # No trigger_event_id.
        )
    )
    # Default (0.0) keeps the fallback disabled.
    records = await list_interventions(sid, store=store, drifts_provider=drifts)
    # Two cards: the merged annotation+drift (strict-id via
    # annotation_id), PLUS the standalone plan-row (no
    # trigger_event_id so no strict merge; legacy window is
    # disabled). The plan row's STEER kind comes from the
    # revision_kind so it rolls up as source="user".
    assert len(records) == 2
    merged_card = next(
        r for r in records if r.annotation_id == "ann_no_legacy"
    )
    orphan_plan = next(
        r for r in records if r.plan_revision_index == 1 and not r.annotation_id
    )
    # The merged row inherited nothing from the plan — attribution
    # didn't run because strict-id missed and legacy was off.
    assert merged_card.outcome == "recorded"
    assert orphan_plan.outcome == "plan_revised:r1"


async def test_autonomous_drift_and_mismatched_plan_keep_separate_cards(store):
    """Strict-id: drift + plan whose ids don't match → 2 cards (harmonograf#99).

    Under the rescope, the dedup key is ``trigger_event_id``. A drift
    with a given id and a plan revision with a different id (or none)
    do not merge — they surface as separate cards. This preserves the
    intent of the old 5s-window test (unrelated revisions can't
    claim-steal each other) but the mechanism is exact-id match rather
    than a heuristic window.
    """

    sid = "sess_mismatched"
    await _seed_session(store, sid)

    drifts = _StubDrifts(
        {
            sid: [
                {
                    "kind": "looping_reasoning",
                    "severity": "warning",
                    "detail": "loop",
                    "id": "drift_loop_A",
                    "recorded_at": 100.0,
                }
            ]
        }
    )
    # Plan revision 60s later with a DIFFERENT trigger_event_id — an
    # unrelated refine on the same kind.
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
            trigger_event_id="drift_loop_B_unrelated",
        )
    )

    records = await list_interventions(sid, store=store, drifts_provider=drifts)
    # Two cards: the drift (unmerged, outcome=recorded) and the plan as
    # its own entry.
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


# ---------------------------------------------------------------------------
# I6 — server-side condition_id collapse (goldfive#318 mirror)
# ---------------------------------------------------------------------------
#
# The iter_1 escalation report's I6 finding flagged that a session with
# 13 ``drift_detected`` events sharing 9 ``condition_id``s returned 18
# rows from ``Harmonograf/ListInterventions`` instead of 9 collapsed
# rows, and that severity transitions in the event stream did NOT
# surface on the rows. These tests pin the server-side collapse fix so
# the gRPC projection mirrors the frontend's groupDriftConditions
# deriver: one row per condition_id, with count/first_seen/last_seen
# metadata and an ordered list of severity_transitions.
# ---------------------------------------------------------------------------


async def test_collapse_two_drifts_same_condition_id_yields_one_row(store):
    """Two drift_detected emits sharing condition_id collapse to one
    survivor with count=2 and the latest emit's state.
    """

    sid = "sess_collapse_2"
    await _seed_session(store, sid)
    drifts = _StubDrifts(
        {
            sid: [
                {
                    "run_id": "r1",
                    "kind": "looping_reasoning",
                    "severity": "warning",
                    "detail": "first observation",
                    "current_task_id": "t1",
                    "current_agent_id": "a",
                    "id": "drift_1",
                    "condition_id": "cond_loop_t1_a",
                    "lifecycle": "opened",
                    "recorded_at": 100.0,
                },
                {
                    "run_id": "r1",
                    "kind": "looping_reasoning",
                    "severity": "warning",
                    "detail": "second observation",
                    "current_task_id": "t1",
                    "current_agent_id": "a",
                    "id": "drift_2",
                    "condition_id": "cond_loop_t1_a",
                    "lifecycle": "escalating",
                    "recorded_at": 110.0,
                },
            ]
        }
    )

    records = await list_interventions(sid, store=store, drifts_provider=drifts)

    assert len(records) == 1, (
        f"expected collapse to one row; got {[(r.kind, r.at, r.detail if hasattr(r, 'detail') else r.body_or_reason) for r in records]!r}"
    )
    rec = records[0]
    assert rec.condition_id == "cond_loop_t1_a"
    assert rec.count == 2
    assert rec.first_seen == 100.0
    assert rec.last_seen == 110.0
    # Survivor reflects the LATEST observation (second) — its body /
    # lifecycle / trigger_event_id win so the row reads as the
    # condition's CURRENT state.
    assert rec.body_or_reason == "second observation"
    assert rec.lifecycle == "escalating"
    assert rec.trigger_event_id == "drift_2"
    # No severity transition: both observations had severity=warning.
    assert rec.severity_transitions == []


async def test_collapse_three_distinct_condition_ids_yields_three_rows(store):
    """Three drift_detected emits with DIFFERENT condition_ids stay as
    three rows, each with count=1 (no collapse).
    """

    sid = "sess_collapse_3"
    await _seed_session(store, sid)
    drifts = _StubDrifts(
        {
            sid: [
                {
                    "kind": "looping_reasoning",
                    "severity": "warning",
                    "detail": "loop",
                    "id": "d1",
                    "condition_id": "cond_a",
                    "lifecycle": "opened",
                    "recorded_at": 100.0,
                },
                {
                    "kind": "tool_error",
                    "severity": "warning",
                    "detail": "err",
                    "id": "d2",
                    "condition_id": "cond_b",
                    "lifecycle": "opened",
                    "recorded_at": 110.0,
                },
                {
                    "kind": "confabulation_risk",
                    "severity": "info",
                    "detail": "conf",
                    "id": "d3",
                    "condition_id": "cond_c",
                    "lifecycle": "opened",
                    "recorded_at": 120.0,
                },
            ]
        }
    )

    records = await list_interventions(sid, store=store, drifts_provider=drifts)
    assert len(records) == 3
    for rec in records:
        assert rec.count == 1
        assert rec.first_seen == rec.at
        assert rec.last_seen == rec.at
        assert rec.severity_transitions == []
    assert {r.condition_id for r in records} == {"cond_a", "cond_b", "cond_c"}


async def test_collapse_empty_condition_id_passes_through_pre_318(store):
    """Pre-#318 events (empty condition_id) bypass the collapse pass —
    backward compat: each emit gets its own row with count=1.
    """

    sid = "sess_collapse_empty"
    await _seed_session(store, sid)
    # Two drifts of the same kind, both with empty condition_id —
    # represents pre-#318 goldfive output. Should NOT collapse.
    drifts = _StubDrifts(
        {
            sid: [
                {
                    "kind": "looping_reasoning",
                    "severity": "warning",
                    "detail": "pre-318 emit 1",
                    "id": "d1",
                    "condition_id": "",
                    "recorded_at": 100.0,
                },
                {
                    "kind": "looping_reasoning",
                    "severity": "warning",
                    "detail": "pre-318 emit 2",
                    "id": "d2",
                    "condition_id": "",
                    "recorded_at": 110.0,
                },
            ]
        }
    )

    records = await list_interventions(sid, store=store, drifts_provider=drifts)
    assert len(records) == 2
    for rec in records:
        assert rec.condition_id == ""
        assert rec.count == 1
        assert rec.severity_transitions == []


async def test_collapse_severity_transition_warning_to_critical(store):
    """A condition that bumps WARNING → CRITICAL across two emits
    surfaces ONE row whose severity_transitions captures the bump.
    """

    sid = "sess_collapse_sev"
    await _seed_session(store, sid)
    drifts = _StubDrifts(
        {
            sid: [
                {
                    "kind": "looping_reasoning",
                    "severity": "warning",
                    "detail": "first emit",
                    "id": "d1",
                    "condition_id": "cond_x",
                    "lifecycle": "opened",
                    "prev_severity": "",  # OPENED has no prior — no transition
                    "recorded_at": 100.0,
                },
                {
                    "kind": "looping_reasoning",
                    "severity": "critical",
                    "detail": "second emit, bumped",
                    "id": "d2",
                    "condition_id": "cond_x",
                    "lifecycle": "escalating",
                    "prev_severity": "warning",  # the bump
                    "recorded_at": 110.0,
                },
            ]
        }
    )

    records = await list_interventions(sid, store=store, drifts_provider=drifts)
    assert len(records) == 1
    rec = records[0]
    assert rec.count == 2
    # Survivor reflects current state — CRITICAL.
    assert rec.severity == "critical"
    assert rec.lifecycle == "escalating"
    # Severity transition surfaced on the collapsed row.
    assert len(rec.severity_transitions) == 1
    trans = rec.severity_transitions[0]
    assert trans.frm == "warning"
    assert trans.to == "critical"
    assert trans.at == 110.0


async def test_collapse_donates_outcome_from_earlier_emit(store):
    """A multi-emit condition where an EARLY emit triggered a refine
    has its outcome donated to the (latest) survivor row — so the
    collapsed view does not lose the strict-id outcome attribution.
    """

    sid = "sess_collapse_outcome"
    await _seed_session(store, sid)
    drifts = _StubDrifts(
        {
            sid: [
                {
                    "kind": "looping_reasoning",
                    "severity": "warning",
                    "detail": "first (triggered refine)",
                    "id": "drift_early",  # plan trigger_event_id matches this
                    "condition_id": "cond_q",
                    "lifecycle": "opened",
                    "recorded_at": 100.0,
                },
                {
                    "kind": "looping_reasoning",
                    "severity": "warning",
                    "detail": "second (no follow-up)",
                    "id": "drift_late",
                    "condition_id": "cond_q",
                    "lifecycle": "escalating",
                    "recorded_at": 110.0,
                },
            ]
        }
    )
    # Plan revision strict-id-matches the EARLY drift.
    await store.put_task_plan(
        TaskPlan(
            id="p1",
            session_id=sid,
            created_at=105.0,
            summary="refined",
            tasks=[],
            edges=[],
            revision_reason="loop refine",
            revision_kind="looping_reasoning",
            revision_severity="warning",
            revision_index=2,
            trigger_event_id="drift_early",
        )
    )

    records = await list_interventions(sid, store=store, drifts_provider=drifts)
    assert len(records) == 1
    rec = records[0]
    assert rec.count == 2
    # Outcome donated from the early emit, even though survivor is the
    # late emit (whose own trigger_event_id didn't match the plan).
    assert rec.outcome == "plan_revised:r2"
    assert rec.plan_revision_index == 2


async def test_record_to_pb_emits_collapse_fields(store):
    """The proto translation populates count / first_seen / last_seen /
    lifecycle / severity_transitions on the wire so opt-in clients can
    prefer server-collapsed data.
    """

    from harmonograf_server.interventions import record_to_pb
    from harmonograf_server.pb.harmonograf.v1 import types_pb2

    sid = "sess_pb_collapse"
    await _seed_session(store, sid)
    drifts = _StubDrifts(
        {
            sid: [
                {
                    "kind": "looping_reasoning",
                    "severity": "warning",
                    "detail": "first",
                    "id": "d1",
                    "condition_id": "cond_pb",
                    "lifecycle": "opened",
                    "recorded_at": 100.0,
                },
                {
                    "kind": "looping_reasoning",
                    "severity": "critical",
                    "detail": "second",
                    "id": "d2",
                    "condition_id": "cond_pb",
                    "lifecycle": "escalating",
                    "prev_severity": "warning",
                    "recorded_at": 110.0,
                },
            ]
        }
    )

    records = await list_interventions(sid, store=store, drifts_provider=drifts)
    assert len(records) == 1
    pb = record_to_pb(records[0], types_pb2)
    assert pb.condition_id == "cond_pb"
    assert pb.count == 2
    assert pb.lifecycle == "escalating"
    assert pb.first_seen.seconds == 100
    assert pb.last_seen.seconds == 110
    assert len(pb.severity_transitions) == 1
    trans = pb.severity_transitions[0]
    # ``from`` is a Python keyword — read via getattr against the proto
    # message (the descriptor still exposes the proto field name).
    assert getattr(trans, "from") == "warning"
    assert trans.to == "critical"
    assert trans.at.seconds == 110


async def test_collapse_iter1_escalation_scenario(store):
    """Reproduce the iter_1 escalation: 13 drift events across 9
    condition_ids → 9 rows from list_interventions (NOT 13 / 18).

    This is the regression pin for I6's headline finding.
    """

    sid = "sess_iter1"
    await _seed_session(store, sid)

    # 9 distinct condition_ids; 4 of them have a duplicate emit so the
    # total observation count is 13. Pattern matches the report: same
    # condition_id repeats only for "logical drift evolved across emits"
    # (lifecycle OPENED → ESCALATING / severity bumps).
    raw_drifts: list[dict] = []
    t = 1000.0
    for i in range(9):
        raw_drifts.append(
            {
                "kind": "looping_reasoning",
                "severity": "warning",
                "detail": f"first emit cond_{i}",
                "id": f"d{i}_1",
                "condition_id": f"cond_{i}",
                "lifecycle": "opened",
                "recorded_at": t,
            }
        )
        t += 1.0
    # Add 4 second-emits to 4 of the conditions. Two are pure re-emits
    # (no severity bump), two bump severity to critical.
    extras = [
        ("cond_0", "warning", ""),
        ("cond_1", "critical", "warning"),
        ("cond_3", "warning", ""),
        ("cond_5", "critical", "warning"),
    ]
    for cid, sev, prev in extras:
        raw_drifts.append(
            {
                "kind": "looping_reasoning",
                "severity": sev,
                "detail": f"re-emit {cid}",
                "id": f"{cid}_2",
                "condition_id": cid,
                "lifecycle": "escalating",
                "prev_severity": prev,
                "recorded_at": t,
            }
        )
        t += 1.0
    assert len(raw_drifts) == 13

    drifts = _StubDrifts({sid: raw_drifts})
    records = await list_interventions(sid, store=store, drifts_provider=drifts)

    # 9 rows, NOT 13 / 18.
    assert len(records) == 9, (
        f"expected 9 rows (one per condition_id); got {len(records)}: "
        f"{[(r.condition_id, r.count) for r in records]!r}"
    )
    # 4 rows have count=2; 5 rows have count=1.
    counts = sorted(r.count for r in records)
    assert counts == [1, 1, 1, 1, 1, 2, 2, 2, 2]
    # 2 severity transitions surface (cond_1 + cond_5), not 0.
    transitioning_rows = [r for r in records if r.severity_transitions]
    assert len(transitioning_rows) == 2
    assert {r.condition_id for r in transitioning_rows} == {"cond_1", "cond_5"}
    for r in transitioning_rows:
        assert len(r.severity_transitions) == 1
        assert r.severity_transitions[0].frm == "warning"
        assert r.severity_transitions[0].to == "critical"
    # Conditions that bumped to critical reflect that on the survivor row.
    by_cid = {r.condition_id: r for r in records}
    assert by_cid["cond_1"].severity == "critical"
    assert by_cid["cond_5"].severity == "critical"
    # Conditions that just re-emitted at warning stay warning.
    assert by_cid["cond_0"].severity == "warning"
    assert by_cid["cond_3"].severity == "warning"
