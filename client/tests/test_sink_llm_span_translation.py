"""Tests for LLM-call Event → SpanStart/SpanEnd translation at the
:class:`HarmonografSink` boundary (harmonograf Option X).

Background
----------
Goldfive emits three LLM-call-flavored Event oneof variants:

* ``GoldfiveLLMCallStart`` / ``GoldfiveLLMCallEnd`` — pairs around every
  internal ``await call_llm(...)`` (planner refine, goal-derive, ...)
* ``ReasoningJudgeInvoked`` — terminal-only record of every judge call.

Pre-Option-X these rode as ``goldfive_event`` envelopes and the frontend
synthesized "spans" locally — requiring a new handler for every new
event kind and producing spans that weren't stored in the server's
``spans`` table. Option X translates at the client sink so the server
stores them as real spans and the frontend renders them uniformly.

These tests pin the contract:

* translated variants never reach ``emit_goldfive_event`` — they become
  ``SPAN_START`` / ``SPAN_END`` envelopes on the same ring buffer;
* every other goldfive oneof passes through unchanged;
* the judge variant preserves the full ``judge.*`` attribute vocabulary
  so :class:`JudgeInvocationDetail` (harmonograf#147) keeps reading the
  same keys post-migration.
"""

from __future__ import annotations

from typing import Any

import pytest
from goldfive.pb.goldfive.v1 import events_pb2 as ge

from harmonograf_client.buffer import EnvelopeKind, SpanEnvelope
from harmonograf_client.client import Client
from harmonograf_client.sink import HarmonografSink

from tests._fixtures import FakeTransport, make_factory


CLIENT_AGENT_ID = "presentation-orchestrated-abc123"
GOLDFIVE_AGENT_ID = f"{CLIENT_AGENT_ID}:goldfive"


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="presentation",
        agent_id=CLIENT_AGENT_ID,
        session_id="sess-x",
        framework="ADK",
        buffer_size=32,
        _transport_factory=make_factory(made),
    )


@pytest.fixture
def sink(client: Client) -> HarmonografSink:
    return HarmonografSink(client)


def _drain(client: Client) -> list[SpanEnvelope]:
    return list(client._events.drain())


def _event(which_setter, event_id: str = "e-1", run_id: str = "run-1") -> ge.Event:
    evt = ge.Event()
    evt.event_id = event_id
    evt.run_id = run_id
    evt.sequence = 0
    # Stamp a non-zero emitted_at so translators can derive timestamps.
    evt.emitted_at.seconds = 1_700_000_000
    evt.emitted_at.nanos = 250_000_000
    which_setter(evt)
    return evt


def _attr_str(env: SpanEnvelope, key: str) -> str:
    return env.payload.span.attributes[key].string_value


def _attr_bool(env: SpanEnvelope, key: str) -> bool:
    return env.payload.span.attributes[key].bool_value


# ---------------------------------------------------------------------------
# GoldfiveLLMCallStart → SpanStart
# ---------------------------------------------------------------------------


