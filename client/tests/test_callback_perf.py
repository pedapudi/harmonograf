"""Task #16 — Callback overhead profiling + metrics counter validation.

Drives a 10-task plan through ``_AdkState`` directly (no real ADK
runner, no gRPC) and measures per-callback wall-clock overhead using
``time.perf_counter_ns``. The point is *not* to benchmark absolute
speed — fake ADK stand-ins are faster than the real thing — but to
catch regressions where a callback starts doing pathological work
(e.g. O(n²) scans over plan_state on every tick).

Budget per callback: ~5ms p95. We log (not assert) on breach so CI
stays green on slow machines but any real regression is visible.

The test also pins:
  * Metrics counters increment on the expected paths
  * ``submit_task_status_update`` and ``refine_fires`` counts match
    the scripted scenario exactly
  * ``format_protocol_metrics`` returns a non-empty report

Run:  .venv/bin/pytest client/tests/test_callback_perf.py -q
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Optional

from harmonograf_client.adk import (
    DriftReason,
    PlanState,
    _AdkState,
    _write_plan_context_to_session_state,
)
from harmonograf_client.metrics import format_protocol_metrics
from harmonograf_client.planner import Plan, Task, TaskEdge


# ---------------------------------------------------------------------------
# Fakes — identical pattern to test_protocol_callbacks.py
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

    def on_control(self, kind: str, cb) -> None:
        self.calls.append(("on_control", kind, cb))

    def submit_plan(self, plan, **kwargs) -> str:
        self._counter += 1
        pid = f"plan-{self._counter}"
        self.calls.append(("submit_plan", pid, {"plan": plan, **kwargs}))
        return pid

    def submit_task_status_update(
        self, plan_id: str, task_id: str, status: str, **kwargs
    ) -> None:
        self.calls.append(
            ("submit_task_status_update", task_id, {"plan_id": plan_id, "status": status, **kwargs})
        )


@dataclass
class FakeAgent:
    name: str = "researcher"


@dataclass
class FakeSession:
    id: str = "adk_sess_perf"
    state: dict = field(default_factory=dict)


@dataclass
class FakeIc:
    invocation_id: str = "inv_perf"
    agent: FakeAgent = field(default_factory=FakeAgent)
    session: FakeSession = field(default_factory=FakeSession)
    user_id: str = "alice"


@dataclass
class FakeCc:
    _invocation_context: FakeIc = field(default_factory=FakeIc)
    invocation_id: str = "inv_perf"


@dataclass
class FakeTool:
    name: str = "report_task_started"


@dataclass
class FakeToolContext:
    _invocation_context: FakeIc = field(default_factory=FakeIc)
    function_call_id: str = "call-1"


def _mk_part(text: str = "", thought: bool = False, fc: Any = None):
    return SimpleNamespace(text=text, thought=thought, function_call=fc)


def _mk_llm_response(text: str):
    content = SimpleNamespace(parts=[_mk_part(text=text)])
    return SimpleNamespace(content=content, finish_reason="STOP")


def _mk_llm_request():
    return SimpleNamespace(contents=[], model="gemini-test", config=None)


HSESSION = "hs_perf"
NUM_TASKS = 10


def _seed_state() -> tuple[_AdkState, FakeClient, PlanState]:
    client = FakeClient()
    state = _AdkState(client=client, planner=None)  # type: ignore[arg-type]
    tasks = [
        Task(id=f"t{i}", title=f"task {i}", assignee_agent_id="researcher")
        for i in range(1, NUM_TASKS + 1)
    ]
    edges = [
        TaskEdge(from_task_id=f"t{i}", to_task_id=f"t{i+1}")
        for i in range(1, NUM_TASKS)
    ]
    plan = Plan(tasks=list(tasks), edges=list(edges), summary="perf plan")
    ps = PlanState(
        plan=plan,
        plan_id="plan-perf",
        tasks={t.id: t for t in tasks},
        available_agents=["researcher"],
        generating_invocation_id="inv_perf",
        remaining_for_fallback=list(tasks),
        host_agent_name="coordinator",
    )
    with state._lock:
        state._active_plan_by_session[HSESSION] = ps
        state._adk_to_h_session["adk_sess_perf"] = HSESSION
    return state, client, ps


# ---------------------------------------------------------------------------
# Per-callback timer
# ---------------------------------------------------------------------------


@dataclass
class TimingBucket:
    samples_ns: list[int] = field(default_factory=list)

    def record(self, ns: int) -> None:
        self.samples_ns.append(ns)

    @property
    def mean_ms(self) -> float:
        if not self.samples_ns:
            return 0.0
        return statistics.mean(self.samples_ns) / 1e6

    @property
    def p95_ms(self) -> float:
        if not self.samples_ns:
            return 0.0
        # quantiles requires >= 2 samples.
        if len(self.samples_ns) < 2:
            return self.samples_ns[0] / 1e6
        return statistics.quantiles(self.samples_ns, n=20)[18] / 1e6


# ---------------------------------------------------------------------------
# Scenario driver — simulate a full 10-task run on the state machine.
# ---------------------------------------------------------------------------


def _drive_one_task(
    state: _AdkState,
    client: FakeClient,
    task_id: str,
    plan_state: PlanState,
    buckets: dict[str, TimingBucket],
) -> None:
    """Fire the full before_model → after_model → before_tool (reporting
    started) → after_tool → before_tool (reporting completed) → after_tool
    sequence for a single task. Each boundary is measured separately so we
    can attribute overhead to the callback that actually did the work.
    """
    ic = FakeIc(invocation_id=f"inv_{task_id}")
    cc = FakeCc(_invocation_context=ic, invocation_id=f"inv_{task_id}")
    # Match invocation_id so on_invocation_start's supersession guard does
    # not pop the seeded plan. Real ADK would submit a fresh plan each
    # top-level turn; here we re-use the same plan across iterations.
    plan_state.generating_invocation_id = ic.invocation_id

    t0 = time.perf_counter_ns()
    state.on_invocation_start(ic)
    buckets["on_invocation_start"].record(time.perf_counter_ns() - t0)
    # Supersession may still have cleared it on the very first turn
    # (when the seeded plan has no matching generating_invocation_id
    # in _invocations). Re-install so the dispatch path can find it.
    with state._lock:
        state._active_plan_by_session[HSESSION] = plan_state

    t0 = time.perf_counter_ns()
    state.on_model_start(cc, _mk_llm_request())
    buckets["on_model_start"].record(time.perf_counter_ns() - t0)

    # before_model plan-context write pass — part of the plugin hot path.
    t0 = time.perf_counter_ns()
    _write_plan_context_to_session_state(state, cc)
    buckets["before_model_write_plan_ctx"].record(time.perf_counter_ns() - t0)

    resp = _mk_llm_response(f"calling report_task_started for {task_id}")
    t0 = time.perf_counter_ns()
    state.on_model_end(cc, resp)
    buckets["on_model_end"].record(time.perf_counter_ns() - t0)

    # Reporting tool: started. The real plugin intercepts reporting tools
    # in before_tool_callback (dispatch) BEFORE on_tool_start emits the
    # TOOL_CALL span — we mirror that ordering here.
    tool = FakeTool(name="report_task_started")
    tctx = FakeToolContext(_invocation_context=ic, function_call_id=f"call-{task_id}-a")
    t0 = time.perf_counter_ns()
    state._dispatch_reporting_tool("report_task_started", {"task_id": task_id}, HSESSION)
    buckets["dispatch_reporting_tool"].record(time.perf_counter_ns() - t0)

    t0 = time.perf_counter_ns()
    state.on_tool_start(tool, {"task_id": task_id}, tctx)
    buckets["on_tool_start"].record(time.perf_counter_ns() - t0)

    t0 = time.perf_counter_ns()
    state.on_tool_end(tool, tctx, result={"acknowledged": True}, error=None)
    buckets["on_tool_end"].record(time.perf_counter_ns() - t0)

    # Reporting tool: completed.
    tool2 = FakeTool(name="report_task_completed")
    tctx2 = FakeToolContext(_invocation_context=ic, function_call_id=f"call-{task_id}-b")
    state._dispatch_reporting_tool(
        "report_task_completed",
        {"task_id": task_id, "summary": "ok"},
        HSESSION,
    )
    state.on_tool_start(tool2, {"task_id": task_id, "summary": "ok"}, tctx2)
    state.on_tool_end(tool2, tctx2, result={"acknowledged": True}, error=None)

    t0 = time.perf_counter_ns()
    state.on_invocation_end(ic)
    buckets["on_invocation_end"].record(time.perf_counter_ns() - t0)


def _drive_full_plan(
    state: _AdkState, client: FakeClient, plan_state: PlanState
) -> dict[str, TimingBucket]:
    buckets: dict[str, TimingBucket] = {
        "on_invocation_start": TimingBucket(),
        "on_model_start": TimingBucket(),
        "before_model_write_plan_ctx": TimingBucket(),
        "on_model_end": TimingBucket(),
        "on_tool_start": TimingBucket(),
        "dispatch_reporting_tool": TimingBucket(),
        "on_tool_end": TimingBucket(),
        "on_invocation_end": TimingBucket(),
    }
    for i in range(1, NUM_TASKS + 1):
        _drive_one_task(state, client, f"t{i}", plan_state, buckets)
    return buckets


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


CALLBACK_BUDGET_MS = 5.0


class TestCallbackPerf:
    def test_ten_task_run_within_callback_budget(self, capsys):
        # Warm-up once so the first-run imports/JIT don't skew the sample.
        state, client, _ps = _seed_state()
        _drive_full_plan(state, client, _ps)

        # Aggregated across 10 runs so p95 is meaningful (>= 100 samples
        # per callback for the small buckets, 10 * 10 = 100).
        all_buckets: dict[str, TimingBucket] = {}
        wall_clocks_ms: list[float] = []
        for _ in range(10):
            state, client, _ps = _seed_state()
            t0 = time.perf_counter_ns()
            buckets = _drive_full_plan(state, client, _ps)
            wall_clocks_ms.append((time.perf_counter_ns() - t0) / 1e6)
            for key, b in buckets.items():
                all_buckets.setdefault(key, TimingBucket()).samples_ns.extend(b.samples_ns)

        lines: list[str] = [
            "",
            f"10-task plan perf ({len(wall_clocks_ms)} runs):",
            f"  wall_clock mean={statistics.mean(wall_clocks_ms):.3f}ms "
            f"p95={statistics.quantiles(wall_clocks_ms, n=20)[18]:.3f}ms",
            "  per-callback (samples × runs):",
        ]
        over_budget: list[str] = []
        for key in sorted(all_buckets.keys()):
            b = all_buckets[key]
            lines.append(
                f"    {key:32s} n={len(b.samples_ns):3d} "
                f"mean={b.mean_ms:.4f}ms p95={b.p95_ms:.4f}ms"
            )
            if b.p95_ms > CALLBACK_BUDGET_MS:
                over_budget.append(f"{key} p95={b.p95_ms:.3f}ms")
        if over_budget:
            lines.append(f"  OVER BUDGET (>{CALLBACK_BUDGET_MS}ms p95): {over_budget}")
        # capsys only surfaces on failure or with -s. Print anyway so that
        # running with -s produces a useful report.
        print("\n".join(lines))

        # Sanity bar: p95 must be well below any conceivable real-world
        # budget. Fake ADK stand-ins should easily come in under 5ms.
        for key, b in all_buckets.items():
            assert b.p95_ms < CALLBACK_BUDGET_MS, (
                f"{key} p95={b.p95_ms:.3f}ms exceeds {CALLBACK_BUDGET_MS}ms budget"
            )

    def test_metrics_counters_match_scripted_scenario(self):
        state, client, ps = _seed_state()
        _drive_full_plan(state, client, ps)

        m = state.get_protocol_metrics()
        # Every task fires the full callback sequence exactly once.
        assert m.callbacks_fired["on_invocation_start"] == NUM_TASKS
        assert m.callbacks_fired["on_invocation_end"] == NUM_TASKS
        assert m.callbacks_fired["on_model_start"] == NUM_TASKS
        assert m.callbacks_fired["on_model_end"] == NUM_TASKS
        # Each task fires two reporting tools (started + completed).
        assert m.callbacks_fired["on_tool_start"] == NUM_TASKS * 2
        assert m.callbacks_fired["on_tool_end"] == NUM_TASKS * 2
        assert m.reporting_tools_invoked["report_task_started"] == NUM_TASKS
        assert m.reporting_tools_invoked["report_task_completed"] == NUM_TASKS

        # Each task transitions PENDING → RUNNING → COMPLETED = 2 transitions.
        # The scripted driver runs every task to completion, so:
        assert m.task_transitions["RUNNING"] == NUM_TASKS
        assert m.task_transitions["COMPLETED"] == NUM_TASKS

        # Happy path — no drift observed.
        assert sum(m.refine_fires.values()) == 0

        # All tasks land in COMPLETED.
        assert all(
            ps.tasks[f"t{i}"].status == "COMPLETED"
            for i in range(1, NUM_TASKS + 1)
        )

    def test_metrics_counters_count_refine_on_drift(self):
        state, client, ps = _seed_state()
        # Simulate three drift events over the course of a run.
        state.refine_plan_on_drift(HSESSION, DriftReason(kind="tool_error", detail="a"))
        state.refine_plan_on_drift(
            HSESSION, DriftReason(kind="agent_reported_divergence", detail="b")
        )
        state.refine_plan_on_drift(HSESSION, DriftReason(kind="tool_error", detail="c"))
        m = state.get_protocol_metrics()
        # tool_error throttle collapses the second "tool_error" fire into
        # the first; we still COUNT the attempt (counter increments at
        # function entry, not after the throttle guard).
        assert m.refine_fires["tool_error"] == 2
        assert m.refine_fires["agent_reported_divergence"] == 1

    def test_format_protocol_metrics_non_empty(self):
        state, client, ps = _seed_state()
        _drive_full_plan(state, client, ps)
        rendered = format_protocol_metrics(state.get_protocol_metrics())
        assert "callbacks_fired" in rendered
        assert "task_transitions" in rendered
        assert "on_invocation_start" in rendered

    def test_wire_call_counts(self):
        """Pin the number of ``submit_task_status_update`` calls so any
        regression in the reporting-tool path (e.g. double-emit) trips
        here.

        The assignee-fallback stamp path inside ``on_model_start``
        transitions each task PENDING → RUNNING silently (no wire
        call), so ``report_task_started`` sees the task already
        RUNNING and does not re-emit. The only wire call per task is
        therefore the COMPLETED emit from ``_handle_report_task_completed``.
        """
        state, client, _ps = _seed_state()
        _drive_full_plan(state, client, _ps)
        submits = [c for c in client.calls if c[0] == "submit_task_status_update"]
        statuses = [c[2]["status"] for c in submits]
        assert statuses.count("COMPLETED") == NUM_TASKS
        assert len(submits) == NUM_TASKS
