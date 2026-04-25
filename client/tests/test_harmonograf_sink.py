"""Tests for :class:`harmonograf_client.sink.HarmonografSink`.

The sink is the goldfive -> harmonograf seam introduced in Phase B of
the goldfive migration (issue #3). Each ``goldfive.v1.Event`` ``emit``-ed
through the sink must end up on the underlying ``Client``'s ring buffer
as a ``GOLDFIVE_EVENT`` envelope, with the transport notified. The payload
must round-trip through a ``TelemetryUp(goldfive_event=...)`` wrapper
identically to how the transport serializer would see it.
"""

from __future__ import annotations

import pytest
from goldfive.pb.goldfive.v1 import events_pb2 as ge
from goldfive.pb.goldfive.v1 import types_pb2 as gt

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
        name="test-agent",
        agent_id="agent-sink",
        session_id="sess-sink",
        framework="CUSTOM",
        buffer_size=32,
        _transport_factory=make_factory(made),
    )


@pytest.fixture
def sink(client: Client) -> HarmonografSink:
    return HarmonografSink(client)


def _drain(client: Client) -> list:
    return list(client._events.drain())


def _make_run_started(run_id: str = "run-1", sequence: int = 0) -> ge.Event:
    evt = ge.Event()
    evt.event_id = "e-run-started"
    evt.run_id = run_id
    evt.sequence = sequence
    evt.run_started.run_id = run_id
    evt.run_started.goal_summary = "summarize Q3"
    return evt


def _make_plan_submitted(run_id: str = "run-1", sequence: int = 1) -> ge.Event:
    evt = ge.Event()
    evt.event_id = "e-plan"
    evt.run_id = run_id
    evt.sequence = sequence
    plan = evt.plan_submitted.plan
    plan.id = "plan-1"
    plan.run_id = run_id
    plan.summary = "two-step"
    t1 = plan.tasks.add()
    t1.id = "t1"
    t1.title = "step one"
    t1.status = gt.TASK_STATUS_PENDING
    t2 = plan.tasks.add()
    t2.id = "t2"
    t2.title = "step two"
    t2.status = gt.TASK_STATUS_PENDING
    edge = plan.edges.add()
    edge.from_task_id = "t1"
    edge.to_task_id = "t2"
    return evt


def _make_task_started(task_id: str = "t1", sequence: int = 2) -> ge.Event:
    evt = ge.Event()
    evt.event_id = "e-task-started"
    evt.run_id = "run-1"
    evt.sequence = sequence
    evt.task_started.task_id = task_id
    evt.task_started.detail = "calling tool"
    return evt


def _make_drift_detected(sequence: int = 3) -> ge.Event:
    evt = ge.Event()
    evt.event_id = "e-drift"
    evt.run_id = "run-1"
    evt.sequence = sequence
    evt.drift_detected.kind = gt.DRIFT_KIND_TOOL_ERROR
    evt.drift_detected.severity = gt.DRIFT_SEVERITY_WARNING
    evt.drift_detected.detail = "flaky backend"
    evt.drift_detected.current_task_id = "t1"
    return evt


def _make_task_transitioned(sequence: int = 4) -> ge.Event:
    """Build a goldfive#267 / #251 R4 ``TaskTransitioned`` envelope.

    Used to verify the sink's proto passthrough handles the new oneof
    variant the same way it handles every other typed
    ``goldfive.v1.Event`` payload — no special-casing on the sink side
    because the variant rides the same buffer / transport pipeline.
    """
    evt = ge.Event()
    evt.event_id = "e-transition"
    evt.run_id = "run-1"
    evt.sequence = sequence
    evt.task_transitioned.task_id = "t-7"
    evt.task_transitioned.from_status = "RUNNING"
    evt.task_transitioned.to_status = "COMPLETED"
    evt.task_transitioned.source = "llm_report"
    evt.task_transitioned.revision_stamp = 0
    evt.task_transitioned.agent_name = "researcher_agent"
    evt.task_transitioned.invocation_id = "inv-7"
    return evt


