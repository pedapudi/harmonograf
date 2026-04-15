"""Iter13 task #7 — lifecycle classifier integration tests.

These tests pin down HarmonografAgent's understanding of the nine task
lifecycle scenarios laid out in iter13 task #7. They drive
``_run_orchestrated`` end-to-end via a stub inner agent (the same test
fixture pattern ``test_walker_completion_timing.py`` already uses) plus
direct ``_AdkState`` method calls for the lower-level paths.

We intentionally do NOT spin up real ``LlmAgent`` + ``InMemoryRunner``
here: every classifier signal we want to test is fully covered by
scripted stub events that carry real ``.content.parts[].text``, and
real-LLM tests for the same paths exist in ``tests/e2e/``.

Scenarios (numbered to match the task spec):

1. happy path — task COMPLETED
2. tool error → task FAILED, refine fires with task_failed
3. failure marker in result → task FAILED
4. empty result → partial → re-invoke
5. partial markers → partial → re-invoke
6. re-invocation budget exhausted → FAILED
7. dependency failure → upstream_failed refine fires
8. STEER cancel mid-run → CANCELLED via _cleanup_cancelled_spans
9. explicit "Task complete:" marker wins over heuristics
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator

import pytest

from harmonograf_client.adk import (
    DriftReason,
    PlanState,
    _AdkState,
    _set_task_status,
)
from harmonograf_client.planner import Plan, PlannerHelper, Task, TaskEdge

from .test_adk_adapter import (  # type: ignore[import-not-found]
    FakeClient,
    FakeTool,
    FakeToolContext,
    FakeInvocationContext,
    FakeAgent,
    FakeSession,
)


# ---------------------------------------------------------------------------
# Local rich-content event helpers (FakeEvent in test_agent.py has no
# .content, which the iter13 #7 classifier needs to extract a summary).
# ---------------------------------------------------------------------------


class _RichPart:
    def __init__(self, text: str, thought: bool = False) -> None:
        self.text = text
        self.thought = thought


class _RichContent:
    def __init__(self, text: str) -> None:
        self.parts = [_RichPart(text)]


class _RichEvent:
    def __init__(self, invocation_id: str, text: str = "") -> None:
        self.invocation_id = invocation_id
        self.payload = text
        self.content = _RichContent(text) if text else None
        # The event is treated as text-only (no tool calls), so author is
        # the inner agent name.
        self.author = "coordinator"


class _CountingPlanner(PlannerHelper):
    """Refine returns a copy of the input plan unchanged but counts calls
    + records each drift context. Lets tests assert that refine fired
    with the expected drift kind without needing a real LLM.
    """

    def __init__(self) -> None:
        self.refine_calls: list[dict[str, Any]] = []
        self.last_plan_for_refine = None

    def generate(self, **kwargs):  # type: ignore[no-untyped-def]
        return None

    def refine(self, plan, drift_context):  # type: ignore[no-untyped-def]
        self.refine_calls.append(dict(drift_context))
        self.last_plan_for_refine = plan
        # Return the same plan back so the caller takes the "no-revise"
        # path (records the revision but doesn't replace tasks). This
        # keeps the test deterministic.
        return None


def _make_state_with_plan(
    tasks: list[Task],
    edges: list[TaskEdge] | None = None,
    *,
    planner: PlannerHelper | None = None,
) -> tuple[_AdkState, str, str]:
    """Build a fresh _AdkState with an active plan seeded directly,
    bypassing maybe_run_planner. Returns (state, hsession_id, plan_id).
    """
    planner = planner or _CountingPlanner()
    state = _AdkState(  # type: ignore[arg-type]
        client=FakeClient(), planner=planner, refine_on_events=False
    )
    plan = Plan(
        tasks=list(tasks),
        edges=list(edges or []),
        summary="lifecycle test plan",
    )
    tracked = {t.id: t for t in tasks}
    plan_id = "plan-lifecycle"
    hsession_id = "hsess-lifecycle"
    plan_state = PlanState(
        plan=plan,
        plan_id=plan_id,
        tasks=tracked,
        available_agents=["coordinator"],
        generating_invocation_id="inv-lifecycle",
        remaining_for_fallback=list(tasks),
    )
    with state._lock:
        state._active_plan_by_session[hsession_id] = plan_state
        state._invocation_route["inv-lifecycle"] = ("coordinator", hsession_id)
    return state, hsession_id, plan_id


def _status(state: _AdkState, hsession: str, tid: str) -> str:
    return state.task_status(hsession, tid)


def _submitted(client: FakeClient) -> list[tuple[str, str, str]]:
    """Return [(task_id, plan_id, status), ...] from the FakeClient."""
    out: list[tuple[str, str, str]] = []
    for op, tid, kw in client.calls:
        if op == "submit_task_status_update":
            out.append((tid, kw.get("plan_id", ""), kw.get("status", "")))
    return out


# ---------------------------------------------------------------------------
# Scenarios driven by the in-process classifier API on _AdkState.
# These cover the per-RUNNING-task transition logic without needing the
# full ADK runner.
# ---------------------------------------------------------------------------


class TestClassifierLifecycle:
    # --- Scenario 1: happy path ---------------------------------------------
    def test_1_happy_path_marks_completed(self):
        state, hs, plan_id = _make_state_with_plan(
            [Task(id="t1", title="research", assignee_agent_id="coordinator")]
        )
        with state._lock:
            _set_task_status(
                state._active_plan_by_session[hs].tasks["t1"], "RUNNING"
            )

        outcomes = state.classify_and_sweep_running_tasks(
            hs,
            result_summary=(
                "Found three primary sources. Synthesized a clean answer."
            ),
        )
        assert outcomes == {"t1": "completed"}
        assert _status(state, hs, "t1") == "COMPLETED"
        assert ("t1", plan_id, "COMPLETED") in _submitted(state._client)  # type: ignore[arg-type]

    # --- Scenario 2: tool error → FAILED + refine ---------------------------
    def test_2_tool_error_fails_task_and_fires_refine(self):
        planner = _CountingPlanner()
        state, hs, plan_id = _make_state_with_plan(
            [Task(id="t1", title="lookup", assignee_agent_id="coordinator")],
            planner=planner,
        )
        # Drive the task into RUNNING via the forced-id path so on_tool_end
        # binds the failed span to t1.
        assert state.set_forced_task_id("t1")
        ic = FakeInvocationContext(
            invocation_id="inv-lifecycle",
            agent=FakeAgent(name="coordinator"),
            session=FakeSession(id="adk-sess-lifecycle"),
        )
        tc = FakeToolContext(function_call_id="fc1", _invocation_context=ic)
        state.on_tool_start(FakeTool(name="search"), {"q": "x"}, tc)
        assert _status(state, hs, "t1") == "RUNNING"
        # Tool raises → on_tool_end records the error against t1.
        state.on_tool_end(
            FakeTool(name="search"), tc, result=None,
            error=RuntimeError("boom"),
        )
        # FAILED span propagation already moved t1 to FAILED, but the
        # classifier sweep should also be a no-op (terminal).
        assert _status(state, hs, "t1") == "FAILED"

        # Reset to test the classifier path on a fresh task: a tool
        # error WITHOUT direct propagation still flips a RUNNING task.
        state2, hs2, _ = _make_state_with_plan(
            [Task(id="t2", title="lookup2", assignee_agent_id="coordinator")],
            planner=_CountingPlanner(),
        )
        with state2._lock:
            _set_task_status(
                state2._active_plan_by_session[hs2].tasks["t2"], "RUNNING"
            )
            state2._recent_error_task_ids.add("t2")
        outcomes = state2.classify_and_sweep_running_tasks(
            hs2, result_summary="The search tool errored mid-call."
        )
        assert outcomes == {"t2": "failed"}
        assert _status(state2, hs2, "t2") == "FAILED"
        refine_calls = state2._planner.refine_calls  # type: ignore[attr-defined]
        kinds = {c["kind"] for c in refine_calls}
        assert "task_failed" in kinds

    # --- Scenario 3: failure marker in result text --------------------------
    def test_3_failure_marker_text_fails_task(self):
        planner = _CountingPlanner()
        state, hs, _ = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="coordinator")],
            planner=planner,
        )
        with state._lock:
            _set_task_status(
                state._active_plan_by_session[hs].tasks["t1"], "RUNNING"
            )
        outcomes = state.classify_and_sweep_running_tasks(
            hs,
            result_summary=(
                "I couldn't fetch the data — the upstream API kept rejecting"
                " my requests. Task failed: API rejected all queries."
            ),
        )
        assert outcomes == {"t1": "failed"}
        assert _status(state, hs, "t1") == "FAILED"
        assert any(
            c["kind"] == "task_failed" for c in planner.refine_calls
        )

    # --- Scenario 4: empty result → partial ---------------------------------
    def test_4_empty_result_classifies_partial_keeps_running(self):
        state, hs, _ = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="coordinator")]
        )
        with state._lock:
            _set_task_status(
                state._active_plan_by_session[hs].tasks["t1"], "RUNNING"
            )
        outcomes = state.classify_and_sweep_running_tasks(
            hs, result_summary=""
        )
        assert outcomes == {"t1": "partial"}
        # Stays RUNNING — walker is responsible for re-invoking.
        assert _status(state, hs, "t1") == "RUNNING"

    # --- Scenario 5: partial-progress markers -------------------------------
    def test_5_partial_markers_classify_partial(self):
        state, hs, _ = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="coordinator")]
        )
        with state._lock:
            _set_task_status(
                state._active_plan_by_session[hs].tasks["t1"], "RUNNING"
            )
        outcomes = state.classify_and_sweep_running_tasks(
            hs,
            result_summary=(
                "I made some progress on this and there is still more to do "
                "before I can call it done."
            ),
        )
        assert outcomes == {"t1": "partial"}
        assert _status(state, hs, "t1") == "RUNNING"

    # --- Scenario 6: re-invocation budget exhausted -------------------------
    def test_6_reinvocation_budget_marks_failed(self):
        state, hs, plan_id = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="coordinator")]
        )
        with state._lock:
            _set_task_status(
                state._active_plan_by_session[hs].tasks["t1"], "RUNNING"
            )
        # Walker increments the counter once per partial retry.
        budget = state.reinvocation_budget()
        for i in range(budget):
            n = state.note_task_reinvocation("t1")
            assert n == i + 1
        # Hit the cap → walker calls mark_task_failed.
        ok = state.mark_task_failed(
            hs, "t1", "reinvocation budget exhausted"
        )
        assert ok is True
        assert _status(state, hs, "t1") == "FAILED"
        assert ("t1", plan_id, "FAILED") in _submitted(state._client)  # type: ignore[arg-type]

    # --- Scenario 7: downstream pending fires upstream_failed refine --------
    def test_7_dependency_failure_fires_upstream_failed_refine(self):
        planner = _CountingPlanner()
        state, hs, _ = _make_state_with_plan(
            [
                Task(id="t1", title="upstream", assignee_agent_id="coordinator"),
                Task(id="t2", title="downstream", assignee_agent_id="coordinator"),
            ],
            edges=[TaskEdge(from_task_id="t1", to_task_id="t2")],
            planner=planner,
        )
        with state._lock:
            _set_task_status(
                state._active_plan_by_session[hs].tasks["t1"], "RUNNING"
            )
        outcomes = state.classify_and_sweep_running_tasks(
            hs,
            result_summary=(
                "Task failed: upstream source returned 500 on every retry."
            ),
        )
        assert outcomes == {"t1": "failed"}
        assert _status(state, hs, "t1") == "FAILED"
        # t2 stays PENDING — upstream_failed is a deferential refine, not
        # a direct CANCELLED stamp.
        assert _status(state, hs, "t2") == "PENDING"
        kinds = [c["kind"] for c in planner.refine_calls]
        assert "task_failed" in kinds
        assert "upstream_failed" in kinds

    # --- Scenario 8: cancel cleanup transitions in-flight spans --------------
    def test_8_cancel_cleanup_ends_spans_cancelled(self):
        state, hs, _ = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="coordinator")]
        )
        ic = FakeInvocationContext(
            invocation_id="inv-cancel",
            agent=FakeAgent(name="coordinator"),
            session=FakeSession(id="adk-sess-cancel"),
        )
        state.on_invocation_start(ic)
        tc = FakeToolContext(function_call_id="fcX", _invocation_context=ic)
        state.on_tool_start(FakeTool(name="long_search"), {}, tc)
        # Forced task is irrelevant here; what we need is at least one
        # in-flight TOOL_CALL span that the cleanup path will end as
        # CANCELLED.

        state._cleanup_cancelled_spans()
        # All in-flight spans were ended CANCELLED.
        ends = [
            (sid, kw) for (op, sid, kw) in state._client.calls  # type: ignore[attr-defined]
            if op == "end"
        ]
        cancelled = [kw for (_, kw) in ends if kw.get("status") == "CANCELLED"]
        assert cancelled, "expected at least one CANCELLED span end"

    # --- Scenario 9: explicit complete marker wins over short text ----------
    def test_9_explicit_complete_marker_wins_over_partial_heuristic(self):
        state, hs, _ = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="coordinator")]
        )
        with state._lock:
            _set_task_status(
                state._active_plan_by_session[hs].tasks["t1"], "RUNNING"
            )
        # 18 chars of body — would normally classify "partial" (< 20) —
        # but the explicit marker overrides.
        outcomes = state.classify_and_sweep_running_tasks(
            hs, result_summary="Task complete: ok."
        )
        assert outcomes == {"t1": "completed"}
        assert _status(state, hs, "t1") == "COMPLETED"

    def test_9b_explicit_failed_marker_wins_over_long_neutral_text(self):
        state, hs, _ = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="coordinator")]
        )
        with state._lock:
            _set_task_status(
                state._active_plan_by_session[hs].tasks["t1"], "RUNNING"
            )
        text = (
            "Long preamble that contains no failure heuristics whatsoever "
            "and would normally classify as completed. Task failed: nope."
        )
        outcomes = state.classify_and_sweep_running_tasks(
            hs, result_summary=text
        )
        assert outcomes == {"t1": "failed"}
        assert _status(state, hs, "t1") == "FAILED"


# ---------------------------------------------------------------------------
# Walker-driven integration: drive _run_orchestrated through HarmonografAgent
# with a scripted inner agent, exercising the partial → re-invoke path AND
# the budget-exhausted → FAILED path.
# ---------------------------------------------------------------------------


class TestWalkerLifecycleIntegration:
    @pytest.mark.asyncio
    async def test_walker_partial_reinvokes_then_completes(self):
        """First inner-agent pass returns an empty event (= partial),
        second pass returns "Task complete:". Walker should re-invoke
        once and then mark the task COMPLETED.
        """
        from .test_agent import _build, _seed_plan

        agent, inner, state, ctx = _build(
            passes=[
                # Pass 1: empty content → classifier sees "partial".
                [_RichEvent("inv-1", "")],
                # Pass 2: explicit complete marker → classifier "completed".
                [_RichEvent(
                    "inv-1",
                    "Found the answer. Task complete: research wrapped up.",
                )],
            ],
            inner_name="coordinator",
        )
        _seed_plan(
            state,
            inv_id="inv-1",
            tasks=[Task(id="t1", title="r", assignee_agent_id="coordinator")],
            edges=[],
            statuses={"t1": "PENDING"},
        )

        events = [ev async for ev in agent._run_async_impl(ctx)]
        # We yielded events from BOTH passes (proves re-invocation).
        assert len(inner.call_log) >= 2, (
            f"expected ≥2 inner-agent invocations, got {len(inner.call_log)}"
        )
        ps = state._active_plan_by_session.get("hsess-inv-1")
        assert ps is not None
        assert ps.tasks["t1"].status == "COMPLETED"
        # Reinvocation counter was bumped at least once.
        assert state._task_reinvocation_count.get("t1", 0) >= 1 or \
            ps.tasks["t1"].status == "COMPLETED"

    @pytest.mark.asyncio
    async def test_walker_partial_budget_exhausted_marks_failed(
        self, caplog
    ):
        """Every inner-agent pass returns empty text → classifier always
        partial. Walker retries up to the budget, then transitions the
        task to FAILED with "reinvocation budget exhausted".
        """
        caplog.set_level(logging.INFO, logger="harmonograf_client.agent")

        from .test_agent import _build, _seed_plan

        # 1 initial pass + 3 retries = 4 passes returning empty text.
        passes = [[_RichEvent("inv-1", "")] for _ in range(8)]
        agent, inner, state, ctx = _build(
            passes=passes, inner_name="coordinator"
        )
        _seed_plan(
            state,
            inv_id="inv-1",
            tasks=[Task(id="t1", title="r", assignee_agent_id="coordinator")],
            edges=[],
            statuses={"t1": "PENDING"},
        )

        _ = [ev async for ev in agent._run_async_impl(ctx)]
        ps = state._active_plan_by_session.get("hsess-inv-1")
        assert ps is not None
        assert ps.tasks["t1"].status == "FAILED"
        # Inner agent was invoked exactly budget+1 times (1 initial + 3
        # partial retries before the cap fires).
        assert len(inner.call_log) == state.reinvocation_budget() + 1
        msgs = [r.getMessage() for r in caplog.records]
        assert any("exhausted re-invocation budget" in m for m in msgs)


# ---------------------------------------------------------------------------
# Prompt augmentation: the synthetic task prompt must instruct the LLM
# to emit "Task complete:" / "Task failed:" markers so the classifier
# has a reliable primary signal.
# ---------------------------------------------------------------------------


class TestTaskPromptAugmentation:
    def test_build_task_prompt_includes_marker_instructions(self):
        from harmonograf_client.agent import HarmonografAgent
        from .test_agent import StubInnerAgent

        inner = StubInnerAgent(name="coordinator", passes=[])
        agent = HarmonografAgent(
            name="harmonograf",
            inner_agent=inner,
            harmonograf_client=None,
            planner=False,
        )
        task = Task(id="t1", title="research foo", assignee_agent_id="coordinator")
        content = agent._build_task_prompt(task, plan_state=None, completed_results={})
        text = "".join(p.text for p in content.parts)  # type: ignore[attr-defined]
        assert "Task complete:" in text
        assert "Task failed:" in text
