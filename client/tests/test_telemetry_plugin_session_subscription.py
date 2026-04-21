"""Tests that :class:`HarmonografTelemetryPlugin` auto-registers every
ADK session it sees with the Client's control-subscription manager
(harmonograf #53).

Without this wiring the frontend STEER button is wired to the ADK
session id, but the Client's home SubscribeControl stream is keyed on
the harmonograf-assigned session — so the server's router finds no
matching sub and returns ``delivery=FAILURE``. The plugin is the one
place with visibility into the ADK session id on the hot path, so it
owns the auto-registration.
"""

from __future__ import annotations

from typing import Any

import pytest

from harmonograf_client.client import Client
from harmonograf_client.telemetry_plugin import HarmonografTelemetryPlugin

from tests._fixtures import FakeTransport, make_factory


# ---------------------------------------------------------------------------
# ADK-shaped stand-ins (mirror the fixtures in test_telemetry_plugin_session_id)
# ---------------------------------------------------------------------------


class _Session:
    def __init__(self, sid: str) -> None:
        self.id = sid


class _Agent:
    def __init__(self, name: str) -> None:
        self.name = name


class _InvocationContext:
    def __init__(self, invocation_id: str, session_id: str) -> None:
        self.invocation_id = invocation_id
        self.session = _Session(session_id)
        self.agent = _Agent("root-agent")


class _CallbackContext:
    def __init__(self, invocation_id: str, session_id: str) -> None:
        self.invocation_id = invocation_id
        self.session = _Session(session_id)


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class _LlmRequest:
    def __init__(self, model: str = "gpt-test") -> None:
        self.model = model


ADK_SESSION = "adk-sess-abc123"
OTHER_SESSION = "adk-sess-def456"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="session-sub-plugin-test",
        agent_id="agent-X",
        session_id="stream-default-session",
        buffer_size=16,
        _transport_factory=make_factory(made),
    )


@pytest.fixture
def plugin(client: Client) -> HarmonografTelemetryPlugin:
    return HarmonografTelemetryPlugin(client)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plugin_registers_session_on_first_invocation_span(
    plugin: HarmonografTelemetryPlugin,
    client: Client,
    made: list[FakeTransport],
) -> None:
    """``before_run_callback`` is the earliest ADK-visible hook that
    carries the invocation_context, so the plugin must register the
    session on that entry point at the latest."""
    ctx = _InvocationContext(invocation_id="inv-1", session_id=ADK_SESSION)
    await plugin.before_run_callback(invocation_context=ctx)
    assert made[0].opened_sessions == [ADK_SESSION]


@pytest.mark.asyncio
async def test_plugin_registers_session_on_first_model_span(
    plugin: HarmonografTelemetryPlugin,
    client: Client,
    made: list[FakeTransport],
) -> None:
    """Some flows take a before_model path before before_run (e.g. when
    another plugin gates invocation). The session must still get
    registered lazily on the first observed span."""
    cb = _CallbackContext(invocation_id="inv-1", session_id=ADK_SESSION)
    await plugin.before_model_callback(callback_context=cb, llm_request=_LlmRequest())
    assert made[0].opened_sessions == [ADK_SESSION]


@pytest.mark.asyncio
async def test_plugin_registers_session_on_first_tool_span(
    plugin: HarmonografTelemetryPlugin,
    client: Client,
    made: list[FakeTransport],
) -> None:
    tc = _CallbackContext(invocation_id="inv-1", session_id=ADK_SESSION)
    await plugin.before_tool_callback(
        tool=_Tool("search"), tool_args={"q": "hi"}, tool_context=tc
    )
    assert made[0].opened_sessions == [ADK_SESSION]


@pytest.mark.asyncio
async def test_plugin_does_not_re_register_same_session(
    plugin: HarmonografTelemetryPlugin,
    client: Client,
    made: list[FakeTransport],
) -> None:
    """Many spans per ADK session — before_run, before_model, before_tool,
    etc — must all collapse to a single register_session call to avoid
    leaking one SubscribeControl RPC per span."""
    ic = _InvocationContext(invocation_id="inv-1", session_id=ADK_SESSION)
    cb = _CallbackContext(invocation_id="inv-1", session_id=ADK_SESSION)
    tc = _CallbackContext(invocation_id="inv-1", session_id=ADK_SESSION)

    await plugin.before_run_callback(invocation_context=ic)
    await plugin.before_model_callback(callback_context=cb, llm_request=_LlmRequest())
    await plugin.before_tool_callback(
        tool=_Tool("fetch"), tool_args=None, tool_context=tc
    )

    assert made[0].opened_sessions == [ADK_SESSION]


@pytest.mark.asyncio
async def test_plugin_registers_each_distinct_session(
    plugin: HarmonografTelemetryPlugin,
    client: Client,
    made: list[FakeTransport],
) -> None:
    """Two ADK sessions sharing one process-level Client must each get
    their own SubscribeControl — the motivating scenario for the fix."""
    await plugin.before_run_callback(
        invocation_context=_InvocationContext("inv-A", ADK_SESSION)
    )
    await plugin.before_run_callback(
        invocation_context=_InvocationContext("inv-B", OTHER_SESSION)
    )
    assert made[0].opened_sessions == [ADK_SESSION, OTHER_SESSION]


@pytest.mark.asyncio
async def test_plugin_skips_registration_when_session_missing(
    plugin: HarmonografTelemetryPlugin,
    client: Client,
    made: list[FakeTransport],
) -> None:
    """A ctx without a session service (bare unit tests, offline replays)
    must not attempt to open a useless empty-session subscription —
    register_session would no-op anyway, but we assert the plugin also
    skips tracking so a later real session still gets registered."""

    class _Bare:
        invocation_id = "inv-bare"
        agent = _Agent("root")
        session = None

    await plugin.before_run_callback(invocation_context=_Bare())
    assert made[0].opened_sessions == []

    # Now a real session should still register.
    await plugin.before_run_callback(
        invocation_context=_InvocationContext("inv-real", ADK_SESSION)
    )
    assert made[0].opened_sessions == [ADK_SESSION]


@pytest.mark.asyncio
async def test_register_session_failure_does_not_crash_plugin(
    plugin: HarmonografTelemetryPlugin,
    client: Client,
    made: list[FakeTransport],
) -> None:
    """Observability must never take down the ADK invocation. If the
    transport bombs inside register_session, the span still emits."""

    def _boom(_: Any) -> None:
        raise RuntimeError("transport is on fire")

    # Monkey-patch the FakeTransport's open method to raise — mimics
    # a scenario where the underlying grpc layer has already torn down.
    made[0].open_session_subscription = _boom  # type: ignore[assignment]

    ctx = _InvocationContext(invocation_id="inv-1", session_id=ADK_SESSION)
    # Must not raise.
    await plugin.before_run_callback(invocation_context=ctx)
    # The span still rode through to the events buffer.
    from harmonograf_client.buffer import EnvelopeKind

    starts = [e for e in client._events.drain() if e.kind is EnvelopeKind.SPAN_START]
    assert len(starts) == 1
