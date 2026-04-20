"""Tests for :mod:`harmonograf_client.telemetry_plugin` — specifically
the ``reasoning_content`` extraction + span attachment path.

The plugin's ``after_model_callback`` is exercised against fabricated
response shapes for the three backends (OpenAI-compat, Anthropic,
Google) to confirm:

* Small reasoning rides inline as a ``llm.reasoning`` span attribute.
* Reasoning above :data:`REASONING_INLINE_MAX_BYTES` is attached as a
  payload_ref with ``role="reasoning"``.
* Responses without reasoning leave the span attribute-clean.
* Error responses short-circuit before reasoning extraction.
"""

from __future__ import annotations

from typing import Any

import pytest

from harmonograf_client.buffer import EnvelopeKind
from harmonograf_client.client import Client
from harmonograf_client.telemetry_plugin import (
    REASONING_INLINE_MAX_BYTES,
    HarmonografTelemetryPlugin,
    _extract_reasoning,
)

from tests._fixtures import FakeTransport, make_factory


# ---------------------------------------------------------------------------
# Helpers: fabricated response shapes
# ---------------------------------------------------------------------------


class _Part:
    def __init__(self, text: str = "", thought: bool = False) -> None:
        self.text = text
        self.thought = thought


class _Content:
    def __init__(self, parts: list[_Part]) -> None:
        self.parts = parts


class _Gemini:
    """ADK-style LlmResponse with thought parts."""

    def __init__(self, parts: list[_Part]) -> None:
        self.content = _Content(parts)


class _OpenAIMsg:
    def __init__(self, reasoning_content: str) -> None:
        self.reasoning_content = reasoning_content


class _OpenAIChoice:
    def __init__(self, reasoning_content: str) -> None:
        self.message = _OpenAIMsg(reasoning_content)


class _OpenAI:
    def __init__(self, reasoning_content: str) -> None:
        self.choices = [_OpenAIChoice(reasoning_content)]


class _AnthropicBlock:
    def __init__(self, type: str = "text", thinking: str = "", text: str = "") -> None:
        self.type = type
        self.thinking = thinking
        self.text = text


class _Anthropic:
    def __init__(self, blocks: list[_AnthropicBlock]) -> None:
        self.content = blocks


class _CallbackContext:
    """ADK-shaped stand-in carrying an ``invocation_id``.

    The plugin pairs ``before_model`` / ``after_model`` calls via
    ``invocation_id``, not object identity — ADK reconstructs the
    ``CallbackContext`` between the two callbacks, so ``id(ctx)`` is
    unreliable. Tests pass distinct Python objects with matching
    ``invocation_id`` to mirror that production shape.
    """

    def __init__(self, invocation_id: str = "inv-1") -> None:
        self.invocation_id = invocation_id


def _ctx_pair(
    invocation_id: str = "inv-1",
) -> tuple[_CallbackContext, _CallbackContext]:
    """Return (before_ctx, after_ctx) sharing an ``invocation_id``.

    ADK builds a fresh ``CallbackContext`` object for each plugin
    callback invocation, so the two contexts a plugin sees across one
    LLM call are *not* the same Python object. Use this helper in
    tests to mirror that.
    """
    return _CallbackContext(invocation_id), _CallbackContext(invocation_id)


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="telemetry-test",
        agent_id="agent-T",
        session_id="sess-T",
        buffer_size=64,
        _transport_factory=make_factory(made),
    )


@pytest.fixture
def plugin(client: Client) -> HarmonografTelemetryPlugin:
    return HarmonografTelemetryPlugin(client)


def _drain(client: Client) -> list:
    return list(client._events.drain())


def _first_span_end(client: Client) -> Any:
    """Return the first SpanEnd envelope's payload (a SpanEnd proto)."""
    for env in _drain(client):
        if env.kind is EnvelopeKind.SPAN_END:
            return env.payload
    raise AssertionError("no SPAN_END envelope emitted")


# ---------------------------------------------------------------------------
# _extract_reasoning unit tests
# ---------------------------------------------------------------------------


def test_extract_reasoning_from_gemini_thought_parts() -> None:
    resp = _Gemini(
        [
            _Part("visible text"),
            _Part("internal reasoning step 1", thought=True),
            _Part("more visible text"),
        ]
    )
    assert _extract_reasoning(resp) == "internal reasoning step 1"


def test_extract_reasoning_from_openai_compat_response() -> None:
    resp = _OpenAI("step-by-step chain of thought")
    assert _extract_reasoning(resp) == "step-by-step chain of thought"


def test_extract_reasoning_from_anthropic_thinking_block() -> None:
    resp = _Anthropic(
        [
            _AnthropicBlock(type="text", text="hi"),
            _AnthropicBlock(type="thinking", thinking="claude's reasoning"),
        ]
    )
    assert _extract_reasoning(resp) == "claude's reasoning"


def test_extract_reasoning_returns_empty_for_plain_response() -> None:
    resp = _Gemini([_Part("no thought here", thought=False)])
    assert _extract_reasoning(resp) == ""


