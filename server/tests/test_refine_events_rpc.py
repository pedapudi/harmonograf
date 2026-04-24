"""Tests for the frontend RPC surfacing of RefineAttempted /
RefineFailed events (goldfive#264).

Covers the translation of ``DELTA_REFINE_ATTEMPTED`` /
``DELTA_REFINE_FAILED`` bus deltas into the corresponding
``SessionUpdate`` oneof variants.
"""

from __future__ import annotations

from harmonograf_server.bus import (
    DELTA_REFINE_ATTEMPTED,
    DELTA_REFINE_FAILED,
    Delta,
)
from harmonograf_server.rpc.frontend import _delta_to_session_update


def _attempted_payload(
    *,
    run_id: str = "run-1",
    sequence: int = 11,
    attempt_id: str = "att-uuid-1",
    drift_id: str = "drift-uuid-1",
    trigger_kind: str = "looping_reasoning",
    trigger_severity: str = "warning",
    current_task_id: str = "task-7",
    current_agent_id: str = "presentation-orchestrated-abc:researcher_agent",
    emitted_at: float | None = 1_700_000_000.25,
    recorded_at: float | None = 1_000_000.0,
) -> dict:
    return {
        "run_id": run_id,
        "sequence": sequence,
        "attempt_id": attempt_id,
        "drift_id": drift_id,
        "trigger_kind": trigger_kind,
        "trigger_severity": trigger_severity,
        "current_task_id": current_task_id,
        "current_agent_id": current_agent_id,
        "emitted_at": emitted_at,
        "recorded_at": recorded_at,
    }


def _failed_payload(
    *,
    failure_kind: str = "validator_rejected",
    reason: str = "supersedes coverage missing",
    detail: str = "task t1 superseded but no replacement",
    **base,
) -> dict:
    p = _attempted_payload(**base)
    p["failure_kind"] = failure_kind
    p["reason"] = reason
    p["detail"] = detail
    return p


class TestDeltaToSessionUpdate:
    def test_refine_attempted_delta_produces_session_update_variant(self):
        delta = Delta("sess_r", DELTA_REFINE_ATTEMPTED, _attempted_payload())
        update = _delta_to_session_update(delta)
        assert update is not None
        assert update.WhichOneof("kind") == "refine_attempted"
        a = update.refine_attempted
        assert a.run_id == "run-1"
        assert a.sequence == 11
        assert a.session_id == "sess_r"
        assert a.attempt_id == "att-uuid-1"
        assert a.drift_id == "drift-uuid-1"
        assert a.trigger_kind == "looping_reasoning"
        assert a.trigger_severity == "warning"
        assert a.current_task_id == "task-7"
        assert (
            a.current_agent_id
            == "presentation-orchestrated-abc:researcher_agent"
        )
        # emitted_at preferred over recorded_at when available.
        assert a.emitted_at.seconds == 1_700_000_000

    def test_refine_failed_delta_produces_session_update_variant(self):
        delta = Delta("sess_r", DELTA_REFINE_FAILED, _failed_payload())
        update = _delta_to_session_update(delta)
        assert update is not None
        assert update.WhichOneof("kind") == "refine_failed"
        f = update.refine_failed
        assert f.attempt_id == "att-uuid-1"
        assert f.failure_kind == "validator_rejected"
        assert f.reason == "supersedes coverage missing"
        assert f.detail == "task t1 superseded but no replacement"

    def test_emitted_at_fallback_to_recorded_at(self):
        """When the bus payload's emitted_at is None, the translator
        falls back to recorded_at (mirrors the InvocationCancelled
        timestamp-fallback test)."""
        delta = Delta(
            "sess_r",
            DELTA_REFINE_ATTEMPTED,
            _attempted_payload(emitted_at=None, recorded_at=2_000_000.0),
        )
        update = _delta_to_session_update(delta)
        assert update.refine_attempted.emitted_at.seconds == 2_000_000

    def test_no_timestamp_leaves_emitted_at_unset(self):
        """Both fields missing — the proto's emitted_at stays at its
        default (seconds=0, nanos=0). The frontend's tsToMsAbs handler
        treats that as 'no timestamp' and falls back to wall-clock at
        ingest time."""
        delta = Delta(
            "sess_r",
            DELTA_REFINE_FAILED,
            _failed_payload(emitted_at=None, recorded_at=None),
        )
        update = _delta_to_session_update(delta)
        f = update.refine_failed
        assert f.emitted_at.seconds == 0
        assert f.emitted_at.nanos == 0

    def test_each_failure_kind_string_passes_through(self):
        for fk in (
            "parse_error",
            "validator_rejected",
            "llm_error",
            "other",
            "future_kind",
        ):
            delta = Delta(
                "sess_r",
                DELTA_REFINE_FAILED,
                _failed_payload(failure_kind=fk, attempt_id=f"att-{fk}"),
            )
            update = _delta_to_session_update(delta)
            assert update.refine_failed.failure_kind == fk
            assert update.refine_failed.attempt_id == f"att-{fk}"

    def test_optional_fields_default_to_empty(self):
        """Required-style proto string fields default to '' when the
        delta payload reports None — same defensive coercion the
        InvocationCancelled translator does."""
        delta = Delta(
            "sess_r",
            DELTA_REFINE_ATTEMPTED,
            {
                "run_id": "",
                "sequence": 0,
                "attempt_id": None,
                "drift_id": None,
                "trigger_kind": None,
                "trigger_severity": None,
                "current_task_id": None,
                "current_agent_id": None,
                "emitted_at": None,
                "recorded_at": None,
            },
        )
        update = _delta_to_session_update(delta)
        a = update.refine_attempted
        assert a.attempt_id == ""
        assert a.drift_id == ""
        assert a.trigger_kind == ""
        assert a.current_agent_id == ""
