"""Tests for the small pure helpers in :mod:`harmonograf_client.adk`.

These helpers don't touch ADK internals; they're safe to exercise
directly without constructing a plugin or runner.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harmonograf_client.adk import (
    DriftReason,
    _canonicalize_assignee,
    _canonicalize_plan_assignees,
    _harmonograf_session_id_for_adk,
    _is_agent_tool,
    _normalize_agent_id,
    _parts_text,
    _safe_attr,
    _summarize_thinking,
)
from harmonograf_client.agent import _extract_thought_text
from harmonograf_client.planner import Plan, Task


class TestSafeAttr:
    def test_returns_value(self):
        obj = SimpleNamespace(a=5)
        assert _safe_attr(obj, "a", 0) == 5

    def test_missing_returns_default(self):
        obj = SimpleNamespace()
        assert _safe_attr(obj, "missing", "fallback") == "fallback"

    def test_none_object(self):
        assert _safe_attr(None, "x", "d") == "d"

    def test_none_value_returns_default(self):
        obj = SimpleNamespace(x=None)
        assert _safe_attr(obj, "x", "d") == "d"

    def test_getattr_exception_returns_default(self):
        class Bad:
            @property
            def boom(self):
                raise RuntimeError("nope")

        assert _safe_attr(Bad(), "boom", "safe") == "safe"


class TestPartsText:
    def _mk_part(self, text: str, thought: bool = False):
        return SimpleNamespace(text=text, thought=thought)

    def test_non_thought_parts_only(self):
        parts = [
            self._mk_part("hello ", thought=False),
            self._mk_part("internal", thought=True),
            self._mk_part("world", thought=False),
        ]
        assert _parts_text(parts, thought=False) == "hello world"

    def test_thought_parts_only(self):
        parts = [
            self._mk_part("visible", thought=False),
            self._mk_part("secret ", thought=True),
            self._mk_part("thoughts", thought=True),
        ]
        assert _parts_text(parts, thought=True) == "secret thoughts"

    def test_handles_none(self):
        assert _parts_text(None, thought=False) == ""

    def test_skips_non_string_text(self):
        parts = [self._mk_part(None, thought=False), self._mk_part("ok", thought=False)]
        assert _parts_text(parts, thought=False) == "ok"


class TestExtractThoughtText:
    def test_extracts_thoughts(self):
        event = SimpleNamespace(
            content=SimpleNamespace(
                parts=[
                    SimpleNamespace(text="visible", thought=False),
                    SimpleNamespace(text="thinking", thought=True),
                ]
            )
        )
        assert _extract_thought_text(event) == "thinking"

    def test_event_with_no_content(self):
        assert _extract_thought_text(SimpleNamespace(content=None)) == ""

    def test_broken_event_does_not_raise(self):
        class Evil:
            @property
            def content(self):
                raise ValueError("broken")

        assert _extract_thought_text(Evil()) == ""


class TestSummarizeThinking:
    def test_empty_input(self):
        assert _summarize_thinking("") == ""
        assert _summarize_thinking("   ") == ""

    def test_prefers_action_sentence(self):
        text = (
            "I should look at this. "
            "Now I will call search_web to fetch papers. "
            "That seems fine."
        )
        summary = _summarize_thinking(text)
        assert "search_web" in summary or "call" in summary.lower()

    def test_truncates_long_sentence(self):
        long = "call " + ("x" * 500) + ". tail."
        summary = _summarize_thinking(long, max_chars=50)
        assert len(summary) <= 50

    def test_fallback_tail_when_no_complete_sentence(self):
        text = "continuing a very long unfinished thought without terminal punctuation"
        summary = _summarize_thinking(text, max_chars=40)
        assert summary.startswith("\u2026")
        assert len(summary) <= 40

    def test_prefers_most_recent_complete_sentence(self):
        text = "First sentence here. Second one here."
        # Neither contains an action verb that's short enough — picks last complete.
        summary = _summarize_thinking(text)
        assert "Second" in summary or "First" in summary


class TestNormalizeAgentId:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("ResearchAgent", "researchagent"),
            ("research-agent", "researchagent"),
            ("research_agent", "researchagent"),
            ("Research Agent", "researchagent"),
            ("", ""),
        ],
    )
    def test_normalize(self, raw, expected):
        assert _normalize_agent_id(raw) == expected


class TestCanonicalizeAssignee:
    def test_exact_match(self):
        assert _canonicalize_assignee("writer", ["writer", "researcher"]) == "writer"

    def test_case_insensitive(self):
        assert _canonicalize_assignee("Writer", ["writer"]) == "writer"

    def test_hyphen_underscore(self):
        assert (
            _canonicalize_assignee("research_agent", ["research-agent", "writer"])
            == "research-agent"
        )

    def test_prefix_substring(self):
        assert (
            _canonicalize_assignee("research", ["research-agent", "writer"])
            == "research-agent"
        )

    def test_unresolvable_returns_raw(self):
        assert _canonicalize_assignee("ghost", ["writer"]) == "ghost"

    def test_empty_returns_empty(self):
        assert _canonicalize_assignee("", ["writer"]) == ""


class TestCanonicalizePlanAssignees:
    def test_empty_backfill_to_first_non_host(self):
        plan = Plan(
            tasks=[
                Task(id="t1", title="A", assignee_agent_id=""),
                Task(id="t2", title="B", assignee_agent_id=""),
            ],
            edges=[],
        )
        _canonicalize_plan_assignees(plan, ["host", "worker1", "worker2"], host_agent_name="host")
        assert plan.tasks[0].assignee_agent_id == "worker1"
        assert plan.tasks[1].assignee_agent_id == "worker1"

    def test_empty_backfill_to_first_when_no_host(self):
        plan = Plan(tasks=[Task(id="t", title="x", assignee_agent_id="")], edges=[])
        _canonicalize_plan_assignees(plan, ["writer", "researcher"])
        assert plan.tasks[0].assignee_agent_id == "writer"

    def test_exact_match_preserved(self):
        plan = Plan(tasks=[Task(id="t", title="x", assignee_agent_id="writer")], edges=[])
        _canonicalize_plan_assignees(plan, ["writer", "researcher"])
        assert plan.tasks[0].assignee_agent_id == "writer"

    def test_case_insensitive_rewrite(self):
        plan = Plan(tasks=[Task(id="t", title="x", assignee_agent_id="Writer")], edges=[])
        _canonicalize_plan_assignees(plan, ["writer", "researcher"])
        assert plan.tasks[0].assignee_agent_id == "writer"

    def test_prefix_rewrite(self):
        plan = Plan(
            tasks=[Task(id="t", title="x", assignee_agent_id="research")], edges=[]
        )
        _canonicalize_plan_assignees(plan, ["research-agent", "writer"])
        assert plan.tasks[0].assignee_agent_id == "research-agent"

    def test_unresolvable_preserved_with_warning(self, caplog):
        plan = Plan(
            tasks=[Task(id="t", title="x", assignee_agent_id="ghost")], edges=[]
        )
        with caplog.at_level("WARNING"):
            _canonicalize_plan_assignees(plan, ["writer", "researcher"])
        assert plan.tasks[0].assignee_agent_id == "ghost"
        assert any("ghost" in r.message for r in caplog.records)

    def test_host_only_leaves_empty(self, caplog):
        plan = Plan(tasks=[Task(id="t", title="x", assignee_agent_id="")], edges=[])
        with caplog.at_level("WARNING"):
            _canonicalize_plan_assignees(plan, ["host"], host_agent_name="host")
        assert plan.tasks[0].assignee_agent_id == ""

    def test_none_plan_noops(self):
        _canonicalize_plan_assignees(None, ["writer"])

    def test_empty_known_agents_noops(self):
        plan = Plan(
            tasks=[Task(id="t", title="x", assignee_agent_id="writer")], edges=[]
        )
        _canonicalize_plan_assignees(plan, [])
        assert plan.tasks[0].assignee_agent_id == "writer"


class TestHarmonografSessionIdForAdk:
    def test_empty_returns_empty(self):
        assert _harmonograf_session_id_for_adk("") == ""

    def test_adk_prefix(self):
        out = _harmonograf_session_id_for_adk("abc123")
        assert out == "adk_abc123"

    def test_unsafe_chars_replaced(self):
        out = _harmonograf_session_id_for_adk("a.b/c@d")
        assert out == "adk_a_b_c_d"
        # Matches regex ^[a-zA-Z0-9_-]{1,128}$
        import re
        assert re.match(r"^[a-zA-Z0-9_-]{1,128}$", out)

    def test_deterministic(self):
        a = _harmonograf_session_id_for_adk("sess-x")
        b = _harmonograf_session_id_for_adk("sess-x")
        assert a == b

    def test_truncated_to_128_chars(self):
        out = _harmonograf_session_id_for_adk("x" * 500)
        assert len(out) == 128
        assert out.startswith("adk_")


class TestIsAgentTool:
    def test_duck_typed_agent_with_name_and_desc(self):
        agent = SimpleNamespace(name="a", description="d")
        tool = SimpleNamespace(agent=agent)
        assert _is_agent_tool(tool) is True

    def test_agent_without_name_returns_false(self):
        agent = SimpleNamespace(description="d")
        tool = SimpleNamespace(agent=agent)
        assert _is_agent_tool(tool) is False

    def test_no_agent_attr(self):
        tool = SimpleNamespace()
        assert _is_agent_tool(tool) is False

    def test_agent_is_none(self):
        tool = SimpleNamespace(agent=None)
        assert _is_agent_tool(tool) is False


class TestDriftReason:
    def test_defaults(self):
        d = DriftReason(kind="k", detail="d")
        assert d.kind == "k"
        assert d.detail == "d"
        assert d.event_id == ""

    def test_with_event_id(self):
        d = DriftReason(kind="tool_call_wrong_agent", detail="x", event_id="e1")
        assert d.event_id == "e1"
