"""Regression tests for iter13 task #6 — task completion is driven
exclusively by the walker (``mark_forced_task_completed``) and NEVER
by span_end.

Bug (iter13 post-monotonic-guard): the coordinator's very first
LLM_CALL span ending (the transfer-decision call) triggered a
``_mark_task_for_span(..., "COMPLETED")`` path, flipping the task to
COMPLETED before research_agent had even started working. The
monotonic guard subsequently logged "REJECTED stamping forced task ...
as RUNNING — already COMPLETED" warnings on every follow-up span that
tried to re-bind.

The fix: walker is the EXCLUSIVE source of task completion; span_end
is a telemetry signal, not a task-state signal. These tests pin that
invariant.
"""

from __future__ import annotations

import logging

import pytest

from harmonograf_client.adk import _AdkState
from harmonograf_client.planner import Plan, PlannerHelper, Task


# Reuse the lightweight fixtures from test_adk_adapter so we don't
# re-implement them. They're internal test helpers, not public API.
from .test_adk_adapter import (  # type: ignore[import-not-found]
    FakeAgent,
    FakeClient,
    FakeInvocationContext,
    FakeSession,
    FakeTool,
    FakeToolContext,
    _StubPlannerIC,
)


def _coord_cc():
    return FakeInvocationContext(
        invocation_id="inv_plan",
        agent=FakeAgent(name="coordinator"),
        session=FakeSession(id="sess_plan"),
    )


def _make_state_with_plan(tasks: list[Task]) -> _AdkState:
    class StubPlanner(PlannerHelper):
        def generate(self, **kwargs):
            return Plan(tasks=list(tasks), edges=[])

    state = _AdkState(  # type: ignore[arg-type]
        client=FakeClient(), planner=StubPlanner(), refine_on_events=False
    )
    ic = _StubPlannerIC("go")
    state.on_invocation_start(ic)
    state.maybe_run_planner(ic)
    return state


def _task_status(state: _AdkState, tid: str) -> str:
    for ps in state._active_plan_by_session.values():
        if tid in ps.tasks:
            return getattr(ps.tasks[tid], "status", "") or ""
    return ""


def _count_rejected_stamping_warnings(caplog) -> int:
    return sum(
        1
        for rec in caplog.records
        if "REJECTED stamping" in rec.getMessage()
    )


