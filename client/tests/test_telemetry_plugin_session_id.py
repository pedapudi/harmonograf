"""Regression tests for per-span ADK ``session_id`` stamping.

Before the fix, :class:`HarmonografTelemetryPlugin` emitted every span
with an empty ``session_id`` field on the wire, causing the server to
fall back to the stream's default session (assigned once on first
``Hello``). Every ADK session sharing a process-level
:class:`harmonograf_client.Client` therefore collapsed into one
harmonograf session (``sess_<date>_<nnnn>``), making the Gantt /
timeline unusable for long-lived presentation_agent-style processes.

The fix reads the ADK ``session.id`` from the
``invocation_context`` / ``callback_context`` / ``tool_context`` and
passes it through :meth:`Client.emit_span_start`. These tests exercise
each of the five callbacks and assert the stamped ``session_id``
survives into the protobuf envelope.
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    # Seed with a DIFFERENT default session so we can prove the
    # per-span override actually lands (instead of the wire simply
    # reflecting the transport default).
    return Client(
        name="session-id-test",
        agent_id="agent-X",
        session_id="stream-default-session",
        buffer_size=64,
        _transport_factory=make_factory(made),
    )


@pytest.fixture
def plugin(client: Client) -> HarmonografTelemetryPlugin:
    return HarmonografTelemetryPlugin(client)


def _span_session_ids(client: Client) -> list[str]:
    ids: list[str] = []
    for env in client._events.drain():
        # Only SPAN_START envelopes carry a full Span message; SPAN_END
        # and SPAN_UPDATE only reference the span by id, so session_id
        # only needs to ride on SPAN_START.
        if env.kind is EnvelopeKind.SPAN_START:
            ids.append(env.payload.span.session_id)
    return ids


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_run_stamps_session_id_from_invocation_context(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    ctx = _InvocationContext(invocation_id="inv-1", session_id=ADK_SESSION)
    await plugin.before_run_callback(invocation_context=ctx)
    assert _span_session_ids(client) == [ADK_SESSION]


@pytest.mark.asyncio
async def test_before_model_stamps_session_id_from_callback_context(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    cb = _CallbackContext(invocation_id="inv-1", session_id=ADK_SESSION)
    await plugin.before_model_callback(callback_context=cb, llm_request=_LlmRequest())
    assert _span_session_ids(client) == [ADK_SESSION]


@pytest.mark.asyncio
async def test_before_tool_stamps_session_id_from_tool_context(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    tc = _CallbackContext(invocation_id="inv-1", session_id=ADK_SESSION)
    await plugin.before_tool_callback(
        tool=_Tool("search"), tool_args={"q": "hi"}, tool_context=tc
    )
    assert _span_session_ids(client) == [ADK_SESSION]


@pytest.mark.asyncio
async def test_full_invocation_spans_all_carry_same_adk_session_id(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """End-to-end: before_run + before_model + before_tool on the same
    ADK session should yield three spans all stamped with the ADK
    session id — the exact regression that motivated the fix.
    """
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
    assert all(sid == ADK_SESSION for sid in sids)


@pytest.mark.asyncio
async def test_two_adk_sessions_share_one_process_and_stay_distinct(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """The motivating bug: a module-level Client shared across two ADK
    sessions must surface both sessions distinctly on the wire, not
    collapse them into the transport's default.
    """
    await plugin.before_run_callback(
        invocation_context=_InvocationContext("inv-A", ADK_SESSION)
    )
    await plugin.before_run_callback(
        invocation_context=_InvocationContext("inv-B", OTHER_SESSION)
    )
    assert _span_session_ids(client) == [ADK_SESSION, OTHER_SESSION]


@pytest.mark.asyncio
async def test_missing_session_falls_back_to_transport_default(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """If ADK runs without a session service (unit-test harnesses,
    offline replays), ``_adk_session_id`` returns ``""`` and the
    Client falls back to its default session_id — the pre-fix
    behaviour. This matters so we don't break agents whose tests
    stub out the context shape.
    """

    class _Bare:
        invocation_id = "inv-bare"
        agent = _Agent("root")
        session = None

    await plugin.before_run_callback(invocation_context=_Bare())
    # emit_span_start resolves `session_id=""` to the Client's own
    # default, preserving legacy behavior.
    assert _span_session_ids(client) == ["stream-default-session"]
