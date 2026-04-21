"""Regression tests for per-span ``session_id`` stamping.

After harmonograf#61, :class:`HarmonografTelemetryPlugin` stamps the
**Client's home / Hello session** on every span instead of the per-ctx
ADK session id. Under ADK's ``AgentTool`` delegation, each sub-Runner
has its own ``InMemorySessionService`` and mints a fresh ADK
session_id per invocation — stamping those directly scatters spans
across N sessions per adk-web run. Rolling onto the home session
produces one coherent harmonograf session per process.

The ADK session is preserved as a span attribute (``adk.session_id``)
on INVOCATION spans so debug tooling can still correlate a span back
to its sub-Runner ADK session.

These tests exercise each of the three span-emitting callbacks and
assert:
1. The wire-level ``session_id`` field on the Span is the Client's
   home session (not the per-ctx ADK session_id).
2. The ADK session_id survives as the ``adk.session_id`` attribute
   on INVOCATION spans so routing-forensics stays possible.
"""

from __future__ import annotations

from typing import Any

import pytest

from harmonograf_client.buffer import EnvelopeKind
from harmonograf_client.client import Client
from harmonograf_client.telemetry_plugin import HarmonografTelemetryPlugin

from tests._fixtures import FakeTransport, make_factory


# ---------------------------------------------------------------------------
# ADK-shaped stand-ins
# ---------------------------------------------------------------------------


class _Session:
    def __init__(self, sid: str) -> None:
        self.id = sid


class _Agent:
    def __init__(self, name: str) -> None:
        self.name = name


class _InvocationContext:
    """Mirrors :class:`google.adk.agents.invocation_context.InvocationContext`
    just enough for the plugin's ``_safe_attr`` accessors.
    """

    def __init__(self, invocation_id: str, session_id: str) -> None:
        self.invocation_id = invocation_id
        self.session = _Session(session_id)
        self.agent = _Agent("root-agent")


class _CallbackContext:
    """ADK unifies CallbackContext / ToolContext / Context under a single
    type exposing ``invocation_id`` and ``session`` on the surface. The
    fake mirrors that (the plugin only ever touches those two fields).
    """

    def __init__(self, invocation_id: str, session_id: str) -> None:
        self.invocation_id = invocation_id
        self.session = _Session(session_id)


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class _LlmRequest:
    def __init__(self, model: str = "gpt-test") -> None:
        self.model = model


class _LlmResponse:
    def __init__(self) -> None:
        self.partial = False
        self.error_message = None


ADK_SESSION = "adk-sess-abc123"
OTHER_SESSION = "adk-sess-def456"
HOME_SESSION = "stream-default-session"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="session-id-test",
        agent_id="agent-X",
        session_id=HOME_SESSION,
        buffer_size=64,
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


def _span_session_ids(client: Client) -> list[str]:
    return [s.session_id for s in _span_starts(client)]


# ---------------------------------------------------------------------------
# Tests — home-session rollup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_run_stamps_home_session_on_span(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """INVOCATION span's wire session_id is the Client's home session —
    not the per-ctx ADK session — so adk-web runs roll up to one
    harmonograf session even when ADK sub-Runners mint their own ADK
    session_ids.
    """
    ctx = _InvocationContext(invocation_id="inv-1", session_id=ADK_SESSION)
    await plugin.before_run_callback(invocation_context=ctx)
    assert _span_session_ids(client) == [HOME_SESSION]


@pytest.mark.asyncio
async def test_before_model_stamps_home_session_on_span(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    cb = _CallbackContext(invocation_id="inv-1", session_id=ADK_SESSION)
    await plugin.before_model_callback(callback_context=cb, llm_request=_LlmRequest())
    assert _span_session_ids(client) == [HOME_SESSION]


@pytest.mark.asyncio
async def test_before_tool_stamps_home_session_on_span(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    tc = _CallbackContext(invocation_id="inv-1", session_id=ADK_SESSION)
    await plugin.before_tool_callback(
        tool=_Tool("search"), tool_args={"q": "hi"}, tool_context=tc
    )
    assert _span_session_ids(client) == [HOME_SESSION]


@pytest.mark.asyncio
async def test_full_invocation_spans_all_carry_home_session(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Three callbacks fire across one ADK invocation; all three spans
    land on the Client's home session (the rollup contract)."""
    ic = _InvocationContext(invocation_id="inv-1", session_id=ADK_SESSION)
    cb = _CallbackContext(invocation_id="inv-1", session_id=ADK_SESSION)
    tc = _CallbackContext(invocation_id="inv-1", session_id=ADK_SESSION)

    await plugin.before_run_callback(invocation_context=ic)
    await plugin.before_model_callback(callback_context=cb, llm_request=_LlmRequest())
    await plugin.before_tool_callback(
        tool=_Tool("fetch"), tool_args=None, tool_context=tc
    )

    sids = _span_session_ids(client)
    assert len(sids) == 3
    assert all(sid == HOME_SESSION for sid in sids)


