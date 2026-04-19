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
        else:
            self._client.emit_span_end(sid, status=SpanStatus.COMPLETED)

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
