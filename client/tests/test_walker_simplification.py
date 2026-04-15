"""Tests for the simplified single-pass sequential walker.

The default ``HarmonografAgent`` orchestrator path is now a single
delegation: it writes the full plan into ``session.state`` via
``state_protocol.write_plan_context``, injects ONE plan-overview user
turn, and runs ``inner_agent.run_async`` exactly once. Per-task
lifecycle is driven by callbacks watching the reporting tools — the
walker no longer iterates the DAG itself.

These tests pin that behavior. The rigid parallel mode
(``parallel_mode=True``) is verified separately to confirm the older
walker path is still wired up.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

import pytest

from google.adk.agents.base_agent import BaseAgent

from harmonograf_client.agent import HarmonografAgent

from .test_agent import (
    FakeClient,
    FakeEvent,
    FakePlugin,
    StubInnerAgent,
    _make_ctx,
    _seed_plan,
)


def _build_seq(
    *,
    passes: list[list[Any]],
    inner_name: str = "coordinator",
    parallel: bool = False,
):
    from harmonograf_client.adk import _AdkState

    inner = StubInnerAgent(name=inner_name, passes=passes)
    client = FakeClient()
    state = _AdkState(client=client)  # type: ignore[arg-type]
    plugin = FakePlugin(state)
    agent = HarmonografAgent(
        name="harmonograf",
        inner_agent=inner,
        harmonograf_client=client,
        planner=False,
        enforce_plan=True,
        max_plan_reinvocations=0,
        parallel_mode=parallel,
    )
    ctx = _make_ctx(agent=agent, inv_id="inv-1", plugin=plugin)
    return agent, inner, state, ctx


@pytest.mark.asyncio
async def test_sequential_runs_inner_agent_exactly_once():
    """With a 3-task plan and sequential default mode, the inner agent
    is invoked exactly once for the whole plan, not once per task.
    """
    try:
        from google.genai import types as genai_types  # noqa: F401
    except ImportError:
        pytest.skip("google.genai not installed")
    from harmonograf_client.planner import Task, TaskEdge

    agent, inner, state, ctx = _build_seq(
        passes=[[FakeEvent("inv-1", "single-pass-output")]],
    )
    _seed_plan(
        state,
        inv_id="inv-1",
        tasks=[
            Task(id="t1", title="research", assignee_agent_id="coordinator"),
            Task(id="t2", title="write", assignee_agent_id="coordinator"),
            Task(id="t3", title="review", assignee_agent_id="coordinator"),
        ],
        edges=[
            TaskEdge(from_task_id="t1", to_task_id="t2"),
            TaskEdge(from_task_id="t2", to_task_id="t3"),
        ],
        statuses={"t1": "PENDING", "t2": "PENDING", "t3": "PENDING"},
    )
    got = [ev async for ev in agent._run_async_impl(ctx)]
    assert [e.payload for e in got] == ["single-pass-output"]
    # Inner agent was called exactly once for the whole plan.
    assert len(inner.call_log) == 1


@pytest.mark.asyncio
async def test_sequential_injects_single_plan_overview_with_all_tasks():
    """The sequential path injects ONE user nudge containing every task
    in the plan, not one nudge per task. The reporting-tool instructions
    are part of the overview.
    """
    try:
        from google.genai import types as genai_types  # noqa: F401
    except ImportError:
        pytest.skip("google.genai not installed")
    from harmonograf_client.planner import Task, TaskEdge

    agent, inner, state, ctx = _build_seq(
        passes=[[FakeEvent("inv-1", "ok")]],
    )
    _seed_plan(
        state,
        inv_id="inv-1",
        tasks=[
            Task(id="t1", title="research foo", assignee_agent_id="coordinator"),
            Task(id="t2", title="write slides", assignee_agent_id="coordinator"),
            Task(id="t3", title="review draft", assignee_agent_id="coordinator"),
        ],
        edges=[TaskEdge(from_task_id="t1", to_task_id="t2")],
        statuses={"t1": "PENDING", "t2": "PENDING", "t3": "PENDING"},
    )
    _ = [ev async for ev in agent._run_async_impl(ctx)]

    user_events = [
        e for e in ctx.session.events
        if e.author == "user"
        and e.content is not None
        and any(
            "Here is your plan" in (getattr(p, "text", "") or "")
            for p in (e.content.parts or [])
        )
    ]
    assert len(user_events) == 1, (
        "sequential path should inject a SINGLE plan-overview nudge"
    )
    text = "".join(
        getattr(p, "text", "") or ""
        for p in (user_events[0].content.parts or [])
    )
    # All task titles present.
    assert "research foo" in text
    assert "write slides" in text
    assert "review draft" in text
    # Reporting-tool instructions present.
    assert "report_task_started" in text
    assert "report_task_completed" in text
    assert "report_task_failed" in text
    assert "report_new_work_discovered" in text


@pytest.mark.asyncio
async def test_sequential_writes_plan_context_to_session_state():
    """The sequential path projects the plan into ``session.state`` via
    ``state_protocol.write_plan_context`` so callbacks see the full plan
    before the very first model call.
    """
    try:
        from google.genai import types as genai_types  # noqa: F401
    except ImportError:
        pytest.skip("google.genai not installed")
    from harmonograf_client.planner import Task
    from harmonograf_client.state_protocol import (
        KEY_AVAILABLE_TASKS,
        KEY_PLAN_ID,
    )

    seen_state_during_run: dict[str, Any] = {}

    class _Inspector(StubInnerAgent):
        async def _run_async_impl(self, ctx: Any) -> AsyncGenerator[Any, None]:
            self._call_log.append(ctx)
            sess_state = ctx.session.state
            seen_state_during_run["plan_id"] = sess_state.get(KEY_PLAN_ID)
            seen_state_during_run["available"] = list(
                sess_state.get(KEY_AVAILABLE_TASKS) or []
            )
            yield FakeEvent("inv-1", "done")

    from harmonograf_client.adk import _AdkState

    inner = _Inspector(name="coordinator", passes=[[]])
    client = FakeClient()
    state = _AdkState(client=client)  # type: ignore[arg-type]
    plugin = FakePlugin(state)
    agent = HarmonografAgent(
        name="harmonograf",
        inner_agent=inner,
        harmonograf_client=client,
        planner=False,
        enforce_plan=True,
        parallel_mode=False,
    )
    ctx = _make_ctx(agent=agent, inv_id="inv-1", plugin=plugin)

    _seed_plan(
        state,
        inv_id="inv-1",
        tasks=[
            Task(id="t1", title="a", assignee_agent_id="coordinator"),
            Task(id="t2", title="b", assignee_agent_id="coordinator"),
        ],
        edges=[],
        statuses={"t1": "PENDING", "t2": "PENDING"},
    )
    _ = [ev async for ev in agent._run_async_impl(ctx)]
    # The session.state was populated BEFORE the inner agent ran.
    assert seen_state_during_run.get("plan_id"), "plan id should be set"
    avail = seen_state_during_run.get("available") or []
    assert {entry["id"] for entry in avail} == {"t1", "t2"}


@pytest.mark.asyncio
async def test_sequential_calls_classify_and_sweep_at_end():
    """After the single inner-agent run exhausts, the walker calls
    ``classify_and_sweep_running_tasks`` exactly once, scoped to the
    active hsession, and excludes pre-running tasks.
    """
    try:
        from google.genai import types as genai_types  # noqa: F401
    except ImportError:
        pytest.skip("google.genai not installed")
    from harmonograf_client.planner import Task

    agent, inner, state, ctx = _build_seq(
        passes=[[FakeEvent("inv-1", "Task complete: done")]],
    )
    _seed_plan(
        state,
        inv_id="inv-1",
        tasks=[Task(id="t1", title="do", assignee_agent_id="coordinator")],
        edges=[],
        statuses={"t1": "PENDING"},
    )

    sweep_calls: list[tuple] = []
    real_sweep = state.classify_and_sweep_running_tasks

    def _spy(hsession_id="", *, result_summary="", exclude=None):
        sweep_calls.append((hsession_id, result_summary, exclude))
        return real_sweep(
            hsession_id, result_summary=result_summary, exclude=exclude
        )

    state.classify_and_sweep_running_tasks = _spy  # type: ignore[assignment]

    _ = [ev async for ev in agent._run_async_impl(ctx)]
    # Called exactly once, scoped to the seeded hsession.
    assert len(sweep_calls) == 1
    hs, _summary, exclude = sweep_calls[0]
    assert hs == "hsess-inv-1"
    # Pre-running snapshot should be a set (excluded from sweep).
    assert isinstance(exclude, set)


@pytest.mark.asyncio
async def test_sequential_no_plan_falls_back_to_delegated():
    """When no plan exists for the session, the sequential path
    degrades to the delegated path so a plan-less run still produces
    inner-agent output.
    """
    agent, inner, state, ctx = _build_seq(
        passes=[[FakeEvent("inv-1", "fallback-output")]],
    )
    # No _seed_plan: there is no active plan for the inv's session.
    got = [ev async for ev in agent._run_async_impl(ctx)]
    assert [e.payload for e in got] == ["fallback-output"]
    assert len(inner.call_log) == 1


@pytest.mark.asyncio
async def test_sequential_does_not_set_forced_task_id():
    """Sequential mode must NOT touch ``state.set_forced_task_id`` —
    the per-task forced-id mechanism only belongs to the parallel path.
    """
    try:
        from google.genai import types as genai_types  # noqa: F401
    except ImportError:
        pytest.skip("google.genai not installed")
    from harmonograf_client.planner import Task

    agent, inner, state, ctx = _build_seq(
        passes=[[FakeEvent("inv-1", "ok")]],
    )
    _seed_plan(
        state,
        inv_id="inv-1",
        tasks=[
            Task(id="t1", title="do", assignee_agent_id="coordinator"),
            Task(id="t2", title="next", assignee_agent_id="coordinator"),
        ],
        edges=[],
        statuses={"t1": "PENDING", "t2": "PENDING"},
    )
    forced_writes: list[str] = []
    real_set = state.set_forced_task_id

    def _spy(value: str) -> None:
        forced_writes.append(value)
        real_set(value)

    state.set_forced_task_id = _spy  # type: ignore[assignment]

    _ = [ev async for ev in agent._run_async_impl(ctx)]
    assert forced_writes == [], (
        "sequential path must not set forced_task_id; that belongs to parallel mode"
    )


@pytest.mark.asyncio
async def test_parallel_mode_still_uses_per_task_forced_task_id():
    """Opting into ``parallel_mode=True`` still drives the rigid DAG
    walker that scopes a forced task id per task via the ContextVar.
    """
    try:
        from google.genai import types as genai_types  # noqa: F401
    except ImportError:
        pytest.skip("google.genai not installed")
    from harmonograf_client.planner import Task, TaskEdge

    agent, inner, state, ctx = _build_seq(
        passes=[
            [FakeEvent("inv-1", "first-pass")],
            [FakeEvent("inv-1", "second-pass")],
            [FakeEvent("inv-1", "third-pass")],
            [FakeEvent("inv-1", "fourth-pass")],
        ],
        parallel=True,
    )
    _seed_plan(
        state,
        inv_id="inv-1",
        tasks=[
            Task(id="t1", title="research", assignee_agent_id="coordinator"),
            Task(id="t2", title="write", assignee_agent_id="coordinator"),
        ],
        edges=[TaskEdge(from_task_id="t1", to_task_id="t2")],
        statuses={"t1": "PENDING", "t2": "PENDING"},
    )
    _ = [ev async for ev in agent._run_async_impl(ctx)]
    # Parallel walker dispatches inner agent multiple times (one per
    # task at minimum, plus possible per-task partial re-invocations).
    assert len(inner.call_log) >= 2
    # Per-task synthetic prompts injected (the legacy walker path),
    # *not* the new plan-overview prompt.
    prompt_events = [
        e for e in ctx.session.events
        if e.author == "user"
        and e.content is not None
        and any(
            "Your current task is:" in (getattr(p, "text", "") or "")
            for p in (e.content.parts or [])
        )
    ]
    assert len(prompt_events) >= 2
    overview_events = [
        e for e in ctx.session.events
        if e.author == "user"
        and e.content is not None
        and any(
            "Here is your plan" in (getattr(p, "text", "") or "")
            for p in (e.content.parts or [])
        )
    ]
    assert overview_events == [], (
        "parallel mode must NOT emit the new plan-overview prompt"
    )


@pytest.mark.asyncio
async def test_sequential_reinvocation_loop_kicks_in_on_partial():
    """If the classifier returns a ``partial`` outcome and the state's
    re-invocation budget is non-zero, the walker re-invokes the inner
    agent and re-classifies, capping at the budget.
    """
    try:
        from google.genai import types as genai_types  # noqa: F401
    except ImportError:
        pytest.skip("google.genai not installed")
    from harmonograf_client.planner import Task

    agent, inner, state, ctx = _build_seq(
        passes=[
            [FakeEvent("inv-1", "first")],
            [FakeEvent("inv-1", "retry-1")],
            [FakeEvent("inv-1", "retry-2")],
        ],
    )
    _seed_plan(
        state,
        inv_id="inv-1",
        tasks=[Task(id="t1", title="do", assignee_agent_id="coordinator")],
        edges=[],
        statuses={"t1": "PENDING"},
    )

    call_outcomes = [
        {"t1": "partial"},
        {"t1": "partial"},
        {"t1": "completed"},
    ]
    call_idx = {"i": 0}

    def _fake_sweep(hsession_id="", *, result_summary="", exclude=None):
        out = call_outcomes[min(call_idx["i"], len(call_outcomes) - 1)]
        call_idx["i"] += 1
        return out

    state.classify_and_sweep_running_tasks = _fake_sweep  # type: ignore[assignment]
    state.reinvocation_budget = lambda: 3  # type: ignore[assignment]

    _ = [ev async for ev in agent._run_async_impl(ctx)]
    # First run + 2 retries until classifier reports completed.
    assert len(inner.call_log) == 3
