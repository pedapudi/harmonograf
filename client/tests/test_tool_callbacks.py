"""Unit tests for the rewritten tool callbacks (task #4).

These tests exercise the pure surface of the rewritten tool-callback
path without touching ADK internals:

* ``_classify_tool_response`` — failure / unexpected-result detection
* ``_AdkState._dispatch_reporting_tool`` — each reporting-tool handler
  applies the right side effect (status transition, drift refine,
  progress / blocker / result storage)

Together with ``test_walker_completion_timing.py`` and the legacy
``test_adk_adapter.py`` tool tests (which still cover the TOOL_CALL
span lifecycle through ``on_tool_start`` / ``on_tool_end``), this
pins the contract the plugin-layer callbacks rely on.
"""

from __future__ import annotations

from typing import Any

import pytest

from harmonograf_client.adk import (
    DriftReason,
    _AdkState,
    _classify_tool_response,
)
from harmonograf_client.planner import Plan, PlannerHelper, Task, TaskEdge

from .test_adk_adapter import (  # type: ignore[import-not-found]
    FakeClient,
    _StubPlannerIC,
)


HSESSION = "adk_sess_plan"


def _make_state_with_plan(
    tasks: list[Task],
    edges: list[TaskEdge] | None = None,
    *,
    refine_calls: list[Plan] | None = None,
) -> _AdkState:
    """Return an ``_AdkState`` with a plan already submitted under
    :data:`HSESSION` so reporting-tool handlers can resolve task ids.
    """

    class StubPlanner(PlannerHelper):
        def generate(self, **kwargs: Any) -> Plan:
            return Plan(
                tasks=list(tasks),
                edges=list(edges or []),
                summary="stub",
            )

        def refine(self, plan: Plan, drift_context: dict) -> Plan | None:
            if refine_calls is not None:
                refine_calls.append((plan, drift_context))  # type: ignore[arg-type]
            return None

    state = _AdkState(  # type: ignore[arg-type]
        client=FakeClient(), planner=StubPlanner(), refine_on_events=False
    )
    ic = _StubPlannerIC("go")
    state.on_invocation_start(ic)
    state.maybe_run_planner(ic)
    # Sanity: the planner submitted under HSESSION.
    assert HSESSION in state._active_plan_by_session, list(
        state._active_plan_by_session.keys()
    )
    return state


def _task_status(state: _AdkState, tid: str) -> str:
    ps = state._active_plan_by_session[HSESSION]
    return getattr(ps.tasks[tid], "status", "") or ""


# ---------------------------------------------------------------------------
# _classify_tool_response — pure helper, no state needed.
# ---------------------------------------------------------------------------


class TestClassifyToolResponse:
    def test_none_is_error(self):
        d = _classify_tool_response("search", None)
        assert d is not None and d.kind == "tool_returned_error"
        assert "returned None" in d.detail

    def test_dict_with_error_key(self):
        d = _classify_tool_response("search", {"error": "boom"})
        assert d is not None and d.kind == "tool_returned_error"
        assert "boom" in d.detail

    def test_dict_with_status_failed(self):
        d = _classify_tool_response("search", {"status": "failed"})
        assert d is not None and d.kind == "tool_returned_error"

    def test_dict_with_ok_false(self):
        d = _classify_tool_response("search", {"ok": False})
        assert d is not None and d.kind == "tool_returned_error"

    def test_empty_dict_is_unexpected(self):
        d = _classify_tool_response("search", {})
        assert d is not None and d.kind == "tool_unexpected_result"

    def test_empty_list_is_unexpected(self):
        d = _classify_tool_response("search", [])
        assert d is not None and d.kind == "tool_unexpected_result"

    def test_empty_string_is_unexpected(self):
        d = _classify_tool_response("search", "   ")
        assert d is not None and d.kind == "tool_unexpected_result"

    def test_healthy_dict_is_none(self):
        assert _classify_tool_response("search", {"results": [1, 2]}) is None

    def test_healthy_list_is_none(self):
        assert _classify_tool_response("search", [1]) is None

    def test_healthy_string_is_none(self):
        assert _classify_tool_response("search", "hello") is None

    def test_dict_status_ok_is_none(self):
        assert (
            _classify_tool_response("search", {"status": "ok", "data": 1})
            is None
        )


