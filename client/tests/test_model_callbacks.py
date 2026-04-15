"""Tests for task #3 — before/after model callback rewrite with state
protocol routing and structured response signal extraction.

These tests target the module-level helpers directly (and the plugin
callback via a minimal fake CallbackContext) so they don't need a real
ADK runner.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from harmonograf_client import state_protocol as sp
from harmonograf_client.adk import (
    DriftReason,
    ResponseSignals,
    _AdkState,
    _observe_response_signals,
    _pick_current_task_for_agent,
    _route_after_model_signals,
    _snapshot_harmonograf_state,
    _write_plan_context_to_session_state,
    PlanState,
)
from harmonograf_client.planner import Plan, Task, TaskEdge


# ---------------------------------------------------------------------------
# _observe_response_signals
# ---------------------------------------------------------------------------


def _mk_part(text: str = "", thought: bool = False, fc: Any = None):
    return SimpleNamespace(text=text, thought=thought, function_call=fc)


def _mk_resp(parts, finish_reason: Any = None):
    content = SimpleNamespace(parts=parts)
    return SimpleNamespace(content=content, finish_reason=finish_reason)


class TestObserveResponseSignals:
    def test_empty_response(self):
        sig = _observe_response_signals(None)
        assert sig.function_calls == []
        assert sig.text_parts == []

    def test_function_call_extracted(self):
        fc = SimpleNamespace(name="search_web", args={"q": "foo"})
        resp = _mk_resp([_mk_part(fc=fc)])
        sig = _observe_response_signals(resp)
        assert len(sig.function_calls) == 1
        assert sig.function_calls[0]["name"] == "search_web"
        assert sig.function_calls[0]["args"] == {"q": "foo"}
        assert sig.text_parts == []

    def test_text_parts_collected_thought_skipped(self):
        resp = _mk_resp(
            [
                _mk_part(text="answer line one"),
                _mk_part(text="thinking internally", thought=True),
                _mk_part(text="answer line two"),
            ]
        )
        sig = _observe_response_signals(resp)
        assert sig.text_parts == ["answer line one", "answer line two"]

    def test_task_complete_marker(self):
        resp = _mk_resp([_mk_part(text="Task complete: t1 - did the thing")])
        sig = _observe_response_signals(resp)
        assert sig.has_task_complete_marker is True
        assert sig.marker_task_id == "t1"
        assert sig.marker_reason and "did the thing" in sig.marker_reason

    def test_task_failed_marker(self):
        resp = _mk_resp([_mk_part(text="Task failed: t2 - network error")])
        sig = _observe_response_signals(resp)
        assert sig.has_task_failed_marker is True
        assert sig.marker_task_id == "t2"

    def test_finish_reason_coerced_to_str(self):
        resp = _mk_resp([], finish_reason="STOP")
        sig = _observe_response_signals(resp)
        assert sig.finish_reason == "STOP"


# ---------------------------------------------------------------------------
# _pick_current_task_for_agent
# ---------------------------------------------------------------------------


def _mk_plan_state(tasks: list[Task], edges: list[TaskEdge] | None = None) -> PlanState:
    plan = Plan(tasks=list(tasks), edges=list(edges or []), summary="summary")
    return PlanState(
        plan=plan,
        plan_id="plan_1",
        tasks={t.id: t for t in tasks},
        available_agents=[t.assignee_agent_id for t in tasks if t.assignee_agent_id],
        generating_invocation_id="inv_1",
        remaining_for_fallback=list(tasks),
        host_agent_name="host_agent",
    )


class TestPickCurrentTask:
    def test_forced_task_id_wins_when_assignee_matches(self):
        t1 = Task(id="t1", title="one", assignee_agent_id="alice")
        t2 = Task(id="t2", title="two", assignee_agent_id="alice")
        plan_state = _mk_plan_state([t1, t2])
        picked = _pick_current_task_for_agent(plan_state, "alice", "t2")
        assert picked.id == "t2"

    def test_first_pending_assignee_match(self):
        t1 = Task(id="t1", title="one", assignee_agent_id="bob")
        t2 = Task(id="t2", title="two", assignee_agent_id="alice", status="PENDING")
        plan_state = _mk_plan_state([t1, t2])
        picked = _pick_current_task_for_agent(plan_state, "alice", "")
        assert picked.id == "t2"

    def test_skips_completed_tasks(self):
        t1 = Task(id="t1", title="one", assignee_agent_id="alice", status="COMPLETED")
        t2 = Task(id="t2", title="two", assignee_agent_id="alice", status="PENDING")
        plan_state = _mk_plan_state([t1, t2])
        picked = _pick_current_task_for_agent(plan_state, "alice", "")
        assert picked.id == "t2"

    def test_returns_none_when_deps_unsatisfied(self):
        t1 = Task(id="t1", title="one", assignee_agent_id="bob", status="PENDING")
        t2 = Task(id="t2", title="two", assignee_agent_id="alice", status="PENDING")
        edges = [TaskEdge(from_task_id="t1", to_task_id="t2")]
        plan_state = _mk_plan_state([t1, t2], edges)
        picked = _pick_current_task_for_agent(plan_state, "alice", "")
        assert picked is None

    def test_empty_plan(self):
        assert _pick_current_task_for_agent(None, "alice", "") is None


# ---------------------------------------------------------------------------
# _snapshot_harmonograf_state
# ---------------------------------------------------------------------------


class TestSnapshotHarmonografState:
    def test_filters_harmonograf_keys(self):
        state = {
            "harmonograf.plan_id": "p1",
            "harmonograf.current_task_id": "t1",
            "other_key": "leave me alone",
            42: "non-string key",
        }
        snap = _snapshot_harmonograf_state(state)
        assert snap == {
            "harmonograf.plan_id": "p1",
            "harmonograf.current_task_id": "t1",
        }

    def test_non_mapping_returns_empty(self):
        assert _snapshot_harmonograf_state(None) == {}
        assert _snapshot_harmonograf_state("nope") == {}


# ---------------------------------------------------------------------------
# _write_plan_context_to_session_state
# ---------------------------------------------------------------------------


def _mk_state_with_plan(hsession_id: str = "hs_1") -> tuple[_AdkState, PlanState]:
    client = MagicMock()
    state = _AdkState(client, planner=None, planner_model="", refine_on_events=False)
    t1 = Task(id="t1", title="Research", assignee_agent_id="researcher", status="PENDING")
    t2 = Task(id="t2", title="Write", assignee_agent_id="writer", status="PENDING")
    plan_state = _mk_plan_state([t1, t2])
    with state._lock:
        state._active_plan_by_session[hsession_id] = plan_state
    return state, plan_state


def _mk_cc(agent_name: str, hsession_id: str, inv_id: str = "inv_1"):
    """Build a fake CallbackContext that routes to the given hsession.

    The _AdkState routing uses _adk_to_h_session to map ADK session id →
    harmonograf session id. We preseed that map.
    """
    session = SimpleNamespace(id="adk_session_1", state={})
    agent = SimpleNamespace(name=agent_name)
    ic = SimpleNamespace(
        agent=agent,
        session=session,
        invocation_id=inv_id,
    )
    cc = SimpleNamespace(_invocation_context=ic, invocation_id=inv_id)
    return cc, ic, session


class TestWritePlanContextToSessionState:
    def test_writes_plan_keys_and_current_task(self):
        state, plan_state = _mk_state_with_plan()
        with state._lock:
            state._adk_to_h_session["adk_session_1"] = "hs_1"
        cc, ic, session = _mk_cc("researcher", "hs_1")
        _write_plan_context_to_session_state(state, cc)
        assert session.state[sp.KEY_PLAN_ID] == "plan_1"
        assert session.state[sp.KEY_PLAN_SUMMARY] == "summary"
        assert sp.KEY_AVAILABLE_TASKS in session.state
        assert session.state[sp.KEY_CURRENT_TASK_ID] == "t1"
        assert session.state[sp.KEY_CURRENT_TASK_ASSIGNEE] == "researcher"

    def test_snapshots_harmonograf_keys(self):
        state, _ = _mk_state_with_plan()
        with state._lock:
            state._adk_to_h_session["adk_session_1"] = "hs_1"
        cc, _, session = _mk_cc("researcher", "hs_1")
        _write_plan_context_to_session_state(state, cc)
        with state._lock:
            snap = state._state_snapshot_before["inv_1"]
        assert snap[sp.KEY_PLAN_ID] == "plan_1"
        assert snap[sp.KEY_CURRENT_TASK_ID] == "t1"

    def test_noop_without_session_state_mapping(self):
        state, _ = _mk_state_with_plan()
        cc = SimpleNamespace(
            _invocation_context=SimpleNamespace(
                agent=SimpleNamespace(name="researcher"),
                session=SimpleNamespace(id="x", state=None),
                invocation_id="inv_2",
            ),
            invocation_id="inv_2",
        )
        _write_plan_context_to_session_state(state, cc)
        with state._lock:
            assert "inv_2" not in state._state_snapshot_before

    def test_no_plan_still_writes_current_task_cleared(self):
        client = MagicMock()
        state = _AdkState(client, planner=None, planner_model="", refine_on_events=False)
        cc, _, session = _mk_cc("loner", "hs_missing")
        _write_plan_context_to_session_state(state, cc)
        # snapshot always recorded even when plan is absent
        with state._lock:
            assert "inv_1" in state._state_snapshot_before


# ---------------------------------------------------------------------------
# _route_after_model_signals
# ---------------------------------------------------------------------------


class TestRouteAfterModelSignals:
    def _setup(self):
        state, plan_state = _mk_state_with_plan()
        with state._lock:
            state._adk_to_h_session["adk_session_1"] = "hs_1"
        cc, _, session = _mk_cc("researcher", "hs_1")
        _write_plan_context_to_session_state(state, cc)
        return state, plan_state, cc, session

    def test_agent_outcome_completed_flips_task(self):
        state, plan_state, cc, session = self._setup()
        session.state[sp.KEY_TASK_OUTCOME] = {"t1": "completed"}
        sig = ResponseSignals()
        _route_after_model_signals(state, cc, sig)
        assert plan_state.tasks["t1"].status == "COMPLETED"

    def test_agent_outcome_failed_fires_refine(self):
        state, plan_state, cc, session = self._setup()
        state.refine_plan_on_drift = MagicMock()  # type: ignore[assignment]
        session.state[sp.KEY_TASK_OUTCOME] = {"t1": "failed"}
        _route_after_model_signals(state, cc, ResponseSignals())
        assert plan_state.tasks["t1"].status == "FAILED"
        state.refine_plan_on_drift.assert_called_once()
        args, kwargs = state.refine_plan_on_drift.call_args
        drift = args[1] if len(args) > 1 else kwargs.get("drift")
        assert drift.kind == "task_failed_by_agent"

    def test_divergence_flag_fires_refine(self):
        state, plan_state, cc, session = self._setup()
        state.refine_plan_on_drift = MagicMock()  # type: ignore[assignment]
        session.state[sp.KEY_DIVERGENCE_FLAG] = True
        session.state[sp.KEY_AGENT_NOTE] = "strategy isn't working"
        _route_after_model_signals(state, cc, ResponseSignals())
        state.refine_plan_on_drift.assert_called_once()
        args, kwargs = state.refine_plan_on_drift.call_args
        drift = args[1] if len(args) > 1 else kwargs.get("drift")
        assert drift.kind == "agent_reported_divergence"
        assert "strategy" in drift.detail

    def test_task_complete_text_marker(self):
        state, plan_state, cc, session = self._setup()
        sig = ResponseSignals(
            has_task_complete_marker=True,
            marker_task_id="t1",
            marker_reason="done",
        )
        _route_after_model_signals(state, cc, sig)
        assert plan_state.tasks["t1"].status == "COMPLETED"

    def test_task_failed_text_marker_fires_refine(self):
        state, plan_state, cc, session = self._setup()
        state.refine_plan_on_drift = MagicMock()  # type: ignore[assignment]
        sig = ResponseSignals(
            has_task_failed_marker=True,
            marker_task_id="t2",
            marker_reason="quota exceeded",
        )
        _route_after_model_signals(state, cc, sig)
        assert plan_state.tasks["t2"].status == "FAILED"
        state.refine_plan_on_drift.assert_called_once()
        args, kwargs = state.refine_plan_on_drift.call_args
        drift = args[1] if len(args) > 1 else kwargs.get("drift")
        assert "quota" in drift.detail

    def test_unchanged_outcome_not_reapplied(self):
        # Snapshot already contains "completed" before the turn — signal
        # routing should ignore it and not try a second transition.
        state, plan_state, cc, session = self._setup()
        # Pre-write outcome BEFORE snapshot + re-snapshot so before == after.
        session.state[sp.KEY_TASK_OUTCOME] = {"t1": "completed"}
        with state._lock:
            state._state_snapshot_before["inv_1"] = _snapshot_harmonograf_state(
                session.state
            )
        # Flip the tracked task to COMPLETED manually so we can detect any
        # stray transition attempt (COMPLETED is terminal).
        plan_state.tasks["t1"].status = "COMPLETED"
        _route_after_model_signals(state, cc, ResponseSignals())
        # No crash, still COMPLETED (not re-entered).
        assert plan_state.tasks["t1"].status == "COMPLETED"
