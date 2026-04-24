"""Tests for dict→proto translation of goldfive's ``refine_attempted``
and ``refine_failed`` sink events (goldfive#264).

Goldfive ships these events as dict envelopes (same forward-compat
pattern used for ``invocation_cancelled``). The harmonograf sink
materializes them into the local ``harmonograf.v1.RefineAttempted`` /
``harmonograf.v1.RefineFailed`` proto messages and pushes via the
transport's dedicated ``TelemetryUp.refine_attempted`` /
``.refine_failed`` slots.

Coverage mirrors ``test_sink_invocation_cancelled.py``:

* Well-formed dicts round-trip into protos with every payload field
  populated; ``current_agent_id`` is canonicalized bare→compound.
* Already-compound agent ids pass through idempotently.
* Missing / empty / negative envelope metadata (no emitted_at,
  sequence=-1) is tolerated.
* Unknown dict ``kind`` fields are still swallowed at DEBUG.
* ``plan_revised`` dict envelopes (the steerer fires them as
  correlation side-cars carrying ``attempt_id``) are dropped — the
  goldfive proto path is authoritative for plan revisions.
* The transport materializes each envelope into the correct
  ``TelemetryUp`` oneof variant.
"""

from __future__ import annotations

import logging

import pytest

from harmonograf_client.buffer import EnvelopeKind
from harmonograf_client.client import Client
from harmonograf_client.sink import HarmonografSink

from tests._fixtures import FakeTransport, make_factory


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="research",
        agent_id="presentation-orchestrated-abc",
        session_id="sess-refine",
        framework="CUSTOM",
        buffer_size=32,
        _transport_factory=make_factory(made),
    )


@pytest.fixture
def sink(client: Client) -> HarmonografSink:
    return HarmonografSink(client)


def _drain(client: Client) -> list:
    return list(client._events.drain())


def _attempted_envelope(**overrides) -> dict:
    base = {
        "event_id": "evt-att-1",
        "run_id": "run-1",
        "sequence": 11,
        "emitted_at": {"seconds": 1_700_000_000, "nanos": 250_000_000},
        "session_id": "sess-refine",
        "kind": "refine_attempted",
        "payload": {
            "attempt_id": "att-uuid-1",
            "drift_id": "drift-uuid-1",
            "trigger_kind": "looping_reasoning",
            "trigger_severity": "warning",
            "current_task_id": "task-7",
            "current_agent_id": "researcher_agent",
        },
    }
    base.update(overrides)
    return base


def _failed_envelope(**overrides) -> dict:
    base = {
        "event_id": "evt-fail-1",
        "run_id": "run-1",
        "sequence": 12,
        "emitted_at": {"seconds": 1_700_000_001, "nanos": 100_000_000},
        "session_id": "sess-refine",
        "kind": "refine_failed",
        "payload": {
            "attempt_id": "att-uuid-1",
            "drift_id": "drift-uuid-1",
            "trigger_kind": "looping_reasoning",
            "trigger_severity": "warning",
            "failure_kind": "validator_rejected",
            "reason": "supersedes coverage missing",
            "detail": "task t1 superseded but no replacement",
            "current_task_id": "task-7",
            "current_agent_id": "researcher_agent",
        },
    }
    base.update(overrides)
    return base


