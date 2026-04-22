"""Regression tests for per-span ``session_id`` stamping.

Under overlay + ``AgentTool``, ADK rebuilds the ``CallbackContext`` per
sub-invocation and sub-Runners mint their own ``InMemorySessionService``
session ids. Stamping the per-ctx id on every span fanned each adk-web
run out across N harmonograf sessions — one per sub-Runner — while the
plan view (which carries goldfive ``Session.id``, itself pinned to the
outer adk-web session via goldfive#161) sat alone on the root.

harmonograf#65 / goldfive#161 collapses this fan-out at the plugin
layer: :class:`HarmonografTelemetryPlugin` caches the ROOT
``ctx.session.id`` on the first ``before_run_callback`` and stamps it
on every subsequent span (including sub-Runner callbacks whose per-ctx
session id differs). Result: all spans co-locate on the outer adk-web
session, matching where goldfive events already land.

Contract preserved:

1. The cached root session id is stamped on every span produced during
   that run — root invocation, sub-Runner invocations, model calls, tool
   calls alike.
2. The ADK session id still rides as the ``adk.session_id`` span
   attribute on INVOCATION spans for forensic lookups
   (harmonograf#62 hook — unchanged).
3. After the root's ``after_run_callback`` fires, the cache is cleared
   so the next adk-web invocation picks up its own root id.
4. Pre-connect / offline / bare-ctx degraded paths still work: no
   cached root → fall back to per-ctx ADK session id → fall back to
   the Client's home session → fall back to ``""``.
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


ROOT_SESSION = "adk-sess-root-abc"
SUB_RUNNER_SESSION = "adk-sess-subrunner-xyz"
OTHER_ROOT = "adk-sess-other-root"
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
# Tests — root session caching + rollup of sub-Runner spans.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_run_caches_root_session_id(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """The first ``before_run_callback`` caches ``ctx.session.id`` as
    the ROOT session id and stamps it on its own INVOCATION span."""
    ctx = _InvocationContext(invocation_id="inv-root", session_id=ROOT_SESSION)
    await plugin.before_run_callback(invocation_context=ctx)
    assert _span_session_ids(client) == [ROOT_SESSION]
    assert plugin._root_session_id == ROOT_SESSION


@pytest.mark.asyncio
async def test_plugin_caches_root_session_id_on_first_before_run_callback(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Root before_run then sub-Runner before_run: both spans land on ROOT.

    Real adk-web firing order: root invocation starts, an AgentTool
    inside the root spawns a sub-Runner whose own
    ``before_run_callback`` fires against the plugin (ADK propagates
    the plugin manager into sub-Runners). The sub-Runner's ctx.session
    is a fresh InMemorySessionService session — its id differs. The
    plugin rolls the sub-Runner span up onto the cached root.
    """
    await plugin.before_run_callback(
        invocation_context=_InvocationContext("inv-root", ROOT_SESSION)
    )
    # Sub-Runner invocation fires NEXT — with a different session id.
    await plugin.before_run_callback(
        invocation_context=_InvocationContext("inv-sub", SUB_RUNNER_SESSION)
    )
    # Both spans land on the ROOT session (rollup).
    assert _span_session_ids(client) == [ROOT_SESSION, ROOT_SESSION]


