"""Tests for per-ADK-agent attribution (harmonograf#74).

The plugin now derives a per-ADK-agent harmonograf ``agent_id`` on each
``before_agent_callback`` and stamps it on every span emitted during
that agent's execution (INVOCATION / LLM_CALL / TOOL_CALL). Sub-agents
invoked via AgentTool spawn a sub-Runner with its own
``invocation_id``, so their spans correctly land on the sub-agent's
row rather than collapsing onto the coordinator's.

Contract verified here:

* The per-agent id format is ``<client.agent_id>:<adk_agent_name>``
  and spans emitted inside a ``before_agent`` window stamp it on
  ``span.agent_id``.
* Nested agents (coordinator → AgentTool sub-Runner with specialist)
  push/pop correctly: the specialist's spans carry the specialist's
  id, and when control returns to the coordinator, its subsequent
  spans carry the coordinator's id again.
* The FIRST span from a given per-agent id carries
  ``hgraf.agent.{name,parent_id,kind,branch}`` attributes; subsequent
  spans from the same agent don't pay the stamping cost. The server
  harvests these into ``Agent.metadata`` on auto-register.
* Back-compat: spans emitted outside any before/after_agent window
  fall back to the client's root agent_id.
* Cancel: ``_close_stale_spans_for_invocation`` clears the agent
  stash for the cancelled invocation so long-lived plugin instances
  don't leak per-invocation entries on steering / user-cancel.
"""

from __future__ import annotations

from typing import Any

import pytest

from harmonograf_client.buffer import EnvelopeKind
from harmonograf_client.client import Client
from harmonograf_client.enums import SpanStatus
from harmonograf_client.telemetry_plugin import HarmonografTelemetryPlugin

from tests._fixtures import FakeTransport, make_factory


ROOT_SESSION = "root-session-1"
CLIENT_AGENT_ID = "client-root-agent-UUID"


# ---------------------------------------------------------------------------
# ADK-shaped stand-ins (matching the session-id fixtures file)
# ---------------------------------------------------------------------------


class _Session:
    def __init__(self, sid: str) -> None:
        self.id = sid


class _BaseAgent:
    """Subclass-matched to look like ``google.adk.agents.Agent`` / ``LlmAgent``.

    The plugin walks ``type(agent).__mro__`` to derive the kind hint;
    picking ``Agent`` as the class name makes ``_agent_kind_hint``
    return ``"llm"`` which is the expected default for worker agents.
    """

    def __init__(self, name: str, parent: Any = None) -> None:
        self.name = name
        self.parent_agent = parent


class Agent(_BaseAgent):
    """Class-name matches ADK's ``Agent``/``LlmAgent`` mapping."""


class SequentialAgent(_BaseAgent):
    """Class-name matches ADK's workflow container mapping."""


class _InvocationContext:
    def __init__(
        self,
        invocation_id: str,
        session_id: str,
        agent: Any,
        branch: str = "",
    ) -> None:
        self.invocation_id = invocation_id
        self.session = _Session(session_id)
        self.agent = agent
        self.branch = branch


class _CallbackContext:
    """ADK unifies CallbackContext / ToolContext under one surface.

    Tests pass ``_invocation_context`` so the plugin can recover the
    agent object when the callback only has a CallbackContext (real
    ADK flows hit this path).
    """

    def __init__(
        self,
        invocation_id: str,
        session_id: str,
        agent: Any,
        branch: str = "",
    ) -> None:
        self.invocation_id = invocation_id
        self.session = _Session(session_id)
        self.branch = branch
        # The plugin probes both ctx.agent and ctx._invocation_context.agent.
        self._invocation_context = _InvocationContext(
            invocation_id, session_id, agent, branch
        )


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class _LlmRequest:
    def __init__(self, model: str = "mock/llm") -> None:
        self.model = model


class _LlmResponse:
    def __init__(self) -> None:
        self.partial = False
        self.error_message = None


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="per-agent-test",
        agent_id=CLIENT_AGENT_ID,
        session_id="home",
        buffer_size=256,
        _transport_factory=make_factory(made),
    )