class TestRefineAttemptedDictTranslation:
    @pytest.mark.asyncio
    async def test_dict_envelope_translated_to_proto(
        self, sink: HarmonografSink, client: Client, made: list[FakeTransport]
    ):
        """A well-formed RefineAttempted dict translates field-for-field
        into the harmonograf proto with the agent id canonicalized."""
        await sink.emit(_attempted_envelope())
        envs = _drain(client)
        assert len(envs) == 1
        env = envs[0]
        assert env.kind is EnvelopeKind.REFINE_ATTEMPTED
        payload = env.payload
        assert payload.run_id == "run-1"
        assert payload.sequence == 11
        assert payload.session_id == "sess-refine"
        assert payload.attempt_id == "att-uuid-1"
        assert payload.drift_id == "drift-uuid-1"
        assert payload.trigger_kind == "looping_reasoning"
        assert payload.trigger_severity == "warning"
        assert payload.current_task_id == "task-7"
        assert (
            payload.current_agent_id
            == "presentation-orchestrated-abc:researcher_agent"
        )
        assert payload.emitted_at.seconds == 1_700_000_000
        assert payload.emitted_at.nanos == 250_000_000
        assert made[0].notify_count >= 1

    @pytest.mark.asyncio
    async def test_already_compound_agent_id_is_idempotent(
        self, sink: HarmonografSink, client: Client
    ):
        evt = _attempted_envelope()
        evt["payload"]["current_agent_id"] = (
            "presentation-orchestrated-abc:researcher_agent"
        )
        await sink.emit(evt)
        env = _drain(client)[0]
        assert (
            env.payload.current_agent_id
            == "presentation-orchestrated-abc:researcher_agent"
        )

    @pytest.mark.asyncio
    async def test_missing_emitted_at_leaves_field_unset(
        self, sink: HarmonografSink, client: Client
    ):
        evt = _attempted_envelope()
        evt["emitted_at"] = {}
        await sink.emit(evt)
        env = _drain(client)[0]
        assert env.payload.emitted_at.seconds == 0
        assert env.payload.emitted_at.nanos == 0

    @pytest.mark.asyncio
    async def test_negative_sequence_clamped_to_zero(
        self, sink: HarmonografSink, client: Client
    ):
        evt = _attempted_envelope(sequence=-1)
        await sink.emit(evt)
        env = _drain(client)[0]
        assert env.payload.sequence == 0

    @pytest.mark.asyncio
    async def test_empty_payload_tolerated(
        self, sink: HarmonografSink, client: Client
    ):
        """Defensive: an envelope with an empty payload still produces a
        proto (with empty strings) — observability must never raise."""
        evt = _attempted_envelope(payload={})
        await sink.emit(evt)
        env = _drain(client)[0]
        assert env.kind is EnvelopeKind.REFINE_ATTEMPTED
        assert env.payload.attempt_id == ""
        assert env.payload.drift_id == ""
        assert env.payload.current_agent_id == ""


class TestRefineFailedDictTranslation:
    @pytest.mark.asyncio
    async def test_dict_envelope_translated_to_proto(
        self, sink: HarmonografSink, client: Client, made: list[FakeTransport]
    ):
        await sink.emit(_failed_envelope())
        envs = _drain(client)
        assert len(envs) == 1
        env = envs[0]
        assert env.kind is EnvelopeKind.REFINE_FAILED
        payload = env.payload
        assert payload.run_id == "run-1"
        assert payload.sequence == 12
        assert payload.session_id == "sess-refine"
        assert payload.attempt_id == "att-uuid-1"
        assert payload.drift_id == "drift-uuid-1"
        assert payload.trigger_kind == "looping_reasoning"
        assert payload.trigger_severity == "warning"
        assert payload.failure_kind == "validator_rejected"
        assert payload.reason == "supersedes coverage missing"
        assert payload.detail == "task t1 superseded but no replacement"
        assert payload.current_task_id == "task-7"
        assert (
            payload.current_agent_id
            == "presentation-orchestrated-abc:researcher_agent"
        )
        assert payload.emitted_at.seconds == 1_700_000_001
        assert payload.emitted_at.nanos == 100_000_000

    @pytest.mark.asyncio
    async def test_each_failure_kind_rides_through_verbatim(
        self, sink: HarmonografSink, client: Client
    ):
        """The four taxonomy values goldfive uses (parse_error,
        validator_rejected, llm_error, other) plus an unknown future
        value all ride through the wire verbatim — the failure_kind
        field is intentionally a string, not an enum, so that
        forward-compat additions on goldfive don't require a
        harmonograf proto bump."""
        for fk in (
            "parse_error",
            "validator_rejected",
            "llm_error",
            "other",
            "future_value_we_havent_seen_yet",
        ):
            evt = _failed_envelope()
            evt["payload"]["failure_kind"] = fk
            evt["payload"]["attempt_id"] = f"att-{fk}"
            await sink.emit(evt)
        envs = _drain(client)
        assert [e.payload.failure_kind for e in envs] == [
            "parse_error",
            "validator_rejected",
            "llm_error",
            "other",
            "future_value_we_havent_seen_yet",
        ]


