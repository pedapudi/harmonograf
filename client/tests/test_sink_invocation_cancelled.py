"""Tests for dict→proto translation of goldfive's ``invocation_cancelled``
sink event (goldfive#251 Stream C / #259).

The goldfive side ships the cancel event as a dict via
``goldfive.events.make_event`` because the goldfive proto envelope does
not (yet) have an ``InvocationCancelled`` message. The harmonograf sink
converts that dict to the local ``harmonograf.v1.InvocationCancelled``
proto and pushes it via the transport's
``TelemetryUp.invocation_cancelled`` slot.

These tests cover the translation in both directions:

* A well-formed dict round-trips into a proto with every field
  populated; ``agent_name`` is canonicalized to the compound id form;
  ``emitted_at`` lands as a google.protobuf.Timestamp.
* A missing / empty dict envelope is tolerated (observability must
  never raise).
* Unknown dict kinds are swallowed at DEBUG (no forwarding, no raise).
* The transport materializes the envelope into the
  ``TelemetryUp.invocation_cancelled`` oneof variant with the expected
  discriminator.
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
        session_id="sess-cancel",
        framework="CUSTOM",
        buffer_size=32,
        _transport_factory=make_factory(made),
    )


@pytest.fixture
def sink(client: Client) -> HarmonografSink:
    return HarmonografSink(client)


def _drain(client: Client) -> list:
    return list(client._events.drain())


def _make_dict_envelope(**overrides) -> dict:
    base = {
        "event_id": "evt-cancel-1",
        "run_id": "run-abc",
        "sequence": 7,
        "emitted_at": {"seconds": 1_700_000_000, "nanos": 123_000_000},
        "session_id": "sess-cancel",
        "kind": "invocation_cancelled",
        "payload": {
            "invocation_id": "inv-42",
            "agent_name": "researcher_agent",
            "reason": "drift",
            "severity": "critical",
            "drift_id": "drift-uuid-1",
            "drift_kind": "off_topic",
            "detail": "assistant veered off task",
            "tool_name": "",
        },
    }
    base.update(overrides)
    return base


class TestInvocationCancelledDictTranslation:
    @pytest.mark.asyncio
    async def test_dict_envelope_translated_to_proto(
        self, sink: HarmonografSink, client: Client, made: list[FakeTransport]
    ):
        """A well-formed dict envelope is converted field-for-field into
        the harmonograf ``InvocationCancelled`` proto."""
        evt = _make_dict_envelope()
        await sink.emit(evt)
        envs = _drain(client)
        assert len(envs) == 1
        env = envs[0]
        assert env.kind is EnvelopeKind.INVOCATION_CANCELLED
        payload = env.payload
        assert payload.run_id == "run-abc"
        assert payload.sequence == 7
        assert payload.session_id == "sess-cancel"
        assert payload.invocation_id == "inv-42"
        # Agent name canonicalized bare→compound using the client's
        # agent_id (same rule as the goldfive_event path).
        assert payload.agent_name == "presentation-orchestrated-abc:researcher_agent"
        assert payload.reason == "drift"
        assert payload.severity == "critical"
        assert payload.drift_id == "drift-uuid-1"
        assert payload.drift_kind == "off_topic"
        assert payload.detail == "assistant veered off task"
        assert payload.tool_name == ""
        # emitted_at populated from the dict pair.
        assert payload.emitted_at.seconds == 1_700_000_000
        assert payload.emitted_at.nanos == 123_000_000
        assert made[0].notify_count >= 1

    @pytest.mark.asyncio
    async def test_already_compound_agent_name_is_idempotent(
        self, sink: HarmonografSink, client: Client
    ):
        """Already-compound agent_name passes through unchanged (no
        double-prefixing)."""
        evt = _make_dict_envelope(
            payload={
                "invocation_id": "inv-1",
                "agent_name": "presentation-orchestrated-abc:researcher_agent",
                "reason": "user_steer",
                "severity": "warning",
                "drift_id": "",
                "drift_kind": "user_steer",
                "detail": "user asked to stop",
                "tool_name": "",
            }
        )
        await sink.emit(evt)
        env = _drain(client)[0]
        assert (
            env.payload.agent_name
            == "presentation-orchestrated-abc:researcher_agent"
        )

    @pytest.mark.asyncio
    async def test_tool_name_field_carried_when_present(
        self, sink: HarmonografSink, client: Client
    ):
        """``tool_name`` rides through verbatim for cancels that fired at
        a tool-dispatch checkpoint."""
        evt = _make_dict_envelope(
            payload={
                "invocation_id": "inv-1",
                "agent_name": "researcher_agent",
                "reason": "drift",
                "severity": "critical",
                "drift_id": "drift-xyz",
                "drift_kind": "off_topic",
                "detail": "tool call veered off task",
                "tool_name": "search_web",
            }
        )
        await sink.emit(evt)
        env = _drain(client)[0]
        assert env.payload.tool_name == "search_web"

    @pytest.mark.asyncio
    async def test_missing_emitted_at_leaves_field_unset(
        self, sink: HarmonografSink, client: Client
    ):
        """When the dict doesn't carry ``emitted_at`` (e.g. Stream C
        fell back to next_sequence=0 / no clock read), the proto's
        emitted_at stays zero so the server can stamp wall-clock on
        receipt."""
        evt = _make_dict_envelope()
        evt["emitted_at"] = {}
        await sink.emit(evt)
        env = _drain(client)[0]
        assert env.payload.emitted_at.seconds == 0
        assert env.payload.emitted_at.nanos == 0

    @pytest.mark.asyncio
    async def test_negative_sequence_clamped_to_zero(
        self, sink: HarmonografSink, client: Client
    ):
        """Defensive: a negative sequence (goldfive's
        ``session.next_sequence()`` raised and fell back to -1) is
        clamped to 0 so it doesn't wrap into a huge uint64."""
        evt = _make_dict_envelope(sequence=-1)
        await sink.emit(evt)
        env = _drain(client)[0]
        assert env.payload.sequence == 0

    @pytest.mark.asyncio
    async def test_unknown_dict_kind_is_dropped(
        self,
        sink: HarmonografSink,
        client: Client,
        caplog: pytest.LogCaptureFixture,
    ):
        """Dict envelopes with an unknown ``kind`` are swallowed at
        DEBUG with no forwarding — observability must never raise."""
        caplog.set_level(logging.DEBUG, logger="harmonograf_client.sink")
        evt = {"kind": "some_future_event", "payload": {}}
        await sink.emit(evt)
        assert _drain(client) == []
        assert any(
            "ignoring unknown dict goldfive event" in rec.getMessage()
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_transport_wraps_in_telemetry_up_variant(
        self, sink: HarmonografSink, client: Client
    ):
        """The envelope materializes into the
        ``TelemetryUp.invocation_cancelled`` oneof variant — mirrors the
        equivalent ``goldfive_event`` round-trip test in
        ``test_harmonograf_sink.py``."""
        from harmonograf_client.pb import telemetry_pb2

        evt = _make_dict_envelope()
        await sink.emit(evt)
        env = _drain(client)[0]
        up = telemetry_pb2.TelemetryUp(invocation_cancelled=env.payload)
        assert up.WhichOneof("msg") == "invocation_cancelled"
        assert up.invocation_cancelled.invocation_id == "inv-42"
        assert up.invocation_cancelled.severity == "critical"

    @pytest.mark.asyncio
    async def test_emit_after_close_is_silent_drop(
        self, sink: HarmonografSink, client: Client
    ):
        """Late cancel events from a tearing-down runner are dropped."""
        await sink.close()
        await sink.emit(_make_dict_envelope())
        assert _drain(client) == []