class TestEmit:
    @pytest.mark.asyncio
    async def test_emit_run_started_pushes_envelope(
        self, sink: HarmonografSink, client: Client, made: list[FakeTransport]
    ):
        evt = _make_run_started()
        await sink.emit(evt)
        envs = _drain(client)
        assert len(envs) == 1
        env = envs[0]
        assert env.kind is EnvelopeKind.GOLDFIVE_EVENT
        assert env.span_id == ""
        # Payload round-trips intact.
        assert env.payload.run_id == "run-1"
        assert env.payload.WhichOneof("payload") == "run_started"
        assert env.payload.run_started.goal_summary == "summarize Q3"
        # Transport was pinged so its send loop can drain the envelope.
        assert made[0].notify_count >= 1

    @pytest.mark.asyncio
    async def test_emit_preserves_sequence_and_run_id(
        self, sink: HarmonografSink, client: Client
    ):
        await sink.emit(_make_run_started(run_id="run-xyz", sequence=0))
        await sink.emit(_make_plan_submitted(run_id="run-xyz", sequence=1))
        await sink.emit(_make_task_started(sequence=2))
        envs = _drain(client)
        assert [e.payload.sequence for e in envs] == [0, 1, 2]
        assert {e.payload.run_id for e in envs} == {"run-xyz", "run-1"}

    @pytest.mark.asyncio
    async def test_emit_wraps_in_telemetry_up_via_transport_path(
        self, sink: HarmonografSink, client: Client, made: list[FakeTransport]
    ):
        """The envelope payload the sink pushes must be compatible with the
        ``_envelope_to_up`` serializer the transport calls at dequeue
        time — i.e. ``TelemetryUp(goldfive_event=payload)`` must succeed
        without a type/identity mismatch."""

        from harmonograf_client.pb import telemetry_pb2

        evt = _make_plan_submitted()
        await sink.emit(evt)
        env = _drain(client)[0]
        up = telemetry_pb2.TelemetryUp(goldfive_event=env.payload)
        assert up.WhichOneof("msg") == "goldfive_event"
        assert up.goldfive_event.WhichOneof("payload") == "plan_submitted"
        assert up.goldfive_event.plan_submitted.plan.id == "plan-1"
        assert len(up.goldfive_event.plan_submitted.plan.tasks) == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "factory,expected_kind",
        [
            (_make_run_started, "run_started"),
            (_make_plan_submitted, "plan_submitted"),
            (_make_task_started, "task_started"),
            (_make_drift_detected, "drift_detected"),
            # goldfive#267 / #251 R4: TaskTransitioned rides the same
            # proto-passthrough path; no sink-side translation needed
            # (the new variant is forwarded as-is to the wire).
            (_make_task_transitioned, "task_transitioned"),
        ],
    )
    async def test_emit_covers_each_payload_kind(
        self,
        sink: HarmonografSink,
        client: Client,
        factory,
        expected_kind: str,
    ):
        await sink.emit(factory())
        envs = _drain(client)
        assert len(envs) == 1
        assert envs[0].payload.WhichOneof("payload") == expected_kind

    @pytest.mark.asyncio
    async def test_emit_task_transitioned_payload_round_trips(
        self, sink: HarmonografSink, client: Client
    ):
        """The TaskTransitioned payload survives the sink with every
        field intact — no canonicalization rewrite (the sink's
        ``_canonicalize_agent_ids`` does not currently rewrite
        ``TaskTransitioned.agent_name``; the field is bare and the
        frontend tolerates either form). This test pins the current
        passthrough contract so a future bare→compound rewrite is a
        deliberate change, not an accident.
        """
        evt = _make_task_transitioned()
        await sink.emit(evt)
        envs = _drain(client)
        assert len(envs) == 1
        payload = envs[0].payload
        assert payload.WhichOneof("payload") == "task_transitioned"
        t = payload.task_transitioned
        assert t.task_id == "t-7"
        assert t.from_status == "RUNNING"
        assert t.to_status == "COMPLETED"
        assert t.source == "llm_report"
        assert t.agent_name == "researcher_agent"
        assert t.invocation_id == "inv-7"


class TestClose:
    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, sink: HarmonografSink):
        await sink.close()
        await sink.close()
        assert sink._closed

    @pytest.mark.asyncio
    async def test_close_does_not_shut_down_client(
        self, sink: HarmonografSink, client: Client, made: list[FakeTransport]
    ):
        await sink.close()
        # Client lifecycle is orthogonal to the sink's.
        assert made[0].shutdown_called is False

    @pytest.mark.asyncio
    async def test_emit_after_close_is_silent_drop(
        self, sink: HarmonografSink, client: Client
    ):
        await sink.close()
        await sink.emit(_make_run_started())
        assert _drain(client) == []


class TestBackpressure:
    @pytest.mark.asyncio
    async def test_emit_does_not_raise_when_buffer_full(
        self, made: list[FakeTransport]
    ):
        """A tiny buffer plus enough events forces the drop policy to run.

        The sink must never raise — agents depend on non-blocking emit.
        """

        small = Client(
            name="small",
            agent_id="a",
            session_id="s",
            buffer_size=2,
            _transport_factory=make_factory(made),
        )
        sink = HarmonografSink(small)
        # Push well beyond capacity; the underlying ring buffer's eviction
        # tiers kick in. All we assert is no exception escapes.
        for i in range(10):
            await sink.emit(_make_run_started(sequence=i))
        # Buffer is bounded.
        assert len(small._events) <= 2
