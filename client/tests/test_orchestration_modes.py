"""Orchestration mode test matrix (task #10 follow-up).

Drives the same 3-task plan through a real ``InMemoryRunner`` against a
``HarmonografAgent`` wired in each of the three orchestration modes:

* **sequential** — ``orchestrator_mode=True``, ``parallel_mode=False``
* **parallel**   — ``orchestrator_mode=True``, ``parallel_mode=True``
* **delegated**  — ``orchestrator_mode=False``

For every mode the test asserts that all 3 tasks reach ``COMPLETED``,
that the ``report_task_*`` reporting tools were intercepted by the
plugin's ``before_tool_callback`` seam (spy installed on
``_AdkState._dispatch_reporting_tool``), that every task had a span
bound to it during the run (``state._span_to_task`` captured snapshot),
plus mode-specific expectations (walker honouring edges in parallel
mode; observer scanning events after delegated runs).

No network, no real LLM. FakeClient + scripted FakeLlm.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
from typing import Any, AsyncGenerator, Optional

import pytest

from harmonograf_client.adk import _AdkState, attach_adk
from harmonograf_client.agent import HarmonografAgent
from harmonograf_client.planner import Plan, PlannerHelper, Task, TaskEdge


_ADK_AVAILABLE = importlib.util.find_spec("google.adk") is not None

pytestmark = pytest.mark.skipif(
    not _ADK_AVAILABLE,
    reason="google.adk not installed — run `make install`",
)


# ---------------------------------------------------------------------------
# FakeClient — minimal duck type of harmonograf_client.Client for the plugin.
# ---------------------------------------------------------------------------


class FakeClient:
    def __init__(self, name: str = "coordinator") -> None:
        self._name = name
        self.calls: list[tuple] = []
        self._counter = 0
        self._current_activity = ""
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


# ---------------------------------------------------------------------------
# Scripted FakeLlm — queue of LlmResponse (or callable side-effect hooks).
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


def _make_llm_agent(name: str, responses: list, sub_agents=None, tools=None):
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
# Planner stub — static plan, records refine events.
# ---------------------------------------------------------------------------


class StaticPlanner(PlannerHelper):
    def __init__(self, plan: Plan) -> None:
        self._plan = plan
        self.generate_calls = 0
        self.refine_events: list[Any] = []

    def generate(self, *, request, available_agents, context=None):
        self.generate_calls += 1
        return self._plan

    def refine(self, plan, event):
        self.refine_events.append(dict(event) if isinstance(event, dict) else event)
        return None


# ---------------------------------------------------------------------------
# Drivers + helpers
# ---------------------------------------------------------------------------


async def _drive(runner: Any, user_text: str) -> str:
    from google.genai import types as genai_types

    session = await runner.session_service.create_session(
        app_name=runner.app_name, user_id="orch_user"
    )
    async for _evt in runner.run_async(
        user_id="orch_user",
        session_id=session.id,
        new_message=genai_types.Content(
            role="user", parts=[genai_types.Part(text=user_text)]
        ),
    ):
        pass
    return session.id


def _state_for(handle: Any) -> _AdkState:
    return handle.plugin._hg_state  # type: ignore[no-any-return,attr-defined]


def _install_reporting_spy(state: _AdkState) -> list[tuple[str, dict]]:
    """Wrap ``state._dispatch_reporting_tool`` with a spy that records
    ``(tool_name, args)`` and delegates to the original implementation.
    Returns the live list — mutated by side effect as calls land.
    """
    recorded: list[tuple[str, dict]] = []
    original = state._dispatch_reporting_tool

    def _spy(name: str, args: Any, hsession_id: str) -> Any:
        recorded.append((name, dict(args or {})))
        return original(name, args, hsession_id)

    state._dispatch_reporting_tool = _spy  # type: ignore[assignment]
    return recorded


def _install_span_binding_spy(state: _AdkState) -> dict[str, str]:
    """Snapshot every ``span_id → task_id`` binding the plugin makes
    during a run. ``_span_to_task`` is cleared at invocation end, so
    we capture writes live via a wrapper on ``_bind_span_to_task``.
    """
    seen: dict[str, str] = {}
    original = state._bind_span_to_task

    def _spy(span_id: str, attrs: Any) -> Any:
        result = original(span_id, attrs)
        mapped = state._span_to_task.get(span_id, "")
        if mapped:
            seen[span_id] = mapped
        return result

    state._bind_span_to_task = _spy  # type: ignore[assignment]
    return seen


def _final_task_states(state: _AdkState) -> dict[str, str]:
    """Merge live PlanState task statuses with per-invocation snapshots
    retained after the generating invocation ends.
    """
    out: dict[str, str] = {}
    with state._lock:
        for ps in state._active_plan_by_session.values():
            for tid, t in ps.tasks.items():
                out[tid] = getattr(t, "status", "") or ""
        for _inv, snap in state._plan_snapshot_for_inv.items():
            _plan, tracked = snap
            for tid, t in tracked.items():
                cur = out.get(tid)
                new = getattr(t, "status", "") or ""
                # Prefer COMPLETED/FAILED terminal states over PENDING/RUNNING.
                if cur in (None, "", "PENDING", "RUNNING") or (
                    cur == "RUNNING" and new in ("COMPLETED", "FAILED")
                ):
                    out[tid] = new
    return out


# ---------------------------------------------------------------------------
# Fixture builder: plan + 3-task coordinator with sub-agents + HarmonografAgent
# ---------------------------------------------------------------------------


def _three_task_plan() -> Plan:
    return Plan(
        summary="research → write → review",
        tasks=[
            Task(id="t1", title="Research", assignee_agent_id="coordinator"),
            Task(id="t2", title="Write", assignee_agent_id="coordinator"),
            Task(id="t3", title="Review", assignee_agent_id="coordinator"),
        ],
        edges=[
            TaskEdge(from_task_id="t1", to_task_id="t2"),
            TaskEdge(from_task_id="t2", to_task_id="t3"),
        ],
    )


def _reporting_script(*, per_task_text_terminator: bool) -> list[Any]:
    """Script a coordinator LLM that executes all three tasks.

    In ADK, an LLM turn ends as soon as the model yields a response
    with no function_call parts. Callers choose:

    * ``per_task_text_terminator=True`` — insert a text response after
      each task's report_task_completed. Use this for modes where the
      walker invokes ``inner_agent.run_async`` once per task (parallel
      mode) and the LLM cursor advances across calls.
    * ``per_task_text_terminator=False`` — emit only function_calls for
      all three tasks and finish with a single text flush at the end.
      Use this when the whole plan runs inside ONE inner invocation
      (sequential / delegated), otherwise the first per-task text
      ends the turn and later tasks never execute.
    """
    script: list[Any] = []
    for tid, title in [("t1", "Research"), ("t2", "Write"), ("t3", "Review")]:
        script.append(_fc("report_task_started", {"task_id": tid}))
        script.append(
            _fc(
                "report_task_completed",
                {"task_id": tid, "summary": f"{title} done"},
            )
        )
        if per_task_text_terminator:
            script.append(_text(f"Task complete: {tid}"))
    script.append(_text("all done"))
    return script


def _build_root(
    *,
    orchestrator_mode: bool,
    parallel_mode: bool,
    script: Optional[list[Any]] = None,
) -> tuple[HarmonografAgent, StaticPlanner]:
    """Wrap a coordinator (with two decorative sub-agents for
    auto-registration coverage) inside a ``HarmonografAgent`` in the
    requested mode. Returns the root and the planner stub.
    """
    if script is None:
        # Parallel mode re-enters the inner agent once per task (each
        # call ends on a text response); sequential/delegated modes run
        # the whole plan inside a single invocation so the first text
        # would end the turn before later tasks execute.
        script = _reporting_script(per_task_text_terminator=parallel_mode)
    responses = script

    helper_a = _make_llm_agent("helper_a", [_text("ack a")])
    helper_b = _make_llm_agent("helper_b", [_text("ack b")])
    coordinator = _make_llm_agent(
        "coordinator", responses, sub_agents=[helper_a, helper_b]
    )
    root = HarmonografAgent(
        name="hg_root",
        description="harmonograf orchestrator",
        inner_agent=coordinator,
        orchestrator_mode=orchestrator_mode,
        parallel_mode=parallel_mode,
        refine_on_events=True,
    )
    return root, StaticPlanner(_three_task_plan())


# ===========================================================================
# Parametrized: all three modes complete the plan through the reporting seam
# ===========================================================================


MODES = [
    pytest.param(True, False, id="sequential"),
    pytest.param(True, True, id="parallel"),
    pytest.param(False, False, id="delegated"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("orchestrator_mode,parallel_mode", MODES)
async def test_plan_completes_via_reporting_tools(
    orchestrator_mode: bool, parallel_mode: bool
):
    root, planner = _build_root(
        orchestrator_mode=orchestrator_mode, parallel_mode=parallel_mode
    )
    runner = _make_runner(
        root,
        f"orch_{int(orchestrator_mode)}_{int(parallel_mode)}",
    )
    client = FakeClient()
    handle = attach_adk(runner, client, planner=planner)
    state = _state_for(handle)
    reporting_calls = _install_reporting_spy(state)
    try:
        await asyncio.wait_for(_drive(runner, "do it"), timeout=8.0)
    finally:
        handle.detach()

    assert planner.generate_calls >= 1, "planner.generate should fire once"
    states = _final_task_states(state)

    # Assertion 1: every task reached COMPLETED.
    for tid in ("t1", "t2", "t3"):
        assert states.get(tid) == "COMPLETED", (
            f"task {tid} should be COMPLETED in mode "
            f"orchestrator={orchestrator_mode} parallel={parallel_mode}; "
            f"final states={states}"
        )

    # Assertion 2: the reporting-tool dispatch seam saw started+completed
    # for each task id.
    started_ids = {
        args.get("task_id") for name, args in reporting_calls
        if name == "report_task_started"
    }
    completed_ids = {
        args.get("task_id") for name, args in reporting_calls
        if name == "report_task_completed"
    }
    assert {"t1", "t2", "t3"}.issubset(started_ids), (
        f"reporting spy missed start for some tasks; recorded: {reporting_calls}"
    )
    assert {"t1", "t2", "t3"}.issubset(completed_ids), (
        f"reporting spy missed completion for some tasks; "
        f"recorded: {reporting_calls}"
    )


# ===========================================================================
# Parametrized: every task gets a span bound during the run (all modes)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("orchestrator_mode,parallel_mode", MODES)
async def test_tasks_bound_to_spans_during_run(
    orchestrator_mode: bool, parallel_mode: bool
):
    root, planner = _build_root(
        orchestrator_mode=orchestrator_mode, parallel_mode=parallel_mode
    )
    runner = _make_runner(
        root,
        f"span_{int(orchestrator_mode)}_{int(parallel_mode)}",
    )
    client = FakeClient()
    handle = attach_adk(runner, client, planner=planner)
    state = _state_for(handle)
    span_bindings = _install_span_binding_spy(state)
    try:
        await asyncio.wait_for(_drive(runner, "go"), timeout=8.0)
    finally:
        handle.detach()

    bound_task_ids = set(span_bindings.values())
    for tid in ("t1", "t2", "t3"):
        assert tid in bound_task_ids, (
            f"task {tid} never had a span bound to it in mode "
            f"orch={orchestrator_mode} par={parallel_mode}; "
            f"bindings captured: {span_bindings}"
        )


# ===========================================================================
# Parallel-mode: walker honours task edges (t2 does not start before t1 ends)
# ===========================================================================


@pytest.mark.asyncio
async def test_parallel_mode_respects_task_edges():
    """In parallel mode with a linear chain, the rigid walker must pick
    one eligible task per batch. Record task status transitions as the
    reporting tools land and assert that ``t2`` never enters ``RUNNING``
    before ``t1`` has reached ``COMPLETED`` (and same for ``t3``/``t2``).
    """
    root, planner = _build_root(
        orchestrator_mode=True, parallel_mode=True
    )
    runner = _make_runner(root, "parallel_edges")
    client = FakeClient()
    handle = attach_adk(runner, client, planner=planner)
    state = _state_for(handle)

    transition_log: list[tuple[str, dict[str, str]]] = []
    original = state._dispatch_reporting_tool

    def _tap(name: str, args: Any, hsession_id: str) -> Any:
        result = original(name, args, hsession_id)
        snap: dict[str, str] = {}
        with state._lock:
            for ps in state._active_plan_by_session.values():
                for tid, t in ps.tasks.items():
                    snap[tid] = getattr(t, "status", "") or ""
        transition_log.append((f"{name}:{(args or {}).get('task_id')}", snap))
        return result

    state._dispatch_reporting_tool = _tap  # type: ignore[assignment]

    try:
        await asyncio.wait_for(_drive(runner, "go"), timeout=8.0)
    finally:
        handle.detach()

    assert transition_log, "expected at least one reporting-tool transition"

    # Find the first snapshot where each task entered RUNNING.
    first_running_idx: dict[str, int] = {}
    first_completed_idx: dict[str, int] = {}
    for idx, (_label, snap) in enumerate(transition_log):
        for tid in ("t1", "t2", "t3"):
            if snap.get(tid) == "RUNNING" and tid not in first_running_idx:
                first_running_idx[tid] = idx
            if snap.get(tid) == "COMPLETED" and tid not in first_completed_idx:
                first_completed_idx[tid] = idx

    for tid in ("t1", "t2", "t3"):
        assert tid in first_running_idx, (
            f"task {tid} never observed RUNNING in log: {transition_log}"
        )
        assert tid in first_completed_idx, (
            f"task {tid} never observed COMPLETED in log: {transition_log}"
        )

    # Edge invariant: t2 cannot start before t1 completes; t3 before t2.
    assert first_completed_idx["t1"] <= first_running_idx["t2"], (
        f"t2 started at {first_running_idx['t2']} before t1 completed at "
        f"{first_completed_idx['t1']}"
    )
    assert first_completed_idx["t2"] <= first_running_idx["t3"], (
        f"t3 started at {first_running_idx['t3']} before t2 completed at "
        f"{first_completed_idx['t2']}"
    )


# ===========================================================================
# Delegated mode: event observer scans events after the inner run finishes
# ===========================================================================


@pytest.mark.asyncio
async def test_delegated_mode_runs_event_observer():
    """In delegated mode (``orchestrator_mode=False``) ``HarmonografAgent``
    runs ``_run_delegated`` which delegates a single inner_agent pass
    and relies on the post-run event observer / classifier sweep for
    liveness. Verify both that the inner agent's task sequencing
    produced COMPLETED state and that ``on_event`` fired at least once
    (proves the observer path is wired under this mode).
    """
    root, planner = _build_root(
        orchestrator_mode=False, parallel_mode=False
    )
    runner = _make_runner(root, "delegated_observer")
    client = FakeClient()
    handle = attach_adk(runner, client, planner=planner)
    state = _state_for(handle)

    on_event_calls = {"count": 0}
    original_on_event = state.on_event

    def _on_event_spy(ic: Any, event: Any) -> Any:
        on_event_calls["count"] += 1
        return original_on_event(ic, event)

    state.on_event = _on_event_spy  # type: ignore[assignment]

    try:
        await asyncio.wait_for(_drive(runner, "go"), timeout=8.0)
    finally:
        handle.detach()

    assert on_event_calls["count"] > 0, (
        "delegated mode should still route ADK events through on_event — "
        "the observer never fired"
    )
    states = _final_task_states(state)
    for tid in ("t1", "t2", "t3"):
        assert states.get(tid) == "COMPLETED", (
            f"delegated mode: task {tid} should be COMPLETED; states={states}"
        )


# ===========================================================================
# Sequential mode: inner agent runs exactly once (no per-task re-invocation)
# ===========================================================================


@pytest.mark.asyncio
async def test_sequential_mode_runs_inner_agent_once_per_plan():
    """The simplified sequential walker invokes ``inner_agent.run_async``
    exactly once for the whole plan — the per-task reporting-tool
    callbacks drive state transitions without re-entering the agent.
    Counts invocation-start callback fires on the inner coordinator
    agent via ``_AdkState`` metrics.
    """
    root, planner = _build_root(
        orchestrator_mode=True, parallel_mode=False
    )
    runner = _make_runner(root, "sequential_once")
    client = FakeClient()
    handle = attach_adk(runner, client, planner=planner)
    state = _state_for(handle)

    inner_invocations: list[str] = []
    original = state.on_invocation_start

    def _spy(ic: Any) -> Any:
        agent = getattr(ic, "agent", None)
        agent_name = str(getattr(agent, "name", "") or "")
        if agent_name == "coordinator":
            inner_invocations.append(
                str(getattr(ic, "invocation_id", "") or "")
            )
        return original(ic)

    state.on_invocation_start = _spy  # type: ignore[assignment]

    try:
        await asyncio.wait_for(_drive(runner, "go"), timeout=8.0)
    finally:
        handle.detach()

    # The inner coordinator should receive exactly ONE invocation in
    # sequential mode (the whole plan is delivered as one turn).
    assert len(inner_invocations) == 1, (
        f"sequential mode should invoke coordinator exactly once; "
        f"got {len(inner_invocations)}: {inner_invocations}"
    )