class TestSinkDispatch:
    @pytest.mark.asyncio
    async def test_unknown_dict_kind_swallowed(
        self,
        sink: HarmonografSink,
        client: Client,
        caplog: pytest.LogCaptureFixture,
    ):
        with caplog.at_level(logging.DEBUG, logger="harmonograf_client.sink"):
            await sink.emit({"kind": "not_a_real_kind", "payload": {}})
        assert _drain(client) == []
        assert any(
            "ignoring unknown dict goldfive event kind" in r.message
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_plan_revised_dict_dropped(
        self, sink: HarmonografSink, client: Client
    ):
        """``plan_revised`` is a dict correlation side-car emitted by
        goldfive#264 carrying ``attempt_id`` for refine correlation. The
        full plan revision still rides as a goldfive proto Event on the
        primary path, so the dict path is intentionally a no-op on the
        sink to avoid double-publishing the revision."""
        await sink.emit(
            {
                "run_id": "run-1",
                "sequence": 13,
                "session_id": "sess-refine",
                "kind": "plan_revised",
                "payload": {
                    "attempt_id": "att-uuid-1",
                    "drift_id": "drift-uuid-1",
                    "revision_index": 3,
                    "trigger_kind": "looping_reasoning",
                    "trigger_severity": "warning",
                    "current_task_id": "task-7",
                    "current_agent_id": "researcher_agent",
                },
            }
        )
        assert _drain(client) == []

    @pytest.mark.asyncio
    async def test_emit_after_close_is_a_noop(
        self, sink: HarmonografSink, client: Client
    ):
        await sink.close()
        await sink.emit(_attempted_envelope())
        await sink.emit(_failed_envelope())
        assert _drain(client) == []


class TestTransportMaterialization:
    @pytest.mark.asyncio
    async def test_round_trip_through_envelope_to_up(
        self, sink: HarmonografSink, client: Client
    ):
        """The transport's ``_envelope_to_up`` materializes each
        envelope into the right ``TelemetryUp`` oneof case so the wire
        carries the correct discriminator."""
        await sink.emit(_attempted_envelope())
        await sink.emit(_failed_envelope())
        envs = _drain(client)
        assert len(envs) == 2
        from harmonograf_client.pb import telemetry_pb2

        attempted_up = telemetry_pb2.TelemetryUp(
            refine_attempted=envs[0].payload
        )
        failed_up = telemetry_pb2.TelemetryUp(refine_failed=envs[1].payload)
        assert attempted_up.WhichOneof("msg") == "refine_attempted"
        assert failed_up.WhichOneof("msg") == "refine_failed"
        assert attempted_up.refine_attempted.attempt_id == "att-uuid-1"
        assert failed_up.refine_failed.failure_kind == "validator_rejected"

    @pytest.mark.asyncio
    async def test_session_id_routed_for_lazy_hello(
        self, sink: HarmonografSink, client: Client
    ):
        """The transport's session-id extraction reads the proto's
        top-level ``session_id`` for refine envelopes (mirrors the
        goldfive_event + invocation_cancelled paths) so a refine event
        emitted before any span on a fresh stream still carries the
        right session id onto the lazy Hello."""
        from harmonograf_client.transport import _session_id_of_envelope

        await sink.emit(_attempted_envelope())
        env = _drain(client)[0]
        assert _session_id_of_envelope(env) == "sess-refine"
