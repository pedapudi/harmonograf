"""Construction-time auto-registration of reporting tools on
HarmonografAgent's subtree.

The plugin-level registration (``adk._register_harmonograf_reporting_tools_for_test``)
only fires when the HarmonografAdkPlugin is installed on the runner. Tests
and bespoke runners that build a HarmonografAgent directly and invoke it
without the plugin were seeing unresolved ``report_plan_divergence`` /
``report_task_started`` tool calls — this file pins the fix in place.
"""

from __future__ import annotations

import pytest

pytest.importorskip("google.adk.agents")

from google.adk.agents import Agent  # noqa: E402
from google.adk.tools import AgentTool, FunctionTool  # noqa: E402

from harmonograf_client import HarmonografAgent  # noqa: E402
from harmonograf_client.tools import REPORTING_TOOL_NAMES  # noqa: E402


def _tool_names(agent: Agent) -> set[str]:
    out: set[str] = set()
    for t in getattr(agent, "tools", None) or ():
        n = getattr(t, "name", None) or getattr(
            getattr(t, "func", None), "__name__", None
        )
        if n:
            out.add(n)
    return out


def _dummy_tool(x: str) -> str:
    """Dummy."""
    return x


def _build_tree():
    leaf_a = Agent(
        name="leaf_a",
        model="gemini-2.5-flash",
        instruction="Do A things.",
        description="leaf a",
        tools=[],
    )
    leaf_b = Agent(
        name="leaf_b",
        model="gemini-2.5-flash",
        instruction="Do B things.",
        description="leaf b",
        tools=[FunctionTool(_dummy_tool)],
    )
    grandchild = Agent(
        name="grandchild",
        model="gemini-2.5-flash",
        instruction="Deep nested leaf.",
        description="grandchild",
        tools=[],
    )
    # ``middle`` exposes grandchild through an AgentTool wrapper, the
    # shape the presentation_agent demo uses — not a ``sub_agents`` list.
    middle = Agent(
        name="middle",
        model="gemini-2.5-flash",
        instruction="Middle coordinator.",
        description="middle",
        tools=[AgentTool(grandchild)],
    )
    coordinator = Agent(
        name="coordinator",
        model="gemini-2.5-flash",
        instruction="Coordinator.",
        description="coordinator",
        tools=[AgentTool(leaf_a), AgentTool(leaf_b), AgentTool(middle)],
    )
    return coordinator, {
        "leaf_a": leaf_a,
        "leaf_b": leaf_b,
        "middle": middle,
        "grandchild": grandchild,
    }


def test_construction_registers_reporting_tools_on_every_subagent():
    coordinator, leaves = _build_tree()
    HarmonografAgent(
        name="harmonograf",
        description="wrapper",
        inner_agent=coordinator,
        harmonograf_client=None,
        planner=None,
    )
    for name, agent in leaves.items():
        names = _tool_names(agent)
        for tool_name in REPORTING_TOOL_NAMES:
            assert tool_name in names, (
                f"{name} missing reporting tool {tool_name}; has {names}"
            )


def test_leaf_existing_tools_preserved():
    coordinator, leaves = _build_tree()
    HarmonografAgent(
        name="harmonograf",
        description="wrapper",
        inner_agent=coordinator,
        harmonograf_client=None,
        planner=None,
    )
    names = _tool_names(leaves["leaf_b"])
    assert "_dummy_tool" in names  # function name form
    for tool_name in REPORTING_TOOL_NAMES:
        assert tool_name in names


def test_instructions_augmented_on_every_subagent():
    coordinator, leaves = _build_tree()
    HarmonografAgent(
        name="harmonograf",
        description="wrapper",
        inner_agent=coordinator,
        harmonograf_client=None,
        planner=None,
    )
    for name, agent in leaves.items():
        assert "report_task_started(task_id)" in (agent.instruction or ""), (
            f"{name} missing reporting-tool appendix in instruction"
        )
    # Inner coordinator also gets the appendix — it isn't the
    # HarmonografAgent root itself.
    assert "report_task_started(task_id)" in (coordinator.instruction or "")


def test_registration_is_idempotent_across_constructions():
    """Reuse the subtree shape twice and assert leaves each carry the
    reporting tools exactly once. ADK forbids reparenting the same
    agent, so we build fresh trees but compare the post-registration
    counts to catch any double-append regression."""
    coordinator1, leaves1 = _build_tree()
    HarmonografAgent(
        name="harmonograf_1",
        description="wrapper",
        inner_agent=coordinator1,
        harmonograf_client=None,
        planner=None,
    )
    leaf_b_count_first = len(leaves1["leaf_b"].tools)

    coordinator2, leaves2 = _build_tree()
    HarmonografAgent(
        name="harmonograf_2",
        description="wrapper",
        inner_agent=coordinator2,
        harmonograf_client=None,
        planner=None,
    )
    leaf_b_count_second = len(leaves2["leaf_b"].tools)
    assert leaf_b_count_first == leaf_b_count_second
    # Exactly one copy of each reporting tool on leaf_b:
    #   1 dummy + 7 reporting tools = 8
    assert leaf_b_count_first == 1 + len(REPORTING_TOOL_NAMES)
    # And the instructions gained exactly one appendix.
    assert leaves1["leaf_a"].instruction.count("report_task_started(task_id)") == 1
    assert leaves2["leaf_a"].instruction.count("report_task_started(task_id)") == 1


def test_harmonograf_root_itself_has_no_reporting_tools():
    """The HarmonografAgent wrapper is a BaseAgent, not an LlmAgent —
    it shouldn't receive the reporting tools itself."""
    coordinator, _ = _build_tree()
    root = HarmonografAgent(
        name="harmonograf",
        description="wrapper",
        inner_agent=coordinator,
        harmonograf_client=None,
        planner=None,
    )
    root_tools = getattr(root, "tools", None)
    # HarmonografAgent doesn't declare a tools field; if it did, it
    # should not contain reporting tools.
    if root_tools:
        root_names = _tool_names(root)
        for tool_name in REPORTING_TOOL_NAMES:
            assert tool_name not in root_names


def test_grandchild_reached_through_nested_agenttool():
    coordinator, leaves = _build_tree()
    HarmonografAgent(
        name="harmonograf",
        description="wrapper",
        inner_agent=coordinator,
        harmonograf_client=None,
        planner=None,
    )
    grandchild = leaves["grandchild"]
    names = _tool_names(grandchild)
    for tool_name in REPORTING_TOOL_NAMES:
        assert tool_name in names, (
            f"grandchild missing {tool_name} — nested AgentTool traversal broken"
        )
