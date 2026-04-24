"""Tests for the frontend RPC surfacing of InvocationCancelled events
(goldfive#251 Stream C / harmonograf PR).

Covers the translation of ``DELTA_INVOCATION_CANCELLED`` bus deltas
into ``SessionUpdate.invocation_cancelled`` wire messages — both the
live-delta path used by WatchSession's fan-out loop and the
initial-burst replay from the per-session ring.
"""

from __future__ import annotations

import pytest

from harmonograf_server.bus import (
    DELTA_INVOCATION_CANCELLED,
    Delta,
)
from harmonograf_server.pb import telemetry_pb2
from harmonograf_server.rpc.frontend import _delta_to_session_update


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


class TestDeltaToSessionUpdate:
    def test_cancel_delta_produces_session_update_variant(self):
        delta = Delta("sess_c", DELTA_INVOCATION_CANCELLED, _cancel_payload())
        update = _delta_to_session_update(delta)
        assert update is not None
        assert update.WhichOneof("kind") == "invocation_cancelled"
        c = update.invocation_cancelled
        assert c.run_id == "run-1"
        assert c.sequence == 3
        assert c.session_id == "sess_c"
        assert c.invocation_id == "inv-42"
        assert (
            c.agent_name
            == "presentation-orchestrated-abc:researcher_agent"
        )
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
        assert update.invocation_cancelled.tool_name == "search_web"

    def test_cancel_delta_emitted_at_stamped(self):
        delta = Delta("sess_c", DELTA_INVOCATION_CANCELLED, _cancel_payload())
        update = _delta_to_session_update(delta)
        # emitted_at = 1_700_000_000.123 → seconds=1_700_000_000, nanos≈123_000_000
        c = update.invocation_cancelled
        assert c.emitted_at.seconds == 1_700_000_000
        assert abs(c.emitted_at.nanos - 123_000_000) < 1000

    def test_cancel_delta_falls_back_to_recorded_at(self):
        """When the goldfive-side emitted_at is absent, the ingest-side
        recorded_at is used so the frontend always has a timestamp."""
        payload = _cancel_payload(emitted_at=None, recorded_at=500_000.0)
        delta = Delta("sess_c", DELTA_INVOCATION_CANCELLED, payload)
        update = _delta_to_session_update(delta)
        c = update.invocation_cancelled
        assert c.emitted_at.seconds == 500_000

    def test_cancel_delta_no_timestamp_leaves_field_unset(self):
        payload = _cancel_payload(emitted_at=None, recorded_at=None)
        delta = Delta("sess_c", DELTA_INVOCATION_CANCELLED, payload)
        update = _delta_to_session_update(delta)
        c = update.invocation_cancelled
        assert c.emitted_at.seconds == 0
        assert c.emitted_at.nanos == 0

    def test_cancel_delta_empty_optional_fields(self):
        """user-cancel / plan-revised paths fire without a drift_id. The
        translator still produces a well-formed message."""
        payload = _cancel_payload(
            drift_id="", drift_kind="", reason="user_steer"
        )
        delta = Delta("sess_c", DELTA_INVOCATION_CANCELLED, payload)
        update = _delta_to_session_update(delta)
        c = update.invocation_cancelled
        assert c.drift_id == ""
        assert c.drift_kind == ""
        assert c.reason == "user_steer"

    def test_telemetry_up_round_trip_compatible(self):
        """The harmonograf.v1.InvocationCancelled message the SessionUpdate
        carries is the same shape the transport wrapper expects on
        TelemetryUp — sanity check to guard against future field-number
        drift between the telemetry and frontend protos."""
        delta = Delta("sess_c", DELTA_INVOCATION_CANCELLED, _cancel_payload())
        update = _delta_to_session_update(delta)
        ic = update.invocation_cancelled
        up = telemetry_pb2.TelemetryUp(invocation_cancelled=ic)
        assert up.WhichOneof("msg") == "invocation_cancelled"
        assert up.invocation_cancelled.invocation_id == "inv-42"