@pytest.mark.asyncio
async def test_sub_runner_model_and_tool_spans_roll_up_to_root(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Model and tool spans fired inside a sub-Runner stamp ROOT too.

    The plugin's ``_stamp_session_id`` path is exercised from every
    callback — ``before_model`` / ``before_tool`` must use the cached
    root even when their per-ctx session id is the sub-Runner's id.
    """
    # Root-of-tree invocation primes the cache.
    await plugin.before_run_callback(
        invocation_context=_InvocationContext("inv-root", ROOT_SESSION)
    )
    # Sub-Runner emits a model call and a tool call.
    sub_cb = _CallbackContext("inv-sub", SUB_RUNNER_SESSION)
    sub_tc = _CallbackContext("inv-sub", SUB_RUNNER_SESSION)
    await plugin.before_model_callback(callback_context=sub_cb, llm_request=_LlmRequest())
    await plugin.before_tool_callback(
        tool=_Tool("search"), tool_args={"q": "hi"}, tool_context=sub_tc
    )
    sids = _span_session_ids(client)
    assert sids == [ROOT_SESSION, ROOT_SESSION, ROOT_SESSION], (
        f"expected every span on ROOT; got {sids!r}"
    )


@pytest.mark.asyncio
async def test_plugin_clears_root_session_on_after_run_callback(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """After the ROOT's ``after_run_callback`` fires, the cache clears
    and the NEXT root invocation captures its own session id."""
    root = _InvocationContext("inv-root", ROOT_SESSION)
    await plugin.before_run_callback(invocation_context=root)
    await plugin.after_run_callback(invocation_context=root)
    assert plugin._root_session_id is None

    # Next adk-web invocation arrives with a different session id.
    await plugin.before_run_callback(
        invocation_context=_InvocationContext("inv-root-2", OTHER_ROOT)
    )
    assert plugin._root_session_id == OTHER_ROOT


@pytest.mark.asyncio
async def test_sub_runner_after_run_does_not_clear_root_cache(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """A sub-Runner's ``after_run_callback`` must NOT clear the cache.

    ADK fires one before_run/after_run pair per sub-Runner invocation.
    If we cleared on the first sub-Runner's after_run, later sub-Runner
    callbacks + the root's own after_run would see an empty cache,
    shattering the rollup. The clear is gated on matching the root's
    invocation_id.
    """
    root = _InvocationContext("inv-root", ROOT_SESSION)
    sub = _InvocationContext("inv-sub", SUB_RUNNER_SESSION)
    await plugin.before_run_callback(invocation_context=root)
    await plugin.before_run_callback(invocation_context=sub)
    # Sub-Runner finishes FIRST.
    await plugin.after_run_callback(invocation_context=sub)
    assert plugin._root_session_id == ROOT_SESSION, (
        "sub-Runner after_run must not clear the cached root id"
    )


@pytest.mark.asyncio
async def test_before_run_preserves_adk_session_id_as_attribute(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """The per-ctx ADK session id still rides as the ``adk.session_id``
    span attribute for forensics (harmonograf#62 hook — preserved).

    Under the rollup contract, two pieces of session info coexist on
    an INVOCATION span: ``span.session_id`` (= cached root) is the
    routing key; ``adk.session_id`` (= per-ctx) is the exact
    sub-Runner session whose callback produced the span.
    """
    # Root span: attribute equals cached root id.
    await plugin.before_run_callback(
        invocation_context=_InvocationContext("inv-root", ROOT_SESSION)
    )
    # Sub-Runner span: attribute equals sub-Runner id (forensic).
    await plugin.before_run_callback(
        invocation_context=_InvocationContext("inv-sub", SUB_RUNNER_SESSION)
    )
    spans = _span_starts(client)
    assert len(spans) == 2
    root_attr = dict(spans[0].attributes or {})
    sub_attr = dict(spans[1].attributes or {})
    assert root_attr["adk.session_id"].string_value == ROOT_SESSION
    assert sub_attr["adk.session_id"].string_value == SUB_RUNNER_SESSION


@pytest.mark.asyncio
async def test_plugin_falls_back_to_ctx_session_when_no_root_cached(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Standalone callback (no ``before_run_callback`` first) still works.

    Unit-test harnesses and offline replays may drive individual
    callbacks without ever firing a ``before_run_callback``. In that
    mode the cache stays ``None`` and the plugin falls back to the
    per-ctx ADK session id — backward-compatible with the pre-rollup
    shape.
    """
    assert plugin._root_session_id is None
    cb = _CallbackContext("inv-1", SUB_RUNNER_SESSION)
    await plugin.before_model_callback(callback_context=cb, llm_request=_LlmRequest())
    assert _span_session_ids(client) == [SUB_RUNNER_SESSION]


@pytest.mark.asyncio
async def test_missing_adk_session_falls_back_to_home_session(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Bare ctx (no session) with no cached root stamps the Client's home.

    Matches the pre-rollup contract: if nothing upstream told us a
    session id, harmonograf routes to the home (Hello-assigned) session.
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
    # Bare ctx doesn't prime the cache — session.id was unreadable.
    assert plugin._root_session_id is None


@pytest.mark.asyncio
async def test_missing_everything_falls_back_to_empty(
    made: list[FakeTransport],
) -> None:
    """Fully degraded state: no Hello session + no ctx session + no cache.

    Client has no home session; ctx has no session; plugin cache empty.
    The plugin stamps ``""`` rather than crashing —
    ``emit_span_start`` resolves that to the Client's default (also
    empty in this harness) and the server auto-creates a home session
    on first span.
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
    assert span.session_id == ""


# ---------------------------------------------------------------------------
# Tests — additional control subscription lifecycle.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_run_opens_additional_control_subscription(
    plugin: HarmonografTelemetryPlugin,
    client: Client,
    made: list[FakeTransport],
) -> None:
    """The plugin opens a per-session control sub on the cached root
    session id (goldfive#162). Exactly ONE extra sub per adk-web run —
    not one per sub-Runner."""
    root = _InvocationContext("inv-root", ROOT_SESSION)
    sub = _InvocationContext("inv-sub", SUB_RUNNER_SESSION)
    await plugin.before_run_callback(invocation_context=root)
    # Sub-Runner's before_run must not open another sub.
    await plugin.before_run_callback(invocation_context=sub)

    [transport] = made
    assert transport.opened_session_subs == [ROOT_SESSION], (
        "expected exactly one additional control sub on root session; "
        f"saw {transport.opened_session_subs!r}"
    )


@pytest.mark.asyncio
async def test_after_run_closes_additional_control_subscription(
    plugin: HarmonografTelemetryPlugin,
    client: Client,
    made: list[FakeTransport],
) -> None:
    """The plugin tears down the additional sub when the ROOT ends."""
    root = _InvocationContext("inv-root", ROOT_SESSION)
    await plugin.before_run_callback(invocation_context=root)
    await plugin.after_run_callback(invocation_context=root)

    [transport] = made
    assert transport.opened_session_subs == [ROOT_SESSION]
    assert transport.closed_session_subs == [ROOT_SESSION]


@pytest.mark.asyncio
async def test_sub_runner_after_run_does_not_close_additional_sub(
    plugin: HarmonografTelemetryPlugin,
    client: Client,
    made: list[FakeTransport],
) -> None:
    """A sub-Runner's after_run must not tear down the root's extra sub."""
    root = _InvocationContext("inv-root", ROOT_SESSION)
    sub = _InvocationContext("inv-sub", SUB_RUNNER_SESSION)
    await plugin.before_run_callback(invocation_context=root)
    await plugin.before_run_callback(invocation_context=sub)
    # Sub-Runner finishes first.
    await plugin.after_run_callback(invocation_context=sub)

    [transport] = made
    assert transport.closed_session_subs == [], (
        "sub-Runner after_run must not close the root-session sub"
    )


@pytest.mark.asyncio
async def test_no_additional_sub_for_bare_ctx(
    plugin: HarmonografTelemetryPlugin,
    client: Client,
    made: list[FakeTransport],
) -> None:
    """When ctx has no session id, no extra sub is opened."""

    class _Bare:
        invocation_id = "inv-1"
        agent = _Agent("root")
        session = None

    await plugin.before_run_callback(invocation_context=_Bare())
    [transport] = made
    assert transport.opened_session_subs == []
