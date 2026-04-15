"""Unit tests for the on_event_callback signal handlers (task #5).

Covers the post-rewrite ``_AdkState.on_event`` method and its private
helpers — ``_on_event_partial`` (regression: thinking/streaming summary
preserved), ``_on_event_state_delta`` (harmonograf.task_outcome →
transitions, divergence_flag → refine, agent_note → span attribute),
``_on_event_transfer`` (matches plan = no drift; mismatch = drift), and
``_on_event_escalate`` (always drift + span attribute).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from harmonograf_client.adk import (
    DriftReason,
    PlanState,
    _AdkState,
    _expected_next_assignee,
)
from harmonograf_client.planner import Plan, PlannerHelper, Task, TaskEdge


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._counter = 0
        self._current_activity: str = ""

    def emit_span_start(self, **kwargs) -> str:
        self._counter += 1
        sid = f"span-{self._counter}"
        self.calls.append(("start", sid, kwargs))
        return sid

    def emit_span_update(self, span_id: str, **kwargs) -> None:
        self.calls.append(("update", span_id, kwargs))

    def emit_span_end(self, span_id: str, **kwargs) -> None:
        self.calls.append(("end", span_id, kwargs))

    def set_current_activity(self, text: str) -> None:
        self._current_activity = text
        self.calls.append(("set_activity", text, {}))

    def submit_plan(self, plan, **kwargs) -> str:
        self._counter += 1
        pid = f"plan-{self._counter}"
        self.calls.append(("submit_plan", pid, {"plan": plan, **kwargs}))
        return pid

    def submit_task_status_update(
        self, plan_id: str, task_id: str, status: str, **kwargs
    ) -> None:
        self.calls.append(
            ("submit_task_status_update", task_id,
             {"plan_id": plan_id, "status": status, **kwargs})
        )

    def starts(self) -> list[tuple[str, dict]]:
        return [(sid, kw) for (op, sid, kw) in self.calls if op == "start"]

    def updates(self) -> list[tuple[str, dict]]:
        return [(sid, kw) for (op, sid, kw) in self.calls if op == "update"]

    def ends(self) -> list[tuple[str, dict]]:
        return [(sid, kw) for (op, sid, kw) in self.calls if op == "end"]


@dataclass
class FakeAgent:
    name: str = "worker"


@dataclass
class FakeSession:
    id: str = "adk_sess_evt"
    state: dict = field(default_factory=dict)


@dataclass
class FakeIc:
    invocation_id: str = "inv_evt"
    agent: FakeAgent = field(default_factory=FakeAgent)
    session: FakeSession = field(default_factory=FakeSession)


@dataclass
class FakeActions:
    state_delta: Optional[dict] = None
    transfer_to_agent: Optional[str] = None
    escalate: bool = False


@dataclass
class FakeEvent:
    actions: Optional[FakeActions] = None
    partial: bool = False
    content: Any = None


@dataclass
class FakePart:
    text: str = ""
    thought: bool = False
    function_call: Any = None


@dataclass
class FakeContent:
    parts: list = field(default_factory=list)


class RecordingPlanner(PlannerHelper):
    def __init__(self) -> None:
        self.refine_calls: list[dict] = []

    def generate(self, **kwargs):  # type: ignore[override]
        return None

    def refine(self, plan, event):  # type: ignore[override]
        self.refine_calls.append(event)
        return None  # no-op so the original plan_state is preserved


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_state(planner: Optional[PlannerHelper] = None):
    """Build a wired _AdkState with a 3-task plan whose tasks belong to
    ``worker``. ``other_agent`` is intentionally NOT in the plan so we
    can test unexpected_transfer drift.
    """
    client = FakeClient()
    state = _AdkState(client=client, planner=planner)  # type: ignore[arg-type]

    t1 = Task(id="t1", title="a", assignee_agent_id="worker", status="PENDING")
    t2 = Task(id="t2", title="b", assignee_agent_id="worker", status="PENDING")
    t3 = Task(id="t3", title="c", assignee_agent_id="reviewer", status="PENDING")
    plan = Plan(
        tasks=[t1, t2, t3],
        edges=[
            TaskEdge(from_task_id="t1", to_task_id="t2"),
            TaskEdge(from_task_id="t2", to_task_id="t3"),
        ],
        summary="three",
    )
    ps = PlanState(
        plan=plan,
        plan_id="plan-evt",
        tasks={"t1": t1, "t2": t2, "t3": t3},
        available_agents=["worker", "reviewer"],
        generating_invocation_id="inv_evt",
        remaining_for_fallback=[t1, t2, t3],
    )
    hsession_id = "hs_evt"
    with state._lock:
        state._active_plan_by_session[hsession_id] = ps
        state._adk_to_h_session["adk_sess_evt"] = hsession_id

    # Open an invocation so the helpers can find a span to attach to.
    ic = FakeIc()
    state.on_invocation_start(ic)

    return state, client, ps, ic


# ---------------------------------------------------------------------------
# state_delta routing
# ---------------------------------------------------------------------------


class TestStateDeltaOutcomes:
    def test_task_outcome_completed_transitions_task(self):
        state, client, ps, ic = _seed_state()
        # Pre-condition: t1 is RUNNING (the agent is actively working on it).
        ps.tasks["t1"].status = "RUNNING"

        evt = FakeEvent(
            actions=FakeActions(
                state_delta={"harmonograf.task_outcome": {"t1": "completed"}}
            )
        )
        state.on_event(ic, evt)

        assert ps.tasks["t1"].status == "COMPLETED"

    def test_task_outcome_failed_transitions_and_refines(self):
        planner = RecordingPlanner()
        state, client, ps, ic = _seed_state(planner=planner)
        ps.tasks["t1"].status = "RUNNING"

        evt = FakeEvent(
            actions=FakeActions(
                state_delta={"harmonograf.task_outcome": {"t1": "failed"}}
            )
        )
        state.on_event(ic, evt)

        assert ps.tasks["t1"].status == "FAILED"
        assert any(c.get("kind") == "task_failed_by_agent"
                   for c in planner.refine_calls)

    def test_task_outcome_blocked_refines_without_transition(self):
        planner = RecordingPlanner()
        state, client, ps, ic = _seed_state(planner=planner)
        ps.tasks["t1"].status = "RUNNING"

        evt = FakeEvent(
            actions=FakeActions(
                state_delta={"harmonograf.task_outcome": {"t1": "blocked"}}
            )
        )
        state.on_event(ic, evt)

        # blocked is NOT a tracked terminal state — the task stays RUNNING
        # but the planner still gets a refine call so it can re-plan.
        assert ps.tasks["t1"].status == "RUNNING"
        assert any(c.get("kind") == "task_blocked"
                   for c in planner.refine_calls)

    def test_divergence_flag_fires_refine(self):
        planner = RecordingPlanner()
        state, client, ps, ic = _seed_state(planner=planner)

        evt = FakeEvent(
            actions=FakeActions(
                state_delta={
                    "harmonograf.divergence_flag": True,
                    "harmonograf.agent_note": "this plan is wrong",
                }
            )
        )
        state.on_event(ic, evt)

        kinds = [c.get("kind") for c in planner.refine_calls]
        assert "agent_reported_divergence" in kinds

    def test_agent_note_surfaces_as_invocation_attribute(self):
        state, client, ps, ic = _seed_state()
        evt = FakeEvent(
            actions=FakeActions(
                state_delta={"harmonograf.agent_note": "halfway done"}
            )
        )
        state.on_event(ic, evt)

        invocation_sid = state._invocations["inv_evt"]
        agent_note_updates = [
            kw for (sid, kw) in client.updates()
            if sid == invocation_sid
            and (kw.get("attributes") or {}).get("agent_note") == "halfway done"
        ]
        assert agent_note_updates, "agent_note should land on the invocation span"

    def test_non_harmonograf_state_delta_only_emits_attribute(self):
        planner = RecordingPlanner()
        state, client, ps, ic = _seed_state(planner=planner)

        evt = FakeEvent(
            actions=FakeActions(state_delta={"unrelated_key": "value"})
        )
        state.on_event(ic, evt)

        # Pre-rewrite behavior preserved: the raw key still becomes an
        # attribute on the enclosing span.
        attr_updates = [
            kw for (_, kw) in client.updates()
            if (kw.get("attributes") or {}).get("state_delta.unrelated_key")
        ]
        assert attr_updates
        # …and no refines are fired (no harmonograf keys to route).
        assert planner.refine_calls == []


# ---------------------------------------------------------------------------
# transfer_to_agent
# ---------------------------------------------------------------------------


class TestTransferToAgent:
    def test_transfer_matching_plan_emits_span_no_drift(self):
        planner = RecordingPlanner()
        state, client, ps, ic = _seed_state(planner=planner)
        # Plan's first dispatchable task is t1 / worker. Transferring to
        # "worker" matches the expected next assignee → no drift.
        evt = FakeEvent(actions=FakeActions(transfer_to_agent="worker"))
        state.on_event(ic, evt)

        kinds = [kw["kind"] for (_, kw) in client.starts()]
        assert "TRANSFER" in kinds
        assert all(c.get("kind") != "unexpected_transfer"
                   for c in planner.refine_calls)

    def test_transfer_mismatching_plan_fires_drift(self):
        planner = RecordingPlanner()
        state, client, ps, ic = _seed_state(planner=planner)

        evt = FakeEvent(actions=FakeActions(transfer_to_agent="ghost"))
        state.on_event(ic, evt)

        # TRANSFER span still emitted for telemetry.
        kinds = [kw["kind"] for (_, kw) in client.starts()]
        assert "TRANSFER" in kinds
        # Drift fired.
        unexpected = [c for c in planner.refine_calls
                      if c.get("kind") == "unexpected_transfer"]
        assert unexpected, "expected unexpected_transfer drift"
        assert "ghost" in unexpected[0]["detail"]
        assert "worker" in unexpected[0]["detail"]

    def test_transfer_with_no_plan_does_not_fire_drift(self):
        planner = RecordingPlanner()
        state, client, ps, ic = _seed_state(planner=planner)
        # Wipe the plan so the helper has nothing to compare against.
        with state._lock:
            state._active_plan_by_session.clear()

        evt = FakeEvent(actions=FakeActions(transfer_to_agent="ghost"))
        state.on_event(ic, evt)

        assert planner.refine_calls == []

    def test_expected_next_assignee_skips_completed_tasks(self):
        state, client, ps, ic = _seed_state()
        # t1 and t2 done; the next dispatchable task is t3 / reviewer.
        ps.tasks["t1"].status = "COMPLETED"
        ps.tasks["t2"].status = "COMPLETED"
        assert _expected_next_assignee(ps) == "reviewer"


# ---------------------------------------------------------------------------
# escalate
# ---------------------------------------------------------------------------


class TestEscalate:
    def test_escalate_fires_drift_and_marks_invocation(self):
        planner = RecordingPlanner()
        state, client, ps, ic = _seed_state(planner=planner)

        evt = FakeEvent(actions=FakeActions(escalate=True))
        state.on_event(ic, evt)

        kinds = [c.get("kind") for c in planner.refine_calls]
        assert "agent_escalated" in kinds

        invocation_sid = state._invocations["inv_evt"]
        escalated_updates = [
            kw for (sid, kw) in client.updates()
            if sid == invocation_sid
            and (kw.get("attributes") or {}).get("escalated") is True
        ]
        assert escalated_updates


# ---------------------------------------------------------------------------
# Partial-event regression — the live thinking summary path must still
# fire so the existing UI liveness behavior is unchanged by the rewrite.
# ---------------------------------------------------------------------------


class TestPartialEventsPreserved:
    def test_partial_event_drives_streaming_text_update(self):
        state, client, ps, ic = _seed_state()

        # Open an LLM_CALL span so the partial helper has somewhere to
        # write streaming_text/thinking_text attributes.
        @dataclass
        class FakeReq:
            model: str = "gpt-test"
            contents: list = field(default_factory=list)

        @dataclass
        class FakeCallbackContext:
            _invocation_context: Any = None

        cc = FakeCallbackContext(_invocation_context=ic)
        state.on_model_start(cc, FakeReq())
        llm_sid = state._llm_by_invocation["inv_evt"]

        # Drive a partial event with a thinking part — should land as a
        # thinking_text attribute on the LLM span.
        thinking_part = FakePart(text="weighing options", thought=True)
        evt = FakeEvent(
            partial=True,
            content=FakeContent(parts=[thinking_part]),
            actions=None,
        )
        state.on_event(ic, evt)

        thinking_updates = [
            kw for (sid, kw) in client.updates()
            if sid == llm_sid
            and "thinking_text" in (kw.get("attributes") or {})
        ]
        assert thinking_updates, "partial event should emit thinking_text update"

    def test_partial_event_does_not_fire_refines(self):
        planner = RecordingPlanner()
        state, client, ps, ic = _seed_state(planner=planner)

        # Open an LLM_CALL span so the partial path can record state.
        @dataclass
        class FakeReq:
            model: str = "gpt-test"
            contents: list = field(default_factory=list)

        @dataclass
        class FakeCallbackContext:
            _invocation_context: Any = None

        cc = FakeCallbackContext(_invocation_context=ic)
        state.on_model_start(cc, FakeReq())

        evt = FakeEvent(
            partial=True,
            content=FakeContent(parts=[FakePart(text="thinking…", thought=False)]),
            actions=FakeActions(escalate=True),  # would normally drift
        )
        state.on_event(ic, evt)

        # Partial events bail out before action processing, so escalate
        # is intentionally ignored.
        assert planner.refine_calls == []
