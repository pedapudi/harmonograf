"""Unit tests for :class:`HarmonografAgent`.

HarmonografAgent is a BaseAgent subclass that wraps an inner agent and
owns the plan-enforcement re-invocation loop in its ``_run_async_impl``.
These tests drive it with a stub inner agent + a minimal fake context
so a live google.adk event loop is not required.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

import pytest

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.plugins.plugin_manager import PluginManager
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.sessions.session import Session

from harmonograf_client.agent import HarmonografAgent


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.span_updates: list[tuple[str, dict]] = []

    def emit_span_start(self, **kwargs) -> str:
        return "span-1"

    def emit_span_update(self, span_id: str = "", *args, **kwargs) -> None:
        attrs = kwargs.get("attributes") or {}
        self.span_updates.append((span_id, dict(attrs)))

    def emit_span_end(self, *args, **kwargs) -> None:
        pass

    def on_control(self, kind, cb) -> None:
        pass

    def set_current_activity(self, text: str) -> None:
        pass

    def submit_plan(self, plan, **kwargs) -> str:
        self.calls.append(("submit_plan", plan, kwargs))
        return "plan-1"


def _make_ctx(*, agent: BaseAgent, inv_id: str, plugin: Any = None) -> InvocationContext:
    """Build a minimal-but-real InvocationContext for tests."""
    pm = PluginManager()
    if plugin is not None:
        pm.plugins.append(plugin)
    session = Session(
        id="s-1",
        app_name="test-app",
        user_id="u-1",
        events=[],
    )
    return InvocationContext(
        invocation_id=inv_id,
        agent=agent,
        session=session,
        session_service=InMemorySessionService(),
        plugin_manager=pm,
    )


class StubInnerAgent(BaseAgent):
    """BaseAgent subclass with a scripted ``_run_async_impl`` — each
    call consumes the next scripted event list. HarmonografAgent calls
    ``inner_agent.run_async(ctx)`` which routes through BaseAgent.
    """

    model_config = {"arbitrary_types_allowed": True}

    _passes: list
    _call_log: list

    def __init__(self, name: str, passes: list[list[Any]]) -> None:  # noqa: D401
        super().__init__(name=name)
        object.__setattr__(self, "_passes", list(passes))
        object.__setattr__(self, "_call_log", [])

    @property
    def call_log(self) -> list:
        return self._call_log

    async def _run_async_impl(self, ctx: Any) -> AsyncGenerator[Any, None]:
        self._call_log.append(ctx)
        idx = len(self._call_log) - 1
        if idx >= len(self._passes):
            return
        for ev in self._passes[idx]:
            yield ev


class _FakeEventPart:
    def __init__(self, text: str) -> None:
        self.text = text
        self.thought = False


class _FakeEventContent:
    def __init__(self, text: str) -> None:
        self.parts = [_FakeEventPart(text)] if text else []
        self.role = "model"


class FakeEvent:
    def __init__(self, invocation_id: str, payload: str = "") -> None:
        self.invocation_id = invocation_id
        self.payload = payload
        # Carry a real text part so ``_extract_result_summary`` picks up
        # the payload and the iter13 classifier sees a non-empty turn
        # result (→ "completed" via the default branch) instead of
        # treating the stub turn as partial and triggering the walker's
        # re-invocation loop.
        self.content = _FakeEventContent(payload)
        self.author = "coordinator"


from google.adk.plugins.base_plugin import BasePlugin


class FakePlugin(BasePlugin):
    """Stub plugin holding a real :class:`_AdkState`. Subclasses
    BasePlugin so ADK's PluginManager treats it like a first-class
    plugin (inheriting all the default no-op callback implementations).
    """

    def __init__(self, state: Any) -> None:
        super().__init__(name="harmonograf")
        self._hg_state = state


def _seed_plan(state, inv_id: str, tasks: list, edges: list, *, statuses: dict) -> None:
    """Seed an active plan on the given invocation's hsession so the
    orchestrator walker can pick up tasks from it. Uses the canonical
    session-id routing (``adk_sess_<inv>``) the tests already rely on.
    """
    from harmonograf_client.adk import PlanState
    from harmonograf_client.planner import Plan, Task

    tracked: dict[str, Any] = {}
    live_tasks: list[Any] = []
    for t in tasks:
        live = Task(
            id=t.id,
            title=t.title,
            description=t.description,
            assignee_agent_id=t.assignee_agent_id,
            status=statuses.get(t.id, "PENDING"),
        )
        tracked[t.id] = live
        live_tasks.append(live)
    plan = Plan(tasks=live_tasks, edges=list(edges), summary="stub plan")
    plan_state = PlanState(
        plan=plan,
        plan_id=f"plan-{inv_id}",
        tasks=tracked,
        available_agents=["coordinator"],
        generating_invocation_id=inv_id,
        remaining_for_fallback=list(live_tasks),
    )
    hsess_id = f"hsess-{inv_id}"
    with state._lock:
        state._active_plan_by_session[hsess_id] = plan_state
        state._invocation_route[inv_id] = ("coordinator", hsess_id)
        # Also populate the legacy snapshot so other tests that reach
        # into it continue to work.
        state._plan_snapshot_for_inv[inv_id] = (plan, tracked)


def _build(
    *,
    inner_name: str = "coordinator",
    passes: list[list[Any]] = None,
    max_reinvocations: int = 3,
    enforce_plan: bool = True,
):
    from harmonograf_client.adk import _AdkState

    inner = StubInnerAgent(name=inner_name, passes=passes or [])
    client = FakeClient()
    state = _AdkState(client=client)  # type: ignore[arg-type]
    plugin = FakePlugin(state)
    agent = HarmonografAgent(
        name="harmonograf",
        inner_agent=inner,
        harmonograf_client=client,
        planner=False,
        enforce_plan=enforce_plan,
        max_plan_reinvocations=max_reinvocations,
        parallel_mode=True,
    )
    ctx = _make_ctx(agent=agent, inv_id="inv-1", plugin=plugin)
    return agent, inner, state, ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_inner_agent_wired_into_sub_agents(self):
        inner = StubInnerAgent(name="coordinator", passes=[])
        agent = HarmonografAgent(
            name="harmonograf", inner_agent=inner, planner=False
        )
        assert agent.sub_agents == [inner]
        assert agent.inner_agent is inner

    def test_is_harmonograf_agent_marker(self):
        inner = StubInnerAgent(name="coordinator", passes=[])
        agent = HarmonografAgent(
            name="harmonograf", inner_agent=inner, planner=False
        )
        assert getattr(agent, "_is_harmonograf_agent", False) is True


class TestRunAsyncImpl:
    @pytest.mark.asyncio
    async def test_yields_inner_events_without_plan(self):
        agent, inner, state, ctx = _build(
            passes=[[FakeEvent("inv-1", "hello"), FakeEvent("inv-1", "world")]]
        )
        got = [ev async for ev in agent._run_async_impl(ctx)]
        assert [e.payload for e in got] == ["hello", "world"]
        assert len(inner.call_log) == 1

    @pytest.mark.asyncio
    async def test_orchestrator_walks_pending_tasks_in_dag_order(self):
        """The orchestrator walker should iterate through PENDING tasks
        in dependency order, calling the inner agent once per task, and
        inject a synthetic task prompt mentioning the current task.
        """
        try:
            from google.genai import types as genai_types  # noqa: F401
        except ImportError:
            pytest.skip("google.genai not installed")
        from harmonograf_client.planner import Task, TaskEdge

        agent, inner, state, ctx = _build(
            passes=[
                [FakeEvent("inv-1", "first-pass")],
                [FakeEvent("inv-1", "second-pass")],
            ],
            inner_name="coordinator",
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
        got = [ev async for ev in agent._run_async_impl(ctx)]
        assert [e.payload for e in got] == ["first-pass", "second-pass"]
        assert len(inner.call_log) == 2
        # Two synthetic "Your current task is:" events — one per task.
        prompt_events = [
            e for e in ctx.session.events
            if e.author == "user"
            and e.content is not None
            and any(
                "Your current task is:" in (getattr(p, "text", "") or "")
                for p in (e.content.parts or [])
            )
        ]
        assert len(prompt_events) == 2
        texts = [
            "".join(getattr(p, "text", "") or "" for p in (e.content.parts or []))
            for e in prompt_events
        ]
        assert "research" in texts[0]
        assert "write" in texts[1]

    @pytest.mark.asyncio
    async def test_orchestrator_stops_when_no_eligible_task(self):
        """Once every task has status COMPLETED, the walker picks no
        next task, yields nothing further, and calls the inner agent
        exactly zero additional times beyond the pre-completed plan.
        """
        from harmonograf_client.planner import Task

        agent, inner, state, ctx = _build(
            passes=[[FakeEvent("inv-1", "ignored")]],
        )
        _seed_plan(
            state,
            inv_id="inv-1",
            tasks=[Task(id="t1", title="done", assignee_agent_id="coordinator")],
            edges=[],
            statuses={"t1": "COMPLETED"},
        )
        got = [ev async for ev in agent._run_async_impl(ctx)]
        # No PENDING tasks: iteration 1 falls back to a single delegated
        # pass so a plan-less/completed run still yields the scripted
        # inner output once.
        assert len(got) == 1
        assert len(inner.call_log) == 1

    @pytest.mark.asyncio
    async def test_delegated_mode_observer_refines_on_drift(self):
        """With ``orchestrator_mode=False`` the observer still runs
        after the single inner-agent turn, calling
        :meth:`refine_plan_on_drift` when drift is detected.
        """
        from harmonograf_client.adk import _AdkState
        from harmonograf_client.planner import Plan, PlannerHelper, Task

        class _DriftingInner(BaseAgent):
            model_config = {"arbitrary_types_allowed": True}

            async def _run_async_impl(self, ctx: Any) -> AsyncGenerator[Any, None]:
                class _FC:
                    name = "search"

                class _Part:
                    def __init__(self) -> None:
                        self.function_call = _FC()
                        self.text = None
                        self.thought = False

                class _C:
                    def __init__(self) -> None:
                        self.parts = [_Part()]
                        self.role = "model"

                class _Ev:
                    id = "ev-1"
                    author = "otherbot"  # wrong-agent → drift
                    content = _C()
                    actions = None
                    status = ""
                    task_id = ""
                    completed_task_id = ""
                    invocation_id = "inv-1"
                    partial = False

                yield _Ev()

        refine_calls: list[str] = []

        class _Planner(PlannerHelper):
            def generate(self, **kwargs):  # type: ignore[override]
                return Plan(
                    tasks=[Task(id="t1", title="do", assignee_agent_id="worker")],
                    edges=[],
                )

            def refine(self, plan, event):  # type: ignore[override]
                refine_calls.append(event.get("kind", ""))
                return None

        inner = _DriftingInner(name="worker")
        client = FakeClient()
        state = _AdkState(client=client, planner=_Planner())  # type: ignore[arg-type]
        plugin = FakePlugin(state)
        agent = HarmonografAgent(
            name="harmonograf",
            inner_agent=inner,
            harmonograf_client=client,
            planner=False,
            orchestrator_mode=False,
        )
        ctx = _make_ctx(agent=agent, inv_id="inv-1", plugin=plugin)
        _seed_plan(
            state,
            inv_id="inv-1",
            tasks=[Task(id="t1", title="do", assignee_agent_id="worker")],
            edges=[],
            statuses={"t1": "PENDING"},
        )

        got = [ev async for ev in agent._run_async_impl(ctx)]
        assert len(got) == 1
        assert refine_calls, (
            "observer should have called planner.refine after detecting drift"
        )

    @pytest.mark.asyncio
    async def test_delegated_mode_runs_inner_once_only(self):
        """With ``orchestrator_mode=False`` the agent delegates a single
        turn to the inner agent and does not walk further tasks, even
        when the plan has more PENDING work.
        """
        from harmonograf_client.adk import _AdkState
        from harmonograf_client.planner import Task

        inner = StubInnerAgent(
            name="coordinator",
            passes=[[FakeEvent("inv-1", "one")], [FakeEvent("inv-1", "two")]],
        )
        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        plugin = FakePlugin(state)
        agent = HarmonografAgent(
            name="harmonograf",
            inner_agent=inner,
            harmonograf_client=client,
            planner=False,
            orchestrator_mode=False,
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
        got = [ev async for ev in agent._run_async_impl(ctx)]
        assert len(got) == 1
        assert len(inner.call_log) == 1

    @pytest.mark.asyncio
    async def test_captures_thought_parts_into_llm_thought_attr(self):
        """HarmonografAgent inspects yielded events for Gemini 2.5
        ``thought=True`` parts and attaches the accumulated reasoning
        text to the in-flight LLM_CALL span as ``llm.thought``."""

        class _Part:
            def __init__(self, text: str, thought: bool) -> None:
                self.text = text
                self.thought = thought

        class _Content:
            def __init__(self, parts: list) -> None:
                self.parts = parts

        class _ThoughtEvent:
            def __init__(self, thought: str, response: str = "") -> None:
                parts = []
                if thought:
                    parts.append(_Part(thought, True))
                if response:
                    parts.append(_Part(response, False))
                self.content = _Content(parts)
                self.invocation_id = "inv-1"
                self.payload = ""

        agent, inner, state, ctx = _build(
            passes=[
                [
                    _ThoughtEvent("Considering options. "),
                    _ThoughtEvent("Picking plan A. ", response="Here is plan A."),
                ]
            ],
        )
        # Seed an open LLM span on the state so the agent's thought
        # capture path can find it.
        with state._lock:
            state._llm_by_invocation["inv-1"] = "llm-span-1"

        _ = [ev async for ev in agent._run_async_impl(ctx)]

        client = agent.harmonograf_client
        thought_updates = [
            (sid, attrs["llm.thought"])
            for sid, attrs in client.span_updates
            if "llm.thought" in attrs
        ]
        assert thought_updates, "expected llm.thought span update"
        assert all(sid == "llm-span-1" for sid, _ in thought_updates)
        # Latest update should contain the concatenated reasoning trace.
        assert "Considering options" in thought_updates[-1][1]
        assert "Picking plan A" in thought_updates[-1][1]

    @pytest.mark.asyncio
    async def test_no_plugin_means_pure_passthrough(self):
        inner = StubInnerAgent(
            name="coordinator",
            passes=[[FakeEvent("inv-1", "only")]],
        )
        agent = HarmonografAgent(
            name="harmonograf", inner_agent=inner, planner=False
        )
        ctx = _make_ctx(agent=agent, inv_id="inv-1", plugin=None)
        got = [ev async for ev in agent._run_async_impl(ctx)]
        assert [e.payload for e in got] == ["only"]


class TestForcedTaskIdPath:
    """Forced-task-id: HarmonografAgent declares which plan task is
    currently in flight, and every span emitted during that step binds
    to that task id regardless of the (fragile) assignee-string
    heuristic. This makes the agent's loop authoritative over plan state
    so stamping doesn't drift just because span.agent_id doesn't match
    task.assignee_agent_id verbatim.
    """

    @pytest.mark.asyncio
    async def test_forced_task_id_stamps_current_task(self):
        from harmonograf_client.adk import PlanState, _AdkState
        from harmonograf_client.planner import Plan, Task

        client = FakeClient()
        state = _AdkState(client=client)  # type: ignore[arg-type]
        # Seed a plan with two tasks assigned to a DIFFERENT agent id
        # than the one _stamp_attrs_with_task will be called with, so
        # the assignee heuristic would normally decline to stamp.
        tracked = {
            "t1": Task(
                id="t1",
                title="research",
                assignee_agent_id="planner_style_name",
                status="PENDING",
            ),
            "t2": Task(
                id="t2",
                title="write",
                assignee_agent_id="planner_style_name",
                status="PENDING",
            ),
        }
        plan = Plan(tasks=list(tracked.values()), edges=[])
        plan_state = PlanState(
            plan=plan,
            plan_id="plan-1",
            tasks=dict(tracked),
            available_agents=["planner_style_name"],
            generating_invocation_id="inv-1",
            remaining_for_fallback=list(tracked.values()),
        )
        with state._lock:
            state._active_plan_by_session["hsess-1"] = plan_state
            state._invocation_route["inv-1"] = ("adk_runtime_name", "hsess-1")

        state.set_forced_task_id("t1")
        stamped = state._stamp_attrs_with_task({}, "adk_runtime_name", "hsess-1")
        assert stamped is not None
        assert stamped.get("hgraf.task_id") == "t1"
        assert plan_state.tasks["t1"].status == "RUNNING"

        # A subsequent call with the same forced id stamps the same task
        # (no pop-from-remaining side effect like the fallback path).
        stamped2 = state._stamp_attrs_with_task({}, "adk_runtime_name", "hsess-1")
        assert stamped2 is not None
        assert stamped2.get("hgraf.task_id") == "t1"

        # mark_forced_task_completed marks COMPLETED and clears forced id.
        cleared = state.mark_forced_task_completed()
        assert cleared == "t1"
        assert plan_state.tasks["t1"].status == "COMPLETED"
        assert state.forced_task_id() == ""

        # With forced id cleared, the assignee fallback path runs. It
        # won't find a match for a foreign agent id, so no stamping.
        stamped3 = state._stamp_attrs_with_task({}, "adk_runtime_name", "hsess-1")
        assert stamped3 is not None
        assert "hgraf.task_id" not in stamped3

    @pytest.mark.asyncio
    async def test_agent_sets_forced_task_id_before_inner_run(self):
        """HarmonografAgent._run_async_impl should declare the forced
        task id on the state before delegating to the inner agent, and
        clear (marking COMPLETED) after the inner generator exhausts.
        """
        from harmonograf_client.planner import Task

        observed: list[str] = []

        class _ObservingInner(StubInnerAgent):
            async def _run_async_impl(self, ctx: Any) -> AsyncGenerator[Any, None]:
                self._call_log.append(ctx)
                # Peek at the state held by the test's plugin manager.
                pm = ctx.plugin_manager
                for p in pm.plugins:
                    st = getattr(p, "_hg_state", None)
                    if st is not None:
                        observed.append(st.forced_task_id())
                idx = len(self._call_log) - 1
                if idx < len(self._passes):
                    for ev in self._passes[idx]:
                        yield ev

        from harmonograf_client.adk import _AdkState

        inner = _ObservingInner(
            name="coordinator",
            passes=[[FakeEvent("inv-1", "e1")]],
        )
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
            parallel_mode=True,
        )
        ctx = _make_ctx(agent=agent, inv_id="inv-1", plugin=plugin)

        # Seed an active plan visible to _next_task_for_agent.
        from harmonograf_client.adk import PlanState
        from harmonograf_client.planner import Plan

        plan = Plan(
            tasks=[
                Task(id="t1", title="do", assignee_agent_id="coordinator"),
            ],
            edges=[],
        )
        tracked_t1 = Task(
            id="t1",
            title="do",
            assignee_agent_id="coordinator",
            status="PENDING",
        )
        plan_state = PlanState(
            plan=plan,
            plan_id="plan-1",
            tasks={"t1": tracked_t1},
            available_agents=["coordinator"],
            generating_invocation_id="inv-1",
            remaining_for_fallback=[tracked_t1],
        )
        with state._lock:
            state._active_plan_by_session["hsess-1"] = plan_state
            state._invocation_route["inv-1"] = ("coordinator", "hsess-1")

        _ = [ev async for ev in agent._run_async_impl(ctx)]

        assert observed == ["t1"], (
            "forced task id should be set before inner agent runs"
        )
        # After the step, the forced id is cleared and the task is
        # marked COMPLETED authoritatively by the outer loop.
        assert state.forced_task_id() == ""
        assert plan_state.tasks["t1"].status == "COMPLETED"


class TestRefineLivePropagation:
    """_maybe_refine_plan must call submit_plan with the SAME plan_id
    so the server/UI treats the refined plan as a live upsert rather
    than a brand-new plan. Verifies logging + submit shape.
    """

    def test_refine_updates_plan_live(self, caplog):
        from harmonograf_client.adk import _AdkState
        from harmonograf_client.planner import Plan, Task

        class _RefiningPlanner:
            def __init__(self) -> None:
                self.refine_calls = 0

            def generate(self, **_kwargs: Any) -> Plan:
                return Plan(
                    tasks=[
                        Task(
                            id="t1",
                            title="old",
                            assignee_agent_id="coordinator",
                        ),
                    ],
                    edges=[],
                )

            def refine(self, plan: Plan, event: dict) -> Plan:
                self.refine_calls += 1
                return Plan(
                    tasks=[
                        Task(
                            id="t1",
                            title="old",
                            assignee_agent_id="coordinator",
                            status="COMPLETED",
                        ),
                        Task(
                            id="t2",
                            title="NEW refined task",
                            assignee_agent_id="coordinator",
                        ),
                    ],
                    edges=[],
                )

        client = FakeClient()
        planner = _RefiningPlanner()
        state = _AdkState(client=client, planner=planner, refine_on_events=True)  # type: ignore[arg-type]

        # Seed submitted plan directly (bypassing maybe_run_planner's
        # ic-extraction plumbing). This mirrors what that method does.
        from harmonograf_client.adk import PlanState

        initial_plan = planner.generate()
        plan_id = client.submit_plan(initial_plan, invocation_span_id="", session_id=None)
        plan_state = PlanState(
            plan=initial_plan,
            plan_id=plan_id,
            tasks={t.id: t for t in initial_plan.tasks},
            available_agents=["coordinator"],
            generating_invocation_id="inv-1",
            remaining_for_fallback=list(initial_plan.tasks),
        )
        with state._lock:
            state._active_plan_by_session["hsess-1"] = plan_state
            state._invocation_route["inv-1"] = ("coordinator", "hsess-1")

        import logging

        with caplog.at_level(logging.INFO, logger="harmonograf_client.adk"):
            state._maybe_refine_plan(
                "inv-1", {"kind": "tool_end", "tool_name": "search"}
            )

        assert planner.refine_calls == 1
        # Two submit_plan calls: initial + refine. Refine call must
        # carry plan_id to upsert rather than create.
        submit_kwargs = [c[2] for c in client.calls if c[0] == "submit_plan"]
        assert len(submit_kwargs) == 2
        assert submit_kwargs[1].get("plan_id") == plan_id

        # Tracking dict was replaced with refined-plan tasks.
        assert set(plan_state.tasks.keys()) == {"t1", "t2"}
        assert any(
            "planner.refine: invoking" in rec.message for rec in caplog.records
        )
        assert any(
            "refined plan" in rec.message and "live upsert" in rec.message
            for rec in caplog.records
        )


class TestPluginPlannerNoOpWhenWrapped:
    """When ``ic.agent`` is a HarmonografAgent, the plugin's own
    ``maybe_run_planner`` should no-op so planning isn't duplicated —
    HarmonografAgent itself owns the host_agent override path.
    """

    def test_maybe_run_planner_skips_harmonograf_agent(self):
        from harmonograf_client.adk import _AdkState
        from harmonograf_client.planner import (
            PassthroughPlanner,
            Plan,
            Task,
        )

        class FakeIC:
            def __init__(self, agent: Any) -> None:
                self.invocation_id = "inv-1"
                self.agent = agent

        class _FixedPlanner:
            calls: list[Any] = []

            def generate(self, **kwargs: Any) -> Plan:
                _FixedPlanner.calls.append(kwargs)
                return Plan(
                    tasks=[Task(id="t1", title="x", assignee_agent_id="x")],
                    edges=[],
                )

            def refine(self, plan, event):
                return None

        state = _AdkState(client=FakeClient(), planner=_FixedPlanner())  # type: ignore[arg-type]
        inner = StubInnerAgent(name="coordinator", passes=[])
        harmonograf = HarmonografAgent(
            name="harmonograf", inner_agent=inner, planner=False
        )
        ic = FakeIC(agent=harmonograf)
        state.maybe_run_planner(ic)
        # No planner generate call when host_agent is None AND ic.agent
        # is marked as harmonograf.
        assert _FixedPlanner.calls == []


# ---------------------------------------------------------------------------
# Part A — parallel within-stage execution
# ---------------------------------------------------------------------------


class _SleepAgent(BaseAgent):
    """Sub-agent that sleeps ``delay`` seconds then yields a single
    scripted text response. Used to measure parallel vs. serial timing.
    """

    model_config = {"arbitrary_types_allowed": True}

    _delay: float
    _response_text: str

    def __init__(self, name: str, delay: float, response_text: str) -> None:
        super().__init__(name=name)
        object.__setattr__(self, "_delay", delay)
        object.__setattr__(self, "_response_text", response_text)

    async def _run_async_impl(self, ctx: Any) -> AsyncGenerator[Any, None]:
        import asyncio as _asyncio

        await _asyncio.sleep(self._delay)

        class _Part:
            def __init__(self, text: str) -> None:
                self.text = text
                self.thought = False

        class _Content:
            def __init__(self, text: str) -> None:
                self.parts = [_Part(text)]
                self.role = "model"

        class _Ev:
            def __init__(self, name: str, text: str) -> None:
                self.invocation_id = "inv-1"
                self.author = name
                self.content = _Content(text)
                self.actions = None
                self.status = ""
                self.task_id = ""
                self.completed_task_id = ""
                self.id = f"ev-{name}"
                self.partial = False
                self.payload = text

        yield _Ev(self.name, self._response_text)


def _build_parallel(tasks_and_agents: list, edges: list):
    """Build a HarmonografAgent whose inner_agent is a coordinator with
    sub-agents matching the assignees in ``tasks_and_agents``. Returns
    (agent, state, ctx, sub_agents).
    """
    from harmonograf_client.adk import _AdkState

    sub_agents: list = []
    seen: set[str] = set()
    for _t, assignee, delay, response in tasks_and_agents:
        if assignee in seen:
            continue
        seen.add(assignee)
        sub_agents.append(_SleepAgent(name=assignee, delay=delay, response_text=response))

    class _Coord(BaseAgent):
        model_config = {"arbitrary_types_allowed": True}

        async def _run_async_impl(self, ctx: Any) -> AsyncGenerator[Any, None]:
            return
            yield  # pragma: no cover

    coordinator = _Coord(name="coordinator", sub_agents=sub_agents)
    client = FakeClient()
    state = _AdkState(client=client)  # type: ignore[arg-type]
    plugin = FakePlugin(state)
    agent = HarmonografAgent(
        name="harmonograf",
        inner_agent=coordinator,
        harmonograf_client=client,
        planner=False,
        parallel_mode=True,
    )
    ctx = _make_ctx(agent=agent, inv_id="inv-1", plugin=plugin)

    from harmonograf_client.planner import Task

    planner_tasks = [
        Task(id=tid, title=tid, assignee_agent_id=assignee)
        for tid, assignee, _d, _r in tasks_and_agents
    ]
    statuses = {t.id: "PENDING" for t in planner_tasks}
    _seed_plan(
        state,
        inv_id="inv-1",
        tasks=planner_tasks,
        edges=edges,
        statuses=statuses,
    )
    return agent, state, ctx, sub_agents


class TestParallelWithinStage:
    @pytest.mark.asyncio
    async def test_parallel_stage_executes_concurrently(self):
        """Three within-stage tasks assigned to different sub-agents
        must run concurrently. Each sub-agent sleeps ~150ms; serial
        execution would take >450ms, parallel should take <400ms.
        """
        try:
            from google.genai import types as genai_types  # noqa: F401
        except ImportError:
            pytest.skip("google.genai not installed")
        from harmonograf_client.planner import TaskEdge

        DELAY = 0.15
        agent, state, ctx, _subs = _build_parallel(
            tasks_and_agents=[
                ("t_a", "agent_a", DELAY, "done a"),
                ("t_b", "agent_b", DELAY, "done b"),
                ("t_c", "agent_c", DELAY, "done c"),
                ("t_d", "agent_a", 0.0, "done d"),  # stage 1 — depends on all
            ],
            edges=[
                TaskEdge(from_task_id="t_a", to_task_id="t_d"),
                TaskEdge(from_task_id="t_b", to_task_id="t_d"),
                TaskEdge(from_task_id="t_c", to_task_id="t_d"),
            ],
        )

        import time as _time
        start = _time.monotonic()
        events = [ev async for ev in agent._run_async_impl(ctx)]
        elapsed = _time.monotonic() - start

        # Parallel: 3x150ms in parallel + 1x 0 = ~150ms, well under
        # a 450ms serial lower bound. Use 400ms as a comfortable cap.
        assert elapsed < 0.40, (
            f"expected parallel execution < 0.40s, got {elapsed:.3f}s "
            f"(serial would be >0.45s)"
        )
        # We yielded events from all 4 tasks (3 stage-0 + 1 stage-1).
        authors = {getattr(e, "author", "") for e in events}
        assert {"agent_a", "agent_b", "agent_c"}.issubset(authors)

        # Stage 1's t_d prompt saw all three predecessors' summaries.
        prompts = [
            "".join(getattr(p, "text", "") or "" for p in (e.content.parts or []))
            for e in ctx.session.events
            if getattr(e, "author", "") == "user"
            and e.content is not None
            and any(
                "t_d" in (getattr(p, "text", "") or "")
                for p in (e.content.parts or [])
            )
        ]
        assert prompts, "expected a synthetic prompt for stage-1 task t_d"
        t_d_prompt = prompts[-1]
        assert "done a" in t_d_prompt
        assert "done b" in t_d_prompt
        assert "done c" in t_d_prompt

    @pytest.mark.asyncio
    async def test_serial_same_assignee(self):
        """Two tasks assigned to the SAME sub-agent must serialize —
        total time ≈ 2×delay, not ≈ 1×delay.
        """
        try:
            from google.genai import types as genai_types  # noqa: F401
        except ImportError:
            pytest.skip("google.genai not installed")

        DELAY = 0.08
        agent, state, ctx, _subs = _build_parallel(
            tasks_and_agents=[
                ("t_a", "agent_a", DELAY, "a1"),
                ("t_b", "agent_a", DELAY, "a2"),  # same assignee
            ],
            edges=[],
        )

        import time as _time
        start = _time.monotonic()
        _ = [ev async for ev in agent._run_async_impl(ctx)]
        elapsed = _time.monotonic() - start
        # Serial within group: >= 2*DELAY. Allow generous slack.
        assert elapsed >= 2 * DELAY * 0.9, (
            f"same-assignee tasks must serialize: got {elapsed:.3f}s, "
            f"expected ≥ {2 * DELAY * 0.9:.3f}s"
        )

    @pytest.mark.asyncio
    async def test_refined_plan_picked_up_next_batch(self):
        """When refine adds a new task mid-walk, the next batch picks
        it up. Uses a PlannerHelper whose ``refine`` returns a plan
        with one more PENDING task than the current plan.
        """
        try:
            from google.genai import types as genai_types  # noqa: F401
        except ImportError:
            pytest.skip("google.genai not installed")
        from harmonograf_client.adk import PlanState, _AdkState
        from harmonograf_client.planner import (
            Plan,
            PlannerHelper,
            Task,
        )

        refined_once = {"v": False}

        class _Planner(PlannerHelper):
            def generate(self, **kwargs):  # type: ignore[override]
                return Plan(tasks=[], edges=[])

            def refine(self, plan, event):  # type: ignore[override]
                if refined_once["v"]:
                    return None
                refined_once["v"] = True
                return Plan(
                    tasks=[
                        Task(
                            id="t1",
                            title="orig",
                            assignee_agent_id="agent_a",
                            status="COMPLETED",
                        ),
                        Task(
                            id="t2",
                            title="refined-new",
                            assignee_agent_id="agent_a",
                        ),
                    ],
                    edges=[],
                )

        # t1 returns a result mentioning new work → triggers semantic
        # drift (task_result_new_work) → refine fires → refined plan
        # adds t2 → next batch picks it up. iter13 classifier treats the
        # non-empty default-branch text as "completed" for t1 itself.
        # Single-task batches in parallel mode run ``inner_agent``
        # directly (not the assignee sub), so the inner IS the worker.
        inner = _SleepAgent(
            name="agent_a",
            delay=0.0,
            response_text=(
                "I completed the first half, but I need to gather more "
                "data about the second half before continuing."
            ),
        )
        client = FakeClient()
        state = _AdkState(client=client, planner=_Planner())  # type: ignore[arg-type]
        plugin = FakePlugin(state)
        agent = HarmonografAgent(
            name="harmonograf",
            inner_agent=inner,
            harmonograf_client=client,
            planner=False,
            parallel_mode=True,
        )
        ctx = _make_ctx(agent=agent, inv_id="inv-1", plugin=plugin)
        _seed_plan(
            state,
            inv_id="inv-1",
            tasks=[Task(id="t1", title="orig", assignee_agent_id="agent_a")],
            edges=[],
            statuses={"t1": "PENDING"},
        )

        _ = [ev async for ev in agent._run_async_impl(ctx)]
        # After the walker finishes, the refined plan's t2 should have
        # been picked up in a subsequent iteration and marked COMPLETED.
        plan_state = state._active_plan_by_session["hsess-inv-1"]
        assert "t2" in plan_state.tasks, "refined plan should add t2"
        assert plan_state.tasks["t2"].status == "COMPLETED", (
            "t2 should have been picked up in a later batch and completed"
        )


# ---------------------------------------------------------------------------
# Part B — semantic drift detection
# ---------------------------------------------------------------------------


class TestSemanticDrift:
    def _state(self) -> Any:
        from harmonograf_client.adk import _AdkState

        return _AdkState(client=FakeClient())  # type: ignore[arg-type]

    def _task(self, tid: str = "t1") -> Any:
        from harmonograf_client.planner import Task

        return Task(id=tid, title="x", assignee_agent_id="agent_a")

    def test_task_failure_triggers_semantic_refine(self):
        state = self._state()
        drift = state.detect_semantic_drift(
            self._task(),
            "Error: the tool could not complete the operation",
            [],
        )
        assert drift is not None
        assert drift.kind == "task_failed"

    def test_task_failure_from_event_status(self):
        state = self._state()

        class _Ev:
            id = "e1"
            status = "FAILED"

        drift = state.detect_semantic_drift(
            self._task(), "nominal result text here padded out", [_Ev()]
        )
        assert drift is not None
        assert drift.kind == "task_failed"

    def test_empty_result_triggers_semantic_refine(self):
        state = self._state()
        drift = state.detect_semantic_drift(self._task(), "", [])
        assert drift is not None
        assert drift.kind == "task_empty_result"

    def test_new_work_keyword_triggers_semantic_refine(self):
        state = self._state()
        drift = state.detect_semantic_drift(
            self._task(),
            "I completed the first half, but I need to gather more data "
            "about the second half before continuing.",
            [],
        )
        assert drift is not None
        assert drift.kind == "task_result_new_work"

    def test_contradicts_plan_triggers_semantic_refine(self):
        state = self._state()
        drift = state.detect_semantic_drift(
            self._task(),
            "After reviewing the prior step, it was incorrect and we "
            "need a different approach entirely.",
            [],
        )
        assert drift is not None
        # First-match wins: "need a different approach" hits the new-work
        # bucket via "need a", not strictly — actually "need a" is not
        # a marker. Should hit contradicts via "was incorrect".
        assert drift.kind in ("task_result_contradicts_plan", "task_result_new_work")

    def test_nominal_result_no_drift(self):
        state = self._state()
        drift = state.detect_semantic_drift(
            self._task(),
            "Research complete: the answer is forty-two and all data "
            "sources agree on this value.",
            [],
        )
        assert drift is None


# ---------------------------------------------------------------------------
# Regression tests for Task #5: STEER(cancel) teardown + orchestration audit
# ---------------------------------------------------------------------------


class TestCancelTeardown:
    """Bug A: STEER(cancel) mid-run must not leak plan state or let an
    unhandled CancelledError propagate through the ASGI response gen.
    """

    @pytest.mark.asyncio
    async def test_cancel_steer_closes_generator_cleanly(self):
        """Drive HarmonografAgent in orchestrator mode; inject a
        CancelledError mid-iteration and assert (a) only CancelledError
        propagates, (b) the plan snapshot for the invocation is cleared
        by the ``finally`` block even on the cancel path.
        """
        import asyncio

        from harmonograf_client.planner import Task

        agent, inner, state, ctx = _build(
            passes=[
                [FakeEvent("inv-1", "first")],
                [FakeEvent("inv-1", "second")],
            ],
            inner_name="coordinator",
        )
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
        assert "inv-1" in state._plan_snapshot_for_inv

        gen = agent._run_async_impl(ctx)
        first = await gen.__anext__()
        assert first.payload == "first"

        # Simulate the ASGI/Runner cancelling the async generator
        # mid-loop (the STEER(cancel) code path).
        with pytest.raises(asyncio.CancelledError):
            await gen.athrow(asyncio.CancelledError)

        # Cleanup ran regardless of cancel path.
        assert "inv-1" not in state._plan_snapshot_for_inv

    @pytest.mark.asyncio
    async def test_generator_exit_also_cleans_up_plan_snapshot(self):
        """Same guarantee for the ``GeneratorExit`` path (e.g. when the
        ASGI driver calls ``aclose()`` on the response gen).
        """
        from harmonograf_client.planner import Task

        agent, inner, state, ctx = _build(
            passes=[
                [FakeEvent("inv-1", "first")],
                [FakeEvent("inv-1", "second")],
            ],
            inner_name="coordinator",
        )
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
        assert "inv-1" in state._plan_snapshot_for_inv

        gen = agent._run_async_impl(ctx)
        await gen.__anext__()
        # aclose() injects GeneratorExit into the paused coroutine.
        await gen.aclose()

        assert "inv-1" not in state._plan_snapshot_for_inv


class TestOrchestratorSingleAgentFanout:
    @pytest.mark.asyncio
    async def test_orchestrator_executes_all_tasks_for_single_agent(self):
        """Seed a plan with 3 tasks all assigned to the same agent and
        assert the walker runs the inner agent 3 times and visits each
        task title in a synthetic prompt exactly once. Regression for
        the iter12 "research agent did not execute on all 3 items" bug.
        """
        try:
            from google.genai import types as genai_types  # noqa: F401
        except ImportError:
            pytest.skip("google.genai not installed")
        from harmonograf_client.planner import Task

        agent, inner, state, ctx = _build(
            passes=[
                [FakeEvent("inv-1", "p1")],
                [FakeEvent("inv-1", "p2")],
                [FakeEvent("inv-1", "p3")],
            ],
            inner_name="coordinator",
            max_reinvocations=5,
        )
        _seed_plan(
            state,
            inv_id="inv-1",
            tasks=[
                Task(id="t1", title="research-alpha", assignee_agent_id="coordinator"),
                Task(id="t2", title="research-beta", assignee_agent_id="coordinator"),
                Task(id="t3", title="research-gamma", assignee_agent_id="coordinator"),
            ],
            edges=[],
            statuses={"t1": "PENDING", "t2": "PENDING", "t3": "PENDING"},
        )

        got = [ev async for ev in agent._run_async_impl(ctx)]
        assert [e.payload for e in got] == ["p1", "p2", "p3"]
        assert len(inner.call_log) == 3

        texts = []
        for e in ctx.session.events:
            if e.author != "user" or e.content is None:
                continue
            joined = "".join(
                getattr(p, "text", "") or "" for p in (e.content.parts or [])
            )
            if "Your current task is:" in joined:
                texts.append(joined)
        assert len(texts) == 3
        assert any("research-alpha" in t for t in texts)
        assert any("research-beta" in t for t in texts)
        assert any("research-gamma" in t for t in texts)


class TestAssigneeCanonicalization:
    def test_empty_assignee_backfilled_to_first_known_agent(self, caplog):
        """Bug B: empty assignees are silently impossible to pick up by
        the walker, causing tasks to sit PENDING forever. They should
        be backfilled to the first known agent at plan submit / refine.
        """
        import logging

        from harmonograf_client.adk import _canonicalize_plan_assignees
        from harmonograf_client.planner import Plan, Task

        plan = Plan(
            tasks=[
                Task(id="t1", title="a", assignee_agent_id=""),
                Task(id="t2", title="b", assignee_agent_id="research_agent"),
            ],
            edges=[],
        )
        with caplog.at_level(logging.INFO, logger="harmonograf_client.adk"):
            _canonicalize_plan_assignees(
                plan, ["research_agent", "web_developer_agent"]
            )
        assert plan.tasks[0].assignee_agent_id == "research_agent"
        assert plan.tasks[1].assignee_agent_id == "research_agent"

    def test_unresolvable_assignee_preserved_with_warning(self, caplog):
        """Non-empty assignees that canonicalization can't resolve (LLM
        hallucinated "presentation" — the Client name — not a real
        sub-agent) are preserved as-is but surfaced via a WARNING log so
        the drift is visible instead of silently swallowed.
        """
        import logging

        from harmonograf_client.adk import _canonicalize_plan_assignees
        from harmonograf_client.planner import Plan, Task

        plan = Plan(
            tasks=[
                Task(
                    id="t1",
                    title="a",
                    assignee_agent_id="presentation",
                ),
            ],
            edges=[],
        )
        with caplog.at_level(logging.WARNING, logger="harmonograf_client.adk"):
            _canonicalize_plan_assignees(
                plan, ["research_agent", "web_developer_agent"]
            )
        assert plan.tasks[0].assignee_agent_id == "presentation"
        assert any(
            "preserving unresolved" in rec.message and "presentation" in rec.message
            for rec in caplog.records
        )


class TestRefineDoesNotDuplicate:
    def test_refine_upserts_by_task_id_preserving_count(self):
        """Bug B: ``refine_plan_on_drift`` must replace tasks by id, not
        append. A refine that updates one of 3 existing tasks (same id,
        new description) must leave the plan with 3 tasks, not 4.
        """
        from harmonograf_client.adk import DriftReason, _AdkState
        from harmonograf_client.planner import Plan, PlannerHelper, Task

        class _Planner(PlannerHelper):
            def generate(self, **kwargs):  # type: ignore[override]
                return Plan(tasks=[], edges=[])

            def refine(self, plan, event):  # type: ignore[override]
                return Plan(
                    tasks=[
                        Task(
                            id="t1",
                            title="a",
                            description="refined description",
                            assignee_agent_id="coordinator",
                        ),
                        Task(
                            id="t2",
                            title="b",
                            assignee_agent_id="coordinator",
                        ),
                        Task(
                            id="t3",
                            title="c",
                            assignee_agent_id="coordinator",
                        ),
                    ],
                    edges=[],
                )

        client = FakeClient()
        state = _AdkState(client=client, planner=_Planner())  # type: ignore[arg-type]
        _seed_plan(
            state,
            inv_id="inv-1",
            tasks=[
                Task(id="t1", title="a", assignee_agent_id="coordinator"),
                Task(id="t2", title="b", assignee_agent_id="coordinator"),
                Task(id="t3", title="c", assignee_agent_id="coordinator"),
            ],
            edges=[],
            statuses={"t1": "PENDING", "t2": "PENDING", "t3": "PENDING"},
        )
        hsess_id = "hsess-inv-1"
        assert len(state._active_plan_by_session[hsess_id].tasks) == 3

        state.refine_plan_on_drift(
            hsess_id,
            DriftReason(kind="test_refine", detail="updating t1"),
            current_task=None,
        )

        plan_state = state._active_plan_by_session[hsess_id]
        assert len(plan_state.tasks) == 3
        assert set(plan_state.tasks.keys()) == {"t1", "t2", "t3"}
        assert plan_state.tasks["t1"].description == "refined description"
        assert len(plan_state.plan.tasks) == 3


# ---------------------------------------------------------------------------
# Iter13 regression: monotonic state machine + walker termination + routing
# ---------------------------------------------------------------------------


class TestMonotonicStateMachine:
    """Iter13: structural fix for the COMPLETED↔RUNNING cycle bug.

    These are the regression tests the user asked for after 5+ iterations
    of patching symptoms. They prove every code path that writes
    ``task.status`` now goes through the monotonic ``_set_task_status``
    guard, that ``set_forced_task_id`` refuses already-terminal tasks,
    that refine cannot reset a completed task, and that the walker
    physically cannot re-pick a task it already ran.
    """

    @pytest.mark.asyncio
    async def test_task_does_not_cycle_completed_to_running(self, caplog):
        """Drive a plan through the full walker. After the run, no
        COMPLETED→RUNNING transition log should exist, and the task
        should be COMPLETED exactly once.
        """
        import logging
        try:
            from google.genai import types as genai_types  # noqa: F401
        except ImportError:
            pytest.skip("google.genai not installed")
        from harmonograf_client.planner import Task

        agent, inner, state, ctx = _build(
            passes=[
                [FakeEvent("inv-1", "first")],
                [FakeEvent("inv-1", "second")],
                [FakeEvent("inv-1", "third")],
            ],
            inner_name="coordinator",
        )
        _seed_plan(
            state,
            inv_id="inv-1",
            tasks=[
                Task(id="research_scattering", title="research",
                     assignee_agent_id="coordinator"),
            ],
            edges=[],
            statuses={"research_scattering": "PENDING"},
        )

        with caplog.at_level(logging.WARNING, logger="harmonograf_client.adk"):
            got = [ev async for ev in agent._run_async_impl(ctx)]

        # The task should be COMPLETED exactly once at the end.
        plan_state = state._active_plan_by_session["hsess-inv-1"]
        assert plan_state.tasks["research_scattering"].status == "COMPLETED"
        # No REJECTED transition warnings — the walker should never even
        # ask to re-run a completed task in normal operation.
        rejected = [
            r for r in caplog.records
            if "REJECTED" in r.message and "research_scattering" in r.message
        ]
        assert rejected == [], (
            f"Expected no REJECTED transitions but got: "
            f"{[r.message for r in rejected]}"
        )
        # Walker ran the inner agent at most once for the single task.
        assert len(inner.call_log) <= 2  # allow safety pass

    def test_set_forced_task_id_refuses_terminal(self, caplog):
        """If the walker ever asks to bind spans to a COMPLETED task,
        set_forced_task_id must REFUSE — return False, log a WARNING,
        and not change the forced id.
        """
        import logging

        from harmonograf_client.adk import _AdkState
        from harmonograf_client.planner import Task

        state = _AdkState(client=FakeClient())  # type: ignore[arg-type]
        _seed_plan(
            state,
            inv_id="inv-x",
            tasks=[Task(id="t_done", title="done",
                        assignee_agent_id="coordinator")],
            edges=[],
            statuses={"t_done": "COMPLETED"},
        )

        with caplog.at_level(logging.WARNING, logger="harmonograf_client.adk"):
            ok = state.set_forced_task_id("t_done")
        assert ok is False
        assert state.forced_task_id() == ""
        assert any(
            "REFUSING set_forced_task_id" in r.message and "t_done" in r.message
            for r in caplog.records
        )

    def test_stamp_forced_path_refuses_terminal_task(self, caplog):
        """Even if forced_task_id was somehow set to a now-terminal task
        via the ContextVar, ``_stamp_attrs_with_task`` must refuse to
        re-bind a span to it and must NOT transition COMPLETED→RUNNING.
        This is the structural fix for the cycle bug at the root.
        """
        import logging

        from harmonograf_client.adk import _AdkState, _forced_task_id_var
        from harmonograf_client.planner import Task

        state = _AdkState(client=FakeClient())  # type: ignore[arg-type]
        _seed_plan(
            state,
            inv_id="inv-y",
            tasks=[Task(id="t_done", title="done",
                        assignee_agent_id="coordinator")],
            edges=[],
            statuses={"t_done": "COMPLETED"},
        )

        token = _forced_task_id_var.set("t_done")
        try:
            with caplog.at_level(logging.WARNING, logger="harmonograf_client.adk"):
                out = state._stamp_attrs_with_task(
                    {}, "coordinator", "hsess-inv-y", span_kind="LLM_CALL"
                )
        finally:
            _forced_task_id_var.reset(token)
        # Status MUST stay COMPLETED — not flip back to RUNNING.
        plan_state = state._active_plan_by_session["hsess-inv-y"]
        assert plan_state.tasks["t_done"].status == "COMPLETED"
        # No hgraf.task_id stamped on this span (refusal short-circuits).
        assert "hgraf.task_id" not in (out or {})
        assert any("REJECTED stamping forced task" in r.message
                   for r in caplog.records)

    def test_refine_cannot_reset_completed_task(self):
        """Mark a task COMPLETED then refine returns it as PENDING. After
        merge, the client's ground truth (COMPLETED) must win.
        """
        from harmonograf_client.adk import DriftReason, _AdkState
        from harmonograf_client.planner import Plan, PlannerHelper, Task

        class _ResetPlanner(PlannerHelper):
            def generate(self, **kwargs):  # type: ignore[override]
                return Plan(tasks=[], edges=[])

            def refine(self, plan, event):  # type: ignore[override]
                return Plan(
                    tasks=[
                        Task(
                            id="t1", title="a",
                            assignee_agent_id="coordinator",
                            status="PENDING",  # tries to reset
                        ),
                    ],
                    edges=[],
                )

        state = _AdkState(client=FakeClient(), planner=_ResetPlanner())  # type: ignore[arg-type]
        _seed_plan(
            state,
            inv_id="inv-1",
            tasks=[Task(id="t1", title="a", assignee_agent_id="coordinator")],
            edges=[],
            statuses={"t1": "COMPLETED"},
        )
        state.refine_plan_on_drift(
            "hsess-inv-1",
            DriftReason(kind="test", detail="reset attempt"),
            current_task=None,
        )
        plan_state = state._active_plan_by_session["hsess-inv-1"]
        assert plan_state.tasks["t1"].status == "COMPLETED"

    @pytest.mark.asyncio
    async def test_walker_exits_after_all_tasks_done(self):
        """Plan with N tasks all already COMPLETED: _pick_next_batch
        returns [], walker breaks out (does not loop forever).
        """
        from harmonograf_client.planner import Task

        agent, inner, state, ctx = _build(
            passes=[[FakeEvent("inv-1", "fallback")]],
        )
        _seed_plan(
            state,
            inv_id="inv-1",
            tasks=[
                Task(id="t1", title="a", assignee_agent_id="coordinator"),
                Task(id="t2", title="b", assignee_agent_id="coordinator"),
                Task(id="t3", title="c", assignee_agent_id="coordinator"),
            ],
            edges=[],
            statuses={"t1": "COMPLETED", "t2": "COMPLETED", "t3": "COMPLETED"},
        )
        # Should terminate quickly — first iteration falls back to a
        # single delegated pass since no eligible tasks.
        got = [ev async for ev in agent._run_async_impl(ctx)]
        assert len(got) == 1  # single delegated fallback
        assert len(inner.call_log) == 1  # one fallback call, then break

        # Verify _pick_next_batch directly returns [] when nothing eligible.
        plan_state = state._active_plan_by_session["hsess-inv-1"]
        next_batch = agent._pick_next_batch(plan_state, {}, set())
        assert next_batch == []

    def test_pick_next_batch_skips_seen_in_walk(self):
        """Defensive: even if a task is somehow PENDING in plan_state,
        the walker MUST NOT re-pick a task already in seen_in_walk.
        """
        from harmonograf_client.adk import _AdkState
        from harmonograf_client.planner import Task

        agent, _, state, _ = _build()
        _seed_plan(
            state,
            inv_id="inv-1",
            tasks=[Task(id="t1", title="a", assignee_agent_id="coordinator")],
            edges=[],
            statuses={"t1": "PENDING"},
        )
        plan_state = state._active_plan_by_session["hsess-inv-1"]
        # Without seen_in_walk: t1 is eligible.
        first = agent._pick_next_batch(plan_state, {}, set())
        assert [getattr(t, "id", "") for t in first] == ["t1"]
        # With t1 in seen_in_walk: no eligible task even though PENDING.
        second = agent._pick_next_batch(plan_state, {}, {"t1"})
        assert second == []


class TestAssigneeBackfillRouting:
    def test_backfill_prefers_first_non_host_agent(self, caplog):
        """When known_agents starts with the host/coordinator, empty
        assignees must be backfilled to the FIRST non-host agent — not
        the coordinator itself. Iter12 silently routed empty assignees
        to known_agents[0] = coordinator, which made the coordinator
        execute leaf research tasks instead of delegating.
        """
        import logging

        from harmonograf_client.adk import _canonicalize_plan_assignees
        from harmonograf_client.planner import Plan, Task

        plan = Plan(
            tasks=[Task(id="t1", title="a", assignee_agent_id="")],
            edges=[],
        )
        with caplog.at_level(logging.INFO, logger="harmonograf_client.adk"):
            _canonicalize_plan_assignees(
                plan,
                ["coordinator_agent", "research_agent", "web_developer_agent"],
                host_agent_name="coordinator_agent",
            )
        assert plan.tasks[0].assignee_agent_id == "research_agent"

    def test_backfill_default_when_no_host_hint(self):
        """Without host_agent_name, falls back to known_agents[0]
        (preserves iter12 behaviour for tests that don't pass a host).
        """
        from harmonograf_client.adk import _canonicalize_plan_assignees
        from harmonograf_client.planner import Plan, Task

        plan = Plan(
            tasks=[Task(id="t1", title="a", assignee_agent_id="")],
            edges=[],
        )
        _canonicalize_plan_assignees(
            plan, ["research_agent", "web_developer_agent"]
        )
        assert plan.tasks[0].assignee_agent_id == "research_agent"

    def test_backfill_warns_when_only_host_known(self, caplog):
        """If the only known agent IS the host, leave assignee empty
        and warn — better than silently routing to the coordinator.
        """
        import logging

        from harmonograf_client.adk import _canonicalize_plan_assignees
        from harmonograf_client.planner import Plan, Task

        plan = Plan(
            tasks=[Task(id="t1", title="a", assignee_agent_id="")],
            edges=[],
        )
        with caplog.at_level(logging.WARNING, logger="harmonograf_client.adk"):
            _canonicalize_plan_assignees(
                plan,
                ["coordinator_agent"],
                host_agent_name="coordinator_agent",
            )
        # Empty preserved, warning emitted.
        assert plan.tasks[0].assignee_agent_id == ""
        assert any(
            "only known agent is the host" in r.message
            for r in caplog.records
        )
