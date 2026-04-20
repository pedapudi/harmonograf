"""ADK plugin that emits harmonograf spans for ADK lifecycle callbacks.

Observability-only: this plugin never makes orchestration decisions. All
plan, task, drift, and steering logic now lives in goldfive (see issue
#2); harmonograf just emits spans so the server's timeline /
Gantt renders per-invocation, per-model-call, and per-tool-call bars.

Install by passing a pre-built ADK runner with this plugin to
:class:`goldfive.adapters.adk.ADKAdapter`, or (more commonly) add the
plugin to the ADK ``App`` that ``adk web`` / ``adk run`` builds::

    from google.adk.apps.app import App
    from harmonograf_client import Client, HarmonografTelemetryPlugin

    client = Client(name="research", server_addr="127.0.0.1:7531")
    app = App(root_agent=..., plugins=[HarmonografTelemetryPlugin(client)])

The plugin is safe to install alongside ``goldfive.adapters.adk`` 's
own plugin — they operate on disjoint responsibilities and do not
interfere with each other's ADK lifecycle callbacks.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .client import Client
from .enums import SpanKind, SpanStatus

log = logging.getLogger("harmonograf_client.telemetry_plugin")


try:
    from google.adk.plugins.base_plugin import BasePlugin  # type: ignore
except ImportError:  # pragma: no cover — ADK optional at import time
    BasePlugin = object  # type: ignore[assignment,misc]


def _safe_attr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name, default)
    except Exception:  # noqa: BLE001
        return default


def _serialize_args(args: Any) -> bytes | None:
    if args is None:
        return None
    try:
        return json.dumps(args, default=str).encode("utf-8")
    except (TypeError, ValueError):
        return None


# Reasoning content above this byte size is attached as a blob payload_ref
# (fetched on-demand by the span drawer); smaller content rides inline as
# a span attribute so the default drawer render does not pay a round trip.
# 2 KiB is roughly 500 tokens -- large enough for short chain-of-thought
# like o1-mini's reasoning summary, small enough to keep span-stream bytes
# bounded.
REASONING_INLINE_MAX_BYTES: int = 2048


def _extract_reasoning(llm_response: Any) -> str:
    """Best-effort per-provider reasoning-content extraction.

    Surfaces chain-of-thought exposed by the three families of backends
    ADK drives today:

    * **Google / Gemini** — thought parts flagged with
      ``part.thought=True`` inside ``response.content.parts``.
    * **OpenAI-compatible** — ``response.choices[0].message.reasoning_content``
      (Qwen3.5 via LiteLLM, some o1-series, Deepseek).
    * **Anthropic** — ``content[i].type == "thinking"`` blocks on the
      extended-thinking surface (``block.thinking``).

    Returns the reasoning text as a ``str`` or ``""`` when the response
    carries no chain-of-thought. Callers compare against the empty
    string to decide whether to emit a payload.
    """
    # Google / Gemini-style thought parts.
    content = _safe_attr(llm_response, "content")
    if content is not None:
        parts = _safe_attr(content, "parts") or []
        thoughts: list[str] = []
        for part in parts:
            if not _safe_attr(part, "thought", False):
                continue
            text = _safe_attr(part, "text") or ""
            if text:
                thoughts.append(str(text))
        if thoughts:
            return "\n".join(thoughts)

    # OpenAI-compatible: response.choices[0].message.reasoning_content.
    try:
        choices = _safe_attr(llm_response, "choices") or []
        if choices:
            msg = _safe_attr(choices[0], "message")
            if msg is not None:
                rc = _safe_attr(msg, "reasoning_content") or _safe_attr(
                    msg, "reasoning"
                )
                if rc:
                    return str(rc)
    except Exception:  # noqa: BLE001 -- best-effort extraction
        pass

    # Anthropic: content blocks of type "thinking".
    blocks = _safe_attr(llm_response, "content")
    if isinstance(blocks, list):
        for block in blocks:
            if _safe_attr(block, "type") == "thinking":
                t = _safe_attr(block, "thinking") or ""
                if t:
                    return str(t)

    # Fallback: flat attribute.
    for attr in ("reasoning_content", "reasoning", "thinking"):
        v = _safe_attr(llm_response, attr)
        if isinstance(v, str) and v:
            return v

    return ""


class HarmonografTelemetryPlugin(BasePlugin):  # type: ignore[misc]
    """Emit one harmonograf span per ADK lifecycle boundary.

    Three span kinds are produced:

    * ``INVOCATION`` — spans an entire ``runner.run_async`` call.
    * ``LLM_CALL`` — one per ``before_model`` / ``after_model`` pair.
    * ``TOOL_CALL`` — one per ``before_tool`` / ``after_tool`` or
      ``on_tool_error`` pair.

    Span IDs are keyed off the ADK callback objects so nested calls are
    balanced even when the same plugin instance handles multiple
    concurrent invocations (ADK guarantees one callback object per
    lifecycle pair).
    """

    def __init__(self, client: Client) -> None:
        try:
            super().__init__(name="harmonograf-telemetry")
        except TypeError:
            # BasePlugin fallback when ADK is not installed; the plugin
            # is never actually invoked in that case.
            pass
        self._client = client
        self._invocation_spans: dict[str, str] = {}
        self._model_spans: dict[int, str] = {}
        self._tool_spans: dict[int, str] = {}

    @property
    def client(self) -> Client:
        return self._client

    # ------------------------------------------------------------------
    # Invocation lifecycle
    # ------------------------------------------------------------------

    async def before_run_callback(self, *, invocation_context: Any) -> None:
        inv_id = str(_safe_attr(invocation_context, "invocation_id", "") or "")
        agent = _safe_attr(invocation_context, "agent")
        name = str(_safe_attr(agent, "name", "") or "invocation")
        sid = self._client.emit_span_start(
            kind=SpanKind.INVOCATION,
            name=name,
            attributes={"adk.invocation_id": inv_id} if inv_id else None,
        )
        if inv_id:
            self._invocation_spans[inv_id] = sid
        else:
            # Fall back to object id so the end callback still balances.
            self._invocation_spans[str(id(invocation_context))] = sid

    async def after_run_callback(self, *, invocation_context: Any) -> None:
        inv_id = str(_safe_attr(invocation_context, "invocation_id", "") or "")
        key = inv_id if inv_id in self._invocation_spans else str(id(invocation_context))
        sid = self._invocation_spans.pop(key, None)
        if sid is not None:
            self._client.emit_span_end(sid, status=SpanStatus.COMPLETED)

    # ------------------------------------------------------------------
    # LLM call lifecycle
    # ------------------------------------------------------------------

    async def before_model_callback(
        self, *, callback_context: Any, llm_request: Any
    ) -> None:
        model = str(_safe_attr(llm_request, "model", "") or "")
        sid = self._client.emit_span_start(
            kind=SpanKind.LLM_CALL,
            name=model or "llm_call",
            attributes={"llm.model": model} if model else None,
        )
        self._model_spans[id(callback_context)] = sid

    async def after_model_callback(
        self, *, callback_context: Any, llm_response: Any
    ) -> None:
        sid = self._model_spans.pop(id(callback_context), None)
        if sid is None:
            return
        error_info = _safe_attr(llm_response, "error_message")
        if error_info:
            self._client.emit_span_end(
                sid,
                status=SpanStatus.FAILED,
                error={"type": "LlmError", "message": str(error_info)},
            )
            return

        reasoning = _extract_reasoning(llm_response)
        attributes: dict[str, Any] = {}
        payload: bytes | None = None
        payload_mime: str = "application/json"
        payload_role: str = "output"
        if reasoning:
            encoded = reasoning.encode("utf-8")
            # has_reasoning is always set so the drawer can render a
            # disclosure even when the reasoning is large enough to
            # ride as a payload_ref instead of an inline attribute.
            attributes["has_reasoning"] = True
            if len(encoded) <= REASONING_INLINE_MAX_BYTES:
                # Small reasoning: inline as a span attribute so the
                # default drawer render doesn't fetch a blob.
                attributes["llm.reasoning"] = reasoning
            else:
                # Large reasoning: upload as a blob and attach a
                # payload_ref with role="reasoning".
                payload = encoded
                payload_mime = "text/plain"
                payload_role = "reasoning"

        self._client.emit_span_end(
            sid,
            status=SpanStatus.COMPLETED,
            attributes=attributes or None,
            payload=payload,
            payload_mime=payload_mime,
            payload_role=payload_role,
        )

    # ------------------------------------------------------------------
    # Tool call lifecycle
    # ------------------------------------------------------------------

    async def before_tool_callback(
        self, *, tool: Any, tool_args: Any, tool_context: Any
    ) -> None:
        tool_name = str(_safe_attr(tool, "name", "") or "tool")
        payload = _serialize_args(tool_args)
        sid = self._client.emit_span_start(
            kind=SpanKind.TOOL_CALL,
            name=tool_name,
            payload=payload,
            payload_role="input",
        )
        self._tool_spans[id(tool_context)] = sid

    async def after_tool_callback(
        self,
        *,
        tool: Any,
        tool_args: Any,
        tool_context: Any,
        result: Any,
    ) -> None:
        sid = self._tool_spans.pop(id(tool_context), None)
        if sid is None:
            return
        payload = _serialize_args(result)
        self._client.emit_span_end(
            sid,
            status=SpanStatus.COMPLETED,
            payload=payload,
            payload_role="output",
        )

    async def on_tool_error_callback(
        self,
        *,
        tool: Any,
        tool_args: Any,
        tool_context: Any,
        error: Any,
    ) -> None:
        sid = self._tool_spans.pop(id(tool_context), None)
        if sid is None:
            return
        self._client.emit_span_end(
            sid,
            status=SpanStatus.FAILED,
            error={"type": type(error).__name__, "message": str(error)},
        )
