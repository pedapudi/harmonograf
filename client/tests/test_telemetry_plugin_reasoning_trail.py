"""Tests for per-INVOCATION reasoning aggregation (harmonograf#108).

The plugin aggregates reasoning from every ``after_model_callback``
finalize within an invocation and stamps the concatenated trail on the
INVOCATION span's ``SpanEnd`` so clicking an agent row in the Gantt
surfaces the agent's full chain-of-thought — the regression introduced
when ``HarmonografAgent`` was deleted in harmonograf#9 (the old aggregate
lived in ``HarmonografAgent._run_async_impl`` and stamped ``llm.thought``
on the INVOCATION span; that aggregate vanished with the migration to
``goldfive.GoldfiveADKAgent``).

Contract verified here:

* Multiple child LLM calls within an invocation → the INVOCATION span's
  SpanEnd carries ``llm.reasoning_trail`` (concatenated), ``has_reasoning``
  and ``reasoning_call_count``.
* An invocation with no reasoning → the SpanEnd carries no trail attrs
  (no empty noise).
* Large aggregates spill to a ``payload_ref`` with ``role="reasoning"`` —
  matches the per-LLM_CALL large-reasoning shape the Drawer already
  resolves on open.
* Cancellation closes the INVOCATION span with whatever reasoning was
  captured up to the cancel point.
* On-run-end sweep covers orphan INVOCATION spans too.
* Concurrent invocations don't cross-talk — each lands on its own span.
"""

from __future__ import annotations

from typing import Any

import pytest

from harmonograf_client.buffer import EnvelopeKind
from harmonograf_client.client import Client
from harmonograf_client.enums import SpanStatus
from harmonograf_client.telemetry_plugin import (
    REASONING_TRAIL_INLINE_MAX_BYTES,
    HarmonografTelemetryPlugin,
    _format_reasoning_trail,
)

from tests._fixtures import FakeTransport, make_factory


# ---------------------------------------------------------------------------
# ADK-shaped stand-ins
# ---------------------------------------------------------------------------


class _Session:
    def __init__(self, sid: str) -> None:
        self.id = sid


class _Agent:
    def __init__(self, name: str = "coordinator") -> None:
        self.name = name
        self.parent_agent = None


class _InvocationContext:
    def __init__(
        self,
        invocation_id: str,
        session_id: str = "sess-1",
        agent: Any | None = None,
    ) -> None:
        self.invocation_id = invocation_id
        self.session = _Session(session_id)
        self.agent = agent or _Agent()


class _CallbackContext:
    """ADK-shaped stand-in for the per-LLM-call callback context."""

    def __init__(self, invocation_id: str) -> None:
        self.invocation_id = invocation_id


class _Part:
    def __init__(self, text: str = "", thought: bool = False) -> None:
        self.text = text
        self.thought = thought


class _Content:
    def __init__(self, parts: list[_Part]) -> None:
        self.parts = parts


class _Gemini:
    def __init__(self, parts: list[_Part]) -> None:
        self.content = _Content(parts)


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="reasoning-trail-test",
        agent_id="agent-T",
        session_id="sess-T",
        buffer_size=256,
        _transport_factory=make_factory(made),
    )


@pytest.fixture
def plugin(client: Client) -> HarmonografTelemetryPlugin:
    return HarmonografTelemetryPlugin(client)


def _invocation_span_end(client: Client, span_id: str) -> Any:
    """Return the SpanEnd payload for ``span_id`` (non-destructive)."""
    for env in list(client._events._dq):  # noqa: SLF001 — test introspection
        if env.kind is EnvelopeKind.SPAN_END and env.payload.span_id == span_id:
            return env.payload
    raise AssertionError(f"no SPAN_END envelope for {span_id}")


def _span_start_ids(client: Client) -> list[str]:
    """Return every SPAN_START envelope id in buffer order (non-destructive)."""
    return [
        env.span_id
        for env in list(client._events._dq)  # noqa: SLF001 — test introspection
        if env.kind is EnvelopeKind.SPAN_START
    ]


def _last_span_start_id(client: Client) -> str:
    ids = _span_start_ids(client)
    if not ids:
        raise AssertionError("no SPAN_START envelope yet")
    return ids[-1]


def _first_span_start_id(client: Client) -> str:
    ids = _span_start_ids(client)
    if not ids:
        raise AssertionError("no SPAN_START envelope yet")
    return ids[0]