class TestWalkerCompletionTiming:
    def test_task_stays_running_across_coordinator_decision_span(self, caplog):
        """Scenario mirroring the bug: the walker sets forced_task_id,
        the coordinator emits a SEQUENCE of leaf spans (e.g. a decision
        LLM call followed by another LLM call), and the task must
        stay RUNNING across all of them. Completion only happens when
        the walker explicitly calls ``mark_forced_task_completed``.

        Also asserts that no "REJECTED stamping" monotonic-guard
        warnings fire — the stamping guard firing would mean we STILL
        have the premature-completion bug.
        """
        caplog.set_level(logging.WARNING, logger="harmonograf_client.adk")
        state = _make_state_with_plan(
            [Task(id="t1", title="research", assignee_agent_id="coordinator")]
        )

        assert state.set_forced_task_id("t1")
        assert _task_status(state, "t1") == "PENDING"

        # First leaf span of the turn — the "decision" LLM call.
        tc1 = FakeToolContext(function_call_id="fc_1", _invocation_context=_coord_cc())
        state.on_tool_start(FakeTool(name="decide"), {"q": "transfer?"}, tc1)
        assert _task_status(state, "t1") == "RUNNING"

        state.on_tool_end(
            FakeTool(name="decide"), tc1, result={"ok": True}, error=None
        )
        # Span ended, but task MUST remain RUNNING — the rest of the
        # turn is still executing inside the inner agent.
        assert _task_status(state, "t1") == "RUNNING"

        # Second leaf span of the same turn — more real work.
        tc2 = FakeToolContext(function_call_id="fc_2", _invocation_context=_coord_cc())
        state.on_tool_start(FakeTool(name="search"), {"q": "topic"}, tc2)
        assert _task_status(state, "t1") == "RUNNING"
        state.on_tool_end(
            FakeTool(name="search"), tc2, result={"ok": True}, error=None
        )
        assert _task_status(state, "t1") == "RUNNING"

        # Now the walker finishes and declares the task done.
        state.mark_forced_task_completed()
        assert _task_status(state, "t1") == "COMPLETED"

        # The monotonic guard must not have fired — if it did, a path
        # was still prematurely stamping the task terminal.
        assert _count_rejected_stamping_warnings(caplog) == 0

    def test_sub_agent_multi_turn_does_not_flip_task_early(self, caplog):
        """Simulate a sub-agent that runs 3 back-and-forth leaf spans
        before its turn is done. The task stays RUNNING throughout and
        only flips on the walker's explicit completion call.
        """
        caplog.set_level(logging.WARNING, logger="harmonograf_client.adk")
        state = _make_state_with_plan(
            [Task(id="t1", title="deep_research", assignee_agent_id="research_agent")]
        )

        # Walker sets forced_task_id before inner_agent.run_async begins.
        assert state.set_forced_task_id("t1")

        inv = FakeInvocationContext(
            invocation_id="inv_plan",
            agent=FakeAgent(name="research_agent"),
            session=FakeSession(id="sess_plan"),
        )
        for i in range(3):
            tc = FakeToolContext(
                function_call_id=f"fc_{i}", _invocation_context=inv
            )
            state.on_tool_start(FakeTool(name=f"step_{i}"), {"i": i}, tc)
            assert _task_status(state, "t1") == "RUNNING", (
                f"task flipped early on turn {i} start"
            )
            state.on_tool_end(
                FakeTool(name=f"step_{i}"), tc, result={"n": i}, error=None
            )
            assert _task_status(state, "t1") == "RUNNING", (
                f"task flipped early on turn {i} end"
            )

        state.mark_forced_task_completed()
        assert _task_status(state, "t1") == "COMPLETED"
        assert _count_rejected_stamping_warnings(caplog) == 0

    def test_span_end_alone_does_not_complete_task(self):
        """Direct-call test: bind a span to a task, then drive the
        span-end → task path (``_mark_task_for_span(..., "COMPLETED")``).
        The task MUST stay RUNNING. Span_end is a telemetry signal,
        not a task-state signal.
        """
        state = _make_state_with_plan(
            [Task(id="t_x", title="x", assignee_agent_id="coordinator")]
        )
        assert state.set_forced_task_id("t_x")

        tc = FakeToolContext(function_call_id="fc_only", _invocation_context=_coord_cc())
        state.on_tool_start(FakeTool(name="work"), {"q": "x"}, tc)
        assert _task_status(state, "t_x") == "RUNNING"

        state.on_tool_end(FakeTool(name="work"), tc, result={"ok": 1}, error=None)
        # This is the core invariant: span_end closing is NOT task done.
        assert _task_status(state, "t_x") == "RUNNING"

        # And a second direct invocation of the helper must still
        # leave the task RUNNING — ``_mark_task_for_span("COMPLETED")``
        # is now a no-op for task state.
        state._mark_task_for_span("some-other-span", "COMPLETED")
        assert _task_status(state, "t_x") == "RUNNING"

    def test_span_end_failed_still_propagates_to_task(self):
        """The FAILED propagation path MUST still work: a leaf span
        ending in FAILED is a real signal that the task's work errored
        and the task itself should transition to FAILED. This is the
        one carve-out from the "walker owns completion" rule.
        """
        state = _make_state_with_plan(
            [Task(id="t_f", title="f", assignee_agent_id="coordinator")]
        )
        assert state.set_forced_task_id("t_f")

        tc = FakeToolContext(function_call_id="fc_f", _invocation_context=_coord_cc())
        state.on_tool_start(FakeTool(name="work"), {}, tc)
        assert _task_status(state, "t_f") == "RUNNING"

        state.on_tool_end(
            FakeTool(name="work"), tc, result=None, error=RuntimeError("boom")
        )
        assert _task_status(state, "t_f") == "FAILED"


class _RichFakePart:
    def __init__(self, text: str) -> None:
        self.text = text
        self.thought = False


class _RichFakeContent:
    def __init__(self, text: str) -> None:
        self.parts = [_RichFakePart(text)]


class _RichFakeEvent:
    """FakeEvent with ``.content.parts[].text`` so the walker's
    ``_extract_result_summary`` returns a real summary the iter13 #7
    classifier can route to completed/failed/partial.
    """

    def __init__(self, invocation_id: str, text: str = "") -> None:
        self.invocation_id = invocation_id
        self.payload = text
        self.content = _RichFakeContent(text) if text else None


class TestWalkerCompletionViaHarmonografAgent:
    @pytest.mark.asyncio
    async def test_walker_marks_task_completed_after_aclosing_exit(self):
        """Drive ``_run_orchestrated`` via HarmonografAgent with a stub
        inner agent. Assert that the classifier marks the task COMPLETED
        only AFTER the inner generator exhausts (i.e. AFTER the Aclosing
        context exits), not during it.

        We detect "after" by recording the task status at each yielded
        event (all should be RUNNING) and at the very end of the outer
        generator (should be COMPLETED).
        """
        from harmonograf_client.planner import Task

        # Reuse test_agent's builders through an import.
        from .test_agent import _build, _seed_plan

        agent, inner, state, ctx = _build(
            passes=[[
                _RichFakeEvent("inv-1", "starting work"),
                _RichFakeEvent(
                    "inv-1",
                    "Did the work and produced an answer. "
                    "Task complete: research finished cleanly.",
                ),
            ]],
            inner_name="coordinator",
        )
        _seed_plan(
            state,
            inv_id="inv-1",
            tasks=[Task(id="t1", title="research", assignee_agent_id="coordinator")],
            edges=[],
            statuses={"t1": "PENDING"},
        )

        statuses_during: list[str] = []
        async for _ev in agent._run_async_impl(ctx):
            # While events are still flowing, the task must be RUNNING.
            ps = state._active_plan_by_session.get("hsess-inv-1")
            if ps is not None and "t1" in ps.tasks:
                statuses_during.append(
                    getattr(ps.tasks["t1"], "status", "") or ""
                )

        assert statuses_during, "expected at least one event"
        assert all(
            s in ("PENDING", "RUNNING") for s in statuses_during
        ), (
            f"task must not be COMPLETED while events are still "
            f"streaming — saw {statuses_during}"
        )

        # After the outer generator exhausts, the walker MUST have
        # called mark_forced_task_completed.
        ps = state._active_plan_by_session.get("hsess-inv-1")
        assert ps is not None
        assert ps.tasks["t1"].status == "COMPLETED"


