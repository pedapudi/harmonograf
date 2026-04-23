"""Tests for ``hgraf.task_id`` SpanStart stamping (harmonograf#3).

Goldfive mirrors ``goldfive.current_task_id`` into ADK's
``session.state`` on every ``before_run_callback`` via
``_adk_state_protocol``. The harmonograf server's ingest and the
frontend's Task tab / Trajectory subtab, TaskRegistry.boundSpanId, and
Gantt/Graph/Timeline dependency arrows ALL key off a ``hgraf.task_id``
string attribute on SpanStart frames — but pre-fix nobody emitted it
(verified: 0/44 spans in a fresh e2e session carried the attribute).

This module verifies:

* Every SpanStart (INVOCATION / LLM_CALL / TOOL_CALL) carries
  ``hgraf.task_id`` when ``session.state['goldfive.current_task_id']``
  is populated.
* Absent / empty state key → no attribute stamped (non-goldfive runs,
  pre-plan spans must not carry an invented id).
* Task id flips between turns: the follow-up SpanStart carries the
  NEW task id, not the cached first-turn value.
"""

from __future__ import annotations

from typing import Any

import pytest

from harmonograf_client.buffer import EnvelopeKind
from harmonograf_client.client import Client
from harmonograf_client.telemetry_plugin import HarmonografTelemetryPlugin

from tests._fixtures import FakeTransport, make_factory


ROOT_SESSION = "root-session-task-id"
CLIENT_AGENT_ID = "client-root-agent-task-id"


# ---------------------------------------------------------------------------
# ADK-shaped stand-ins with session.state (mirrored by _adk_state_protocol)
# ---------------------------------------------------------------------------


class _Session:
    def __init__(self, sid: str, state: dict[str, Any] | None = None) -> None:
        self.id = sid
        # ADK's ``Session.state`` is a MutableMapping; a plain dict
        # duck-types for ``.get()`` which is what the plugin reads.
        self.state = dict(state or {})


class _BaseAgent:
    def __init__(self, name: str, parent: Any = None) -> None:
        self.name = name
        self.parent_agent = parent


class Agent(_BaseAgent):
    """Class-name matches ADK's ``Agent``/``LlmAgent`` mapping."""


class _InvocationContext:
    def __init__(
        self,
        invocation_id: str,
        session_id: str,
        agent: Any,
        state: dict[str, Any] | None = None,
        branch: str = "",
    ) -> None:
        self.invocation_id = invocation_id
        self.session = _Session(session_id, state)
        self.agent = agent
        self.branch = branch


class _CallbackContext:
    def __init__(
        self,
        invocation_id: str,
        session_id: str,
        agent: Any,
        state: dict[str, Any] | None = None,
        branch: str = "",
    ) -> None:
        self.invocation_id = invocation_id
        # CallbackContext carries a *shallow copy* of the session.state
        # in real ADK (see the memory pitfall
        # "Verify plugin callback state handoff is read-readable"). For
        # a read-only stamp we only need the snapshot to contain the
        # key — which goldfive's mirror has populated by the time any
        # before_* callback fires.
        self.session = _Session(session_id, state)
        self.branch = branch
        self._invocation_context = _InvocationContext(
            invocation_id, session_id, agent, state=state, branch=branch
        )


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class _LlmRequest:
    def __init__(self, model: str = "mock/llm") -> None:
        self.model = model


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="task-id-test",
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


def _attr_string(span: Any, key: str) -> str | None:
    attrs = dict(span.attributes or {})
    val = attrs.get(key)
    if val is None:
        return None
    return val.string_value


# ---------------------------------------------------------------------------
# Populated state → every SpanStart carries hgraf.task_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invocation_span_stamps_hgraf_task_id(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """before_run_callback INVOCATION SpanStart carries hgraf.task_id."""
    coord = Agent("coordinator")
    state = {"goldfive.current_task_id": "t1"}
    await plugin.before_run_callback(
        invocation_context=_InvocationContext(
            "inv-1", ROOT_SESSION, coord, state=state
        )
    )
    spans = _span_starts(client)
    assert len(spans) == 1
    assert _attr_string(spans[0], "hgraf.task_id") == "t1"


