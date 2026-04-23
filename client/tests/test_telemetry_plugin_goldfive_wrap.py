"""Regression tests for the goldfive.wrap wrapper-agent detection path.

Under ``goldfive.wrap``, ADK fires ``before_run_callback`` TWICE for
the same logical agent:

1. The outer adk-web Runner fires it against ``GoldfiveADKAgent``,
   whose ``_run_async_impl`` delegates to goldfive's internal
   ``InMemoryRunner`` around the user's real root agent.
2. goldfive's inner Runner then fires it against the real root agent.

``GoldfiveADKAgent`` copies its inner's ``name`` through, so from the
plugin's perspective both invocations look like the same agent. Before
harmonograf#113, this produced two INVOCATION spans on the same
coordinator row — the outer one leaked as RUNNING on user cancel
(``on_cancellation`` only closes one of the two invocation_ids) and
confused Drawer span selection because the OUTER span has no LLM
children (all real work happens inside the inner).

The fix detects ``GoldfiveADKAgent`` via class name
(``type(agent).__mro__``) and skips the wrapper's INVOCATION entirely
— no span opens, ``after_run`` is a no-op, and the inner Runner's
spans carry the full chain-of-thought.

Class-name detection (instead of ``isinstance``) keeps the client
installable in environments that don't ship goldfive at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from harmonograf_client.buffer import EnvelopeKind
from harmonograf_client.client import Client
from harmonograf_client.telemetry_plugin import HarmonografTelemetryPlugin

from tests._fixtures import FakeTransport, make_factory


class _Session:
    def __init__(self, sid: str) -> None:
        self.id = sid


class _RealAgent:
    """Stands in for the user's real root agent (coordinator_agent)."""

    def __init__(self, name: str = "coordinator_agent") -> None:
        self.name = name


class GoldfiveADKAgent(_RealAgent):
    """Class-name-matching stand-in for the real wrapper.

    Only the class name matters for the detection path; the runtime
    object's behaviour is irrelevant. We subclass ``_RealAgent`` so
    the fake ``ctx.agent.name`` still resolves to the inner's name,
    matching the production wrapper that propagates ``name`` through.
    """

    pass


class _InvocationContext:
    def __init__(self, invocation_id: str, agent: Any) -> None:
        self.invocation_id = invocation_id
        self.session = _Session("sess-wrap")
        self.agent = agent


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="test",
        session_id="home-sess",
        buffer_size=64,
        _transport_factory=make_factory(made),
    )


@pytest.fixture
def plugin(client: Client) -> HarmonografTelemetryPlugin:
    return HarmonografTelemetryPlugin(client)


def _span_starts(client: Client) -> list[Any]:
    return [
        env.payload.span
        for env in client._events.drain()
        if env.kind is EnvelopeKind.SPAN_START
    ]


@pytest.mark.asyncio
async def test_wrapper_before_run_opens_no_span(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    wrapper = GoldfiveADKAgent()
    ctx = _InvocationContext("inv-outer", wrapper)
    await plugin.before_run_callback(invocation_context=ctx)
    # No INVOCATION span was emitted.
    assert _span_starts(client) == []
    # Wrapper invocation is tracked so after_run can short-circuit.
    assert "inv-outer" in plugin._goldfive_wrapper_invocations


@pytest.mark.asyncio
async def test_wrapper_after_run_is_noop(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    wrapper = GoldfiveADKAgent()
    ctx = _InvocationContext("inv-outer", wrapper)
    await plugin.before_run_callback(invocation_context=ctx)
    await plugin.after_run_callback(invocation_context=ctx)
    # after_run drops the tracking flag cleanly.
    assert "inv-outer" not in plugin._goldfive_wrapper_invocations
    # No SpanEnd envelope — nothing to close.
    ends = [
        env
        for env in client._events.drain()
        if env.kind is EnvelopeKind.SPAN_END
    ]
    assert ends == []


@pytest.mark.asyncio
async def test_real_agent_before_run_still_opens_span(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Non-wrapper agent — the common case — still emits an INVOCATION."""
    real = _RealAgent()
    ctx = _InvocationContext("inv-inner", real)
    await plugin.before_run_callback(invocation_context=ctx)
    starts = _span_starts(client)
    assert len(starts) == 1
    assert starts[0].name == "coordinator_agent"


@pytest.mark.asyncio
async def test_wrap_then_inner_emits_exactly_one_span(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """End-to-end: outer wrapper fires, then inner real agent fires.

    The outer should be skipped and the inner should produce the only
    INVOCATION — matching the ``coordinator_agent`` row the Gantt
    should show with a single bar.
    """
    wrapper = GoldfiveADKAgent()
    real = _RealAgent()
    outer_ctx = _InvocationContext("inv-outer", wrapper)
    inner_ctx = _InvocationContext("inv-inner", real)

    await plugin.before_run_callback(invocation_context=outer_ctx)
    await plugin.before_run_callback(invocation_context=inner_ctx)

    starts = _span_starts(client)
    assert len(starts) == 1, (
        f"expected one INVOCATION span, got {len(starts)} — wrapper was "
        "not short-circuited"
    )


@pytest.mark.asyncio
async def test_wrapper_cancel_is_noop(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """``on_cancellation`` on a wrapper invocation must not raise.

    The inner real agent's cancellation path closes the only
    INVOCATION span that actually exists; the wrapper's ``on_cancel``
    is a harmless no-op.
    """
    wrapper = GoldfiveADKAgent()
    ctx = _InvocationContext("inv-outer", wrapper)
    await plugin.before_run_callback(invocation_context=ctx)
    # Should not raise:
    plugin.on_cancellation("inv-outer")
    # Still tracked (cancel doesn't unregister the wrapper flag — that
    # happens on after_run) but no span existed to be closed.
    ends = [
        env
        for env in client._events.drain()
        if env.kind is EnvelopeKind.SPAN_END
    ]
    assert ends == []