# ---------------------------------------------------------------------------
# Reporting-tool handlers — exercised via _dispatch_reporting_tool, the
# same entry point the rewritten before_tool_callback uses.
# ---------------------------------------------------------------------------


class TestReportTaskStarted:
    def test_pending_to_running_emits_status(self):
        state = _make_state_with_plan(
            [Task(id="t1", title="research", assignee_agent_id="researcher")]
        )
        client: FakeClient = state._client  # type: ignore[assignment]
        client.calls.clear()

        ack = state._dispatch_reporting_tool(
            "report_task_started",
            {"task_id": "t1", "detail": "starting now"},
            HSESSION,
        )
        assert ack == {"acknowledged": True}
        assert _task_status(state, "t1") == "RUNNING"
        submits = [
            c for c in client.calls if c[0] == "submit_task_status_update"
        ]
        assert len(submits) == 1
        assert submits[0][1] == "t1"
        assert submits[0][2]["status"] == "RUNNING"

    def test_terminal_task_is_left_alone(self):
        state = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="researcher")]
        )
        ps = state._active_plan_by_session[HSESSION]
        ps.tasks["t1"].status = "COMPLETED"
        client: FakeClient = state._client  # type: ignore[assignment]
        client.calls.clear()

        state._dispatch_reporting_tool(
            "report_task_started", {"task_id": "t1"}, HSESSION
        )
        assert _task_status(state, "t1") == "COMPLETED"
        submits = [
            c for c in client.calls if c[0] == "submit_task_status_update"
        ]
        assert submits == []

    def test_unknown_task_is_noop(self):
        state = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="researcher")]
        )
        client: FakeClient = state._client  # type: ignore[assignment]
        client.calls.clear()
        state._dispatch_reporting_tool(
            "report_task_started", {"task_id": "ghost"}, HSESSION
        )
        assert client.calls == []


class TestReportTaskProgress:
    def test_progress_recorded(self):
        state = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="researcher")]
        )
        state._dispatch_reporting_tool(
            "report_task_progress",
            {"task_id": "t1", "fraction": 0.4, "detail": "halfway"},
            HSESSION,
        )
        assert state._task_progress["t1"] == pytest.approx(0.4)

    def test_progress_clamped(self):
        state = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="researcher")]
        )
        state._dispatch_reporting_tool(
            "report_task_progress", {"task_id": "t1", "fraction": 5.0}, HSESSION
        )
        assert state._task_progress["t1"] == 1.0
        state._dispatch_reporting_tool(
            "report_task_progress",
            {"task_id": "t1", "fraction": -0.5},
            HSESSION,
        )
        assert state._task_progress["t1"] == 0.0

    def test_progress_bad_value_falls_back_to_zero(self):
        state = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="researcher")]
        )
        state._dispatch_reporting_tool(
            "report_task_progress",
            {"task_id": "t1", "fraction": "not-a-number"},
            HSESSION,
        )
        assert state._task_progress["t1"] == 0.0


class TestReportTaskCompleted:
    def test_running_to_completed_records_summary(self):
        state = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="researcher")]
        )
        ps = state._active_plan_by_session[HSESSION]
        ps.tasks["t1"].status = "RUNNING"
        client: FakeClient = state._client  # type: ignore[assignment]
        client.calls.clear()

        state._dispatch_reporting_tool(
            "report_task_completed",
            {
                "task_id": "t1",
                "summary": "found 5 papers",
                "artifacts": {"file": "out.md"},
            },
            HSESSION,
        )
        assert _task_status(state, "t1") == "COMPLETED"
        assert state._task_results["t1"] == "found 5 papers"
        assert state._task_artifacts["t1"] == {"file": "out.md"}
        submits = [
            c for c in client.calls if c[0] == "submit_task_status_update"
        ]
        assert len(submits) == 1
        assert submits[0][2]["status"] == "COMPLETED"

    def test_already_terminal_keeps_status_but_records_summary(self):
        state = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="researcher")]
        )
        ps = state._active_plan_by_session[HSESSION]
        ps.tasks["t1"].status = "FAILED"
        state._dispatch_reporting_tool(
            "report_task_completed",
            {"task_id": "t1", "summary": "salvaged"},
            HSESSION,
        )
        assert _task_status(state, "t1") == "FAILED"
        assert state._task_results["t1"] == "salvaged"


