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
    def __init__(
        self, type: str = "text", thinking: str = "", text: str = ""
    ) -> None:
        self.type = type
        self.thinking = thinking
        self.text = text


class _Anthropic:
    def __init__(self, blocks: list[_AnthropicBlock]) -> None:
        self.content = blocks


class _CallbackContext:
    """Opaque-enough stand-in; the plugin keys span lookup by id()."""


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
    ctx = _CallbackContext()
    await plugin.before_model_callback(callback_context=ctx, llm_request=object())
    await plugin.after_model_callback(
        callback_context=ctx, llm_response=_OpenAI("short reasoning")
    )
    span_end = _first_span_end(client)
    assert span_end.attributes["llm.reasoning"].string_value == "short reasoning"
    assert span_end.attributes["has_reasoning"].bool_value is True
    assert list(span_end.payload_refs) == []


@pytest.mark.asyncio
async def test_large_reasoning_attaches_as_payload_ref(
    plugin: HarmonografTelemetryPlugin, client: Client, made: list[FakeTransport]
) -> None:
    ctx = _CallbackContext()
    big = "x" * (REASONING_INLINE_MAX_BYTES + 1024)
    await plugin.before_model_callback(callback_context=ctx, llm_request=object())
    await plugin.after_model_callback(
        callback_context=ctx, llm_response=_OpenAI(big)
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
    ctx = _CallbackContext()
    await plugin.before_model_callback(callback_context=ctx, llm_request=object())
    await plugin.after_model_callback(
        callback_context=ctx, llm_response=_Gemini([_Part("hello")])
    )
    span_end = _first_span_end(client)
    assert "llm.reasoning" not in span_end.attributes
    assert "has_reasoning" not in span_end.attributes
    assert list(span_end.payload_refs) == []


@pytest.mark.asyncio
async def test_error_response_short_circuits_before_reasoning(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    ctx = _CallbackContext()

    class _ErrorResp:
        error_message = "rate limited"
        reasoning_content = "this should not be recorded"

    await plugin.before_model_callback(callback_context=ctx, llm_request=object())
    await plugin.after_model_callback(callback_context=ctx, llm_response=_ErrorResp())
    span_end = _first_span_end(client)
    # On error, reasoning extraction is skipped; the error path runs instead.
    assert "llm.reasoning" not in span_end.attributes
    assert "has_reasoning" not in span_end.attributes
    assert span_end.error.type == "LlmError"


@pytest.mark.asyncio
async def test_anthropic_thinking_rides_inline_when_small(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    ctx = _CallbackContext()
    resp = _Anthropic(
        [_AnthropicBlock(type="thinking", thinking="reflective thought")]
    )
    await plugin.before_model_callback(callback_context=ctx, llm_request=object())
    await plugin.after_model_callback(callback_context=ctx, llm_response=resp)
    span_end = _first_span_end(client)
    assert span_end.attributes["llm.reasoning"].string_value == "reflective thought"


@pytest.mark.asyncio
async def test_gemini_thought_parts_inline_when_small(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    ctx = _CallbackContext()
    resp = _Gemini(
        [
            _Part("visible"),
            _Part("private thought", thought=True),
        ]
    )
    await plugin.before_model_callback(callback_context=ctx, llm_request=object())
    await plugin.after_model_callback(callback_context=ctx, llm_response=resp)
    span_end = _first_span_end(client)
    assert span_end.attributes["llm.reasoning"].string_value == "private thought"