class TestGoldfiveLLMCallStart:
    @pytest.mark.asyncio
    async def test_becomes_span_start(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        def setup(e: ge.Event) -> None:
            s = e.goldfive_llm_call_start
            s.span_id = "span-refine-1"
            s.name = "refine_steer"
            s.model = "gpt-4o"
            s.task_id = "task-42"
            s.start_time_ns = 1_700_000_000_000_000_000
        evt = _event(setup)
        await sink.emit(evt)

        envs = _drain(client)
        # Exactly one SPAN_START, no goldfive_event forwarded.
        assert len(envs) == 1
        assert envs[0].kind is EnvelopeKind.SPAN_START
        span = envs[0].payload.span
        assert span.id == "span-refine-1"
        assert span.agent_id == GOLDFIVE_AGENT_ID
        assert span.name == "refine_steer"
        # LLM_CALL enum value.
        types_pb2 = client._types_pb2
        assert span.kind == types_pb2.SPAN_KIND_LLM_CALL
        # start_time_ns is preserved on the wire.
        assert span.start_time.seconds == 1_700_000_000
        # Attributes round-trip run/model/task.
        assert _attr_str(envs[0], "goldfive.model") == "gpt-4o"
        assert _attr_str(envs[0], "goldfive.task_id") == "task-42"
        assert _attr_str(envs[0], "goldfive.run_id") == "run-1"
        assert _attr_str(envs[0], "goldfive.call_name") == "refine_steer"

    @pytest.mark.asyncio
    async def test_missing_span_id_is_derived_from_event(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        """A runner that forgets to stamp ``span_id`` still produces a
        renderable span — deterministic derivation from event_id."""

        def setup(e: ge.Event) -> None:
            s = e.goldfive_llm_call_start
            s.name = "goal_derive"
            s.start_time_ns = 1_700_000_000_000_000_000
        evt = _event(setup, event_id="e-bare")
        await sink.emit(evt)

        envs = _drain(client)
        assert envs[0].payload.span.id == "goldfive-goldfive_llm_call_start-e-bare"


# ---------------------------------------------------------------------------
# GoldfiveLLMCallEnd → SpanEnd
# ---------------------------------------------------------------------------


class TestGoldfiveLLMCallEnd:
    @pytest.mark.asyncio
    async def test_becomes_span_end_completed(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        def setup(e: ge.Event) -> None:
            end = e.goldfive_llm_call_end
            end.span_id = "span-refine-1"
            end.name = "refine_steer"
            end.end_time_ns = 1_700_000_000_500_000_000
            end.status = "completed"
        evt = _event(setup)
        await sink.emit(evt)

        envs = _drain(client)
        assert len(envs) == 1
        assert envs[0].kind is EnvelopeKind.SPAN_END
        se = envs[0].payload
        types_pb2 = client._types_pb2
        assert se.span_id == "span-refine-1"
        assert se.status == types_pb2.SPAN_STATUS_COMPLETED
        assert se.end_time.seconds == 1_700_000_000
        # Echo of the call name survives for End-before-Start consumers.
        assert se.attributes["goldfive.call_name"].string_value == "refine_steer"
        assert se.attributes["goldfive.status"].string_value == "completed"
        # No error plumbed on success.
        assert se.error.message == ""

    @pytest.mark.asyncio
    async def test_failed_status_plumbs_error_message(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        def setup(e: ge.Event) -> None:
            end = e.goldfive_llm_call_end
            end.span_id = "span-boom"
            end.name = "plan_generate"
            end.end_time_ns = 1_700_000_000_500_000_000
            end.status = "failed"
            end.error = "timeout after 30s"
        evt = _event(setup)
        await sink.emit(evt)

        envs = _drain(client)
        assert len(envs) == 1
        se = envs[0].payload
        types_pb2 = client._types_pb2
        assert se.status == types_pb2.SPAN_STATUS_FAILED
        assert se.error.type == "goldfive_llm_call"
        assert se.error.message == "timeout after 30s"


# ---------------------------------------------------------------------------
# ReasoningJudgeInvoked → SpanStart + SpanEnd (pair)
# ---------------------------------------------------------------------------


class TestReasoningJudgeInvoked:
    @pytest.mark.asyncio
    async def test_becomes_span_pair_in_order(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        def setup(e: ge.Event) -> None:
            ju = e.reasoning_judge_invoked
            ju.run_id = "run-1"
            ju.task_id = "t-work"
            ju.subject_agent_id = "research_agent"
            ju.model = "claude-3-opus"
            ju.elapsed_ms = 250
            ju.reasoning_input = "the model is considering options A and B"
            ju.raw_response = '{"on_task": true}'
            ju.on_task = True
            ju.severity = ""
            ju.reason = "staying within scope"
        evt = _event(setup, event_id="e-judge-1")
        await sink.emit(evt)

        envs = _drain(client)
        # Exactly: SPAN_START then SPAN_END, back-to-back.
        assert [env.kind for env in envs] == [
            EnvelopeKind.SPAN_START,
            EnvelopeKind.SPAN_END,
        ]
        # Ids match across the pair.
        assert envs[0].payload.span.id == envs[1].payload.span_id

    @pytest.mark.asyncio
    async def test_judge_attributes_preserved(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        """All ``judge.*`` attribute reads used by ``JudgeInvocationDetail``
        must land on the translated SpanStart verbatim. This pins the
        contract from ``frontend/src/lib/interventionDetail.ts``."""

        def setup(e: ge.Event) -> None:
            ju = e.reasoning_judge_invoked
            ju.run_id = "run-1"
            ju.task_id = "t-work"
            ju.subject_agent_id = "research_agent"
            ju.model = "claude-3-opus"
            ju.elapsed_ms = 1500
            ju.reasoning_input = "chain of thought text"
            ju.raw_response = '{"on_task": false, "severity": "warning", "reason": "scope creep"}'
            ju.on_task = False
            ju.severity = "warning"
            ju.reason = "scope creep"
        evt = _event(setup, event_id="e-judge-2")
        await sink.emit(evt)

        envs = _drain(client)
        start = envs[0]
        assert _attr_str(start, "judge.kind") == "judge"
        assert _attr_str(start, "judge.event_id") == "e-judge-2"
        assert _attr_str(start, "judge.verdict") == "warning"
        assert _attr_bool(start, "judge.on_task") is False
        assert _attr_str(start, "judge.severity") == "warning"
        assert _attr_str(start, "judge.reason") == "scope creep"
        assert _attr_str(start, "judge.reasoning_input") == "chain of thought text"
        # Back-compat alias (detail resolver reads ``judge.reasoning``).
        assert _attr_str(start, "judge.reasoning") == "scope creep"
        assert _attr_str(start, "judge.raw_response").startswith("{")
        assert _attr_str(start, "judge.elapsed_ms") == "1500"
        assert _attr_str(start, "judge.model") == "claude-3-opus"
        assert _attr_str(start, "judge.subject_agent_id") == "research_agent"
        # Alias checked by detail resolver's fallback read order.
        assert _attr_str(start, "judge.target_agent_id") == "research_agent"
        assert _attr_str(start, "judge.target_task_id") == "t-work"

    @pytest.mark.asyncio
    async def test_span_has_visible_width_from_elapsed_ms(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        """The pre-Option-X synthesizer covered ``[emitted_at - elapsed, emitted_at]``
        so zero-width judge spans weren't invisible on the Gantt. The
        sink's translation must preserve that invariant — see
        harmonograf#149."""

        def setup(e: ge.Event) -> None:
            # emitted_at set by _event() to sec=1_700_000_000 nanos=250_000_000.
            ju = e.reasoning_judge_invoked
            ju.elapsed_ms = 1000
            ju.on_task = True
        evt = _event(setup)
        await sink.emit(evt)

        envs = _drain(client)
        start_span = envs[0].payload.span
        end_msg = envs[1].payload
        # end_time is the event's emitted_at (1_700_000_000.25s).
        assert end_msg.end_time.seconds == 1_700_000_000
        assert end_msg.end_time.nanos == 250_000_000
        # start_time is 1s earlier.
        assert start_span.start_time.seconds == 1_699_999_999
        assert start_span.start_time.nanos == 250_000_000

    @pytest.mark.asyncio
    async def test_verdict_derived_from_on_task(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        """When ``on_task`` is true, verdict stamps ``"on_task"`` even
        with no explicit verdict field on the wire."""

        def setup(e: ge.Event) -> None:
            ju = e.reasoning_judge_invoked
            ju.on_task = True
        evt = _event(setup)
        await sink.emit(evt)

        envs = _drain(client)
        assert _attr_str(envs[0], "judge.verdict") == "on_task"
        assert envs[0].payload.span.name == "judge: on_task"


# ---------------------------------------------------------------------------
# Pass-through: non-LLM events still take the goldfive_event path
# ---------------------------------------------------------------------------


class TestPassThroughUnchanged:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "setup,kind",
        [
            (lambda e: e.run_started.SetInParent(), "run_started"),
            (lambda e: e.drift_detected.SetInParent(), "drift_detected"),
            (lambda e: e.task_started.SetInParent(), "task_started"),
            (lambda e: e.plan_revised.SetInParent(), "plan_revised"),
        ],
    )
    async def test_non_llm_events_forwarded_unchanged(
        self,
        sink: HarmonografSink,
        client: Client,
        setup: Any,
        kind: str,
    ) -> None:
        evt = _event(setup, event_id=f"e-{kind}")
        await sink.emit(evt)

        envs = _drain(client)
        assert len(envs) == 1
        assert envs[0].kind is EnvelopeKind.GOLDFIVE_EVENT
        assert envs[0].payload.WhichOneof("payload") == kind


# ---------------------------------------------------------------------------
# Agent-id compound form + idempotency
# ---------------------------------------------------------------------------


class TestGoldfiveAgentId:
    @pytest.mark.asyncio
    async def test_agent_id_is_compound_goldfive(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        def setup(e: ge.Event) -> None:
            e.goldfive_llm_call_start.span_id = "s1"
            e.goldfive_llm_call_start.name = "goal_derive"
        evt = _event(setup)
        await sink.emit(evt)

        envs = _drain(client)
        assert envs[0].payload.span.agent_id == f"{CLIENT_AGENT_ID}:goldfive"

    @pytest.mark.asyncio
    async def test_judge_span_agent_id_is_compound_goldfive(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        def setup(e: ge.Event) -> None:
            e.reasoning_judge_invoked.on_task = True
        evt = _event(setup)
        await sink.emit(evt)

        envs = _drain(client)
        assert envs[0].payload.span.agent_id == f"{CLIENT_AGENT_ID}:goldfive"


# ---------------------------------------------------------------------------
# Closed-sink semantics
# ---------------------------------------------------------------------------


class TestClosedSinkDropsTranslated:
    @pytest.mark.asyncio
    async def test_closed_sink_drops_llm_start(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        await sink.close()

        def setup(e: ge.Event) -> None:
            e.goldfive_llm_call_start.span_id = "s1"
            e.goldfive_llm_call_start.name = "refine"
        evt = _event(setup)
        await sink.emit(evt)

        assert _drain(client) == []

    @pytest.mark.asyncio
    async def test_closed_sink_drops_judge(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        await sink.close()

        def setup(e: ge.Event) -> None:
            e.reasoning_judge_invoked.on_task = False
            e.reasoning_judge_invoked.severity = "warning"
        evt = _event(setup)
        await sink.emit(evt)

        assert _drain(client) == []