@pytest.fixture
def plugin(client: Client) -> HarmonografTelemetryPlugin:
    return HarmonografTelemetryPlugin(client)


def _span_starts(client: Client) -> list[Any]:
    out: list[Any] = []
    for env in client._events.drain():
        if env.kind is EnvelopeKind.SPAN_START:
            out.append(env.payload.span)
    return out


# ---------------------------------------------------------------------------
# Per-agent id derivation
# ---------------------------------------------------------------------------


def test_derive_agent_id_format(plugin: HarmonografTelemetryPlugin) -> None:
    """Per-agent id is ``<client.agent_id>:<adk_name>`` — stable + unique."""
    assert plugin._derive_agent_id("coordinator") == f"{CLIENT_AGENT_ID}:coordinator"
    assert plugin._derive_agent_id("research_agent") == f"{CLIENT_AGENT_ID}:research_agent"
    # Empty name falls back to the bare client id — pre-#74 behavior.
    assert plugin._derive_agent_id("") == CLIENT_AGENT_ID


def test_agent_kind_hint(plugin: HarmonografTelemetryPlugin) -> None:
    """Class-name heuristic produces the expected kind hint per ADK shape."""
    assert plugin._agent_kind_hint(Agent("a")) == "llm"
    assert plugin._agent_kind_hint(SequentialAgent("s")) == "workflow"
    assert plugin._agent_kind_hint(None) == "unknown"


# ---------------------------------------------------------------------------
# Agent stash drives span.agent_id stamping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_agent_pushes_per_agent_id_onto_stack(
    plugin: HarmonografTelemetryPlugin,
) -> None:
    """before_agent pushes the agent's id; subsequent spans stamp it."""
    coord = Agent("coordinator")
    cb = _CallbackContext("inv-1", ROOT_SESSION, coord)
    await plugin.before_agent_callback(agent=coord, callback_context=cb)
    assert plugin._agent_stash["inv-1"] == [f"{CLIENT_AGENT_ID}:coordinator"]


@pytest.mark.asyncio
async def test_after_agent_pops_stack(plugin: HarmonografTelemetryPlugin) -> None:
    """after_agent pops; when empty, the stash entry is removed."""
    coord = Agent("coordinator")
    cb = _CallbackContext("inv-1", ROOT_SESSION, coord)
    await plugin.before_agent_callback(agent=coord, callback_context=cb)
    await plugin.after_agent_callback(agent=coord, callback_context=cb)
    assert "inv-1" not in plugin._agent_stash


@pytest.mark.asyncio
async def test_nested_agent_stack_pushes_and_pops_correctly(
    plugin: HarmonografTelemetryPlugin,
) -> None:
    """Coordinator → AgentTool(research) pattern.

    AgentTool spawns a sub-Runner with a different invocation_id, so
    the two agents don't even share a stack — the test uses matching
    invocation_id to stress the stacking logic itself (same invocation,
    nested ``before_agent``). Both paths are valid real-world shapes
    depending on how the delegation is wired.
    """
    coord = Agent("coordinator")
    research = Agent("research", parent=coord)
    cb = _CallbackContext("inv-shared", ROOT_SESSION, coord)
    await plugin.before_agent_callback(agent=coord, callback_context=cb)
    # Research enters on the SAME invocation — hypothetical but
    # exercises the stack push/pop path.
    cb_research = _CallbackContext("inv-shared", ROOT_SESSION, research)
    await plugin.before_agent_callback(agent=research, callback_context=cb_research)
    assert plugin._agent_stash["inv-shared"] == [
        f"{CLIENT_AGENT_ID}:coordinator",
        f"{CLIENT_AGENT_ID}:research",
    ]
    # Research finishes; coordinator's id is back on top.
    await plugin.after_agent_callback(agent=research, callback_context=cb_research)
    assert plugin._agent_stash["inv-shared"] == [f"{CLIENT_AGENT_ID}:coordinator"]
    await plugin.after_agent_callback(agent=coord, callback_context=cb)
    assert "inv-shared" not in plugin._agent_stash