class TestReportTaskFailed:
    def test_running_to_failed_fires_refine(self):
        refine_calls: list = []
        state = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="researcher")],
            refine_calls=refine_calls,
        )
        ps = state._active_plan_by_session[HSESSION]
        ps.tasks["t1"].status = "RUNNING"
        client: FakeClient = state._client  # type: ignore[assignment]
        client.calls.clear()

        state._dispatch_reporting_tool(
            "report_task_failed",
            {"task_id": "t1", "reason": "no API key", "recoverable": False},
            HSESSION,
        )
        assert _task_status(state, "t1") == "FAILED"
        submits = [
            c for c in client.calls if c[0] == "submit_task_status_update"
        ]
        assert any(s[2]["status"] == "FAILED" for s in submits)
        # mark_task_failed → _refine_after_task_failure invokes refine.
        kinds = [ctx["kind"] for (_plan, ctx) in refine_calls]
        assert "task_failed" in kinds


class TestReportTaskBlocked:
    def test_blocker_recorded_and_refine_fired(self):
        refine_calls: list = []
        state = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="researcher")],
            refine_calls=refine_calls,
        )
        ps = state._active_plan_by_session[HSESSION]
        ps.tasks["t1"].status = "RUNNING"
        state._dispatch_reporting_tool(
            "report_task_blocked",
            {"task_id": "t1", "blocker": "waiting on review", "needed": "approval"},
            HSESSION,
        )
        # Task remains RUNNING — blockedness is observed, not terminal.
        assert _task_status(state, "t1") == "RUNNING"
        assert "waiting on review" in state._task_blockers["t1"]
        kinds = [ctx["kind"] for (_plan, ctx) in refine_calls]
        assert "task_blocked" in kinds


class TestReportNewWorkDiscovered:
    def test_fires_refine_with_new_work_kind(self):
        refine_calls: list = []
        state = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="researcher")],
            refine_calls=refine_calls,
        )
        state._dispatch_reporting_tool(
            "report_new_work_discovered",
            {
                "parent_task_id": "t1",
                "title": "follow-up",
                "description": "summarize results",
                "assignee": "writer",
            },
            HSESSION,
        )
        kinds = [ctx["kind"] for (_plan, ctx) in refine_calls]
        assert "new_work_discovered" in kinds
        # Detail carries title + description so the planner can act on it.
        details = [
            ctx["detail"] for (_plan, ctx) in refine_calls
            if ctx["kind"] == "new_work_discovered"
        ]
        assert any("follow-up" in d for d in details)
        assert any("summarize results" in d for d in details)


class TestReportPlanDivergence:
    def test_fires_refine_with_divergence_kind(self):
        refine_calls: list = []
        state = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="researcher")],
            refine_calls=refine_calls,
        )
        state._dispatch_reporting_tool(
            "report_plan_divergence",
            {"note": "user changed scope", "suggested_action": "rebuild"},
            HSESSION,
        )
        kinds = [ctx["kind"] for (_plan, ctx) in refine_calls]
        assert "plan_divergence" in kinds


class TestUnknownReportingTool:
    def test_unknown_name_returns_ack_quietly(self):
        state = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="researcher")]
        )
        ack = state._dispatch_reporting_tool(
            "report_made_up", {"task_id": "t1"}, HSESSION
        )
        assert ack == {"acknowledged": True}


class TestHandlerSwallowsExceptions:
    def test_dispatch_with_none_args_does_not_raise(self):
        state = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="researcher")]
        )
        ack = state._dispatch_reporting_tool(
            "report_task_started", {}, HSESSION
        )
        assert ack == {"acknowledged": True}

    def test_dispatch_with_empty_hsession_is_safe(self):
        state = _make_state_with_plan(
            [Task(id="t1", title="x", assignee_agent_id="researcher")]
        )
        ack = state._dispatch_reporting_tool(
            "report_task_started", {"task_id": "t1"}, ""
        )
        assert ack == {"acknowledged": True}
        assert _task_status(state, "t1") == "PENDING"
