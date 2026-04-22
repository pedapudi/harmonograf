"""Tests for :mod:`harmonograf_client.telemetry_plugin` cancellation
cleanup (goldfive#167).

When an ADK invocation is cancelled mid-flight — asyncio.CancelledError
raises inside ``runner.run_async`` — ADK does NOT fire
``after_run_callback`` / ``after_model_callback`` / ``after_tool_callback``
because those are placed after the ``async with Aclosing(...)`` block
in :meth:`google.adk.runners.Runner._exec_with_plugin`, not in a
``finally``. Without explicit cleanup, every span the plugin opened
would stay ``status=RUNNING`` in the harmonograf DB forever, cluttering
the UI for 20+ minutes after the user cancelled.

The plugin now exposes
:meth:`HarmonografTelemetryPlugin.on_cancellation` which goldfive's
``ADKAdapter`` calls from its ``except asyncio.CancelledError`` branch.
This file pins the behaviour:

* All open spans for the cancelled invocation id emit ``SpanEnd``
  with ``status=CANCELLED``.
* A concurrent sibling invocation is NOT affected.
* The normal (non-cancel) path is unchanged — the existing
  ``after_*`` callbacks remain the single source of ``span_end``.
* Idempotency: calling ``on_cancellation`` twice for the same
  invocation (or after a normal close) is a safe no-op.
"""

from __future__ import annotations

from typing import Any

import pytest

from harmonograf_client.buffer import EnvelopeKind
from harmonograf_client.client import Client
from harmonograf_client.enums import SpanStatus
from harmonograf_client.telemetry_plugin import HarmonografTelemetryPlugin

from tests._fixtures import FakeTransport, make_factory


# ---------------------------------------------------------------------------
# ADK-shaped stand-ins (minimal surface used by the plugin)
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
    def __init__(self, invocation_id: str, session_id: str = "adk-sess") -> None:
        self.invocation_id = invocation_id
        self.session = _Session(session_id)


class _ToolContext:
    def __init__(self, invocation_id: str, session_id: str = "adk-sess") -> None:
        self.invocation_id = invocation_id
        self.session = _Session(session_id)


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class _LlmRequest:
    def __init__(self, model: str = "gpt-test") -> None:
        self.model = model


HOME_SESSION = "home-sess"
ROOT_SESSION = "adk-sess-root"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="cancel-test",
        agent_id="agent-C",
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


def _span_end_statuses(client: Client) -> list[int]:
    return [se.status for se in _span_ends(client)]