@pytest.mark.asyncio
async def test_two_adk_sessions_roll_up_to_one_home_session(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Two distinct ADK session_ids from a process-shared Client both
    land on the home session — the intended rollup after #61. ADK's
    AgentTool delegation produces this pattern naturally (each
    sub-Runner has its own InMemorySessionService and mints its own
    session_id); before this fix, that scattered spans across N
    harmonograf sessions per adk-web run.
    """
    await plugin.before_run_callback(
        invocation_context=_InvocationContext("inv-A", ADK_SESSION)
    )
    await plugin.before_run_callback(
        invocation_context=_InvocationContext("inv-B", OTHER_SESSION)
    )
    assert _span_session_ids(client) == [HOME_SESSION, HOME_SESSION]


# ---------------------------------------------------------------------------
# Tests — ADK session preserved as span attribute for forensics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_run_preserves_adk_session_id_as_attribute(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """INVOCATION span's ``adk.session_id`` attribute carries the
    per-ctx ADK session_id so debug tooling can still correlate a
    span back to the exact sub-Runner ADK session that produced it.
    """
    ctx = _InvocationContext(invocation_id="inv-1", session_id=ADK_SESSION)
    await plugin.before_run_callback(invocation_context=ctx)
    [span] = _span_starts(client)
    attrs = dict(span.attributes or {})
    assert "adk.session_id" in attrs
    assert attrs["adk.session_id"].string_value == ADK_SESSION


@pytest.mark.asyncio
async def test_missing_adk_session_omits_adk_session_id_attribute(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """If ADK runs without a session service (unit-test harnesses,
    offline replays), ``_adk_session_id`` returns ``""`` and we omit
    the attribute rather than stamp an empty string. Span session_id
    still rolls up to the home session.
    """

    class _Bare:
        invocation_id = "inv-bare"
        agent = _Agent("root")
        session = None

    await plugin.before_run_callback(invocation_context=_Bare())
    [span] = _span_starts(client)
    attrs = dict(span.attributes or {})
    assert "adk.session_id" not in attrs
    assert span.session_id == HOME_SESSION


@pytest.mark.asyncio
async def test_missing_home_session_falls_back_to_empty(
    made: list[FakeTransport],
) -> None:
    """When the Client has no Hello session assigned yet and no explicit
    session_id was passed, the plugin stamps ``""`` — emit_span_start
    resolves that to the Client's default (which itself is empty). This
    is a degraded state (the transport hasn't connected) and we don't
    want to raise; the downstream server falls back to an auto-created
    home session on first span. The important thing is we don't crash
    the plugin when the Client is pre-connect.
    """
    client = Client(
        name="no-home-session",
        agent_id="agent-Y",
        # session_id not provided; transport hasn't assigned yet either.
        buffer_size=64,
        _transport_factory=make_factory(made),
    )
    plugin = HarmonografTelemetryPlugin(client)
    ctx = _InvocationContext(invocation_id="inv-1", session_id=ADK_SESSION)
    await plugin.before_run_callback(invocation_context=ctx)
    [span] = _span_starts(client)
    # Home session is empty pre-connect; span session_id is whatever the
    # emit_span_start default resolves to (also empty in this harness).
    assert span.session_id == ""