def test_extract_reasoning_fallback_flat_attribute() -> None:
    class _Plain:
        reasoning = "flat reasoning string"

    assert _extract_reasoning(_Plain()) == "flat reasoning string"


# ---------------------------------------------------------------------------
# Plugin integration: span attributes + payload_refs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_reasoning_rides_as_span_attribute(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    before_ctx, after_ctx = _ctx_pair()
    await plugin.before_model_callback(
        callback_context=before_ctx, llm_request=object()
    )
    await plugin.after_model_callback(
        callback_context=after_ctx, llm_response=_OpenAI("short reasoning")
    )
    span_end = _first_span_end(client)
    assert span_end.attributes["llm.reasoning"].string_value == "short reasoning"
    assert span_end.attributes["has_reasoning"].bool_value is True
    assert list(span_end.payload_refs) == []


@pytest.mark.asyncio
async def test_large_reasoning_attaches_as_payload_ref(
    plugin: HarmonografTelemetryPlugin, client: Client, made: list[FakeTransport]
) -> None:
    before_ctx, after_ctx = _ctx_pair()
    big = "x" * (REASONING_INLINE_MAX_BYTES + 1024)
    await plugin.before_model_callback(
        callback_context=before_ctx, llm_request=object()
    )
    await plugin.after_model_callback(
        callback_context=after_ctx, llm_response=_OpenAI(big)
    )
    span_end = _first_span_end(client)
    # No inline attribute for the reasoning text.
    assert "llm.reasoning" not in span_end.attributes
    # Disclosure flag is still set so the UI can render the section.
    assert span_end.attributes["has_reasoning"].bool_value is True
    # Exactly one payload_ref with role="reasoning".
    refs = list(span_end.payload_refs)
    assert len(refs) == 1
    assert refs[0].role == "reasoning"
    assert refs[0].mime == "text/plain"
    assert refs[0].size == len(big.encode("utf-8"))
    # And the bytes were enqueued to the transport.
    assert any(p.digest == refs[0].digest for p in made[0].enqueued)


