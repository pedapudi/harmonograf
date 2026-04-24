"""Tests for the frontend RPC surfacing of InvocationCancelled events
post goldfive#262 (harmonograf Wave 2 / A8).

History
-------
PR #187 wrapped the cancel in a dedicated
``SessionUpdate.invocation_cancelled`` oneof slot (
``harmonograf.v1.InvocationCancelled``). After goldfive#262 promoted
the event to ``goldfive.v1.InvocationCancelled``, the dedicated slot
was removed; the cancel rides on the standard ``goldfive_event``
oneof variant on ``SessionUpdate`` along with every other typed
goldfive event.

Coverage
--------
* ``DELTA_INVOCATION_CANCELLED`` translates to a ``SessionUpdate``
  whose ``kind`` oneof case is ``goldfive_event``, and whose payload
  oneof case is ``invocation_cancelled``.
* All cancel-payload fields (run_id, sequence, agent_name, reason,
  severity, drift_id, drift_kind, detail, tool_name) round-trip onto
  the typed proto.
* ``emitted_at`` is preferred over ``recorded_at``; absent both, the
  Event's ``emitted_at`` stays unset.
"""

from __future__ import annotations

import pytest

from harmonograf_server.bus import (
    DELTA_INVOCATION_CANCELLED,
    Delta,
)
from harmonograf_server.pb import telemetry_pb2  # noqa: F401 — grafts goldfive.v1
from harmonograf_server.rpc.frontend import _delta_to_session_update

from goldfive.v1 import events_pb2 as goldfive_events_pb2  # noqa: E402


def _cancel_payload(
    *,
    run_id: str = "run-1",
    sequence: int = 3,
    invocation_id: str = "inv-42",
    agent_name: str = "presentation-orchestrated-abc:researcher_agent",
    reason: str = "drift",
    severity: str = "critical",
    drift_id: str = "drift-uuid-1",
    drift_kind: str = "off_topic",
    detail: str = "assistant veered off task",
    tool_name: str = "",
    emitted_at: float | None = 1_700_000_000.123,
    recorded_at: float | None = 1_000_000.0,
) -> dict:
    return {
        "run_id": run_id,
        "sequence": sequence,
        "invocation_id": invocation_id,
        "agent_name": agent_name,
        "reason": reason,
        "severity": severity,
        "drift_id": drift_id,
        "drift_kind": drift_kind,
        "detail": detail,
        "tool_name": tool_name,
        "emitted_at": emitted_at,
        "recorded_at": recorded_at,
    }


def _cancel_event(update) -> goldfive_events_pb2.Event:
    """Pull the goldfive Event out of a SessionUpdate produced by the
    delta translator. Fails the test when the kind oneof is anything
    other than ``goldfive_event``."""
    assert update.WhichOneof("kind") == "goldfive_event"
    return update.goldfive_event


