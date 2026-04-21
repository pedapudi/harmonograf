"""Regression tests for per-span ``session_id`` stamping.

After goldfive#155 / PR #157 added ``Event.session_id`` and
harmonograf#63 switched server ingest to route goldfive events by
that per-event id, :class:`HarmonografTelemetryPlugin` stamps the
per-ctx **ADK session id** on every span rather than rolling every
span onto the Client's home (Hello) session. Goldfive events now
carry their own session_id on the wire, so the server lands spans
and goldfive events on the same ADK session without the plugin
having to collapse everything onto the home stream.

These tests replace the harmonograf#61 home-session-rollup assertions
with the restored pre-#61 contract:

1. The wire-level ``session_id`` field on the Span is the per-ctx ADK
   session id (``ctx.session.id``), NOT the Client's home session.
2. The ADK session id also rides as the ``adk.session_id`` attribute
   on INVOCATION spans so routing-forensics stays possible — this is
   the forensic hook from harmonograf#62 and is preserved verbatim.
3. Degraded ctx (no session) falls back to the Client's home session
   so the plugin never crashes pre-connect / in offline replays.
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
# Tests — per-ctx ADK session stamping (the restored pre-#61 contract)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_run_stamps_adk_session_on_span(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """INVOCATION span's wire ``session_id`` is the per-ctx ADK session,
    not the Client's home session. After goldfive#155, goldfive events
    also carry their own per-event ``session_id``, so the server will
    land both kinds on the same ADK session without the plugin having
    to collapse to a process-global home id.
    """
    ctx = _InvocationContext(invocation_id="inv-1", session_id=ADK_SESSION)
    await plugin.before_run_callback(invocation_context=ctx)
    assert _span_session_ids(client) == [ADK_SESSION]


@pytest.mark.asyncio
async def test_before_model_stamps_adk_session_on_span(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    cb = _CallbackContext(invocation_id="inv-1", session_id=ADK_SESSION)
    await plugin.before_model_callback(callback_context=cb, llm_request=_LlmRequest())
    assert _span_session_ids(client) == [ADK_SESSION]


@pytest.mark.asyncio
async def test_before_tool_stamps_adk_session_on_span(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    tc = _CallbackContext(invocation_id="inv-1", session_id=ADK_SESSION)
    await plugin.before_tool_callback(
        tool=_Tool("search"), tool_args={"q": "hi"}, tool_context=tc
    )
    assert _span_session_ids(client) == [ADK_SESSION]


@pytest.mark.asyncio
async def test_full_invocation_spans_all_carry_ctx_session(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Three callbacks fire across one ADK invocation; all three spans
    land on the ctx's ADK session (not the Client home session)."""
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
async def test_two_adk_sessions_keep_separate_span_session_ids(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Two distinct ADK sessions (e.g. an AgentTool spawning a fresh
    sub-Runner) keep their spans on their own session_ids. This is the
    inverse of the harmonograf#61 rollup assertion — harmonograf#63
    server-side routing for goldfive events makes the rollup
    unnecessary at the plugin layer.
    """
    await plugin.before_run_callback(
        invocation_context=_InvocationContext("inv-A", ADK_SESSION)
    )
    await plugin.before_run_callback(
        invocation_context=_InvocationContext("inv-B", OTHER_SESSION)
    )
    assert _span_session_ids(client) == [ADK_SESSION, OTHER_SESSION]


# ---------------------------------------------------------------------------
# Tests — ADK session preserved as span attribute for forensics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_run_preserves_adk_session_id_as_attribute(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """INVOCATION span's ``adk.session_id`` attribute carries the
    per-ctx ADK session id so debug tooling can correlate a span back
    to the exact sub-Runner ADK session that produced it. Preserved
    from harmonograf#62 — the forensic hook is still useful even now
    that ``span.session_id`` also holds the ADK session id, because
    the attribute survives future routing changes that might repoint
    ``session_id`` elsewhere.
    """
    ctx = _InvocationContext(invocation_id="inv-1", session_id=ADK_SESSION)
    await plugin.before_run_callback(invocation_context=ctx)
    [span] = _span_starts(client)
    attrs = dict(span.attributes or {})
    assert "adk.session_id" in attrs
    assert attrs["adk.session_id"].string_value == ADK_SESSION


@pytest.mark.asyncio
async def test_missing_adk_session_falls_back_to_home_session(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """If ADK runs without a session service (unit-test harnesses,
    offline replays, or a bare ctx whose ``session`` is ``None``),
    ``_adk_session_id`` returns ``""`` and the plugin falls back to
    the Client's home session rather than stamping an empty string.
    The ``adk.session_id`` attribute is omitted (there's nothing to
    stamp).
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
async def test_missing_everything_falls_back_to_empty(
    made: list[FakeTransport],
) -> None:
    """Fully degraded state: the Client has no Hello session assigned
    and the ctx has no session either. The plugin stamps ``""`` rather
    than crashing — ``emit_span_start`` resolves that to the Client's
    default (also empty in this harness) and the server auto-creates a
    home session on first span. The important thing is we don't raise.
    """
    client = Client(
        name="no-home-session",
        agent_id="agent-Y",
        # session_id not provided; transport hasn't assigned yet either.
        buffer_size=64,
        _transport_factory=make_factory(made),
    )
    plugin = HarmonografTelemetryPlugin(client)

    class _Bare:
        invocation_id = "inv-1"
        agent = _Agent("root")
        session = None

    await plugin.before_run_callback(invocation_context=_Bare())
    [span] = _span_starts(client)
    # Neither the ctx nor the client carry a session; span.session_id
    # is whatever emit_span_start's default resolves to (also empty).
    assert span.session_id == ""
