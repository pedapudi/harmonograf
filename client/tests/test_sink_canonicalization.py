"""Tests for agent-id canonicalization at the HarmonografSink boundary.

Issue history
-------------
Before harmonograf#125 the server rewrote bare ADK agent names
(``coordinator_agent``) onto the telemetry-plugin's compound row keys
(``client-xyz:coordinator_agent``) at ingest via
``find_agent_id_by_name``. That rewrite raced with the target agent's
first-span registration: a DelegationObserved landing before the
sub-agent's SpanStart resolved to the bare name (no row matched), and
the frontend then couldn't bind the arrow endpoint to a lifeline.
Symptom: "transfer arrows missing until refresh" (#111, #117).

The fix moves the rewrite to the source: :class:`HarmonografSink`
canonicalizes every agent-identity field on every goldfive event
before it leaves the client process. After that, the wire carries
compound ids only; ingest and replay are verbatim.

These tests pin the invariant by driving the sink directly and
asserting the payload landing on the client's ring buffer is already
compound.
"""

from __future__ import annotations

import pytest
from goldfive.pb.goldfive.v1 import events_pb2 as ge
from goldfive.pb.goldfive.v1 import types_pb2 as gt

from harmonograf_client.buffer import EnvelopeKind
from harmonograf_client.client import Client
from harmonograf_client.sink import HarmonografSink

from tests._fixtures import FakeTransport, make_factory


CLIENT_AGENT_ID = "presentation-orchestrated-9b2b3a9c7289"


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="presentation",
        agent_id=CLIENT_AGENT_ID,
        session_id="sess-canon",
        framework="ADK",
        buffer_size=32,
        _transport_factory=make_factory(made),
    )


@pytest.fixture
def sink(client: Client) -> HarmonografSink:
    return HarmonografSink(client)


def _drain(client: Client) -> list:
    return list(client._events.drain())


def _make_event(which: str) -> ge.Event:
    evt = ge.Event()
    evt.event_id = f"e-{which}"
    evt.run_id = "run-1"
    evt.sequence = 0
    return evt


# ---- DelegationObserved ---------------------------------------------------


class TestDelegationObserved:
    @pytest.mark.asyncio
    async def test_bare_from_to_rewritten_to_compound(
        self, sink: HarmonografSink, client: Client
    ):
        evt = _make_event("delegation")
        evt.delegation_observed.from_agent = "coordinator_agent"
        evt.delegation_observed.to_agent = "research_agent"
        await sink.emit(evt)

        env = _drain(client)[0]
        d = env.payload.delegation_observed
        assert d.from_agent == f"{CLIENT_AGENT_ID}:coordinator_agent"
        assert d.to_agent == f"{CLIENT_AGENT_ID}:research_agent"

    @pytest.mark.asyncio
    async def test_already_compound_is_idempotent(
        self, sink: HarmonografSink, client: Client
    ):
        """Re-emitting a compound id must not double-prefix."""
        evt = _make_event("delegation")
        evt.delegation_observed.from_agent = f"{CLIENT_AGENT_ID}:coordinator_agent"
        evt.delegation_observed.to_agent = "other-client:some_agent"
        await sink.emit(evt)

        env = _drain(client)[0]
        d = env.payload.delegation_observed
        # Home-client's own agent stays as-is (contains ':' → already compound).
        assert d.from_agent == f"{CLIENT_AGENT_ID}:coordinator_agent"
        # Foreign compound ids are preserved verbatim too — the sink
        # does NOT rewrite them to the home client's prefix.
        assert d.to_agent == "other-client:some_agent"

    @pytest.mark.asyncio
    async def test_empty_fields_left_alone(
        self, sink: HarmonografSink, client: Client
    ):
        evt = _make_event("delegation")
        # Leave from/to empty — e.g. a goldfive variant that didn't
        # populate them. No empty → ``<client>:`` rewrite.
        await sink.emit(evt)

        env = _drain(client)[0]
        d = env.payload.delegation_observed
        assert d.from_agent == ""
        assert d.to_agent == ""


# ---- AgentInvocationStarted / Completed -----------------------------------


class TestAgentInvocation:
    @pytest.mark.asyncio
    async def test_started_bare_rewritten(
        self, sink: HarmonografSink, client: Client
    ):
        evt = _make_event("inv-start")
        evt.agent_invocation_started.agent_name = "coordinator_agent"
        evt.agent_invocation_started.task_id = "t1"
        evt.agent_invocation_started.invocation_id = "inv-1"
        await sink.emit(evt)

        env = _drain(client)[0]
        payload = env.payload.agent_invocation_started
        assert payload.agent_name == f"{CLIENT_AGENT_ID}:coordinator_agent"
        # Non-agent fields untouched.
        assert payload.task_id == "t1"
        assert payload.invocation_id == "inv-1"

    @pytest.mark.asyncio
    async def test_completed_bare_rewritten(
        self, sink: HarmonografSink, client: Client
    ):
        evt = _make_event("inv-done")
        evt.agent_invocation_completed.agent_name = "research_agent"
        evt.agent_invocation_completed.invocation_id = "inv-2"
        await sink.emit(evt)

        env = _drain(client)[0]
        payload = env.payload.agent_invocation_completed
        assert payload.agent_name == f"{CLIENT_AGENT_ID}:research_agent"