@pytest.mark.asyncio
async def test_tool_span_stamps_hgraf_task_id(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """before_tool_callback TOOL_CALL SpanStart carries hgraf.task_id."""
    research = Agent("research_agent")
    state = {"goldfive.current_task_id": "t2"}
    cb = _CallbackContext("inv-1", ROOT_SESSION, research, state=state)
    await plugin.before_agent_callback(agent=research, callback_context=cb)
    await plugin.before_tool_callback(
        tool=_Tool("read_file"), tool_args={"path": "a.md"}, tool_context=cb
    )
    spans = _span_starts(client)
    tool_spans = [s for s in spans if s.name == "read_file"]
    assert len(tool_spans) == 1
    assert _attr_string(tool_spans[0], "hgraf.task_id") == "t2"


@pytest.mark.asyncio
async def test_model_span_stamps_hgraf_task_id(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """before_model_callback LLM_CALL SpanStart carries hgraf.task_id."""
    coord = Agent("coordinator")
    state = {"goldfive.current_task_id": "t3"}
    await plugin.before_run_callback(
        invocation_context=_InvocationContext(
            "inv-1", ROOT_SESSION, coord, state=state
        )
    )
    cb = _CallbackContext("inv-1", ROOT_SESSION, coord, state=state)
    await plugin.before_agent_callback(agent=coord, callback_context=cb)
    await plugin.before_model_callback(
        callback_context=cb, llm_request=_LlmRequest("gpt-test")
    )
    spans = _span_starts(client)
    # INVOCATION + LLM_CALL.
    assert len(spans) == 2
    assert _attr_string(spans[0], "hgraf.task_id") == "t3"
    assert _attr_string(spans[1], "hgraf.task_id") == "t3"


# ---------------------------------------------------------------------------
# Absent / empty state → no attribute (non-goldfive runs, pre-plan spans)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_state_key_does_not_stamp_hgraf_task_id(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """No ``goldfive.current_task_id`` in state → no attribute; don't
    invent a value, don't raise. Non-goldfive ADK apps must emit
    cleanly without the attribute.
    """
    coord = Agent("coordinator")
    await plugin.before_run_callback(
        invocation_context=_InvocationContext(
            "inv-1", ROOT_SESSION, coord, state={}
        )
    )
    spans = _span_starts(client)
    assert len(spans) == 1
    assert _attr_string(spans[0], "hgraf.task_id") is None


@pytest.mark.asyncio
async def test_empty_state_value_does_not_stamp(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Empty-string value is treated identically to absence — pre-plan
    spans (before goldfive has a task id to mirror) must not carry
    ``hgraf.task_id=""``.
    """
    coord = Agent("coordinator")
    state = {"goldfive.current_task_id": ""}
    await plugin.before_run_callback(
        invocation_context=_InvocationContext(
            "inv-1", ROOT_SESSION, coord, state=state
        )
    )
    spans = _span_starts(client)
    assert _attr_string(spans[0], "hgraf.task_id") is None


@pytest.mark.asyncio
async def test_no_session_attr_does_not_raise(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Malformed ctx (no ``session``) must not raise — telemetry is
    observability-only and must fail quietly."""

    class _BareCtx:
        invocation_id = "inv-1"
        agent = Agent("bare")
        branch = ""

    # Real ADK always populates ``session`` on a runtime callback, but
    # defensive paths matter: ``_safe_attr`` + early-return absorb both.
    await plugin.before_run_callback(invocation_context=_BareCtx())
    spans = _span_starts(client)
    assert len(spans) == 1
    assert _attr_string(spans[0], "hgraf.task_id") is None


# ---------------------------------------------------------------------------
# Follow-up turns: task id flips between tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_id_flips_between_turns(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Goldfive's mirror runs on EVERY before_run_callback, so
    subsequent turns see a fresh (possibly different) task id. The
    plugin reads per-callback — never caches — so the follow-up
    SpanStart carries the NEW value.
    """
    coord = Agent("coordinator")
    await plugin.before_run_callback(
        invocation_context=_InvocationContext(
            "inv-1", ROOT_SESSION, coord, state={"goldfive.current_task_id": "t1"}
        )
    )
    await plugin.after_run_callback(
        invocation_context=_InvocationContext(
            "inv-1", ROOT_SESSION, coord, state={"goldfive.current_task_id": "t1"}
        )
    )
    await plugin.before_run_callback(
        invocation_context=_InvocationContext(
            "inv-2", ROOT_SESSION, coord, state={"goldfive.current_task_id": "t2"}
        )
    )
    spans = _span_starts(client)
    assert len(spans) == 2
    assert _attr_string(spans[0], "hgraf.task_id") == "t1"
    assert _attr_string(spans[1], "hgraf.task_id") == "t2"
