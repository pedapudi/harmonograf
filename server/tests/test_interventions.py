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