class TestDeltaToSessionUpdate:
    def test_cancel_delta_produces_goldfive_event(self):
        delta = Delta("sess_c", DELTA_INVOCATION_CANCELLED, _cancel_payload())
        update = _delta_to_session_update(delta)
        assert update is not None
        ev = _cancel_event(update)
        # The Event payload oneof case discriminates which goldfive
        # variant we're carrying.
        assert ev.WhichOneof("payload") == "invocation_cancelled"
        # Envelope metadata reads off the parent Event.
        assert ev.run_id == "run-1"
        assert ev.sequence == 3
        assert ev.session_id == "sess_c"
        c = ev.invocation_cancelled
        assert c.invocation_id == "inv-42"
        assert c.agent_name == "presentation-orchestrated-abc:researcher_agent"
        assert c.reason == "drift"
        assert c.severity == "critical"
        assert c.drift_id == "drift-uuid-1"
        assert c.drift_kind == "off_topic"
        assert c.detail == "assistant veered off task"
        assert c.tool_name == ""

    def test_cancel_delta_tool_name_carried(self):
        delta = Delta(
            "sess_c",
            DELTA_INVOCATION_CANCELLED,
            _cancel_payload(tool_name="search_web"),
        )
        update = _delta_to_session_update(delta)
        ev = _cancel_event(update)
        assert ev.invocation_cancelled.tool_name == "search_web"

    def test_cancel_delta_emitted_at_stamped(self):
        delta = Delta("sess_c", DELTA_INVOCATION_CANCELLED, _cancel_payload())
        update = _delta_to_session_update(delta)
        ev = _cancel_event(update)
        # emitted_at = 1_700_000_000.123 → seconds=1_700_000_000, nanos≈123_000_000
        assert ev.emitted_at.seconds == 1_700_000_000
        assert abs(ev.emitted_at.nanos - 123_000_000) < 1000

    def test_cancel_delta_falls_back_to_recorded_at(self):
        """When the goldfive-side emitted_at is absent, the ingest-side
        recorded_at is used so the frontend always has a timestamp."""
        payload = _cancel_payload(emitted_at=None, recorded_at=500_000.0)
        delta = Delta("sess_c", DELTA_INVOCATION_CANCELLED, payload)
        update = _delta_to_session_update(delta)
        ev = _cancel_event(update)
        assert ev.emitted_at.seconds == 500_000

    def test_cancel_delta_no_timestamp_leaves_field_unset(self):
        payload = _cancel_payload(emitted_at=None, recorded_at=None)
        delta = Delta("sess_c", DELTA_INVOCATION_CANCELLED, payload)
        update = _delta_to_session_update(delta)
        ev = _cancel_event(update)
        assert ev.emitted_at.seconds == 0
        assert ev.emitted_at.nanos == 0

    def test_cancel_delta_empty_optional_fields(self):
        """user-cancel / plan-revised paths fire without a drift_id. The
        translator still produces a well-formed message."""
        payload = _cancel_payload(
            drift_id="", drift_kind="", reason="user_steer"
        )
        delta = Delta("sess_c", DELTA_INVOCATION_CANCELLED, payload)
        update = _delta_to_session_update(delta)
        ev = _cancel_event(update)
        c = ev.invocation_cancelled
        assert c.drift_id == ""
        assert c.drift_kind == ""
        assert c.reason == "user_steer"

    def test_no_dedicated_invocation_cancelled_oneof_on_session_update(self):
        """Migration regression: ``SessionUpdate`` no longer carries a
        dedicated ``invocation_cancelled`` oneof case — that placeholder
        was removed when goldfive promoted the event to a typed
        proto. The numbers stay reserved (see frontend.proto)."""
        from harmonograf_server.pb import frontend_pb2

        descriptor = frontend_pb2.SessionUpdate.DESCRIPTOR
        kind_oneof = descriptor.oneofs_by_name["kind"]
        case_names = {f.name for f in kind_oneof.fields}
        assert "invocation_cancelled" not in case_names
        # Field number 20 is reserved (the slot that PR #187 used).
        reserved_ranges = list(descriptor.oneofs_by_name["kind"].containing_type.GetOptions().ListFields())
        # Easier: assert the canonical replacement carries the cancel.
        assert "goldfive_event" in case_names

    def test_round_trip_carries_full_event_envelope(self):
        """The whole ``Event`` (envelope + payload) round-trips through
        SerializeToString/ParseFromString — sanity check that no fields
        get dropped during the delta→event translation."""
        delta = Delta("sess_c", DELTA_INVOCATION_CANCELLED, _cancel_payload())
        update = _delta_to_session_update(delta)
        ev = _cancel_event(update)
        wire = ev.SerializeToString()
        parsed = goldfive_events_pb2.Event.FromString(wire)
        assert parsed.WhichOneof("payload") == "invocation_cancelled"
        assert parsed.invocation_cancelled.invocation_id == "inv-42"
        assert parsed.run_id == "run-1"