# ---- DriftDetected ---------------------------------------------------------


class TestDriftDetected:
    @pytest.mark.asyncio
    async def test_current_agent_id_bare_rewritten(
        self, sink: HarmonografSink, client: Client
    ):
        evt = _make_event("drift")
        evt.drift_detected.kind = gt.DRIFT_KIND_TOOL_ERROR
        evt.drift_detected.severity = gt.DRIFT_SEVERITY_WARNING
        evt.drift_detected.current_agent_id = "research_agent"
        evt.drift_detected.current_task_id = "t-work"
        await sink.emit(evt)

        env = _drain(client)[0]
        d = env.payload.drift_detected
        assert d.current_agent_id == f"{CLIENT_AGENT_ID}:research_agent"
        # Non-agent fields untouched.
        assert d.current_task_id == "t-work"

    @pytest.mark.asyncio
    async def test_empty_current_agent_id_left_alone(
        self, sink: HarmonografSink, client: Client
    ):
        """Drifts minted without a current agent (e.g. goal-drift at
        plan time) must not grow a stray ``<client>:`` prefix."""
        evt = _make_event("drift-noagent")
        evt.drift_detected.kind = gt.DRIFT_KIND_GOAL_DRIFT
        evt.drift_detected.severity = gt.DRIFT_SEVERITY_INFO
        # current_agent_id left empty.
        await sink.emit(evt)

        env = _drain(client)[0]
        assert env.payload.drift_detected.current_agent_id == ""


# ---- PlanSubmitted / PlanRevised -------------------------------------------


class TestPlanCanonicalization:
    @pytest.mark.asyncio
    async def test_plan_submitted_task_assignees_rewritten(
        self, sink: HarmonografSink, client: Client
    ):
        evt = _make_event("plan")
        plan = evt.plan_submitted.plan
        plan.id = "p1"
        plan.run_id = "run-1"
        t1 = plan.tasks.add()
        t1.id = "t1"
        t1.assignee_agent_id = "coordinator_agent"
        t2 = plan.tasks.add()
        t2.id = "t2"
        t2.assignee_agent_id = "research_agent"
        t3 = plan.tasks.add()
        t3.id = "t3"
        # Unassigned task — empty stays empty.
        await sink.emit(evt)

        env = _drain(client)[0]
        tasks = list(env.payload.plan_submitted.plan.tasks)
        assert tasks[0].assignee_agent_id == f"{CLIENT_AGENT_ID}:coordinator_agent"
        assert tasks[1].assignee_agent_id == f"{CLIENT_AGENT_ID}:research_agent"
        assert tasks[2].assignee_agent_id == ""

    @pytest.mark.asyncio
    async def test_plan_revised_task_assignees_rewritten(
        self, sink: HarmonografSink, client: Client
    ):
        evt = _make_event("plan-rev")
        plan = evt.plan_revised.plan
        plan.id = "p1"
        plan.run_id = "run-1"
        plan.revision_index = 1
        t1 = plan.tasks.add()
        t1.id = "t1"
        t1.assignee_agent_id = "coordinator_agent"
        await sink.emit(evt)

        env = _drain(client)[0]
        tasks = list(env.payload.plan_revised.plan.tasks)
        assert tasks[0].assignee_agent_id == f"{CLIENT_AGENT_ID}:coordinator_agent"

    @pytest.mark.asyncio
    async def test_plan_assignee_idempotent_on_compound(
        self, sink: HarmonografSink, client: Client
    ):
        evt = _make_event("plan-compound")
        plan = evt.plan_submitted.plan
        plan.id = "p1"
        plan.run_id = "run-1"
        t1 = plan.tasks.add()
        t1.id = "t1"
        t1.assignee_agent_id = f"{CLIENT_AGENT_ID}:coordinator_agent"
        await sink.emit(evt)

        env = _drain(client)[0]
        tasks = list(env.payload.plan_submitted.plan.tasks)
        assert tasks[0].assignee_agent_id == f"{CLIENT_AGENT_ID}:coordinator_agent"


# ---- Pass-through events (no agent-id fields) -----------------------------


class TestPassThrough:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "setup,kind",
        [
            (lambda e: e.run_started.SetInParent(), "run_started"),
            (lambda e: e.task_started.SetInParent(), "task_started"),
            (lambda e: e.task_completed.SetInParent(), "task_completed"),
            (lambda e: e.run_completed.SetInParent(), "run_completed"),
            (lambda e: e.approval_requested.SetInParent(), "approval_requested"),
        ],
    )
    async def test_events_without_agent_fields_untouched(
        self, sink: HarmonografSink, client: Client, setup, kind: str
    ):
        """Payload kinds that carry no agent-identity string field pass
        through the sink unchanged. Sanity: no exception, and the
        envelope kind matches."""
        evt = _make_event(kind)
        setup(evt)
        await sink.emit(evt)

        env = _drain(client)[0]
        assert env.kind is EnvelopeKind.GOLDFIVE_EVENT
        assert env.payload.WhichOneof("payload") == kind
