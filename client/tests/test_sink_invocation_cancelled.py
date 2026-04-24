"""Tests for the sink's handling of ``invocation_cancelled`` events
post goldfive#262 / harmonograf migration (Wave 2 / A8).

History
-------
PR #187 added a placeholder ``harmonograf.v1.InvocationCancelled`` proto
+ a dict→proto conversion path on the sink, because goldfive shipped
the cancel event as a dict envelope (Stream C of #251). goldfive#262
then promoted the event to a typed ``goldfive.v1.InvocationCancelled``
variant on the proto envelope; the sink's dict→proto path was retired
in this PR.

What these tests cover now
--------------------------
* The proto path: a goldfive ``Event`` carrying ``invocation_cancelled``
  passes straight through ``emit_goldfive_event`` like every other
  goldfive event. The sink does NOT translate the message body — the
  server consumes the typed payload directly via the goldfive event
  pipeline.
* Migration regression: a pre-#262 dict envelope keyed
  ``"kind": "invocation_cancelled"`` is now dropped at DEBUG with no
  forwarding (no harmonograf proto exists to convert it into). The
  client never raises — observability events must not break a
  tearing-down runner.
* Generic dict envelopes for OTHER kinds (e.g. forward-compat dicts
  goldfive may ship before promoting to proto) keep landing on the
  same dict-drop branch — we don't accidentally re-introduce typed
  conversion for kinds we don't know about.
"""

from __future__ import annotations

import logging

import pytest

from harmonograf_client.buffer import EnvelopeKind
from harmonograf_client.client import Client
from harmonograf_client.pb import telemetry_pb2  # noqa: F401 — grafts goldfive.v1 onto goldfive
from harmonograf_client.sink import HarmonografSink

from goldfive.v1 import events_pb2 as goldfive_events_pb2  # noqa: E402

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


def _make_proto_event(
    *,
    run_id: str = "run-abc",
    sequence: int = 7,
    session_id: str = "sess-cancel",
    invocation_id: str = "inv-42",
    agent_name: str = "researcher_agent",
    reason: str = "drift",
    severity: str = "critical",
    drift_id: str = "drift-uuid-1",
    drift_kind: str = "off_topic",
    detail: str = "assistant veered off task",
    tool_name: str = "",
):
    """Build a goldfive ``Event`` with the typed ``invocation_cancelled``
    payload variant — matches what goldfive#262's
    ``invocation_cancelled_event`` factory produces."""
    evt = goldfive_events_pb2.Event(
        run_id=run_id,
        sequence=sequence,
        session_id=session_id,
    )
    evt.invocation_cancelled.invocation_id = invocation_id
    evt.invocation_cancelled.agent_name = agent_name
    evt.invocation_cancelled.reason = reason
    evt.invocation_cancelled.severity = severity
    evt.invocation_cancelled.drift_id = drift_id
    evt.invocation_cancelled.drift_kind = drift_kind
    evt.invocation_cancelled.detail = detail
    evt.invocation_cancelled.tool_name = tool_name
    return evt


class TestInvocationCancelledProtoPath:
    @pytest.mark.asyncio
    async def test_proto_event_passes_through_goldfive_event_envelope(
        self, sink: HarmonografSink, client: Client, made: list[FakeTransport]
    ):
        """A typed ``goldfive.v1.InvocationCancelled`` event flows
        through the standard ``GOLDFIVE_EVENT`` envelope. No dedicated
        slot, no harmonograf-side translation — the sink just forwards."""
        evt = _make_proto_event()
        await sink.emit(evt)
        envs = _drain(client)
        assert len(envs) == 1
        env = envs[0]
        assert env.kind is EnvelopeKind.GOLDFIVE_EVENT
        # The forwarded payload IS the original Event proto with the
        # typed payload preserved.
        assert env.payload is evt
        assert env.payload.WhichOneof("payload") == "invocation_cancelled"
        assert env.payload.invocation_cancelled.invocation_id == "inv-42"
        assert env.payload.invocation_cancelled.severity == "critical"
        assert made[0].notify_count >= 1

    @pytest.mark.asyncio
    async def test_proto_event_envelope_metadata_preserved(
        self, sink: HarmonografSink, client: Client
    ):
        """Envelope metadata (run_id, sequence, session_id) on the
        ``Event`` rides through verbatim — the server reads these off
        the parent envelope when persisting."""
        evt = _make_proto_event(
            run_id="run-xyz", sequence=42, session_id="sess-other"
        )
        await sink.emit(evt)
        env = _drain(client)[0]
        assert env.payload.run_id == "run-xyz"
        assert env.payload.sequence == 42
        assert env.payload.session_id == "sess-other"


class TestInvocationCancelledDictDropped:
    """Migration regression: the dict envelope path that PR #187
    used to convert into the placeholder
    ``harmonograf.v1.InvocationCancelled`` is gone.

    A pre-#262 goldfive (still emitting dict envelopes for the cancel)
    must NOT crash the sink, but also must not silently push a wrong
    record onto the wire — there is no harmonograf proto to materialize
    into. The dict is dropped at DEBUG.
    """

    @pytest.mark.asyncio
    async def test_invocation_cancelled_dict_is_dropped(
        self,
        sink: HarmonografSink,
        client: Client,
        caplog: pytest.LogCaptureFixture,
    ):
        caplog.set_level(logging.DEBUG, logger="harmonograf_client.sink")
        evt = {
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
        await sink.emit(evt)
        # No envelope pushed — no harmonograf proto exists for this dict
        # shape after the migration.
        assert _drain(client) == []
        # Debug log fires so operators see the drop without an exception.
        assert any(
            "ignoring unknown dict goldfive event" in rec.getMessage()
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_unknown_dict_kind_still_dropped(
        self,
        sink: HarmonografSink,
        client: Client,
        caplog: pytest.LogCaptureFixture,
    ):
        """Generic dict-envelope handling for unknown kinds is preserved
        — only the ``invocation_cancelled``-specific conversion was
        removed. Future dict-only events land on the same drop branch
        until they are typed and routed via ``goldfive_event``."""
        caplog.set_level(logging.DEBUG, logger="harmonograf_client.sink")
        evt = {"kind": "some_future_event", "payload": {}}
        await sink.emit(evt)
        assert _drain(client) == []
        assert any(
            "ignoring unknown dict goldfive event" in rec.getMessage()
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_emit_after_close_is_silent_drop(
        self, sink: HarmonografSink, client: Client
    ):
        """Late events from a tearing-down runner are dropped silently —
        applies to both the proto path and the dict path."""
        await sink.close()
        await sink.emit(_make_proto_event())
        await sink.emit({"kind": "invocation_cancelled", "payload": {}})
        assert _drain(client) == []
