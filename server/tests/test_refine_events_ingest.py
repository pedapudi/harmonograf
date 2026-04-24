"""Tests for the ``TelemetryUp.refine_attempted`` / ``.refine_failed``
ingest paths (goldfive#264).

Mirrors the structure of ``test_invocation_cancelled_ingest.py``: each
new envelope kind is dispatched, stashed on a per-session ring for
reconnect replay, and published onto the bus as a typed delta.

Not persisted in the ``goldfive_events`` table — the events arrive as
dicts on the goldfive side and the ingest path for that table is
strictly proto-serialized via the goldfive Event envelope. Reconnect
replay rides on the in-memory rings, exposed via
``pipe.refine_attempts_for_session`` / ``pipe.refine_failures_for_session``.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from harmonograf_server.bus import (
    DELTA_REFINE_ATTEMPTED,
    DELTA_REFINE_FAILED,
    SessionBus,
)
from harmonograf_server.ingest import IngestPipeline, StreamContext
from harmonograf_server.pb import telemetry_pb2
from harmonograf_server.storage import (
    Session,
    SessionStatus,
    make_store,
)


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


def _stream_ctx(session_id: str = "sess_r") -> StreamContext:
    return StreamContext(
        stream_id="str_test",
        agent_id="agent_r",
        session_id=session_id,
        connected_at=1000.0,
        last_heartbeat=1000.0,
        seen_routes={(session_id, "agent_r")},
    )


def _make_attempted(
    *,
    run_id: str = "run-1",
    sequence: int = 11,
    session_id: str = "sess_r",
    attempt_id: str = "att-uuid-1",
    drift_id: str = "drift-uuid-1",
    trigger_kind: str = "looping_reasoning",
    trigger_severity: str = "warning",
    current_task_id: str = "task-7",
    current_agent_id: str = "presentation-orchestrated-abc:researcher_agent",
) -> telemetry_pb2.RefineAttempted:
    return telemetry_pb2.RefineAttempted(
        run_id=run_id,
        sequence=sequence,
        session_id=session_id,
        attempt_id=attempt_id,
        drift_id=drift_id,
        trigger_kind=trigger_kind,
        trigger_severity=trigger_severity,
        current_task_id=current_task_id,
        current_agent_id=current_agent_id,
    )


def _make_failed(
    *,
    run_id: str = "run-1",
    sequence: int = 12,
    session_id: str = "sess_r",
    attempt_id: str = "att-uuid-1",
    drift_id: str = "drift-uuid-1",
    trigger_kind: str = "looping_reasoning",
    trigger_severity: str = "warning",
    failure_kind: str = "validator_rejected",
    reason: str = "supersedes coverage missing",
    detail: str = "task t1 superseded but no replacement",
    current_task_id: str = "task-7",
    current_agent_id: str = "presentation-orchestrated-abc:researcher_agent",
) -> telemetry_pb2.RefineFailed:
    return telemetry_pb2.RefineFailed(
        run_id=run_id,
        sequence=sequence,
        session_id=session_id,
        attempt_id=attempt_id,
        drift_id=drift_id,
        trigger_kind=trigger_kind,
        trigger_severity=trigger_severity,
        failure_kind=failure_kind,
        reason=reason,
        detail=detail,
        current_task_id=current_task_id,
        current_agent_id=current_agent_id,
    )


def _wrap_attempted(
    msg: telemetry_pb2.RefineAttempted,
) -> telemetry_pb2.TelemetryUp:
    return telemetry_pb2.TelemetryUp(refine_attempted=msg)


def _wrap_failed(
    msg: telemetry_pb2.RefineFailed,
) -> telemetry_pb2.TelemetryUp:
    return telemetry_pb2.TelemetryUp(refine_failed=msg)


@pytest.mark.asyncio
async def test_refine_attempted_publishes_delta(pipeline):
    pipe, bus, _ = pipeline
    sub = await bus.subscribe("sess_r")
    msg = _make_attempted()
    await pipe.handle_message(_stream_ctx(), _wrap_attempted(msg))
    delta = sub.queue.get_nowait()
    assert delta.kind == DELTA_REFINE_ATTEMPTED
    p = delta.payload
    assert p["run_id"] == "run-1"
    assert p["sequence"] == 11
    assert p["attempt_id"] == "att-uuid-1"
    assert p["drift_id"] == "drift-uuid-1"
    assert p["trigger_kind"] == "looping_reasoning"
    assert p["trigger_severity"] == "warning"
    assert p["current_task_id"] == "task-7"
    assert (
        p["current_agent_id"]
        == "presentation-orchestrated-abc:researcher_agent"
    )
    assert p["recorded_at"] == 1_000_000.0


@pytest.mark.asyncio
async def test_refine_failed_publishes_delta(pipeline):
    pipe, bus, _ = pipeline
    sub = await bus.subscribe("sess_r")
    msg = _make_failed()
    await pipe.handle_message(_stream_ctx(), _wrap_failed(msg))
    delta = sub.queue.get_nowait()
    assert delta.kind == DELTA_REFINE_FAILED
    p = delta.payload
    assert p["attempt_id"] == "att-uuid-1"
    assert p["failure_kind"] == "validator_rejected"
    assert p["reason"] == "supersedes coverage missing"
    assert p["detail"] == "task t1 superseded but no replacement"
    assert p["recorded_at"] == 1_000_000.0


@pytest.mark.asyncio
async def test_attempt_failure_paired_via_attempt_id(pipeline):
    """Ingest doesn't merge attempted+failed (the frontend does — via
    the deriver in ``lib/interventions.ts``). But the rings must
    preserve both records ordered by arrival so the merge can run on
    reconnect replay."""
    pipe, _, _ = pipeline
    a = _make_attempted(attempt_id="att-1")
    f = _make_failed(attempt_id="att-1", sequence=15)
    await pipe.handle_message(_stream_ctx(), _wrap_attempted(a))
    await pipe.handle_message(_stream_ctx(), _wrap_failed(f))
    attempts = pipe.refine_attempts_for_session("sess_r")
    failures = pipe.refine_failures_for_session("sess_r")
    assert len(attempts) == 1
    assert len(failures) == 1
    assert attempts[0]["attempt_id"] == "att-1"
    assert failures[0]["attempt_id"] == "att-1"


@pytest.mark.asyncio
async def test_refine_rings_bounded(pipeline):
    """The rings cap at the configured max with oldest-dropped."""
    pipe, _, _ = pipeline
    pipe._refine_ring_max = 3
    for i in range(5):
        await pipe.handle_message(
            _stream_ctx(),
            _wrap_attempted(
                _make_attempted(attempt_id=f"att-{i}", sequence=i)
            ),
        )
    attempts = pipe.refine_attempts_for_session("sess_r")
    assert [a["attempt_id"] for a in attempts] == [
        "att-2",
        "att-3",
        "att-4",
    ]


@pytest.mark.asyncio
async def test_each_failure_kind_propagates(pipeline):
    """Failure-kind taxonomy values ride through verbatim — they're
    string-typed end-to-end so a goldfive addition (new failure kind)
    doesn't require a harmonograf bump."""
    pipe, bus, _ = pipeline
    sub = await bus.subscribe("sess_r")
    for fk in (
        "parse_error",
        "validator_rejected",
        "llm_error",
        "other",
        "future_kind",
    ):
        await pipe.handle_message(
            _stream_ctx(),
            _wrap_failed(
                _make_failed(
                    failure_kind=fk,
                    attempt_id=f"att-{fk}",
                )
            ),
        )
    deltas = []
    for _ in range(5):
        deltas.append(sub.queue.get_nowait())
    assert [d.payload["failure_kind"] for d in deltas] == [
        "parse_error",
        "validator_rejected",
        "llm_error",
        "other",
        "future_kind",
    ]


@pytest.mark.asyncio
async def test_empty_emitted_at_uses_recorded_at_fallback(pipeline):
    """When the envelope's emitted_at is not populated the ingest-side
    recorded_at carries the wall-clock timestamp; the bus payload
    leaves emitted_at=None so the rpc translator's fallback path
    activates (mirrors the InvocationCancelled empty-emitted_at test)."""
    pipe, bus, _ = pipeline
    sub = await bus.subscribe("sess_r")
    msg = _make_attempted()
    assert not msg.HasField("emitted_at")
    await pipe.handle_message(_stream_ctx(), _wrap_attempted(msg))
    delta = sub.queue.get_nowait()
    assert delta.payload["emitted_at"] is None
    assert delta.payload["recorded_at"] == 1_000_000.0
