"""Phase 0 gate: sidecar + drift events survive a server restart, and the
goldfive_events dedup keys on event_id (not the old composite PK).

The intervention aggregator is authoritative for the merged history, so its
inputs must be durable. RefineAttempted / RefineFailed / UserMessageReceived
land in ``sidecar_events``; DriftDetected lands in ``goldfive_events``. A
fresh SqliteStore opened on the same file must read every one of them back,
and WatchSession's initial burst must replay them.
"""

from __future__ import annotations

import pytest

from harmonograf_server.pb import (  # noqa: F401 — grafts goldfive.v1 onto path
    frontend_pb2,
    telemetry_pb2,
)
from goldfive.v1 import events_pb2 as ge

from harmonograf_server.bus import SessionBus
from harmonograf_server.control_router import ControlRouter
from harmonograf_server.convert import (
    _drift_kind_string_to_pb,
    _drift_severity_string_to_pb,
)
from harmonograf_server.ingest import IngestPipeline, StreamContext
from harmonograf_server.rpc.telemetry import TelemetryServicer
from harmonograf_server.storage import (
    GoldfiveEventRecord,
    Session,
    SessionStatus,
    SidecarEventRecord,
)
from harmonograf_server.storage.sqlite import SqliteStore


_SID = "sess_dur"


def _ctx() -> StreamContext:
    return StreamContext(
        stream_id="str_dur",
        agent_id="agent_dur",
        session_id=_SID,
        connected_at=1000.0,
        last_heartbeat=1000.0,
        seen_routes={(_SID, "agent_dur")},
    )


def _drift_event() -> ge.Event:
    ev = ge.Event(run_id="run-1", sequence=3, event_id="run-1:3:drift")
    d = ev.drift_detected
    d.kind = _drift_kind_string_to_pb("looping_reasoning")
    d.severity = _drift_severity_string_to_pb("warning")
    d.detail = "re-reading the same doc"
    d.id = "drift-xyz"
    d.condition_id = "cond-1"
    return ev


async def _feed_all(pipe: IngestPipeline) -> None:
    await pipe.handle_message(
        _ctx(),
        telemetry_pb2.TelemetryUp(
            refine_attempted=telemetry_pb2.RefineAttempted(
                run_id="run-1", sequence=11, session_id=_SID, attempt_id="att-1"
            )
        ),
    )
    await pipe.handle_message(
        _ctx(),
        telemetry_pb2.TelemetryUp(
            refine_failed=telemetry_pb2.RefineFailed(
                run_id="run-1",
                sequence=12,
                session_id=_SID,
                attempt_id="att-1",
                failure_kind="validator_rejected",
            )
        ),
    )
    um = telemetry_pb2.UserMessageReceived(
        run_id="run-1", sequence=1, session_id=_SID, content="forget X, do Y"
    )
    um.emitted_at.seconds = 1_700_000_000
    await pipe.handle_message(_ctx(), telemetry_pb2.TelemetryUp(user_message=um))
    await pipe.handle_message(
        _ctx(), telemetry_pb2.TelemetryUp(goldfive_event=_drift_event())
    )


class _FakeContext:
    async def abort(self, code, details):  # pragma: no cover - error path only
        raise AssertionError(f"WatchSession aborted: {code} {details}")


async def _watch_burst(servicer: TelemetryServicer) -> list:
    """Collect SessionUpdate frames up to and including burst_complete."""
    frames = []
    gen = servicer.WatchSession(
        frontend_pb2.WatchSessionRequest(session_id=_SID), _FakeContext()
    )
    async for update in gen:
        frames.append(update)
        if update.WhichOneof("kind") == "burst_complete":
            break
    await gen.aclose()
    return frames