async def _finalize_llm_call(
    plugin: HarmonografTelemetryPlugin,
    invocation_id: str,
    reasoning: str,
) -> None:
    """Drive a before/after_model pair carrying ``reasoning`` as a thought part."""
    before = _CallbackContext(invocation_id)
    after = _CallbackContext(invocation_id)
    await plugin.before_model_callback(callback_context=before, llm_request=object())
    resp = _Gemini([_Part(reasoning, thought=True), _Part("visible")])
    await plugin.after_model_callback(callback_context=after, llm_response=resp)


# ---------------------------------------------------------------------------
# _format_reasoning_trail unit tests
# ---------------------------------------------------------------------------


def test_format_reasoning_trail_empty() -> None:
    assert _format_reasoning_trail([]) == ""


def test_format_reasoning_trail_single_chunk() -> None:
    out = _format_reasoning_trail(["only one"])
    assert "[LLM call 1]" in out
    assert "only one" in out


def test_format_reasoning_trail_multiple_chunks_joined_with_separator() -> None:
    out = _format_reasoning_trail(["first", "second", "third"])
    assert "[LLM call 1]" in out
    assert "[LLM call 2]" in out
    assert "[LLM call 3]" in out
    assert "first" in out
    assert "second" in out
    assert "third" in out
    # Separator appears exactly N-1 times.
    assert out.count("\n\n---\n\n") == 2


# ---------------------------------------------------------------------------
# End-to-end: INVOCATION span carries the aggregate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_after_run_aggregates_reasoning_onto_invocation_span(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """3 LLM calls with reasoning → INVOCATION SpanEnd carries the trail."""
    ctx = _InvocationContext("inv-trail-1")
    await plugin.before_run_callback(invocation_context=ctx)
    inv_span_id = _first_span_start_id(client)

    await _finalize_llm_call(plugin, "inv-trail-1", "step one: evaluate inputs")
    await _finalize_llm_call(plugin, "inv-trail-1", "step two: pick the tool")
    await _finalize_llm_call(plugin, "inv-trail-1", "step three: synthesize")

    await plugin.after_run_callback(invocation_context=ctx)
    end = _invocation_span_end(client, inv_span_id)

    assert end.attributes["has_reasoning"].bool_value is True
    assert end.attributes["reasoning_call_count"].int_value == 3
    trail = end.attributes["llm.reasoning_trail"].string_value
    assert "step one" in trail
    assert "step two" in trail
    assert "step three" in trail
    # Separator + numbered headers present.
    assert "[LLM call 1]" in trail
    assert "[LLM call 3]" in trail


