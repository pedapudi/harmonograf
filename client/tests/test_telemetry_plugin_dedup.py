"""Tests for duplicate-install dedup in
:class:`harmonograf_client.telemetry_plugin.HarmonografTelemetryPlugin`.

Scenario covered (harmonograf #68 / goldfive #166):

Under ``goldfive.wrap`` + ``adk web`` it is easy to install two
``HarmonografTelemetryPlugin`` instances on the same ADK plugin manager:
one from ``App(plugins=[...])`` and one from ``observe()`` /
``add_plugin``. Each instance was firing every callback and emitting
its own span-start / span-end envelopes, so every agent / model / tool
span showed up twice in the harmonograf Gantt.

The fix is idempotent detection at callback-firing time: when a plugin
instance sees that another plugin of the same ``name`` sits earlier in
``ctx.plugin_manager.plugins``, it sets ``_disabled_as_duplicate`` and
short-circuits all further callbacks. The tests assert:

* two installations → exactly one set of spans
* single installation → unchanged behaviour
* dedup applies across every callback shape (run / model / tool)
* the dedup is a SOFT check — missing / malformed plugin_manager falls
  through to "enabled"
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from harmonograf_client.buffer import EnvelopeKind
from harmonograf_client.client import Client
from harmonograf_client.telemetry_plugin import HarmonografTelemetryPlugin

from tests._fixtures import FakeTransport, make_factory


# ---------------------------------------------------------------------------
# Minimal ADK-shaped fakes
# ---------------------------------------------------------------------------


class _Session:
    def __init__(self, sid: str) -> None:
        self.id = sid


class _Agent:
    def __init__(self, name: str) -> None:
        self.name = name


class _PluginManager:
    """Mirrors :class:`google.adk.plugins.plugin_manager.PluginManager` just
    enough for the dedup path — a list of plugins accessible via
    ``.plugins``."""

    def __init__(self, plugins: list[Any]) -> None:
        self.plugins = plugins


class _InvocationContext:
    def __init__(
        self,
        invocation_id: str,
        session_id: str,
        plugins: list[Any],
    ) -> None:
        self.invocation_id = invocation_id
        self.session = _Session(session_id)
        self.agent = _Agent("root-agent")
        self.plugin_manager = _PluginManager(plugins)


class _CallbackContext:
    def __init__(
        self, invocation_id: str, session_id: str, plugins: list[Any]
    ) -> None:
        self.invocation_id = invocation_id
        self.session = _Session(session_id)
        self.plugin_manager = _PluginManager(plugins)


class _ToolContext:
    def __init__(
        self, invocation_id: str, session_id: str, plugins: list[Any]
    ) -> None:
        self.invocation_id = invocation_id
        self.session = _Session(session_id)
        self.plugin_manager = _PluginManager(plugins)


class _Tool:
    def __init__(self, name: str = "run_tool") -> None:
        self.name = name


class _LlmRequest:
    def __init__(self, model: str = "gpt-test") -> None:
        self.model = model


class _LlmResponse:
    def __init__(self) -> None:
        self.partial = False
        self.error_message = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="dedup-test",
        agent_id="agent-D",
        session_id="home-sess",
        buffer_size=64,
        _transport_factory=make_factory(made),
    )


def _span_start_count(client: Client) -> int:
    return sum(
        1 for env in client._events.drain() if env.kind is EnvelopeKind.SPAN_START
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_install_suppresses_second_instance(
    client: Client,
) -> None:
    """Two plugin instances sharing a plugin_manager → only one set of spans.

    Reproduces the harmonograf #68 / goldfive #166 pathology. Without
    dedup, each of the two instances fires every callback and emits a
    span-start. With dedup, the later instance detects itself as a
    duplicate and stays silent.
    """
    first = HarmonografTelemetryPlugin(client)
    second = HarmonografTelemetryPlugin(client)
    # App(plugins=[first]) then observe()/add_plugin appends `second`.
    plugins = [first, second]

    ctx = _InvocationContext("inv-1", "sess-1", plugins)
    await first.before_run_callback(invocation_context=ctx)
    await second.before_run_callback(invocation_context=ctx)

    # First instance fires normally (1 span-start); second short-circuits.
    assert _span_start_count(client) == 1
    assert second._disabled_as_duplicate is True
    assert first._disabled_as_duplicate is False


@pytest.mark.asyncio
async def test_single_install_unchanged(client: Client) -> None:
    """Single-install regression: plugin fires as before."""
    plugin = HarmonografTelemetryPlugin(client)
    ctx = _InvocationContext("inv-1", "sess-1", [plugin])

    await plugin.before_run_callback(invocation_context=ctx)
    assert _span_start_count(client) == 1
    assert plugin._disabled_as_duplicate is False


@pytest.mark.asyncio
async def test_dedup_applies_to_every_callback_shape(client: Client) -> None:
    """The dedup guard covers run / model / tool callbacks uniformly.

    Without uniform coverage a duplicate instance could stay silent on
    before_run but still emit spans on before_model / before_tool when
    those callbacks sometimes fire without a matching before_run (nested
    AgentTool sub-Runners rebuild CallbackContext mid-flight).
    """
    first = HarmonografTelemetryPlugin(client)
    second = HarmonografTelemetryPlugin(client)
    plugins = [first, second]

    inv_ctx = _InvocationContext("inv-1", "sess-1", plugins)
    cb_ctx = _CallbackContext("inv-1", "sess-1", plugins)
    tool_ctx = _ToolContext("inv-1", "sess-1", plugins)

    # Fire every pre-callback on both plugins.
    await first.before_run_callback(invocation_context=inv_ctx)
    await second.before_run_callback(invocation_context=inv_ctx)

    await first.before_model_callback(
        callback_context=cb_ctx, llm_request=_LlmRequest()
    )
    await second.before_model_callback(
        callback_context=cb_ctx, llm_request=_LlmRequest()
    )

    await first.before_tool_callback(
        tool=_Tool(), tool_args={}, tool_context=tool_ctx
    )
    await second.before_tool_callback(
        tool=_Tool(), tool_args={}, tool_context=tool_ctx
    )

    # 3 callbacks * 1 effective emitter = 3 span-starts (run/model/tool),
    # not 6. The duplicate instance stays silent on every path.
    assert _span_start_count(client) == 3


@pytest.mark.asyncio
async def test_dedup_silent_fallthrough_on_missing_plugin_manager(
    client: Client, caplog: pytest.LogCaptureFixture
) -> None:
    """When plugin_manager is missing, the plugin stays enabled.

    Unit-test harnesses, offline replay, and any context shape that
    omits plugin_manager must continue to work — the dedup path is an
    optimistic guard, not a hard gate.
    """
    plugin = HarmonografTelemetryPlugin(client)

    class _Bare:
        invocation_id = "inv-1"
        session = _Session("sess-1")
        agent = _Agent("root")
        # Note: no plugin_manager attribute.

    with caplog.at_level(logging.INFO, logger="harmonograf_client.telemetry_plugin"):
        await plugin.before_run_callback(invocation_context=_Bare())

    assert _span_start_count(client) == 1
    assert plugin._disabled_as_duplicate is False
    # No INFO log about dedup emitted on the healthy path.
    assert not any("duplicate" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_dedup_logs_once_per_deduped_instance(
    client: Client, caplog: pytest.LogCaptureFixture
) -> None:
    """A deduped instance logs at INFO exactly once so operators can see it.

    Hot callback paths fire many times per run; the dedup log must be
    emitted at most once per plugin instance or it'd become noise.
    """
    first = HarmonografTelemetryPlugin(client)
    second = HarmonografTelemetryPlugin(client)
    plugins = [first, second]
    ctx = _InvocationContext("inv-1", "sess-1", plugins)

    with caplog.at_level(logging.INFO, logger="harmonograf_client.telemetry_plugin"):
        await second.before_run_callback(invocation_context=ctx)
        await second.before_run_callback(invocation_context=ctx)
        await second.before_run_callback(invocation_context=ctx)

    dedup_records = [r for r in caplog.records if "duplicate" in r.getMessage()]
    assert len(dedup_records) == 1
    assert dedup_records[0].levelno == logging.INFO


@pytest.mark.asyncio
async def test_first_instance_is_always_the_survivor(client: Client) -> None:
    """Whichever instance appears first in ``plugins`` is the authoritative emitter.

    Defends against registration-order drift: if ``observe()`` runs
    before ``App(plugins=[...])`` attach, the ``observe()`` instance is
    the first entry in the list and should be the one that fires.
    """
    a = HarmonografTelemetryPlugin(client)
    b = HarmonografTelemetryPlugin(client)

    # a registered first; b is the dupe.
    ctx_a = _InvocationContext("inv-1", "sess-1", [a, b])
    await b.before_run_callback(invocation_context=ctx_a)
    await a.before_run_callback(invocation_context=ctx_a)
    assert b._disabled_as_duplicate is True
    assert a._disabled_as_duplicate is False
    assert _span_start_count(client) == 1

    # Flip the registration order: a fresh pair in reverse order.
    c = HarmonografTelemetryPlugin(client)
    d = HarmonografTelemetryPlugin(client)
    ctx_b = _InvocationContext("inv-2", "sess-2", [d, c])
    await c.before_run_callback(invocation_context=ctx_b)
    await d.before_run_callback(invocation_context=ctx_b)
    # d registered first; c is the dupe this time.
    assert c._disabled_as_duplicate is True
    assert d._disabled_as_duplicate is False
    assert _span_start_count(client) == 1