@pytest.mark.asyncio
async def test_sidecar_and_drift_survive_restart(tmp_path):
    db = tmp_path / "dur.db"

    store = SqliteStore(str(db))
    await store.start()
    await store.create_session(
        Session(id=_SID, title=_SID, created_at=100.0, status=SessionStatus.LIVE)
    )
    bus = SessionBus()
    pipe = IngestPipeline(store, bus, now_fn=lambda: 200.0)
    await _feed_all(pipe)
    await store.close()

    # Reopen a fresh store on the same file — simulates a server restart.
    store2 = SqliteStore(str(db))
    await store2.start()
    try:
        attempts = await store2.list_sidecar_events(_SID, kind="refine_attempted")
        failures = await store2.list_sidecar_events(_SID, kind="refine_failed")
        msgs = await store2.list_sidecar_events(_SID, kind="user_message")
        drifts = await store2.list_goldfive_events(_SID, kind="drift_detected")
        assert len(attempts) == 1
        assert len(failures) == 1
        assert len(msgs) == 1
        assert len(drifts) == 1

        # Full-fidelity: the drift envelope round-trips condition_id.
        ev = ge.Event()
        ev.ParseFromString(drifts[0].payload_bytes)
        assert ev.drift_detected.condition_id == "cond-1"

        # WatchSession burst replays all four from storage.
        router = ControlRouter()
        ingest2 = IngestPipeline(store2, bus, control_sink=router)
        servicer = TelemetryServicer(
            ingest2, router=router, data_dir=str(tmp_path / "payloads")
        )
        frames = await _watch_burst(servicer)
        kinds = [f.WhichOneof("kind") for f in frames]
        assert "refine_attempted" in kinds
        assert "refine_failed" in kinds
        assert "user_message" in kinds
        # drift rides on goldfive_event
        drift_frames = [
            f
            for f in frames
            if f.WhichOneof("kind") == "goldfive_event"
            and f.goldfive_event.WhichOneof("payload") == "drift_detected"
        ]
        assert len(drift_frames) == 1
    finally:
        await store2.close()


@pytest.mark.asyncio
async def test_distinct_goldfive_events_same_composite_both_persist(tmp_path):
    """Two DISTINCT events sharing (session, run, sequence) but with
    distinct event_ids both land — the old composite-PK INSERT OR IGNORE
    silently dropped the second."""
    store = SqliteStore(str(tmp_path / "gf.db"))
    await store.start()
    try:
        a = GoldfiveEventRecord(
            session_id=_SID,
            run_id="r",
            sequence=0,
            kind="drift_detected",
            recorded_at=1.0,
            payload_bytes=b"a",
            event_id="e-a",
        )
        b = GoldfiveEventRecord(
            session_id=_SID,
            run_id="r",
            sequence=0,  # same composite as `a`
            kind="drift_detected",
            recorded_at=2.0,
            payload_bytes=b"b",
            event_id="e-b",  # distinct event_id
        )
        await store.append_goldfive_event(a)
        await store.append_goldfive_event(b)
        rows = await store.list_goldfive_events(_SID, kind="drift_detected")
        assert {r.event_id for r in rows} == {"e-a", "e-b"}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_goldfive_event_dedup_on_repeat_event_id(tmp_path):
    """The same event_id delivered twice collapses to one row."""
    store = SqliteStore(str(tmp_path / "gf2.db"))
    await store.start()
    try:
        rec = GoldfiveEventRecord(
            session_id=_SID,
            run_id="r",
            sequence=5,
            kind="drift_detected",
            recorded_at=1.0,
            payload_bytes=b"x",
            event_id="dup",
        )
        await store.append_goldfive_event(rec)
        await store.append_goldfive_event(rec)
        rows = await store.list_goldfive_events(_SID, kind="drift_detected")
        assert len(rows) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sidecar_dedup_on_event_key(tmp_path):
    store = SqliteStore(str(tmp_path / "sc.db"))
    await store.start()
    try:
        rec = SidecarEventRecord(
            session_id=_SID,
            kind="refine_attempted",
            event_key="refine_attempted:att-1",
            recorded_at=1.0,
            payload_bytes=b"p",
        )
        await store.append_sidecar_event(rec)
        await store.append_sidecar_event(rec)
        rows = await store.list_sidecar_events(_SID, kind="refine_attempted")
        assert len(rows) == 1
    finally:
        await store.close()