# ---------------------------------------------------------------------------
# Normal path still works (regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plugin_closes_spans_on_after_run_callback_success(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Normal completion path: ``after_run_callback`` closes the run span
    with ``status=COMPLETED``. Must be unchanged by the cancel-cleanup
    addition.
    """
    ctx = _InvocationContext("inv-ok", ROOT_SESSION)
    await plugin.before_run_callback(invocation_context=ctx)
    await plugin.after_run_callback(invocation_context=ctx)

    ends = _span_ends(client)
    assert len(ends) == 1
    # Completed status code is 3 (see proto); exact mapping is
    # validated in the transport protocol tests. Here we assert it's
    # NOT the CANCELLED code.
    assert ends[0].status != plugin.client._resolve_status(SpanStatus.CANCELLED)


# ---------------------------------------------------------------------------
# Cancel path closes all open spans
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plugin_closes_open_run_span_on_cancel(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """A run span that was started but never got its paired
    ``after_run_callback`` (ADK's CancelledError path) must be closed
    with ``status=CANCELLED`` by ``on_cancellation``.
    """
    ctx = _InvocationContext("inv-cancel", ROOT_SESSION)
    await plugin.before_run_callback(invocation_context=ctx)
    # Simulate cancellation: after_run_callback never fires. Goldfive
    # invokes on_cancellation instead.
    plugin.on_cancellation("inv-cancel")

    ends = _span_ends(client)
    assert len(ends) == 1
    cancelled = plugin.client._resolve_status(SpanStatus.CANCELLED)
    assert ends[0].status == cancelled


@pytest.mark.asyncio
async def test_plugin_closes_model_and_tool_spans_on_cancel(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Nested tracking maps are cleaned up: an in-flight LLM span and an
    in-flight tool span for the cancelled invocation both emit
    ``SpanEnd(status=CANCELLED)``.
    """
    ctx = _InvocationContext("inv-nested", ROOT_SESSION)
    await plugin.before_run_callback(invocation_context=ctx)

    # Open an LLM span mid-flight.
    llm_ctx = _CallbackContext(invocation_id="inv-nested", session_id=ROOT_SESSION)
    await plugin.before_model_callback(
        callback_context=llm_ctx, llm_request=_LlmRequest("gpt-4")
    )

    # Open a tool span mid-flight.
    tool_ctx = _ToolContext(invocation_id="inv-nested", session_id=ROOT_SESSION)
    await plugin.before_tool_callback(
        tool=_Tool("search"), tool_args={"q": "x"}, tool_context=tool_ctx
    )

    # Drain the start envelopes so we don't count them as ends.
    _ = [env for env in client._events.drain() if env.kind is EnvelopeKind.SPAN_END]

    # Now cancel.
    plugin.on_cancellation("inv-nested")

    ends = _span_ends(client)
    # Three spans: run, llm, tool. All should be CANCELLED.
    assert len(ends) == 3
    cancelled = plugin.client._resolve_status(SpanStatus.CANCELLED)
    assert all(se.status == cancelled for se in ends)


@pytest.mark.asyncio
async def test_invocation_id_scoping(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Cancelling invocation A does NOT close invocation B's spans.

    Concurrent invocations may share the plugin instance (multi-agent
    trees, parallel fan-out). ``on_cancellation(invocation_id)`` must
    filter by invocation so sibling spans are untouched.
    """
    ctx_a = _InvocationContext("inv-A", ROOT_SESSION)
    ctx_b = _InvocationContext("inv-B", ROOT_SESSION)
    await plugin.before_run_callback(invocation_context=ctx_a)
    await plugin.before_run_callback(invocation_context=ctx_b)

    # Open LLM + tool spans on B to make sure they survive.
    llm_b = _CallbackContext(invocation_id="inv-B", session_id=ROOT_SESSION)
    await plugin.before_model_callback(
        callback_context=llm_b, llm_request=_LlmRequest("gpt-4")
    )
    tool_b = _ToolContext(invocation_id="inv-B", session_id=ROOT_SESSION)
    await plugin.before_tool_callback(
        tool=_Tool("search"), tool_args={"q": "x"}, tool_context=tool_b
    )

    # Drain starts.
    _ = [env for env in client._events.drain() if env.kind is EnvelopeKind.SPAN_END]

    # Cancel ONLY A.
    plugin.on_cancellation("inv-A")

    # Exactly one SPAN_END — for A's run span.
    ends = _span_ends(client)
    assert len(ends) == 1

    # B's spans are still open: after-callbacks on B should produce
    # normal COMPLETED ends.
    assert "inv-B" in plugin._invocation_spans
    assert "inv-B" in plugin._model_spans
    assert id(tool_b) in plugin._tool_spans


@pytest.mark.asyncio
async def test_on_cancellation_is_idempotent(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Calling ``on_cancellation`` twice for the same invocation is a
    no-op on the second call. Also safe after a normal successful
    close.
    """
    ctx = _InvocationContext("inv-idem", ROOT_SESSION)
    await plugin.before_run_callback(invocation_context=ctx)
    plugin.on_cancellation("inv-idem")
    # Second call: should not emit another SpanEnd.
    plugin.on_cancellation("inv-idem")
    ends = _span_ends(client)
    assert len(ends) == 1

    # Also: calling on_cancellation after a normal successful close
    # emits nothing.
    ctx2 = _InvocationContext("inv-ok", ROOT_SESSION)
    await plugin.before_run_callback(invocation_context=ctx2)
    await plugin.after_run_callback(invocation_context=ctx2)
    # Drain starts and the normal end.
    _ = _span_ends(client)
    plugin.on_cancellation("inv-ok")
    assert _span_ends(client) == []


@pytest.mark.asyncio
async def test_on_cancellation_empty_invocation_id_is_noop(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Defensive: empty invocation id never emits anything, even if
    spans exist under other keys.
    """
    ctx = _InvocationContext("inv-present", ROOT_SESSION)
    await plugin.before_run_callback(invocation_context=ctx)
    _ = _span_ends(client)
    plugin.on_cancellation("")
    assert _span_ends(client) == []
    assert "inv-present" in plugin._invocation_spans


@pytest.mark.asyncio
async def test_cancel_clears_root_session_cache_for_root_invocation(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Cancelling the ROOT invocation releases the cached root session
    id (and the per-session control subscription). Without this, the
    next adk-web run after a user cancel would still stamp spans onto
    the prior root's session id.
    """
    ctx = _InvocationContext("inv-root", ROOT_SESSION)
    await plugin.before_run_callback(invocation_context=ctx)
    assert plugin._root_session_id == ROOT_SESSION
    assert plugin._root_invocation_id == "inv-root"

    plugin.on_cancellation("inv-root")
    assert plugin._root_session_id is None
    assert plugin._root_invocation_id is None


@pytest.mark.asyncio
async def test_cancel_sub_invocation_does_not_clear_root_cache(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Cancelling a sub-Runner / AgentTool invocation must NOT clear
    the ROOT cache — the root invocation is still live.
    """
    root_ctx = _InvocationContext("inv-root", ROOT_SESSION)
    await plugin.before_run_callback(invocation_context=root_ctx)
    sub_ctx = _InvocationContext("inv-sub", "adk-sess-sub-runner")
    await plugin.before_run_callback(invocation_context=sub_ctx)

    plugin.on_cancellation("inv-sub")

    # Root cache untouched.
    assert plugin._root_session_id == ROOT_SESSION
    assert plugin._root_invocation_id == "inv-root"
    # Sub invocation's span is gone.
    assert "inv-sub" not in plugin._invocation_spans


@pytest.mark.asyncio
async def test_on_cancellation_never_raises_when_transport_errors(
    plugin: HarmonografTelemetryPlugin, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observability must not propagate exceptions into the cancel
    path. If ``emit_span_end`` itself raises (transport dropped,
    protobuf error), ``on_cancellation`` swallows and logs.
    """
    ctx = _InvocationContext("inv-boom", ROOT_SESSION)
    await plugin.before_run_callback(invocation_context=ctx)
    _ = _span_ends(client)

    def _raise(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("transport offline")

    monkeypatch.setattr(client, "emit_span_end", _raise)

    # Must not raise.
    plugin.on_cancellation("inv-boom")
