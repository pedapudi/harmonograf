"""Tests for the frontend RPC surfacing of TaskTransitioned events
(goldfive#267 / #251 R4).

Coverage
--------
* ``DELTA_TASK_TRANSITIONED`` translates to a ``SessionUpdate``
  whose ``kind`` oneof case is ``goldfive_event``, and whose payload
  oneof case is ``task_transitioned``.
* All payload fields (task_id, from_status, to_status, source,
  revision_stamp, agent_name, invocation_id) round-trip onto the typed
  proto.
* ``emitted_at`` is preferred over ``recorded_at``; absent both, the
  Event's ``emitted_at`` stays unset.

Mirrors :mod:`test_invocation_cancelled_rpc` byte-for-byte; the two
events ride the same envelope shape and the translator follows the
same pattern.
"""

from __future__ import annotations

from harmonograf_server.bus import (
    DELTA_TASK_TRANSITIONED,
    Delta,
)
from harmonograf_server.pb import telemetry_pb2  # noqa: F401 — grafts goldfive.v1
from harmonograf_server.rpc.frontend import _delta_to_session_update

from goldfive.v1 import events_pb2 as goldfive_events_pb2  # noqa: E402


def _transition_payload(
    *,
    run_id: str = "run-1",
    sequence: int = 7,
    task_id: str = "t-7",
    from_status: str = "RUNNING",
    to_status: str = "COMPLETED",
    source: str = "llm_report",
    revision_stamp: int = 3,
    agent_name: str = "presentation-orchestrated-abc:researcher_agent",
    invocation_id: str = "inv-7",
    emitted_at: float | None = 1_700_000_000.456,
    recorded_at: float | None = 1_000_000.0,
) -> dict:
    return {
        "run_id": run_id,
        "sequence": sequence,
        "task_id": task_id,
        "from_status": from_status,
        "to_status": to_status,
        "source": source,
        "revision_stamp": revision_stamp,
        "agent_name": agent_name,
        "invocation_id": invocation_id,
        "emitted_at": emitted_at,
        "recorded_at": recorded_at,
    }


def _transition_event(update) -> goldfive_events_pb2.Event:
    """Pull the goldfive Event out of a SessionUpdate produced by the
    delta translator. Fails the test when the kind oneof is anything
    other than ``goldfive_event``."""
    assert update.WhichOneof("kind") == "goldfive_event"
    return update.goldfive_event


class TestDeltaToSessionUpdate:
    def test_transition_delta_produces_goldfive_event(self):
        delta = Delta("sess_t", DELTA_TASK_TRANSITIONED, _transition_payload())
        update = _delta_to_session_update(delta)
        assert update is not None
        ev = _transition_event(update)
        # The Event payload oneof case discriminates which goldfive
        # variant we're carrying.
        assert ev.WhichOneof("payload") == "task_transitioned"
        # Envelope metadata reads off the parent Event.
        assert ev.run_id == "run-1"
        assert ev.sequence == 7
        assert ev.session_id == "sess_t"
        t = ev.task_transitioned
        assert t.task_id == "t-7"
        assert t.from_status == "RUNNING"
        assert t.to_status == "COMPLETED"
        assert t.source == "llm_report"
        assert t.revision_stamp == 3
        assert t.agent_name == "presentation-orchestrated-abc:researcher_agent"
        assert t.invocation_id == "inv-7"

    def test_transition_delta_emitted_at_stamped(self):
        delta = Delta("sess_t", DELTA_TASK_TRANSITIONED, _transition_payload())
        update = _delta_to_session_update(delta)
        ev = _transition_event(update)
        # emitted_at = 1_700_000_000.456 → seconds=1_700_000_000,
        # nanos≈456_000_000.
        assert ev.emitted_at.seconds == 1_700_000_000
        assert abs(ev.emitted_at.nanos - 456_000_000) < 1000

    def test_transition_delta_falls_back_to_recorded_at(self):
        # No emitted_at — the translator falls back to recorded_at so
        # the frontend always has a timestamp to render.
        payload = _transition_payload(emitted_at=None, recorded_at=1_500_500.25)
        delta = Delta("sess_t", DELTA_TASK_TRANSITIONED, payload)
        update = _delta_to_session_update(delta)
        ev = _transition_event(update)
        assert ev.emitted_at.seconds == 1_500_500
        assert abs(ev.emitted_at.nanos - 250_000_000) < 1000

    def test_transition_delta_no_timestamp_leaves_emitted_at_unset(self):
        payload = _transition_payload(emitted_at=None, recorded_at=None)
        delta = Delta("sess_t", DELTA_TASK_TRANSITIONED, payload)
        update = _delta_to_session_update(delta)
        ev = _transition_event(update)
        # Timestamp default-construct ⇒ both fields zero, "unset" on the wire.
        assert ev.emitted_at.seconds == 0
        assert ev.emitted_at.nanos == 0

    def test_transition_delta_unknown_source_passes_through(self):
        # Forward-compat: the goldfive proto comment says readers MUST
        # tolerate unknown source strings. The server is just a
        # passthrough; rendering filters live on the frontend.
        payload = _transition_payload(source="some_future_source")
        delta = Delta("sess_t", DELTA_TASK_TRANSITIONED, payload)
        update = _delta_to_session_update(delta)
        ev = _transition_event(update)
        assert ev.task_transitioned.source == "some_future_source"

    def test_transition_delta_zero_revision_stamp(self):
        # Initial transitions (PENDING → RUNNING on a brand-new plan)
        # carry revision_stamp=0. The translator must not coerce zero
        # to truthy or skip the field.
        payload = _transition_payload(revision_stamp=0)
        delta = Delta("sess_t", DELTA_TASK_TRANSITIONED, payload)
        update = _delta_to_session_update(delta)
        ev = _transition_event(update)
        assert ev.task_transitioned.revision_stamp == 0