@pytest.mark.asyncio
async def test_response_without_reasoning_leaves_span_clean(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    before_ctx, after_ctx = _ctx_pair()
    await plugin.before_model_callback(
        callback_context=before_ctx, llm_request=object()
    )
    await plugin.after_model_callback(
        callback_context=after_ctx, llm_response=_Gemini([_Part("hello")])
    )
    span_end = _first_span_end(client)
    assert "llm.reasoning" not in span_end.attributes
    assert "has_reasoning" not in span_end.attributes
    assert list(span_end.payload_refs) == []


@pytest.mark.asyncio
async def test_error_response_short_circuits_before_reasoning(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    before_ctx, after_ctx = _ctx_pair()

    class _ErrorResp:
        error_message = "rate limited"
        reasoning_content = "this should not be recorded"

    await plugin.before_model_callback(
        callback_context=before_ctx, llm_request=object()
    )
    await plugin.after_model_callback(
        callback_context=after_ctx, llm_response=_ErrorResp()
    )
    span_end = _first_span_end(client)
    # On error, reasoning extraction is skipped; the error path runs instead.
    assert "llm.reasoning" not in span_end.attributes
    assert "has_reasoning" not in span_end.attributes
    assert span_end.error.type == "LlmError"


@pytest.mark.asyncio
async def test_anthropic_thinking_rides_inline_when_small(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    before_ctx, after_ctx = _ctx_pair()
    resp = _Anthropic([_AnthropicBlock(type="thinking", thinking="reflective thought")])
    await plugin.before_model_callback(
        callback_context=before_ctx, llm_request=object()
    )
    await plugin.after_model_callback(callback_context=after_ctx, llm_response=resp)
    span_end = _first_span_end(client)
    assert span_end.attributes["llm.reasoning"].string_value == "reflective thought"


@pytest.mark.asyncio
async def test_gemini_thought_parts_inline_when_small(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    before_ctx, after_ctx = _ctx_pair()
    resp = _Gemini(
        [
            _Part("visible"),
            _Part("private thought", thought=True),
        ]
    )
    await plugin.before_model_callback(
        callback_context=before_ctx, llm_request=object()
    )
    await plugin.after_model_callback(callback_context=after_ctx, llm_response=resp)
    span_end = _first_span_end(client)
    assert span_end.attributes["llm.reasoning"].string_value == "private thought"


# ---------------------------------------------------------------------------
# LiteLlm-wrapping regression (issue: PR #43 shipped with zero
# reasoning attributes on Qwen3.5-via-LiteLLM runs because the plugin
# keyed spans by ``id(callback_context)`` and ADK rebuilds the context
# between before_model and after_model).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_litellm_shape_openai_reasoning_content_pairs_before_after(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """ADK's LiteLlm converts provider ``reasoning_content`` into a
    Gemini-style ``types.Part(text=..., thought=True)`` prepended to
    ``content.parts``. This reproduces that shape and verifies the
    plugin pairs a fresh before-context with a fresh after-context.
    """
    before_ctx, after_ctx = _ctx_pair(invocation_id="inv-litellm")
    # Distinct Python objects: matches what ADK actually passes.
    assert id(before_ctx) != id(after_ctx)
    resp = _Gemini(
        [
            _Part("Let me think step by step. 6*7=42.", thought=True),
            _Part("The answer is 42."),
        ]
    )
    await plugin.before_model_callback(
        callback_context=before_ctx, llm_request=object()
    )
    await plugin.after_model_callback(callback_context=after_ctx, llm_response=resp)
    span_end = _first_span_end(client)
    assert (
        span_end.attributes["llm.reasoning"].string_value
        == "Let me think step by step. 6*7=42."
    )
    assert span_end.attributes["has_reasoning"].bool_value is True


@pytest.mark.asyncio
async def test_litellm_streaming_partials_accumulate_then_finalize(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """LiteLlm SSE streaming yields one ``LlmResponse`` per reasoning
    delta with ``partial=True``, and a final non-partial aggregated
    response. The plugin must accumulate partial reasoning and only
    close the span on the finalize.
    """
    before_ctx = _CallbackContext(invocation_id="inv-stream")
    partial_ctx_1 = _CallbackContext(invocation_id="inv-stream")
    partial_ctx_2 = _CallbackContext(invocation_id="inv-stream")
    final_ctx = _CallbackContext(invocation_id="inv-stream")

    def _partial(chunk_text: str) -> _Gemini:
        r = _Gemini([_Part(chunk_text, thought=True)])
        r.partial = True  # type: ignore[attr-defined]
        return r

    await plugin.before_model_callback(
        callback_context=before_ctx, llm_request=object()
    )
    await plugin.after_model_callback(
        callback_context=partial_ctx_1, llm_response=_partial("Let me think ")
    )
    await plugin.after_model_callback(
        callback_context=partial_ctx_2, llm_response=_partial("step by step.")
    )
    # No SPAN_END should have been emitted yet.
    envelopes = list(client._events.drain())
    client._events.drain()  # no-op: drain already consumed
    assert all(env.kind is not EnvelopeKind.SPAN_END for env in envelopes)
    # Re-enqueue what we popped (drain is destructive) — put them back
    # by just re-running; instead, assert that the SPAN_START was the
    # only LLM-related envelope, which is what we actually need.
    assert any(env.kind is EnvelopeKind.SPAN_START for env in envelopes)

    # Now the finalize. LiteLlm's finalize carries the aggregated
    # reasoning as a single thought part (reproducing the concatenation
    # of all previously streamed deltas).
    final = _Gemini([_Part("Let me think step by step.", thought=True)])
    final.partial = False  # type: ignore[attr-defined]
    await plugin.after_model_callback(callback_context=final_ctx, llm_response=final)
    span_end = _first_span_end(client)
    assert (
        span_end.attributes["llm.reasoning"].string_value
        == "Let me think step by step."
    )


@pytest.mark.asyncio
async def test_sequential_llm_calls_within_invocation_balance(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """A single invocation may make multiple sequential LLM calls
    (tool-calling loop). Each before/after pair in the invocation's
    FIFO queue must match the correct span.
    """
    inv_id = "inv-multi"
    # Call 1
    await plugin.before_model_callback(
        callback_context=_CallbackContext(inv_id), llm_request=object()
    )
    # Call 2 (starts before call 1 finishes would violate ADK's
    # serialization, but we still test proper FIFO ordering).
    await plugin.before_model_callback(
        callback_context=_CallbackContext(inv_id), llm_request=object()
    )
    # After call 1
    await plugin.after_model_callback(
        callback_context=_CallbackContext(inv_id),
        llm_response=_OpenAI("reasoning for call 1"),
    )
    # After call 2
    await plugin.after_model_callback(
        callback_context=_CallbackContext(inv_id),
        llm_response=_OpenAI("reasoning for call 2"),
    )
    ends = [env.payload for env in _drain(client) if env.kind is EnvelopeKind.SPAN_END]
    assert len(ends) == 2
    assert ends[0].attributes["llm.reasoning"].string_value == "reasoning for call 1"
    assert ends[1].attributes["llm.reasoning"].string_value == "reasoning for call 2"


@pytest.mark.asyncio
async def test_concurrent_invocations_do_not_cross_talk(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Two agents running concurrently produce interleaved before/after
    callbacks with distinct ``invocation_id`` values. Reasoning must
    land on the correct span."""
    await plugin.before_model_callback(
        callback_context=_CallbackContext("inv-A"), llm_request=object()
    )
    await plugin.before_model_callback(
        callback_context=_CallbackContext("inv-B"), llm_request=object()
    )
    await plugin.after_model_callback(
        callback_context=_CallbackContext("inv-B"),
        llm_response=_OpenAI("B's reasoning"),
    )
    await plugin.after_model_callback(
        callback_context=_CallbackContext("inv-A"),
        llm_response=_OpenAI("A's reasoning"),
    )
    ends = [env.payload for env in _drain(client) if env.kind is EnvelopeKind.SPAN_END]
    assert len(ends) == 2
    # Order follows finalize order (B first, then A).
    assert ends[0].attributes["llm.reasoning"].string_value == "B's reasoning"
    assert ends[1].attributes["llm.reasoning"].string_value == "A's reasoning"