class TestMultiTaskPerTurnSweep:
    def test_running_to_completed_sweep_at_turn_end(self):
        """``sweep_running_tasks_to_completed`` transitions every
        RUNNING task on the active plan(s) to COMPLETED in one shot.
        This is the augment-1 path: a coordinator dispatched to two
        sub-agents in one turn and both tasks must be marked done at
        turn end, not just the forced one.
        """
        state = _make_state_with_plan(
            [
                Task(id="t1", title="research", assignee_agent_id="research_agent"),
                Task(id="t2", title="build", assignee_agent_id="web_developer_agent"),
            ]
        )
        # Drive both into RUNNING via the stamping path.
        from harmonograf_client.adk import _set_task_status
        ps = next(iter(state._active_plan_by_session.values()))
        with state._lock:
            _set_task_status(ps.tasks["t1"], "RUNNING")
            _set_task_status(ps.tasks["t2"], "RUNNING")

        swept = state.sweep_running_tasks_to_completed()
        assert set(swept) == {"t1", "t2"}
        assert _task_status(state, "t1") == "COMPLETED"
        assert _task_status(state, "t2") == "COMPLETED"

    def test_sweep_excludes_pre_running_tasks(self):
        """Delegated path passes ``exclude=`` to keep tasks that were
        already RUNNING at turn start out of the sweep — they belong
        to a previous turn.
        """
        state = _make_state_with_plan(
            [
                Task(id="t1", title="a", assignee_agent_id="research_agent"),
                Task(id="t2", title="b", assignee_agent_id="research_agent"),
            ]
        )
        from harmonograf_client.adk import _set_task_status
        ps = next(iter(state._active_plan_by_session.values()))
        with state._lock:
            _set_task_status(ps.tasks["t1"], "RUNNING")
            _set_task_status(ps.tasks["t2"], "RUNNING")

        swept = state.sweep_running_tasks_to_completed(exclude={"t1"})
        assert swept == ["t2"]
        assert _task_status(state, "t1") == "RUNNING"
        assert _task_status(state, "t2") == "COMPLETED"

    def test_task_completion_propagates_to_server(self):
        """Augment 2A: walker's completion sweep MUST emit an explicit
        ``submit_task_status_update`` to the server so the server store
        reflects the transition independent of span lifecycle.
        """
        state = _make_state_with_plan(
            [
                Task(id="t1", title="a", assignee_agent_id="research_agent"),
                Task(id="t2", title="b", assignee_agent_id="web_developer_agent"),
            ]
        )
        from harmonograf_client.adk import _set_task_status
        ps = next(iter(state._active_plan_by_session.values()))
        plan_id = ps.plan_id
        with state._lock:
            _set_task_status(ps.tasks["t1"], "RUNNING")
            _set_task_status(ps.tasks["t2"], "RUNNING")

        client = state._client
        client.calls.clear()  # type: ignore[attr-defined]
        state.sweep_running_tasks_to_completed()

        propagated = [
            c for c in client.calls  # type: ignore[attr-defined]
            if c[0] == "submit_task_status_update"
        ]
        assert len(propagated) == 2
        seen = {(tid, kw["plan_id"], kw["status"]) for (_, tid, kw) in propagated}
        assert seen == {
            ("t1", plan_id, "COMPLETED"),
            ("t2", plan_id, "COMPLETED"),
        }

    def test_mark_forced_task_completed_propagates_to_server(self):
        """``mark_forced_task_completed`` is the per-task completion
        path used by the parallel walker. It must also propagate the
        transition to the server.
        """
        state = _make_state_with_plan(
            [Task(id="t_only", title="x", assignee_agent_id="research_agent")]
        )
        assert state.set_forced_task_id("t_only")
        ps = next(iter(state._active_plan_by_session.values()))
        plan_id = ps.plan_id
        from harmonograf_client.adk import _set_task_status
        with state._lock:
            _set_task_status(ps.tasks["t_only"], "RUNNING")

        client = state._client
        client.calls.clear()  # type: ignore[attr-defined]
        state.mark_forced_task_completed()

        propagated = [
            c for c in client.calls  # type: ignore[attr-defined]
            if c[0] == "submit_task_status_update"
        ]
        assert propagated, "expected mark_forced_task_completed to propagate"
        _, tid, kw = propagated[0]
        assert tid == "t_only"
        assert kw["plan_id"] == plan_id
        assert kw["status"] == "COMPLETED"


# TestSubmitPlanPreservesStatus removed in Phase A of the goldfive
# migration (issue #2). Client.submit_plan is gone; plan emission
# migrates to emit_goldfive_event in Phase B, with its own coverage.
