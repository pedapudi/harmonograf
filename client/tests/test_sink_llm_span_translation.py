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


# ---------------------------------------------------------------------------
# Agent-row display-name registration (harmonograf#156 Issue A)
# ---------------------------------------------------------------------------


class TestGoldfiveAgentRegistration:
    """Pin the first-translated-span registration contract.

    The server's auto-register-on-first-span path (see
    ``ingest.py::_register_agent_if_new``) reads ``hgraf.agent.name`` and
    ``hgraf.agent.kind`` off the first span's attributes. Without the
    stamp, the ``<client>:goldfive`` row ends up labeled with whichever
    goldfive-internal call fired first (``goal_derive`` in every real
    session). Once registered, subsequent translations skip the stamp.
    """

    @pytest.mark.asyncio
    async def test_first_goldfive_span_registers_display_name(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        def setup(e: ge.Event) -> None:
            s = e.goldfive_llm_call_start
            s.span_id = "s-first"
            s.name = "goal_derive"
        evt = _event(setup)
        await sink.emit(evt)

        envs = _drain(client)
        assert len(envs) == 1
        assert _attr_str(envs[0], "hgraf.agent.name") == "goldfive"
        assert _attr_str(envs[0], "hgraf.agent.kind") == "goldfive"
        # The span is emitted against the compound agent id — confirms the
        # server will key the harvest against the goldfive row and not
        # the client root.
        assert envs[0].payload.span.agent_id == GOLDFIVE_AGENT_ID

    @pytest.mark.asyncio
    async def test_subsequent_spans_do_not_re_register(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        def setup_a(e: ge.Event) -> None:
            s = e.goldfive_llm_call_start
            s.span_id = "s-1"
            s.name = "goal_derive"

        def setup_b(e: ge.Event) -> None:
            s = e.goldfive_llm_call_start
            s.span_id = "s-2"
            s.name = "refine_steer"

        await sink.emit(_event(setup_a, event_id="e-1"))
        await sink.emit(_event(setup_b, event_id="e-2"))

        envs = _drain(client)
        assert len(envs) == 2
        # First span carries the registration attrs.
        assert _attr_str(envs[0], "hgraf.agent.name") == "goldfive"
        # Second span is free of them — re-stamping burns bytes for no
        # behavioural gain (the server's ``seen_routes`` cache would
        # ignore them anyway).
        assert "hgraf.agent.name" not in envs[1].payload.span.attributes
        assert "hgraf.agent.kind" not in envs[1].payload.span.attributes

    @pytest.mark.asyncio
    async def test_first_translated_judge_also_registers(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        """The registration flag keys off "first translated span in this
        sink's lifetime" — not "first ``goldfive_llm_call_start``". A
        session that opens with a judge event must also register the row.
        """

        def setup(e: ge.Event) -> None:
            ju = e.reasoning_judge_invoked
            ju.on_task = True
            ju.elapsed_ms = 100
        evt = _event(setup)
        await sink.emit(evt)

        envs = _drain(client)
        # Start carries the stamp; End does not (first-sight gates on the
        # Start alone).
        assert _attr_str(envs[0], "hgraf.agent.name") == "goldfive"
        assert _attr_str(envs[0], "hgraf.agent.kind") == "goldfive"


# ---------------------------------------------------------------------------
# Decision-context attribute stamping (harmonograf#156 Issue B)
# ---------------------------------------------------------------------------
#
# Goldfive's sibling PR extends GoldfiveLLMCallStart/End with
# ``input_preview`` / ``output_preview`` / ``target_agent_id`` /
# ``target_task_id`` / ``decision_summary``. At the time this test file
# was written the goldfive submodule is pre-bump so the proto fields
# don't exist yet. We exercise both modes:
#
# * post-bump: build a stub payload + event that quacks like the future
#   proto message (``WhichOneof`` plus the payload attribute accessors).
# * pre-bump: use the real ``ge.Event`` and assert no new attrs leak +
#   no crash.


class _StubEvent:
    """Quacks like a ``ge.Event`` enough for the sink to route.

    The translator reads ``WhichOneof("payload")`` to dispatch and then
    pulls the payload via attribute lookup (``event.goldfive_llm_call_start``
    etc). No other event-level fields are required for the decision-
    context tests — ``run_id``, ``session_id``, ``event_id``,
    ``emitted_at`` are consumed by the translator via ``or ""`` / ``or None``
    fallbacks so defaults are fine.
    """

    def __init__(self, which: str, payload: Any) -> None:
        self._which = which
        # Attach the payload under the oneof-case-name attribute the
        # translator reads. Other oneof-slot attributes are deliberately
        # missing so any accidental cross-field read raises AttributeError
        # rather than silently returning a default proto message.
        setattr(self, which, payload)
        self.event_id = "stub-evt"
        self.run_id = "stub-run"
        self.session_id = "stub-sess"
        # emitted_at: a duck-typed Timestamp with seconds + nanos so the
        # reasoning-judge path can compute start/end.
        self.emitted_at = _StubTs(1_700_000_000, 0)

    def WhichOneof(self, _oneof: str) -> str:
        return self._which


class _StubTs:
    def __init__(self, seconds: int, nanos: int) -> None:
        self.seconds = seconds
        self.nanos = nanos


class _StubLLMPayload:
    """Duck-typed ``GoldfiveLLMCallStart`` / ``GoldfiveLLMCallEnd``.

    Carries every field the translator reads, including the post-bump
    decision-context fields. Any attribute absent from ``**kwargs``
    defaults to the proto-scalar zero value (``""`` / ``0``) so the
    skip-on-empty logic exercises realistically.
    """

    _DEFAULTS = {
        "span_id": "",
        "name": "",
        "model": "",
        "task_id": "",
        "start_time_ns": 0,
        "end_time_ns": 0,
        "status": "",
        "error": "",
        "input_preview": "",
        "output_preview": "",
        "target_agent_id": "",
        "target_task_id": "",
        "decision_summary": "",
    }

    def __init__(self, **kwargs: Any) -> None:
        for k, default in self._DEFAULTS.items():
            setattr(self, k, kwargs.pop(k, default))
        if kwargs:
            raise TypeError(f"unknown stub fields: {list(kwargs)}")


class _StubJudgePayload:
    """Duck-typed ``ReasoningJudgeInvoked`` with post-bump context fields."""

    _DEFAULTS = {
        "run_id": "",
        "task_id": "",
        "subject_agent_id": "",
        "model": "",
        "elapsed_ms": 0,
        "reasoning_input": "",
        "raw_response": "",
        "on_task": False,
        "severity": "",
        "reason": "",
        "input_preview": "",
        "output_preview": "",
        "target_agent_id": "",
        "target_task_id": "",
        "decision_summary": "",
    }

    def __init__(self, **kwargs: Any) -> None:
        for k, default in self._DEFAULTS.items():
            setattr(self, k, kwargs.pop(k, default))
        if kwargs:
            raise TypeError(f"unknown stub fields: {list(kwargs)}")


class TestDecisionContextStamping:
    @pytest.mark.asyncio
    async def test_input_preview_stamped_as_span_attribute(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        payload = _StubLLMPayload(
            span_id="s1",
            name="refine_steer",
            input_preview="foo bar baz",
        )
        await sink.emit(_StubEvent("goldfive_llm_call_start", payload))

        envs = _drain(client)
        assert len(envs) == 1
        assert _attr_str(envs[0], "goldfive.input_preview") == "foo bar baz"

    @pytest.mark.asyncio
    async def test_output_preview_only_on_end(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        """Start doesn't carry ``output_preview`` semantically — even if
        the stub leaks the attribute with an empty value, the sink's
        skip-on-empty logic keeps the Start span clean."""
        start = _StubLLMPayload(
            span_id="s-pair",
            name="refine_steer",
            input_preview="prompt text",
            output_preview="",  # empty → not stamped on Start
        )
        await sink.emit(_StubEvent("goldfive_llm_call_start", start))

        end = _StubLLMPayload(
            span_id="s-pair",
            name="refine_steer",
            status="completed",
            end_time_ns=1_700_000_000_500_000_000,
            output_preview="bar",  # non-empty → stamped on End
        )
        await sink.emit(_StubEvent("goldfive_llm_call_end", end))

        envs = _drain(client)
        assert [env.kind for env in envs] == [
            EnvelopeKind.SPAN_START,
            EnvelopeKind.SPAN_END,
        ]
        assert (
            "goldfive.output_preview"
            not in envs[0].payload.span.attributes
        )
        assert envs[1].payload.attributes["goldfive.output_preview"].string_value == "bar"

    @pytest.mark.asyncio
    async def test_target_agent_id_compound_canonicalization(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        payload = _StubLLMPayload(
            span_id="s-canon",
            name="judge_reasoning",
            target_agent_id="research_agent",  # bare
            target_task_id="t-99",
        )
        await sink.emit(_StubEvent("goldfive_llm_call_start", payload))

        envs = _drain(client)
        assert (
            _attr_str(envs[0], "goldfive.target_agent_id")
            == f"{CLIENT_AGENT_ID}:research_agent"
        )
        assert _attr_str(envs[0], "goldfive.target_task_id") == "t-99"

    @pytest.mark.asyncio
    async def test_target_agent_id_already_compound_passthrough(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        """Already-compound ids pass through untouched — the ``:``
        presence check in :meth:`_compound` is idempotent."""
        compound = f"{CLIENT_AGENT_ID}:research_agent"
        payload = _StubLLMPayload(
            span_id="s-id",
            name="judge_reasoning",
            target_agent_id=compound,
        )
        await sink.emit(_StubEvent("goldfive_llm_call_start", payload))

        envs = _drain(client)
        assert _attr_str(envs[0], "goldfive.target_agent_id") == compound

    @pytest.mark.asyncio
    async def test_decision_summary_stamped_on_end(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        end = _StubLLMPayload(
            span_id="s-end",
            name="plan_generate",
            status="completed",
            decision_summary="picked plan A",
        )
        await sink.emit(_StubEvent("goldfive_llm_call_end", end))

        envs = _drain(client)
        assert envs[0].kind is EnvelopeKind.SPAN_END
        assert (
            envs[0].payload.attributes["goldfive.decision_summary"].string_value
            == "picked plan A"
        )

    @pytest.mark.asyncio
    async def test_empty_fields_not_stamped(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        """All decision-context fields empty → no ``goldfive.{input_preview,
        output_preview,target_*,decision_summary}`` keys on the span."""
        end = _StubLLMPayload(
            span_id="s-empty",
            name="refine_steer",
            status="completed",
        )
        await sink.emit(_StubEvent("goldfive_llm_call_end", end))

        envs = _drain(client)
        attr_keys = set(envs[0].payload.attributes.keys())
        # None of the post-bump keys leaked.
        for k in (
            "goldfive.input_preview",
            "goldfive.output_preview",
            "goldfive.target_agent_id",
            "goldfive.target_task_id",
            "goldfive.decision_summary",
        ):
            assert k not in attr_keys

    @pytest.mark.asyncio
    async def test_judge_event_also_stamps_decision_context(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        """``ReasoningJudgeInvoked`` gains the same decision-context
        fields as the LLM-call pair in the sibling goldfive PR. The
        judge span collapses both preview + summary onto its SpanStart
        (there is no separate End record to carry them)."""
        payload = _StubJudgePayload(
            run_id="run-j",
            task_id="t-j",
            subject_agent_id="research_agent",
            model="claude-3-opus",
            elapsed_ms=250,
            on_task=True,
            input_preview="the reasoning input preview",
            output_preview="the judge output preview",
            target_agent_id="research_agent",
            target_task_id="t-j",
            decision_summary="on_task: yes",
        )
        await sink.emit(_StubEvent("reasoning_judge_invoked", payload))

        envs = _drain(client)
        assert [env.kind for env in envs] == [
            EnvelopeKind.SPAN_START,
            EnvelopeKind.SPAN_END,
        ]
        start = envs[0]
        assert _attr_str(start, "goldfive.input_preview") == "the reasoning input preview"
        assert _attr_str(start, "goldfive.output_preview") == "the judge output preview"
        assert _attr_str(start, "goldfive.decision_summary") == "on_task: yes"
        # target_agent_id canonicalized to compound.
        assert (
            _attr_str(start, "goldfive.target_agent_id")
            == f"{CLIENT_AGENT_ID}:research_agent"
        )
        assert _attr_str(start, "goldfive.target_task_id") == "t-j"


# ---------------------------------------------------------------------------
# Forward-compat: pre-goldfive-submodule-bump protos work without crashes
# ---------------------------------------------------------------------------


class TestMissingProtoFieldsGraceful:
    """The goldfive submodule bump may land before or after this PR. If
    the sibling PR hasn't merged yet, ``hasattr(event, 'input_preview')``
    is False and the sink must continue translating without stamping
    the new attrs — no crash, no spammy logs.

    These tests use the real ``ge.Event`` (pre-bump proto at the time of
    writing this suite) so they exercise the ``hasattr`` guard exactly
    as production would.
    """

    @pytest.mark.asyncio
    async def test_real_proto_translates_without_crash(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        def setup(e: ge.Event) -> None:
            s = e.goldfive_llm_call_start
            s.span_id = "s-compat"
            s.name = "refine_steer"
            s.model = "gpt-4o"
            s.start_time_ns = 1_700_000_000_000_000_000
        evt = _event(setup)
        await sink.emit(evt)

        envs = _drain(client)
        assert len(envs) == 1
        assert envs[0].kind is EnvelopeKind.SPAN_START

    @pytest.mark.asyncio
    async def test_real_proto_skips_new_attributes(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        """Without the new proto fields, none of the new
        ``goldfive.{input_preview,output_preview,target_*,decision_summary}``
        keys land on the span."""

        def setup(e: ge.Event) -> None:
            s = e.goldfive_llm_call_start
            s.span_id = "s-no-extras"
            s.name = "refine_steer"
        evt = _event(setup)
        await sink.emit(evt)

        envs = _drain(client)
        attrs = envs[0].payload.span.attributes
        for k in (
            "goldfive.input_preview",
            "goldfive.output_preview",
            "goldfive.target_agent_id",
            "goldfive.target_task_id",
            "goldfive.decision_summary",
        ):
            assert k not in attrs

    @pytest.mark.asyncio
    async def test_real_proto_end_translates_without_crash(
        self, sink: HarmonografSink, client: Client
    ) -> None:
        def setup(e: ge.Event) -> None:
            end = e.goldfive_llm_call_end
            end.span_id = "s-e"
            end.name = "plan_generate"
            end.end_time_ns = 1_700_000_000_500_000_000
            end.status = "completed"
        evt = _event(setup)
        await sink.emit(evt)

        envs = _drain(client)
        assert len(envs) == 1
        assert envs[0].kind is EnvelopeKind.SPAN_END

    @pytest.mark.asyncio
    async def test_missing_field_logs_once(
        self,
        sink: HarmonografSink,
        client: Client,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """One-shot debug log fires when the proto is pre-bump so
        operators can correlate 'no decision context on spans' with
        'submodule needs bump'."""
        import harmonograf_client.sink as sink_mod

        # Reset the module-level one-shot flag so the test sees the log
        # regardless of test-collection order.
        sink_mod._LOGGED_MISSING_PROTO_FIELDS = False
        caplog.set_level("DEBUG", logger=sink_mod.logger.name)

        def setup(e: ge.Event) -> None:
            e.goldfive_llm_call_start.span_id = "s-log"
            e.goldfive_llm_call_start.name = "refine_steer"
        # Two emissions back-to-back — the log should fire exactly once.
        await sink.emit(_event(setup, event_id="e-1"))
        await sink.emit(_event(setup, event_id="e-2"))

        matches = [
            r
            for r in caplog.records
            if "awaiting goldfive submodule bump" in r.getMessage()
        ]
        assert len(matches) == 1