@pytest.mark.asyncio
async def test_after_run_no_reasoning_leaves_invocation_span_clean(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """LLM calls without reasoning → no trail attrs on the INVOCATION span."""
    ctx = _InvocationContext("inv-trail-2")
    await plugin.before_run_callback(invocation_context=ctx)
    inv_span_id = _first_span_start_id(client)

    # LLM call with no thought parts.
    before = _CallbackContext("inv-trail-2")
    after = _CallbackContext("inv-trail-2")
    await plugin.before_model_callback(callback_context=before, llm_request=object())
    await plugin.after_model_callback(
        callback_context=after, llm_response=_Gemini([_Part("visible only")])
    )

    await plugin.after_run_callback(invocation_context=ctx)
    end = _invocation_span_end(client, inv_span_id)

    assert "llm.reasoning_trail" not in end.attributes
    assert "has_reasoning" not in end.attributes
    assert "reasoning_call_count" not in end.attributes


@pytest.mark.asyncio
async def test_large_reasoning_trail_spills_to_payload_ref(
    plugin: HarmonografTelemetryPlugin, client: Client, made: list[FakeTransport]
) -> None:
    """Aggregated trail > inline cap → payload_ref role="reasoning"."""
    ctx = _InvocationContext("inv-trail-big")
    await plugin.before_run_callback(invocation_context=ctx)
    inv_span_id = _first_span_start_id(client)

    # Build enough per-call reasoning to exceed the trail inline cap.
    chunk = "z" * (REASONING_TRAIL_INLINE_MAX_BYTES // 4)
    for _ in range(6):
        await _finalize_llm_call(plugin, "inv-trail-big", chunk)

    await plugin.after_run_callback(invocation_context=ctx)
    end = _invocation_span_end(client, inv_span_id)

    # No inline attribute for the full trail.
    assert "llm.reasoning_trail" not in end.attributes
    # Disclosure flag and call count are still set.
    assert end.attributes["has_reasoning"].bool_value is True
    assert end.attributes["reasoning_call_count"].int_value == 6
    # payload_ref role="reasoning" carries the aggregate.
    refs = [r for r in end.payload_refs if r.role == "reasoning"]
    assert len(refs) == 1
    assert refs[0].mime == "text/plain"
    # And the bytes were enqueued to the transport.
    assert any(p.digest == refs[0].digest for p in made[0].enqueued)


@pytest.mark.asyncio
async def test_cancel_stamps_partial_reasoning_trail(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """User cancel mid-run → INVOCATION span closes CANCELLED with trail so far."""
    ctx = _InvocationContext("inv-cancel")
    await plugin.before_run_callback(invocation_context=ctx)
    inv_span_id = _first_span_start_id(client)

    await _finalize_llm_call(plugin, "inv-cancel", "partial reasoning A")
    await _finalize_llm_call(plugin, "inv-cancel", "partial reasoning B")

    # Simulate user cancel — bypass after_run_callback, exercise the
    # public hook goldfive's adapter calls on CancelledError.
    plugin.on_cancellation("inv-cancel")

    end = _invocation_span_end(client, inv_span_id)
    assert end.status == client._resolve_status(SpanStatus.CANCELLED)
    assert end.attributes["has_reasoning"].bool_value is True
    assert end.attributes["reasoning_call_count"].int_value == 2
    trail = end.attributes["llm.reasoning_trail"].string_value
    assert "partial reasoning A" in trail
    assert "partial reasoning B" in trail


@pytest.mark.asyncio
async def test_on_run_end_sweeps_orphan_invocation_with_trail(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """on_run_end sweep closes any stranded INVOCATION span with its trail."""
    ctx = _InvocationContext("inv-orphan")
    await plugin.before_run_callback(invocation_context=ctx)
    inv_span_id = _first_span_start_id(client)

    await _finalize_llm_call(plugin, "inv-orphan", "stranded reasoning")

    # Simulate a sub-Runner that completed without ADK firing
    # after_run_callback (e.g. early generator-close on the outer run).
    plugin.on_run_end()

    end = _invocation_span_end(client, inv_span_id)
    assert end.attributes["has_reasoning"].bool_value is True
    assert end.attributes["reasoning_call_count"].int_value == 1
    assert "stranded reasoning" in end.attributes["llm.reasoning_trail"].string_value


@pytest.mark.asyncio
async def test_concurrent_invocations_do_not_share_trail(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Two invocations interleave LLM calls — each trail lands on its own span."""
    ctx_a = _InvocationContext("inv-A")
    ctx_b = _InvocationContext("inv-B")
    await plugin.before_run_callback(invocation_context=ctx_a)
    await plugin.before_run_callback(invocation_context=ctx_b)
    ids = _span_start_ids(client)
    assert len(ids) == 2
    span_a, span_b = ids[0], ids[1]

    await _finalize_llm_call(plugin, "inv-A", "A-first")
    await _finalize_llm_call(plugin, "inv-B", "B-only")
    await _finalize_llm_call(plugin, "inv-A", "A-second")

    await plugin.after_run_callback(invocation_context=ctx_b)
    await plugin.after_run_callback(invocation_context=ctx_a)

    end_a = _invocation_span_end(client, span_a)
    end_b = _invocation_span_end(client, span_b)
    assert end_a.attributes["reasoning_call_count"].int_value == 2
    trail_a = end_a.attributes["llm.reasoning_trail"].string_value
    assert "A-first" in trail_a
    assert "A-second" in trail_a
    assert "B-only" not in trail_a

    assert end_b.attributes["reasoning_call_count"].int_value == 1
    trail_b = end_b.attributes["llm.reasoning_trail"].string_value
    assert "B-only" in trail_b
    assert "A-first" not in trail_b


@pytest.mark.asyncio
async def test_next_run_does_not_inherit_previous_trail(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """After one run ends, the next run starts with an empty reasoning buffer."""
    # First run with reasoning.
    ctx1 = _InvocationContext("inv-prev")
    await plugin.before_run_callback(invocation_context=ctx1)
    span1 = _last_span_start_id(client)
    await _finalize_llm_call(plugin, "inv-prev", "leftover reasoning")
    await plugin.after_run_callback(invocation_context=ctx1)
    end1 = _invocation_span_end(client, span1)
    assert "llm.reasoning_trail" in end1.attributes

    # Second run on a fresh invocation id — no reasoning.
    ctx2 = _InvocationContext("inv-next", session_id="sess-T")
    await plugin.before_run_callback(invocation_context=ctx2)
    span2 = _last_span_start_id(client)
    # No LLM calls in this run.
    await plugin.after_run_callback(invocation_context=ctx2)
    end2 = _invocation_span_end(client, span2)

    assert "llm.reasoning_trail" not in end2.attributes
    assert "has_reasoning" not in end2.attributes
