"""Tests for :meth:`HarmonografTelemetryPlugin.on_run_end` (harmonograf#96
/ goldfive#196).

When the outer :class:`goldfive.adapters.adk_wrap.GoldfiveADKAgent`
exits its ``_run_async_impl`` generator (normal completion, upstream
cancel, or ``aclose()``), it calls ``plugin.on_run_end()`` on every
adapter plugin. This plugin's implementation sweeps all still-open
INVOCATION / model / tool spans the plugin tracks and flushes them with
``status=COMPLETED``.

Why it's needed even with :meth:`on_cancellation`:
``on_cancellation`` is scoped to a single ``invocation_id`` — the
outer run. Sub-Runner invocations spawned by ADK's ``AgentTool`` have
their own ``invocation_id``, so a cancel on the outer invocation does
not reach the sub-Runner's open INVOCATION span. That span leaks
``status=RUNNING`` forever in the harmonograf DB, and the frontend's
"LIVE ACTIVITY · N RUNNING" header stays pinned. ``on_run_end`` is the
broader sweep that closes every tracked span regardless of which
invocation opened it.
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


class _Agent:
    def __init__(self, name: str) -> None:
        self.name = name


class _InvocationContext:
    def __init__(self, invocation_id: str, session_id: str) -> None:
        self.invocation_id = invocation_id
        self.session = _Session(session_id)
        self.agent = _Agent("root-agent")


HOME_SESSION = "home-sess"
ROOT_SESSION = "adk-sess-root"


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="on-run-end-test",
        agent_id="agent-ORE",
        session_id=HOME_SESSION,
        buffer_size=128,
        _transport_factory=make_factory(made),
    )


@pytest.fixture
def plugin(client: Client) -> HarmonografTelemetryPlugin:
    return HarmonografTelemetryPlugin(client)


def _span_ends(client: Client) -> list[Any]:
    out: list[Any] = []
    for env in client._events.drain():
        if env.kind is EnvelopeKind.SPAN_END:
            out.append(env.payload)
    return out


# ---------------------------------------------------------------------------
# on_run_end closes orphan INVOCATION spans
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_run_end_closes_orphan_invocation_span(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """An INVOCATION span opened by a sub-Runner whose
    ``after_run_callback`` never fired must be closed by ``on_run_end``.
    """
    # Simulate a sub-Runner invocation (ADK AgentTool)
    sub_ctx = _InvocationContext("inv-sub", "sub-adk-sess")
    await plugin.before_run_callback(invocation_context=sub_ctx)

    # Outer run ends (e.g. coordinator returns). We intentionally do NOT
    # call ``after_run_callback`` for the sub-Runner — this is exactly
    # the ADK callback gap the fix addresses.
    plugin.on_run_end()

    ends = _span_ends(client)
    assert len(ends) == 1, "orphan INVOCATION span must be swept"


@pytest.mark.asyncio
async def test_on_run_end_closes_multiple_orphan_invocation_spans(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Multiple orphaned sub-Runner invocations (parallel AgentTool
    tree) must all be swept.
    """
    for inv in ("inv-a", "inv-b", "inv-c"):
        ctx = _InvocationContext(inv, f"{inv}-sess")
        await plugin.before_run_callback(invocation_context=ctx)

    plugin.on_run_end()
    ends = _span_ends(client)
    assert len(ends) == 3


@pytest.mark.asyncio
async def test_on_run_end_after_normal_close_is_noop(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """When every ``after_run_callback`` fires normally, ``on_run_end``
    must be a no-op (the open maps are already empty).
    """
    ctx = _InvocationContext("inv-ok", ROOT_SESSION)
    await plugin.before_run_callback(invocation_context=ctx)
    await plugin.after_run_callback(invocation_context=ctx)
    before = _span_ends(client)
    assert len(before) == 1

    # Drain the fixture buffer so the assertion below counts only
    # spans emitted by the subsequent ``on_run_end`` (if any).
    client._events.drain()
    plugin.on_run_end()
    assert _span_ends(client) == []


@pytest.mark.asyncio
async def test_on_run_end_is_idempotent(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Calling ``on_run_end`` twice must not double-emit span ends."""
    ctx = _InvocationContext("inv-dup", ROOT_SESSION)
    await plugin.before_run_callback(invocation_context=ctx)

    plugin.on_run_end()
    first_count = len(_span_ends(client))
    plugin.on_run_end()
    second_count = len(_span_ends(client))
    assert first_count == 1
    assert second_count == 0


@pytest.mark.asyncio
async def test_on_run_end_never_raises_on_transport_error(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Transport failures inside emit_span_end must be swallowed —
    observability must not break the main run path.
    """
    ctx = _InvocationContext("inv-boom", ROOT_SESSION)
    await plugin.before_run_callback(invocation_context=ctx)

    original = client.emit_span_end

    def exploder(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("transport exploded")

    client.emit_span_end = exploder  # type: ignore[assignment]
    try:
        plugin.on_run_end()  # must not raise
    finally:
        client.emit_span_end = original  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_on_run_end_with_no_open_spans_is_noop(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Hook fired before any ``before_run_callback`` is safe."""
    plugin.on_run_end()
    assert _span_ends(client) == []
