"""Unit tests for harmonograf_client.state_protocol."""

from __future__ import annotations

import dataclasses

import pytest

from harmonograf_client import state_protocol as sp


# ---------------------------------------------------------------------------
# Fixtures — lightweight Task/Plan stand-ins (duck-typed, not planner.Task)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class FakeTask:
    id: str
    title: str = ""
    description: str = ""
    assignee_agent_id: str = ""
    status: str = "PENDING"


@dataclasses.dataclass
class FakeEdge:
    from_task_id: str
    to_task_id: str


@dataclasses.dataclass
class FakePlan:
    tasks: list
    edges: list
    summary: str = ""
    id: str = ""


# ---------------------------------------------------------------------------
# Reader defaults
# ---------------------------------------------------------------------------


def test_read_current_task_empty_state_returns_defaults():
    result = sp.read_current_task({})
    assert result == {"id": "", "title": "", "description": "", "assignee": ""}


def test_read_current_task_handles_non_mapping():
    assert sp.read_current_task(None)["id"] == ""
    assert sp.read_current_task("not-a-dict")["title"] == ""


def test_read_plan_id_defaults_empty_string():
    assert sp.read_plan_id({}) == ""


def test_read_completed_results_defaults_to_empty_dict():
    assert sp.read_completed_results({}) == {}
    assert sp.read_completed_results({sp.KEY_COMPLETED_TASK_RESULTS: "bad"}) == {}


def test_read_available_tasks_defaults_to_empty_list():
    assert sp.read_available_tasks({}) == []
    assert sp.read_available_tasks({sp.KEY_AVAILABLE_TASKS: "bad"}) == []


def test_read_tools_available_filters_non_strings():
    state = {sp.KEY_TOOLS_AVAILABLE: ["report_progress", 42, None, "flag_blocker"]}
    assert sp.read_tools_available(state) == ["report_progress", "flag_blocker"]


def test_read_agent_outcome_missing_task_returns_empty():
    assert sp.read_agent_outcome({}, "t1") == ""
    state = {sp.KEY_TASK_OUTCOME: {"t1": "completed"}}
    assert sp.read_agent_outcome(state, "t1") == "completed"
    assert sp.read_agent_outcome(state, "t2") == ""


def test_read_agent_progress_handles_bad_types():
    state = {sp.KEY_TASK_PROGRESS: {"t1": 0.5, "t2": "nope"}}
    assert sp.read_agent_progress(state, "t1") == pytest.approx(0.5)
    assert sp.read_agent_progress(state, "t2") == 0.0
    assert sp.read_agent_progress(state, "missing") == 0.0


def test_read_divergence_flag_defaults_false():
    assert sp.read_divergence_flag({}) is False
    assert sp.read_divergence_flag({sp.KEY_DIVERGENCE_FLAG: True}) is True


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def test_write_current_task_from_task_object():
    state: dict = {}
    task = FakeTask(
        id="t-1",
        title="Gather data",
        description="Collect CSVs",
        assignee_agent_id="worker",
    )
    sp.write_current_task(state, task)
    assert state[sp.KEY_CURRENT_TASK_ID] == "t-1"
    assert state[sp.KEY_CURRENT_TASK_TITLE] == "Gather data"
    assert state[sp.KEY_CURRENT_TASK_DESCRIPTION] == "Collect CSVs"
    assert state[sp.KEY_CURRENT_TASK_ASSIGNEE] == "worker"


def test_write_current_task_from_mapping():
    state: dict = {}
    sp.write_current_task(
        state,
        {"id": "t-2", "title": "T", "description": "D", "assignee": "a"},
    )
    result = sp.read_current_task(state)
    assert result == {"id": "t-2", "title": "T", "description": "D", "assignee": "a"}


def test_write_current_task_none_clears():
    state = {
        sp.KEY_CURRENT_TASK_ID: "t-1",
        sp.KEY_CURRENT_TASK_TITLE: "x",
        sp.KEY_CURRENT_TASK_DESCRIPTION: "y",
        sp.KEY_CURRENT_TASK_ASSIGNEE: "z",
    }
    sp.write_current_task(state, None)
    for key in (
        sp.KEY_CURRENT_TASK_ID,
        sp.KEY_CURRENT_TASK_TITLE,
        sp.KEY_CURRENT_TASK_DESCRIPTION,
        sp.KEY_CURRENT_TASK_ASSIGNEE,
    ):
        assert key not in state


def test_clear_current_task_idempotent():
    state: dict = {}
    sp.clear_current_task(state)  # must not raise
    assert state == {}


def test_write_current_task_round_trip():
    state: dict = {}
    task = FakeTask(
        id="t-42", title="Title", description="Desc", assignee_agent_id="alice"
    )
    sp.write_current_task(state, task)
    result = sp.read_current_task(state)
    assert result == {
        "id": "t-42",
        "title": "Title",
        "description": "Desc",
        "assignee": "alice",
    }


