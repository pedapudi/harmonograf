"""Protocol-level callback + session.state regression tests (iter15 task #10).

These tests exercise the rewritten before/after model + tool callbacks
and the state.state protocol from every angle, without booting a full
ADK runner. They use the same FakeClient / _AdkState pattern as
``test_adk_adapter.py``, ``test_tool_callbacks.py``, and
``test_model_callbacks.py`` so they run in <1s with no network.

The scenarios mirror the ones described in iter15 task #10:

  * before_model writes plan / current task into session.state
  * after_model picks up a "Task complete: tN" text marker
  * after_model picks up a state_delta-style task_outcome write
  * before_tool intercepts a reporting tool and applies the side effect
  * the reporting tool function itself is a no-op ack
  * a tool error fires refine and marks the task FAILED
  * a transfer to an agent not in the plan fires refine

All assertions are written against the spec — if production code
function names drift (e.g. ``_route_after_model_signals`` is renamed),
update the imports here but keep the behavioral assertions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from harmonograf_client import state_protocol as sp
from harmonograf_client.adk import (
    DriftReason,
    PlanState,
    ResponseSignals,
    _AdkState,
    _classify_tool_response,
    _route_after_model_signals,
    _snapshot_harmonograf_state,
    _write_plan_context_to_session_state,
)
from harmonograf_client.planner import Plan, PlannerHelper, Task, TaskEdge
from harmonograf_client.tools import (
    REPORTING_TOOL_NAMES,
    report_new_work_discovered,
    report_plan_divergence,
    report_task_blocked,
    report_task_completed,
    report_task_failed,
    report_task_progress,
    report_task_started,
)


# ---------------------------------------------------------------------------
# Fakes
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

    def submit_plan(self, plan, **kwargs) -> str:
        self._counter += 1
        pid = f"plan-{self._counter}"
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

    def on_control(self, kind: str, cb) -> None:
        self.calls.append(("on_control", kind, cb))


@dataclass
class FakeAgent:
    name: str = "researcher"


@dataclass
class FakeSession:
    id: str = "adk_sess_proto"
    state: dict = field(default_factory=dict)


@dataclass
class FakeIc:
    invocation_id: str = "inv_proto"
    agent: FakeAgent = field(default_factory=FakeAgent)
    session: FakeSession = field(default_factory=FakeSession)


@dataclass
class FakeCc:
    _invocation_context: FakeIc = field(default_factory=FakeIc)
    invocation_id: str = "inv_proto"


class RecordingPlanner(PlannerHelper):
    def __init__(self) -> None:
        self.refine_events: list[dict] = []

    def generate(self, **kwargs: Any) -> Optional[Plan]:
        return None

    def refine(self, plan: Plan, event: Any) -> Optional[Plan]:
        self.refine_events.append(dict(event) if isinstance(event, dict) else {"raw": event})
        return None


HSESSION = "hs_proto"


def _seed_state(
    *,
    planner: Optional[PlannerHelper] = None,
    tasks: Optional[list[Task]] = None,
    edges: Optional[list[TaskEdge]] = None,
) -> tuple[_AdkState, FakeClient, PlanState, FakeIc]:
    """Return a wired _AdkState with a 2-task plan keyed under HSESSION."""
    client = FakeClient()
    state = _AdkState(client=client, planner=planner)  # type: ignore[arg-type]
    if tasks is None:
        tasks = [
            Task(id="t1", title="research", assignee_agent_id="researcher"),
            Task(id="t2", title="write", assignee_agent_id="writer"),
        ]
    if edges is None:
        edges = [TaskEdge(from_task_id="t1", to_task_id="t2")]
    plan = Plan(tasks=list(tasks), edges=list(edges), summary="plan")
    ps = PlanState(
        plan=plan,
        plan_id="plan-proto",
        tasks={t.id: t for t in tasks},
        available_agents=[t.assignee_agent_id for t in tasks if t.assignee_agent_id],
        generating_invocation_id="inv_proto",
        remaining_for_fallback=list(tasks),
        host_agent_name="coordinator",
    )
    with state._lock:
        state._active_plan_by_session[HSESSION] = ps
        state._adk_to_h_session["adk_sess_proto"] = HSESSION
    ic = FakeIc()
    state.on_invocation_start(ic)
    return state, client, ps, ic


# ---------------------------------------------------------------------------
# 1. before_model writes plan + current task into session.state
# ---------------------------------------------------------------------------


class TestBeforeModelWritesPlanContext:
    def test_plan_id_and_current_task_written_to_state(self):
        state, _client, _ps, _ic = _seed_state()
        cc = FakeCc()
        cc._invocation_context.session.state = {}

        _write_plan_context_to_session_state(state, cc)

        st = cc._invocation_context.session.state
        assert st[sp.KEY_PLAN_ID] == "plan-proto"
        assert st[sp.KEY_PLAN_SUMMARY] == "plan"
        # researcher → t1 is the first PENDING assignee match
        assert st[sp.KEY_CURRENT_TASK_ID] == "t1"
        assert st[sp.KEY_CURRENT_TASK_ASSIGNEE] == "researcher"
        # available_tasks must include both planned tasks with deps
        avail = st[sp.KEY_AVAILABLE_TASKS]
        ids = [t["id"] for t in avail]
        assert ids == ["t1", "t2"]
        deps = {t["id"]: t["deps"] for t in avail}
        assert deps == {"t1": [], "t2": ["t1"]}

    def test_snapshot_recorded_for_after_model_diff(self):
        state, _client, _ps, _ic = _seed_state()
        cc = FakeCc()
        cc._invocation_context.session.state = {}

        _write_plan_context_to_session_state(state, cc)

        with state._lock:
            snap = state._state_snapshot_before["inv_proto"]
        assert snap[sp.KEY_PLAN_ID] == "plan-proto"
        assert snap[sp.KEY_CURRENT_TASK_ID] == "t1"


# ---------------------------------------------------------------------------
# 2. after_model detects a "Task complete: tN" text marker
# ---------------------------------------------------------------------------


class TestAfterModelDetectsTextMarker:
    def test_task_complete_marker_transitions_to_completed(self):
        state, _client, ps, _ic = _seed_state()
        cc = FakeCc()
        cc._invocation_context.session.state = {}
        _write_plan_context_to_session_state(state, cc)

        sig = ResponseSignals(
            has_task_complete_marker=True,
            marker_task_id="t1",
            marker_reason="found 5 papers",
            text_parts=["Task complete: t1 - found 5 papers"],
        )
        _route_after_model_signals(state, cc, sig)

        assert ps.tasks["t1"].status == "COMPLETED"

    def test_task_failed_marker_transitions_and_fires_refine(self):
        planner = RecordingPlanner()
        state, _client, ps, _ic = _seed_state(planner=planner)
        cc = FakeCc()
        cc._invocation_context.session.state = {}
        _write_plan_context_to_session_state(state, cc)

        sig = ResponseSignals(
            has_task_failed_marker=True,
            marker_task_id="t1",
            marker_reason="quota exceeded",
            text_parts=["Task failed: t1 - quota exceeded"],
        )
        _route_after_model_signals(state, cc, sig)

        assert ps.tasks["t1"].status == "FAILED"
        assert any(
            e.get("kind") == "task_failed_by_agent" for e in planner.refine_events
        )


# ---------------------------------------------------------------------------
# 3. after_model detects a state_delta task_outcome write
# ---------------------------------------------------------------------------


class TestAfterModelDetectsStateDelta:
    def test_state_delta_completed_transitions_task(self):
        state, _client, ps, _ic = _seed_state()
        cc = FakeCc()
        cc._invocation_context.session.state = {}
        _write_plan_context_to_session_state(state, cc)
        # Agent writes its outcome via state_delta during its turn.
        cc._invocation_context.session.state[sp.KEY_TASK_OUTCOME] = {
            "t1": "completed"
        }
        _route_after_model_signals(state, cc, ResponseSignals())
        assert ps.tasks["t1"].status == "COMPLETED"

    def test_state_delta_failed_fires_refine(self):
        planner = RecordingPlanner()
        state, _client, ps, _ic = _seed_state(planner=planner)
        cc = FakeCc()
        cc._invocation_context.session.state = {}
        _write_plan_context_to_session_state(state, cc)
        cc._invocation_context.session.state[sp.KEY_TASK_OUTCOME] = {
            "t1": "failed"
        }
        _route_after_model_signals(state, cc, ResponseSignals())
        assert ps.tasks["t1"].status == "FAILED"
        kinds = [e.get("kind") for e in planner.refine_events]
        assert "task_failed_by_agent" in kinds

    def test_divergence_flag_fires_refine_with_note(self):
        planner = RecordingPlanner()
        state, _client, _ps, _ic = _seed_state(planner=planner)
        cc = FakeCc()
        cc._invocation_context.session.state = {}
        _write_plan_context_to_session_state(state, cc)
        cc._invocation_context.session.state[sp.KEY_DIVERGENCE_FLAG] = True
        cc._invocation_context.session.state[sp.KEY_AGENT_NOTE] = "scope shifted"
        _route_after_model_signals(state, cc, ResponseSignals())
        kinds = [e.get("kind") for e in planner.refine_events]
        assert "agent_reported_divergence" in kinds


# ---------------------------------------------------------------------------
# 4. before_tool intercepts a reporting tool and applies the side effect
# ---------------------------------------------------------------------------


class TestBeforeToolInterceptsReportingTool:
    def test_report_task_started_transitions_pending_to_running(self):
        state, client, ps, _ic = _seed_state()
        client.calls.clear()

        ack = state._dispatch_reporting_tool(
            "report_task_started", {"task_id": "t1"}, HSESSION
        )
        assert ack == {"acknowledged": True}
        assert ps.tasks["t1"].status == "RUNNING"
        submits = [c for c in client.calls if c[0] == "submit_task_status_update"]
        assert any(c[2]["status"] == "RUNNING" and c[1] == "t1" for c in submits)

    def test_report_task_completed_transitions_running_to_completed(self):
        state, client, ps, _ic = _seed_state()
        ps.tasks["t1"].status = "RUNNING"
        client.calls.clear()

        state._dispatch_reporting_tool(
            "report_task_completed",
            {"task_id": "t1", "summary": "done", "artifacts": {"f": "x"}},
            HSESSION,
        )
        assert ps.tasks["t1"].status == "COMPLETED"
        assert state._task_results.get("t1") == "done"

    def test_report_task_started_runs_before_tool_body(self):
        # The reporting tool function is a stub ack; the side effect is
        # applied in the before_tool callback. Verify that calling the
        # tool function directly does NOT touch _AdkState — only the
        # dispatch path does.
        state, _client, ps, _ic = _seed_state()
        result = report_task_started("t1")
        assert result == {"acknowledged": True}
        # No transition happened just from invoking the tool function.
        assert ps.tasks["t1"].status == "PENDING"
        # Now simulate the before_tool callback.
        state._dispatch_reporting_tool(
            "report_task_started", {"task_id": "t1"}, HSESSION
        )
        assert ps.tasks["t1"].status == "RUNNING"


# ---------------------------------------------------------------------------
# 5. The reporting tool function itself is a no-op ack
# ---------------------------------------------------------------------------


class TestReportingToolNoOp:
    @pytest.mark.parametrize(
        "fn, kwargs",
        [
            (report_task_started, {"task_id": "t1"}),
            (report_task_progress, {"task_id": "t1", "fraction": 0.5}),
            (report_task_completed, {"task_id": "t1", "summary": "ok"}),
            (report_task_failed, {"task_id": "t1", "reason": "boom"}),
            (report_task_blocked, {"task_id": "t1", "blocker": "wait"}),
            (
                report_new_work_discovered,
                {"parent_task_id": "t1", "title": "x", "description": "y"},
            ),
            (report_plan_divergence, {"note": "stale"}),
        ],
    )
    def test_reporting_tools_return_acknowledged(self, fn, kwargs):
        result = fn(**kwargs)
        assert result == {"acknowledged": True}

    def test_all_reporting_tool_names_registered(self):
        # Every reporting helper must show up in REPORTING_TOOL_NAMES so
        # before_tool_callback can intercept it.
        for name in (
            "report_task_started",
            "report_task_progress",
            "report_task_completed",
            "report_task_failed",
            "report_task_blocked",
            "report_new_work_discovered",
            "report_plan_divergence",
        ):
            assert name in REPORTING_TOOL_NAMES


# ---------------------------------------------------------------------------
# 6. Tool error marks the task FAILED + fires refine
# ---------------------------------------------------------------------------


class TestToolErrorMarksTaskFailed:
    def test_classify_tool_response_detects_error_dict(self):
        d = _classify_tool_response("search", {"error": "503"})
        assert d is not None and d.kind == "tool_returned_error"

    def test_refine_plan_on_drift_with_tool_error_kind(self):
        # The on_tool_error_callback path fires refine_plan_on_drift with
        # a "tool_error" DriftReason (see adk.py:1167-1175). We exercise
        # that entry point directly so the test pins the contract.
        planner = RecordingPlanner()
        state, _client, ps, _ic = _seed_state(planner=planner)
        ps.tasks["t1"].status = "RUNNING"

        state.refine_plan_on_drift(
            HSESSION,
            DriftReason(kind="tool_error", detail="search: HTTPError 503"),
        )
        kinds = [e.get("kind") for e in planner.refine_events]
        assert "tool_error" in kinds

    def test_unrecoverable_tool_error_fails_running_task(self):
        # An unrecoverable drift (recoverable=False) cascades the current
        # RUNNING task to FAILED instead of calling the planner.
        planner = RecordingPlanner()
        state, _client, ps, _ic = _seed_state(planner=planner)
        ps.tasks["t1"].status = "RUNNING"

        state.refine_plan_on_drift(
            HSESSION,
            DriftReason(
                kind="tool_error",
                detail="fatal",
                severity="critical",
                recoverable=False,
            ),
            current_task=ps.tasks["t1"],
        )
        assert ps.tasks["t1"].status == "FAILED"


# ---------------------------------------------------------------------------
# 7. Unexpected transfer fires refine
# ---------------------------------------------------------------------------


class TestUnexpectedTransferFiresRefine:
    def test_transfer_to_unplanned_agent_fires_refine(self):
        planner = RecordingPlanner()
        state, _client, _ps, ic = _seed_state(planner=planner)

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

        evt = FakeEvent(actions=FakeActions(transfer_to_agent="ghost_agent"))
        state.on_event(ic, evt)

        kinds = [e.get("kind") for e in planner.refine_events]
        assert "unexpected_transfer" in kinds

    def test_transfer_to_planned_agent_does_not_fire_refine(self):
        planner = RecordingPlanner()
        state, _client, _ps, ic = _seed_state(planner=planner)

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

        evt = FakeEvent(actions=FakeActions(transfer_to_agent="researcher"))
        state.on_event(ic, evt)

        assert all(
            e.get("kind") != "unexpected_transfer" for e in planner.refine_events
        )


# ---------------------------------------------------------------------------
# Snapshot helpers — pin filtering invariant
# ---------------------------------------------------------------------------


class TestSnapshotHarmonografKeys:
    def test_only_harmonograf_prefix_keys_survive(self):
        snap = _snapshot_harmonograf_state(
            {
                "harmonograf.plan_id": "p",
                "harmonograf.current_task_id": "t",
                "user.something": "ignored",
                42: "non-string",
            }
        )
        assert set(snap.keys()) == {
            "harmonograf.plan_id",
            "harmonograf.current_task_id",
        }


# ---------------------------------------------------------------------------
# 8. STEER / CANCEL control handlers fire refine through the drift pipeline
# ---------------------------------------------------------------------------


def _seed_plugin_with_plan(
    *, planner: Optional[PlannerHelper] = None
) -> tuple[Any, FakeClient, "_AdkState", PlanState]:
    """Build a real plugin via make_adk_plugin, seed an active plan, and
    return (plugin, client, state, plan_state). The plugin registers
    STEER / CANCEL handlers on ``client`` via ``on_control`` which the
    test can then retrieve from ``client.calls``.
    """
    pytest.importorskip("google.adk.plugins.base_plugin")
    from harmonograf_client.adk import make_adk_plugin

    client = FakeClient()
    plugin = make_adk_plugin(client, planner=planner)  # type: ignore[arg-type]
    state = plugin._hg_state

    tasks = [
        Task(id="t1", title="research", assignee_agent_id="researcher"),
        Task(id="t2", title="write", assignee_agent_id="writer"),
    ]
    edges = [TaskEdge(from_task_id="t1", to_task_id="t2")]
    plan = Plan(tasks=list(tasks), edges=list(edges), summary="plan")
    ps = PlanState(
        plan=plan,
        plan_id="plan-ctl",
        tasks={t.id: t for t in tasks},
        available_agents=["researcher", "writer"],
        generating_invocation_id="inv_ctl",
        remaining_for_fallback=list(tasks),
        host_agent_name="coordinator",
    )
    with state._lock:
        state._active_plan_by_session[HSESSION] = ps
    return plugin, client, state, ps


def _get_handler(client: FakeClient, kind: str):
    matches = [cb for (op, k, cb) in client.calls if op == "on_control" and k == kind]
    assert matches, f"no {kind} handler registered"
    return matches[-1]


class TestSteerCancelControlHandlers:
    def test_steer_fires_refine_and_emits_revised_plan(self):
        planner = RecordingPlanner()
        _plugin, client, _state, ps = _seed_plugin_with_plan(planner=planner)
        steer = _get_handler(client, "STEER")

        # Clear the pre-seed on_control traffic so we only see what the
        # handler itself produces.
        client.calls = [c for c in client.calls if c[0] != "on_control"]

        evt = SimpleNamespace(payload=b'{"text": "pivot to synthesis", "mode": "append"}')
        ack = steer(evt)

        assert ack.result == "success"
        kinds = [e.get("kind") for e in planner.refine_events]
        assert "user_steer" in kinds

        submits = [c for c in client.calls if c[0] == "submit_plan"]
        assert submits, "expected submit_plan call after STEER drift"
        submitted_plan = submits[-1][2]["plan"]
        assert submitted_plan.revision_reason.startswith("user_steer: ")
        # Recoverable drift → current task stays PENDING/RUNNING (not cascaded).
        assert ps.tasks["t1"].status == "PENDING"
        assert ps.tasks["t2"].status == "PENDING"

    def test_steer_plain_text_payload_still_fires_refine(self):
        planner = RecordingPlanner()
        _plugin, client, _state, _ps = _seed_plugin_with_plan(planner=planner)
        steer = _get_handler(client, "STEER")
        client.calls = [c for c in client.calls if c[0] != "on_control"]

        # Legacy path: raw text, no JSON wrapper. Defaults to mode="cancel".
        steer(SimpleNamespace(payload=b"please reconsider"))

        assert any(
            e.get("kind") == "user_steer" for e in planner.refine_events
        )
        submits = [c for c in client.calls if c[0] == "submit_plan"]
        assert submits and submits[-1][2]["plan"].revision_reason.startswith(
            "user_steer: "
        )

    def test_cancel_fires_unrecoverable_refine_and_cascades(self):
        planner = RecordingPlanner()
        _plugin, client, _state, ps = _seed_plugin_with_plan(planner=planner)
        # Simulate an in-flight task so the cascade has something to fail.
        ps.tasks["t1"].status = "RUNNING"
        cancel = _get_handler(client, "CANCEL")
        client.calls = [c for c in client.calls if c[0] != "on_control"]

        ack = cancel(SimpleNamespace(payload=b""))
        assert ack.result == "success"

        # Unrecoverable → planner.refine is NOT called, but a revised plan
        # MUST still be emitted upstream so the frontend banner fires.
        submits = [c for c in client.calls if c[0] == "submit_plan"]
        assert submits, "expected submit_plan call after CANCEL drift"
        submitted_plan = submits[-1][2]["plan"]
        assert submitted_plan.revision_reason.startswith("user_cancel: ")

        # RUNNING task cascaded to FAILED and downstream PENDING task to CANCELLED.
        assert ps.tasks["t1"].status == "FAILED"
        assert ps.tasks["t2"].status == "CANCELLED"

        # Submit_task_status_update was called for each cascade transition.
        status_updates = [
            c for c in client.calls if c[0] == "submit_task_status_update"
        ]
        transitions = {(c[1], c[2]["status"]) for c in status_updates}
        assert ("t1", "FAILED") in transitions
        assert ("t2", "CANCELLED") in transitions

    def test_cancel_with_no_active_run_still_emits_revision(self):
        planner = RecordingPlanner()
        _plugin, client, _state, ps = _seed_plugin_with_plan(planner=planner)
        cancel = _get_handler(client, "CANCEL")
        client.calls = [c for c in client.calls if c[0] != "on_control"]

        cancel(SimpleNamespace(payload=b""))

        submits = [c for c in client.calls if c[0] == "submit_plan"]
        assert submits and submits[-1][2]["plan"].revision_reason.startswith(
            "user_cancel: "
        )
        # No running task → t1 stays PENDING, but downstream still cascades.
        assert ps.tasks["t1"].status == "PENDING"
