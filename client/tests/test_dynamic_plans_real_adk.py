"""Real-ADK dynamic plan scenarios (iter15 task #10).

These tests use real ``google.adk.agents.LlmAgent`` and
``google.adk.runners.InMemoryRunner`` with scripted ``BaseLlm``
subclasses so the protocol surface (callbacks + state.state +
reporting tools + drift taxonomy) is exercised end-to-end with no
network and no real LLM.

A FakeClient is used in place of a real harmonograf server — these
tests target the client-side protocol contract (plan submit, status
update, drift refine, control routing), not the wire/server. The
existing ``tests/e2e/test_scenarios.py`` covers the wire path with a
real harmonograf server.

Hermetic: no network, no credentials. Capped at ~5s per test.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
from typing import Any, AsyncGenerator, Optional

import pytest

from harmonograf_client.adk import (
    DriftReason,
    _AdkState,
    attach_adk,
)
from harmonograf_client.planner import Plan, PlannerHelper, Task, TaskEdge


_ADK_AVAILABLE = importlib.util.find_spec("google.adk") is not None

pytestmark = pytest.mark.skipif(
    not _ADK_AVAILABLE,
    reason="google.adk not installed — run `make install`",
)


# ---------------------------------------------------------------------------
# FakeClient — same surface as test_adk_adapter.FakeClient but local so this
# file has no in-package import dependencies.
# ---------------------------------------------------------------------------


class FakeClient:
    def __init__(self, name: str = "coordinator") -> None:
        self._name = name
        self.calls: list[tuple] = []
        self._counter = 0
        self._current_activity: str = ""
        self._control_handlers: dict[str, Any] = {}

    @property
    def session_id(self) -> str:
        return ""

    def emit_span_start(self, **kwargs) -> str:
        self._counter += 1
        sid = f"span-{self._counter}"
        self.calls.append(("start", sid, kwargs))
        return sid

    def emit_span_update(self, span_id: str, **kwargs) -> None:
        self.calls.append(("update", span_id, kwargs))

    def emit_span_end(self, span_id: str, **kwargs) -> None:
        self.calls.append(("end", span_id, kwargs))

    def on_control(self, kind: str, cb) -> None:
        self._control_handlers[kind] = cb

    def fire_control(self, kind: str, payload: bytes = b"") -> Any:
        cb = self._control_handlers.get(kind)
        if cb is None:
            return None

        class _Evt:
            pass

        evt = _Evt()
        evt.payload = payload
        return cb(evt)

    def set_current_activity(self, text: str) -> None:
        self._current_activity = text

    def submit_plan(self, plan, **kwargs) -> str:
        self._counter += 1
        pid = kwargs.get("plan_id") or f"plan-{self._counter}"
        self.calls.append(("submit_plan", pid, {"plan": plan, **kwargs}))
        return pid

    def submit_task_status_update(
        self, plan_id: str, task_id: str, status: str, **kwargs
    ) -> None:
        self.calls.append(
            (
                "submit_task_status_update",
                task_id,
                {"plan_id": plan_id, "status": status, **kwargs},
            )
        )

    def shutdown(self, **kwargs) -> None:
        pass

    def status_updates(self) -> list[tuple[str, str]]:
        return [
            (c[1], c[2]["status"])
            for c in self.calls
            if c[0] == "submit_task_status_update"
        ]


# ---------------------------------------------------------------------------
# Scripted FakeLlm — yields a queue of pre-baked LlmResponse objects.
# ---------------------------------------------------------------------------


def _build_fake_llm():
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types as genai_types

    class FakeLlm(BaseLlm):  # type: ignore[misc]
        model: str = "fake-llm"
        responses: list = []
        cursor: int = -1

        @classmethod
        def supported_models(cls) -> list[str]:
            return ["fake-llm"]

        async def generate_content_async(
            self, llm_request, stream: bool = False
        ) -> "AsyncGenerator[LlmResponse, None]":
            self.cursor += 1
            if not self.responses:
                yield LlmResponse(
                    content=genai_types.Content(
                        role="model", parts=[genai_types.Part(text="ok")]
                    )
                )
                return
            idx = min(self.cursor, len(self.responses) - 1)
            item = self.responses[idx]
            # Callable items are side-effect hooks that run mid-invocation
            # (e.g. fire a control envelope while _active_plan_by_session
            # is still populated). Advance the cursor and fall through to
            # the next item for the actual response.
            while callable(item):
                item()
                self.cursor += 1
                idx = min(self.cursor, len(self.responses) - 1)
                item = self.responses[idx]
            yield item

        @contextlib.asynccontextmanager
        async def connect(self, llm_request):
            yield None

    return FakeLlm, LlmResponse, genai_types


def _text(text: str) -> Any:
    _, LlmResponse, gt = _build_fake_llm()
    return LlmResponse(
        content=gt.Content(role="model", parts=[gt.Part(text=text)])
    )


def _fc(name: str, args: dict) -> Any:
    _, LlmResponse, gt = _build_fake_llm()
    return LlmResponse(
        content=gt.Content(
            role="model",
            parts=[gt.Part(function_call=gt.FunctionCall(name=name, args=args))],
        )
    )


def _make_agent(name: str, responses: list, sub_agents=None, tools=None):
    from google.adk.agents.llm_agent import LlmAgent

    FakeLlm, _, _ = _build_fake_llm()
    return LlmAgent(
        name=name,
        model=FakeLlm(responses=list(responses)),
        instruction=f"You are {name}.",
        description=f"{name} agent",
        tools=list(tools or []),
        sub_agents=list(sub_agents or []),
    )


def _make_runner(root_agent: Any, app_name: str) -> Any:
    from google.adk.runners import InMemoryRunner

    return InMemoryRunner(agent=root_agent, app_name=app_name)


# ---------------------------------------------------------------------------
# Planner stubs
# ---------------------------------------------------------------------------


class StaticPlanner(PlannerHelper):
    def __init__(self, plan: Plan, refined: Optional[Plan] = None) -> None:
        self._plan = plan
        self._refined = refined
        self.generate_calls = 0
        self.refine_events: list[Any] = []

    def generate(self, *, request, available_agents, context=None):
        self.generate_calls += 1
        return self._plan

    def refine(self, plan, event):
        self.refine_events.append(dict(event) if isinstance(event, dict) else event)
        return self._refined

    def refine_kinds(self) -> list[str]:
        out: list[str] = []
        for e in self.refine_events:
            if isinstance(e, dict) and "kind" in e:
                out.append(str(e["kind"]))
        return out


# ---------------------------------------------------------------------------
# Drivers / helpers
# ---------------------------------------------------------------------------


async def _drive(runner: Any, user_text: str) -> str:
    from google.genai import types as genai_types

    session = await runner.session_service.create_session(
        app_name=runner.app_name, user_id="proto_user"
    )
    async for _evt in runner.run_async(
        user_id="proto_user",
        session_id=session.id,
        new_message=genai_types.Content(
            role="user", parts=[genai_types.Part(text=user_text)]
        ),
    ):
        pass
    return session.id


def _state_for(handle: Any) -> _AdkState:
    return handle.plugin._hg_state  # type: ignore[no-any-return,attr-defined]


def _final_task_states(handle: Any) -> dict[str, str]:
    state = _state_for(handle)
    out: dict[str, str] = {}
    with state._lock:
        for ps in state._active_plan_by_session.values():
            for tid, t in ps.tasks.items():
                out[tid] = getattr(t, "status", "") or ""
    return out


# ===========================================================================
# Scenario 1 — happy path with reporting tools
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_happy_path_with_reporting():
    """coordinator + sub-agents that call report_task_started / completed.

    Each sub-agent script:
        turn 0: function_call(report_task_started, t<n>)
        turn 1: function_call(report_task_completed, t<n>, summary=...)
        turn 2: text "done"
    """
    plan = Plan(
        summary="research → writer",
        tasks=[
            Task(id="t1", title="Research", assignee_agent_id="researcher"),
            Task(id="t2", title="Write", assignee_agent_id="writer"),
        ],
        edges=[TaskEdge(from_task_id="t1", to_task_id="t2")],
    )
    planner = StaticPlanner(plan)

    researcher = _make_agent(
        "researcher",
        [
            _fc("report_task_started", {"task_id": "t1"}),
            _fc(
                "report_task_completed",
                {"task_id": "t1", "summary": "found 3 sources"},
            ),
            _text("done"),
        ],
    )
    writer = _make_agent(
        "writer",
        [
            _fc("report_task_started", {"task_id": "t2"}),
            _fc(
                "report_task_completed",
                {"task_id": "t2", "summary": "drafted report"},
            ),
            _text("done"),
        ],
    )
    coordinator = _make_agent(
        "coordinator", [_text("ok")], sub_agents=[researcher, writer]
    )
    runner = _make_runner(coordinator, "happy")
    client = FakeClient()
    handle = attach_adk(runner, client, planner=planner)
    try:
        await asyncio.wait_for(_drive(runner, "go"), timeout=5.0)
    finally:
        handle.detach()

    # Even if ADK never auto-routes the coordinator to its sub-agents
    # (the scripted text "ok" is ambiguous), the reporting tools were
    # registered on the sub-agents (we don't drive them here, but the
    # plan + planner.generate must have fired exactly once).
    assert planner.generate_calls == 1
    assert any(c[0] == "submit_plan" for c in client.calls)


# ===========================================================================
# Scenario 2 — research_agent reports new work discovered → refine fires
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_research_agent_reports_new_work():
    plan = Plan(
        summary="research",
        tasks=[Task(id="t1", title="Research", assignee_agent_id="researcher")],
        edges=[],
    )
    refined = Plan(
        summary="research + follow-up",
        tasks=[
            Task(id="t1", title="Research", assignee_agent_id="researcher"),
            Task(id="t2", title="Follow up", assignee_agent_id="researcher"),
        ],
        edges=[TaskEdge(from_task_id="t1", to_task_id="t2")],
    )
    planner = StaticPlanner(plan, refined=refined)

    researcher = _make_agent(
        "researcher",
        [
            _fc(
                "report_new_work_discovered",
                {
                    "parent_task_id": "t1",
                    "title": "summarise findings",
                    "description": "follow-up summary",
                },
            ),
            _text("done"),
        ],
    )
    runner = _make_runner(researcher, "newwork")
    client = FakeClient()
    handle = attach_adk(runner, client, planner=planner)
    try:
        await asyncio.wait_for(_drive(runner, "go"), timeout=5.0)
        kinds = planner.refine_kinds()
        assert "new_work_discovered" in kinds, (
            f"expected new_work_discovered refine, got {kinds}"
        )
    finally:
        handle.detach()


# ===========================================================================
# Scenario 3 — writer reports task failed → upstream_failed cascade
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_writer_reports_failure():
    plan = Plan(
        summary="write → review",
        tasks=[
            Task(id="t1", title="Write", assignee_agent_id="writer"),
            Task(id="t2", title="Review", assignee_agent_id="writer"),
        ],
        edges=[TaskEdge(from_task_id="t1", to_task_id="t2")],
    )
    planner = StaticPlanner(plan)
    client = FakeClient()
    captured: dict[str, str] = {}

    def _snapshot() -> None:
        state = _state_for(handle)
        with state._lock:
            for ps in state._active_plan_by_session.values():
                for tid, t in ps.tasks.items():
                    captured[tid] = getattr(t, "status", "") or ""

    writer = _make_agent(
        "writer",
        [
            _fc("report_task_started", {"task_id": "t1"}),
            _fc(
                "report_task_failed",
                {"task_id": "t1", "reason": "no API key"},
            ),
            _snapshot,
            _text("done"),
        ],
    )
    runner = _make_runner(writer, "failure")
    handle = attach_adk(runner, client, planner=planner)
    try:
        await asyncio.wait_for(_drive(runner, "go"), timeout=5.0)
        assert captured.get("t1") == "FAILED", f"t1 not FAILED: {captured}"
        kinds = planner.refine_kinds()
        # Either task_failed (from reporting tool dispatch) or
        # task_failed_by_agent (from after_model state delta) is fine —
        # the contract is "a failure refine fires".
        assert any(
            k in kinds
            for k in ("task_failed", "task_failed_by_agent", "upstream_failed")
        ), f"expected a failure-related refine, got {kinds}"
    finally:
        handle.detach()


# ===========================================================================
# Scenario 4 — tool error fires refine
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_tool_error_triggers_refine():
    """A FunctionTool that raises during the agent's turn fires
    on_tool_error_callback, which marks the bound task FAILED and
    triggers a tool_error refine.
    """
    from google.adk.tools import FunctionTool

    def broken_search(query: str) -> dict:
        raise RuntimeError("upstream 503")

    plan = Plan(
        summary="search",
        tasks=[Task(id="t1", title="Search", assignee_agent_id="researcher")],
        edges=[],
    )
    planner = StaticPlanner(plan)

    researcher = _make_agent(
        "researcher",
        [
            _fc("broken_search", {"query": "anything"}),
            _text("done"),
        ],
        tools=[FunctionTool(broken_search)],
    )
    runner = _make_runner(researcher, "toolerr")
    client = FakeClient()
    handle = attach_adk(runner, client, planner=planner)
    try:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(_drive(runner, "go"), timeout=5.0)
        kinds = planner.refine_kinds()
        # Either tool_error (from on_tool_error_callback) or
        # tool_returned_error (from after_tool_callback's classifier)
        # is acceptable — both indicate the plugin observed the failure.
        assert any(
            k in kinds for k in ("tool_error", "tool_returned_error")
        ), f"expected tool_error refine, got {kinds}"
    finally:
        handle.detach()


# ===========================================================================
# Scenario 5 — LLM refusal → llm_refused drift
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_llm_refuses():
    plan = Plan(
        summary="research",
        tasks=[Task(id="t1", title="Research", assignee_agent_id="researcher")],
        edges=[],
    )
    planner = StaticPlanner(plan)

    researcher = _make_agent(
        "researcher",
        [_text("I'm unable to help with this request — it's outside my scope.")],
    )
    runner = _make_runner(researcher, "refusal")
    client = FakeClient()
    handle = attach_adk(runner, client, planner=planner)
    try:
        await asyncio.wait_for(_drive(runner, "go"), timeout=5.0)
        kinds = planner.refine_kinds()
        assert "llm_refused" in kinds, f"expected llm_refused refine, got {kinds}"
    finally:
        handle.detach()


# ===========================================================================
# Scenario 6 — agent reports divergence via plan_divergence tool
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_agent_reports_divergence():
    plan = Plan(
        summary="research",
        tasks=[Task(id="t1", title="Research", assignee_agent_id="researcher")],
        edges=[],
    )
    planner = StaticPlanner(plan)

    researcher = _make_agent(
        "researcher",
        [
            _fc(
                "report_plan_divergence",
                {"note": "user changed scope", "suggested_action": "rebuild"},
            ),
            _text("done"),
        ],
    )
    runner = _make_runner(researcher, "divergence")
    client = FakeClient()
    handle = attach_adk(runner, client, planner=planner)
    try:
        await asyncio.wait_for(_drive(runner, "go"), timeout=5.0)
        kinds = planner.refine_kinds()
        # The reporting tool dispatch fires a "plan_divergence" refine
        # (see test_tool_callbacks.TestReportPlanDivergence). The state
        # protocol's divergence_flag path uses "agent_reported_divergence".
        assert any(
            k in kinds for k in ("plan_divergence", "agent_reported_divergence")
        ), f"expected divergence refine, got {kinds}"
    finally:
        handle.detach()


# ===========================================================================
# Scenario 7 — coordinator dispatches to multiple sub-agents in one turn
# (the iter14 bug — must rely on reporting tools, not span lifecycle)
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_coordinator_executes_all_tasks_in_one_turn():
    from google.adk.tools.agent_tool import AgentTool

    plan = Plan(
        summary="three parallel sub-tasks",
        tasks=[
            Task(id="t1", title="A", assignee_agent_id="alpha"),
            Task(id="t2", title="B", assignee_agent_id="beta"),
            Task(id="t3", title="C", assignee_agent_id="gamma"),
        ],
        edges=[],
    )
    planner = StaticPlanner(plan)

    alpha = _make_agent(
        "alpha",
        [
            _fc("report_task_completed", {"task_id": "t1", "summary": "alpha ok"}),
            _text("done"),
        ],
    )
    beta = _make_agent(
        "beta",
        [
            _fc("report_task_completed", {"task_id": "t2", "summary": "beta ok"}),
            _text("done"),
        ],
    )
    gamma = _make_agent(
        "gamma",
        [
            _fc("report_task_completed", {"task_id": "t3", "summary": "gamma ok"}),
            _text("done"),
        ],
    )
    coordinator = _make_agent(
        "coordinator",
        [
            _fc("alpha", {"request": "do A"}),
            _fc("beta", {"request": "do B"}),
            _fc("gamma", {"request": "do C"}),
            _text("all done"),
        ],
    )
    coordinator.tools = [AgentTool(agent=alpha), AgentTool(agent=beta), AgentTool(agent=gamma)]

    runner = _make_runner(coordinator, "multi")
    client = FakeClient()
    handle = attach_adk(runner, client, planner=planner)
    try:
        await asyncio.wait_for(_drive(runner, "do all three"), timeout=5.0)
        states = _final_task_states(handle)
        # Each sub-agent's reporting tool call should have transitioned
        # its task to COMPLETED via the callback protocol — NOT via
        # span lifecycle inference (the iter14 bug).
        for tid in ("t1", "t2", "t3"):
            assert states.get(tid) == "COMPLETED", (
                f"task {tid} not completed via reporting protocol: {states}"
            )
    finally:
        handle.detach()


# ===========================================================================
# Scenario 8 — STEER control event fires user_steer drift
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_user_steer_triggers_refine():
    plan = Plan(
        summary="long task",
        tasks=[Task(id="t1", title="Work", assignee_agent_id="worker")],
        edges=[],
    )
    planner = StaticPlanner(plan)
    client = FakeClient()

    # Side-effect hook runs during generate_content_async (i.e. while
    # the invocation is in flight and _active_plan_by_session is
    # populated) so apply_drift_from_control can fan the STEER drift
    # out to the live session. Control envelopes address the client,
    # not an invocation, so firing after the runner finishes is a
    # no-op by design.
    def _steer_hook() -> None:
        client.fire_control(
            "STEER",
            b'{"text": "switch focus to follow-ups", "mode": "append"}',
        )

    worker = _make_agent("worker", [_steer_hook, _text("done")])
    runner = _make_runner(worker, "steer")
    handle = attach_adk(runner, client, planner=planner)
    try:
        await asyncio.wait_for(_drive(runner, "go"), timeout=5.0)
        kinds = planner.refine_kinds()
        assert "user_steer" in kinds, f"expected user_steer refine, got {kinds}"
    finally:
        handle.detach()


# ===========================================================================
# Scenario 9 — CANCEL control event is unrecoverable; cascades CANCELLED
# ===========================================================================


@pytest.mark.asyncio
async def test_scenario_user_cancel_is_unrecoverable():
    plan = Plan(
        summary="two tasks",
        tasks=[
            Task(id="t1", title="A", assignee_agent_id="worker"),
            Task(id="t2", title="B", assignee_agent_id="worker"),
        ],
        edges=[TaskEdge(from_task_id="t1", to_task_id="t2")],
    )
    planner = StaticPlanner(plan)
    client = FakeClient()
    cancel_snapshot: dict[str, dict[str, str]] = {}

    def _cancel_hook() -> None:
        # Fast-forward t1 to RUNNING so the unrecoverable cascade has
        # something to fail, then fire CANCEL while the invocation is
        # still in flight (otherwise the plan has already been cleared
        # from _active_plan_by_session).
        state = _state_for(handle)
        with state._lock:
            ps = next(iter(state._active_plan_by_session.values()))
            ps.tasks["t1"].status = "RUNNING"
        # CANCEL is critical / unrecoverable; the plugin routes it via
        # apply_drift_from_control → refine_plan_on_drift with
        # recoverable=False, which fails the current task and cascades
        # CANCELLED to downstream PENDING tasks without calling
        # planner.refine.
        client.fire_control("CANCEL", b"")
        # Snapshot task states BEFORE ADK's own invocation end clears
        # the plan from _active_plan_by_session.
        snap: dict[str, str] = {}
        with state._lock:
            for p in state._active_plan_by_session.values():
                for tid, t in p.tasks.items():
                    snap[tid] = getattr(t, "status", "") or ""
        cancel_snapshot.update(snap)

    worker = _make_agent("worker", [_cancel_hook, _text("ok")])
    runner = _make_runner(worker, "cancel")
    handle = attach_adk(runner, client, planner=planner)
    try:
        # Unrecoverable cancel propagates an asyncio.CancelledError up
        # through the runner — that's the whole point of a hard cancel,
        # so swallow it here.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(_drive(runner, "go"), timeout=5.0)
        assert cancel_snapshot.get("t1") == "FAILED", (
            f"t1 should be FAILED after CANCEL: {cancel_snapshot}"
        )
        assert cancel_snapshot.get("t2") == "CANCELLED", (
            f"t2 should be CANCELLED downstream of FAILED t1: {cancel_snapshot}"
        )
    finally:
        handle.detach()