def test_write_plan_context_populates_available_tasks_with_deps():
    plan = FakePlan(
        id="plan-1",
        summary="demo plan",
        tasks=[
            FakeTask(id="a", title="A"),
            FakeTask(id="b", title="B", assignee_agent_id="bob"),
            FakeTask(id="c", title="C"),
        ],
        edges=[FakeEdge("a", "b"), FakeEdge("b", "c")],
    )
    state: dict = {}
    sp.write_plan_context(
        state,
        plan,
        completed_results={"a": "done"},
        host_agent="host",
    )
    assert state[sp.KEY_PLAN_ID] == "plan-1"
    assert state[sp.KEY_PLAN_SUMMARY] == "demo plan"
    available = state[sp.KEY_AVAILABLE_TASKS]
    by_id = {t["id"]: t for t in available}
    assert by_id["a"]["deps"] == []
    assert by_id["b"]["deps"] == ["a"]
    assert by_id["b"]["assignee"] == "bob"
    assert by_id["c"]["deps"] == ["b"]
    assert by_id["a"]["assignee"] == "host"  # fallback
    assert state[sp.KEY_COMPLETED_TASK_RESULTS] == {"a": "done"}


def test_write_plan_context_accepts_none_plan():
    state: dict = {}
    sp.write_plan_context(state, None, completed_results=None, host_agent="h")
    assert state[sp.KEY_PLAN_ID] == ""
    assert state[sp.KEY_PLAN_SUMMARY] == ""
    assert state[sp.KEY_AVAILABLE_TASKS] == []
    assert state[sp.KEY_COMPLETED_TASK_RESULTS] == {}


def test_write_tools_available_filters_non_strings():
    state: dict = {}
    sp.write_tools_available(state, ["report", None, 3, "flag"])
    assert state[sp.KEY_TOOLS_AVAILABLE] == ["report", "flag"]


# ---------------------------------------------------------------------------
# Prefix invariant
# ---------------------------------------------------------------------------


def test_all_writers_only_set_prefixed_keys():
    state: dict = {}
    sp.write_current_task(state, FakeTask(id="t"))
    sp.write_plan_context(
        state,
        FakePlan(id="p", summary="s", tasks=[FakeTask(id="t")], edges=[]),
        completed_results={},
        host_agent="h",
    )
    sp.write_tools_available(state, ["x"])
    assert state, "writers populated state"
    for key in state:
        assert key.startswith(sp.HARMONOGRAF_PREFIX), key


def test_all_declared_keys_carry_prefix():
    for key in sp.ALL_KEYS:
        assert key.startswith(sp.HARMONOGRAF_PREFIX)


# ---------------------------------------------------------------------------
# extract_agent_writes
# ---------------------------------------------------------------------------


def test_extract_agent_writes_detects_added_keys():
    before: dict = {}
    after = {sp.KEY_AGENT_NOTE: "hello"}
    assert sp.extract_agent_writes(before, after) == {sp.KEY_AGENT_NOTE: "hello"}


def test_extract_agent_writes_detects_changed_keys():
    before = {sp.KEY_DIVERGENCE_FLAG: False}
    after = {sp.KEY_DIVERGENCE_FLAG: True}
    assert sp.extract_agent_writes(before, after) == {sp.KEY_DIVERGENCE_FLAG: True}


def test_extract_agent_writes_detects_removed_keys_as_none():
    before = {sp.KEY_AGENT_NOTE: "x"}
    after: dict = {}
    assert sp.extract_agent_writes(before, after) == {sp.KEY_AGENT_NOTE: None}


def test_extract_agent_writes_ignores_non_harmonograf_keys():
    before = {"foo": 1, sp.KEY_AGENT_NOTE: "a"}
    after = {"foo": 2, "bar": 3, sp.KEY_AGENT_NOTE: "a"}
    assert sp.extract_agent_writes(before, after) == {}


def test_extract_agent_writes_unchanged_returns_empty():
    before = {sp.KEY_AGENT_NOTE: "same"}
    after = {sp.KEY_AGENT_NOTE: "same"}
    assert sp.extract_agent_writes(before, after) == {}


def test_extract_agent_writes_handles_non_mapping_inputs():
    assert sp.extract_agent_writes(None, {sp.KEY_AGENT_NOTE: "x"}) == {
        sp.KEY_AGENT_NOTE: "x"
    }
    assert sp.extract_agent_writes({sp.KEY_AGENT_NOTE: "x"}, None) == {
        sp.KEY_AGENT_NOTE: None
    }
    assert sp.extract_agent_writes(None, None) == {}


def test_extract_agent_writes_nested_mutation():
    before = {sp.KEY_TASK_PROGRESS: {"t1": 0.1}}
    after = {sp.KEY_TASK_PROGRESS: {"t1": 0.5, "t2": 0.2}}
    changes = sp.extract_agent_writes(before, after)
    assert changes == {sp.KEY_TASK_PROGRESS: {"t1": 0.5, "t2": 0.2}}