# ---------------------------------------------------------------------------
# Spans emitted inside an agent window stamp that agent's id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_span_stamped_with_per_agent_id(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """LLM_CALL spans emitted inside a coordinator's before_agent window
    carry the coordinator's per-agent id, not the client root id."""
    coord = Agent("coordinator")
    await plugin.before_run_callback(
        invocation_context=_InvocationContext("inv-1", ROOT_SESSION, coord)
    )
    cb = _CallbackContext("inv-1", ROOT_SESSION, coord)
    await plugin.before_agent_callback(agent=coord, callback_context=cb)
    await plugin.before_model_callback(
        callback_context=cb, llm_request=_LlmRequest("gpt-test")
    )
    spans = _span_starts(client)
    # First span = before_run INVOCATION (coordinator), second = LLM_CALL.
    assert len(spans) == 2
    assert spans[0].agent_id == f"{CLIENT_AGENT_ID}:coordinator"
    assert spans[1].agent_id == f"{CLIENT_AGENT_ID}:coordinator"


@pytest.mark.asyncio
async def test_tool_span_stamped_with_per_agent_id(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """TOOL_CALL spans stamp the per-agent id too."""
    research = Agent("research_agent")
    cb = _CallbackContext("inv-1", ROOT_SESSION, research)
    await plugin.before_agent_callback(agent=research, callback_context=cb)
    await plugin.before_tool_callback(
        tool=_Tool("read_file"), tool_args={"path": "a.md"}, tool_context=cb
    )
    spans = _span_starts(client)
    assert len(spans) == 1
    assert spans[0].agent_id == f"{CLIENT_AGENT_ID}:research_agent"


@pytest.mark.asyncio
async def test_sub_runner_specialists_each_get_own_agent_id(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """AgentTool sub-Runner pattern: each specialist invocation gets its
    own invocation_id, so spans from research / web_developer / reviewer
    stamp distinct per-agent ids — the production Gantt shape that
    harmonograf#74 unlocks.
    """
    coord = Agent("coordinator")
    research = Agent("research_agent", parent=coord)
    web_dev = Agent("web_developer_agent", parent=coord)

    # Coordinator begins.
    await plugin.before_run_callback(
        invocation_context=_InvocationContext("inv-coord", ROOT_SESSION, coord)
    )
    cb_coord = _CallbackContext("inv-coord", ROOT_SESSION, coord)
    await plugin.before_agent_callback(agent=coord, callback_context=cb_coord)

    # Sub-Runner for research — fresh invocation_id.
    cb_research = _CallbackContext(
        "inv-research", ROOT_SESSION, research, branch="coordinator.research_agent"
    )
    await plugin.before_agent_callback(agent=research, callback_context=cb_research)
    await plugin.before_tool_callback(
        tool=_Tool("read_spec"), tool_args={}, tool_context=cb_research
    )
    await plugin.after_agent_callback(agent=research, callback_context=cb_research)

    # Sub-Runner for web_developer — another fresh invocation_id.
    cb_web = _CallbackContext(
        "inv-web", ROOT_SESSION, web_dev, branch="coordinator.web_developer_agent"
    )
    await plugin.before_agent_callback(agent=web_dev, callback_context=cb_web)
    await plugin.before_model_callback(
        callback_context=cb_web, llm_request=_LlmRequest("gpt-dev")
    )
    await plugin.after_agent_callback(agent=web_dev, callback_context=cb_web)

    await plugin.after_agent_callback(agent=coord, callback_context=cb_coord)

    spans = _span_starts(client)
    agent_ids = [s.agent_id for s in spans]
    # Coordinator INVOCATION span + research TOOL_CALL + web LLM_CALL.
    assert agent_ids == [
        f"{CLIENT_AGENT_ID}:coordinator",
        f"{CLIENT_AGENT_ID}:research_agent",
        f"{CLIENT_AGENT_ID}:web_developer_agent",
    ]


# ---------------------------------------------------------------------------
# First-sight hgraf.agent.* attribute stamping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_span_stamps_hgraf_agent_attributes(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """The first span from a new per-agent id carries name/parent/kind/branch."""
    coord = Agent("coordinator")
    research = Agent("research_agent", parent=coord)
    # Prime the root session cache.
    await plugin.before_run_callback(
        invocation_context=_InvocationContext("inv-coord", ROOT_SESSION, coord)
    )
    cb = _CallbackContext(
        "inv-research",
        ROOT_SESSION,
        research,
        branch="coordinator.research_agent",
    )
    await plugin.before_agent_callback(agent=research, callback_context=cb)
    await plugin.before_tool_callback(
        tool=_Tool("read_file"), tool_args={}, tool_context=cb
    )
    # Drain and pick the research tool span.
    spans = _span_starts(client)
    research_span = next(
        s for s in spans if s.agent_id == f"{CLIENT_AGENT_ID}:research_agent"
    )
    attrs = dict(research_span.attributes or {})
    assert attrs["hgraf.agent.name"].string_value == "research_agent"
    assert attrs["hgraf.agent.kind"].string_value == "llm"
    assert (
        attrs["hgraf.agent.parent_id"].string_value
        == f"{CLIENT_AGENT_ID}:coordinator"
    )
    assert (
        attrs["hgraf.agent.branch"].string_value == "coordinator.research_agent"
    )


@pytest.mark.asyncio
async def test_second_span_from_same_agent_skips_hgraf_attrs(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """The hot path doesn't re-stamp hgraf.agent.* on every span."""
    research = Agent("research_agent")
    cb = _CallbackContext("inv-1", ROOT_SESSION, research)
    await plugin.before_agent_callback(agent=research, callback_context=cb)
    await plugin.before_tool_callback(
        tool=_Tool("t1"), tool_args={}, tool_context=cb
    )
    await plugin.before_tool_callback(
        tool=_Tool("t2"), tool_args={}, tool_context=cb
    )
    spans = _span_starts(client)
    # First tool span has the hgraf.agent attrs; second does not.
    attrs1 = dict(spans[0].attributes or {})
    attrs2 = dict(spans[1].attributes or {})
    assert "hgraf.agent.name" in attrs1
    assert "hgraf.agent.name" not in attrs2


@pytest.mark.asyncio
async def test_root_client_agent_id_never_gets_hgraf_stamps(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Spans that stamp the client's bare agent_id (pre-agent window,
    degraded fallback) don't carry hgraf.agent.* — those would
    duplicate the Hello-frame agent registration."""
    cb = _CallbackContext("inv-no-agent", ROOT_SESSION, Agent("ignored"))
    # No before_agent_callback → _resolve_agent_id falls back to the
    # client's root agent_id.
    await plugin.before_tool_callback(
        tool=_Tool("anon"), tool_args={}, tool_context=cb
    )
    spans = _span_starts(client)
    assert len(spans) == 1
    assert spans[0].agent_id == CLIENT_AGENT_ID
    attrs = dict(spans[0].attributes or {})
    assert "hgraf.agent.name" not in attrs


# ---------------------------------------------------------------------------
# Cancellation cleans up the stash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_cancellation_clears_agent_stash_entry(
    plugin: HarmonografTelemetryPlugin,
) -> None:
    """``on_cancellation`` drops the cancelled invocation's stash entry.

    Without this, a USER_STEER / USER_CANCEL mid-agent would leak the
    stack entry across subsequent runs, eventually causing wrong
    per-agent attribution.
    """
    coord = Agent("coordinator")
    cb = _CallbackContext("inv-1", ROOT_SESSION, coord)
    await plugin.before_agent_callback(agent=coord, callback_context=cb)
    assert "inv-1" in plugin._agent_stash
    plugin.on_cancellation("inv-1")
    assert "inv-1" not in plugin._agent_stash
