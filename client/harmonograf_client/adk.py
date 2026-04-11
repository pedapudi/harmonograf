"""ADK adapter — one-line integration for google.adk Runner.

Usage::

    from harmonograf_client import Client, attach_adk
    from google.adk.runners import InMemoryRunner

    runner = InMemoryRunner(agent=...)
    client = Client(name="research-agent")
    handle = attach_adk(runner, client)

The adapter installs an ADK ``BasePlugin`` on the runner's
``plugin_manager`` that translates ADK lifecycle callbacks into
harmonograf spans according to the mapping in docs/design/01 §3:

=====================================  ==========================================
ADK callback                            harmonograf span action
=====================================  ==========================================
``before_run_callback``                 start ``INVOCATION`` span
``after_run_callback``                  end the ``INVOCATION`` span
``before_model_callback``               start ``LLM_CALL`` span (child of INVOCATION)
``after_model_callback``                end the ``LLM_CALL`` span (attach completion)
``before_tool_callback``                start ``TOOL_CALL`` span (child of LLM_CALL)
``after_tool_callback``                 end the ``TOOL_CALL`` span with result
``on_tool_error_callback``              end the ``TOOL_CALL`` span FAILED
``on_event_callback`` w/ transfer       emit ``TRANSFER`` span with INVOKED link
``on_event_callback`` w/ state_delta    attributes on the enclosing span
=====================================  ==========================================

Long-running tools (``event.long_running_tool_ids``) mark the in-flight
TOOL_CALL as ``AWAITING_HUMAN`` — the subsequent ``after_tool_callback``
closes it once a response arrives.

Capabilities advertised by ADK itself: ``HUMAN_IN_LOOP`` (long-running
tools) and ``STEERING`` (via injecting into session state). Callers who
want ``PAUSE_RESUME`` / ``REWIND`` need their own runner wrapper and
must advertise those flags on the ``Client`` themselves.

The adapter never imports ``google.adk`` at module load — only when
``attach_adk`` is called. That keeps the main ``harmonograf_client``
import free of the ADK dependency.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Optional

from .client import Client
from .transport import ControlAckSpec

log = logging.getLogger("harmonograf_client.adk")


STEERING_STATE_KEY = "_harmonograf_steering"
INJECT_STATE_KEY = "_harmonograf_inject"


class AdkAdapter:
    """Handle returned by :func:`attach_adk`. Holds the installed plugin
    and exposes :meth:`detach` for clean removal in tests.
    """

    def __init__(self, runner: Any, client: Client, plugin: Any) -> None:
        self._runner = runner
        self._client = client
        self._plugin = plugin

    @property
    def plugin(self) -> Any:
        return self._plugin

    def detach(self) -> None:
        try:
            plugins = self._runner.plugin_manager.plugins
            if self._plugin in plugins:
                plugins.remove(self._plugin)
        except Exception:
            pass


def attach_adk(runner: Any, client: Client) -> AdkAdapter:
    """Install a harmonograf plugin on the ADK runner and return the
    adapter handle. Also wires up STEER / INJECT_MESSAGE control
    handlers that write into ADK session state under known keys.
    """
    from google.adk.plugins.base_plugin import BasePlugin
    from google.adk.events.event import Event

    state: "_AdkState" = _AdkState(client=client)

    class HarmonografAdkPlugin(BasePlugin):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__(name="harmonograf")

        async def before_run_callback(self, *, invocation_context):
            state.on_invocation_start(invocation_context)
            return None

        async def after_run_callback(self, *, invocation_context):
            state.on_invocation_end(invocation_context)
            return None

        async def before_model_callback(self, *, callback_context, llm_request):
            state.on_model_start(callback_context, llm_request)
            return None

        async def after_model_callback(self, *, callback_context, llm_response):
            state.on_model_end(callback_context, llm_response)
            return None

        async def before_tool_callback(self, *, tool, tool_args, tool_context):
            state.on_tool_start(tool, tool_args, tool_context)
            return None

        async def after_tool_callback(self, *, tool, tool_args, tool_context, result):
            state.on_tool_end(tool, tool_context, result=result, error=None)
            return None

        async def on_tool_error_callback(self, *, tool, tool_args, tool_context, error):
            state.on_tool_end(tool, tool_context, result=None, error=error)
            return None

        async def on_event_callback(self, *, invocation_context, event):
            state.on_event(invocation_context, event)
            return None

    plugin = HarmonografAdkPlugin()

    try:
        runner.plugin_manager.plugins.append(plugin)
    except Exception as e:
        raise RuntimeError(f"attach_adk: could not install plugin on runner: {e}") from e

    def _handle_steer(event: Any) -> ControlAckSpec:
        body = event.payload.decode("utf-8", errors="replace") if event.payload else ""
        state.queue_session_mutation(STEERING_STATE_KEY, body)
        return ControlAckSpec(result="success")

    def _handle_inject(event: Any) -> ControlAckSpec:
        body = event.payload.decode("utf-8", errors="replace") if event.payload else ""
        state.queue_session_mutation(INJECT_STATE_KEY, body)
        return ControlAckSpec(result="success")

    client.on_control("STEER", _handle_steer)
    client.on_control("INJECT_MESSAGE", _handle_inject)

    return AdkAdapter(runner=runner, client=client, plugin=plugin)


class _AdkState:
    """Tracks in-flight spans so callbacks can close them at the right
    parent. All access is lock-guarded since ADK callbacks may fire from
    different asyncio tasks within one runner invocation.
    """

    def __init__(self, client: Client) -> None:
        self._client = client
        self._lock = threading.Lock()
        # invocation_id → span_id (INVOCATION)
        self._invocations: dict[str, str] = {}
        # invocation_id → current LLM_CALL span_id
        self._llm_by_invocation: dict[str, str] = {}
        # tool call id → tool span id
        self._tools: dict[str, str] = {}
        # tool call id → long-running flag
        self._long_running: set[str] = set()
        # Session mutations queued by control handlers — surfaced via
        # pending_session_mutations() for agent code to apply.
        self._pending_mutations: list[tuple[str, str]] = []

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------

    def on_invocation_start(self, ic: Any) -> None:
        inv_id = _safe_attr(ic, "invocation_id", "")
        name = _safe_attr(getattr(ic, "agent", None), "name", "agent") or "agent"
        attrs = {
            "invocation_id": inv_id,
            "user_id": _safe_attr(getattr(ic, "user_id", None), "__str__", "") or str(_safe_attr(ic, "user_id", "")),
        }
        session_id = _safe_attr(getattr(ic, "session", None), "id", "")
        if session_id:
            attrs["adk_session_id"] = session_id
        span_id = self._client.emit_span_start(
            kind="INVOCATION",
            name=name,
            attributes=attrs,
        )
        with self._lock:
            self._invocations[inv_id] = span_id

    def on_invocation_end(self, ic: Any) -> None:
        inv_id = _safe_attr(ic, "invocation_id", "")
        with self._lock:
            span_id = self._invocations.pop(inv_id, None)
            self._llm_by_invocation.pop(inv_id, None)
        if span_id:
            self._client.emit_span_end(span_id, status="COMPLETED")

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------

    def on_model_start(self, cc: Any, req: Any) -> None:
        inv_id = _invocation_id_from_callback(cc)
        parent = self._get_invocation_span(inv_id)
        model = _safe_attr(req, "model", "") or "llm"
        attrs: dict[str, Any] = {"model": model}
        payload = _safe_llm_request_payload(req)
        span_id = self._client.emit_span_start(
            kind="LLM_CALL",
            name=str(model),
            parent_span_id=parent,
            attributes=attrs,
            payload=payload,
            payload_mime="application/json",
            payload_role="input",
        )
        with self._lock:
            self._llm_by_invocation[inv_id] = span_id

    def on_model_end(self, cc: Any, resp: Any) -> None:
        inv_id = _invocation_id_from_callback(cc)
        with self._lock:
            span_id = self._llm_by_invocation.get(inv_id)
        if not span_id:
            return
        attrs = _safe_llm_response_attrs(resp)
        payload = _safe_llm_response_payload(resp)
        self._client.emit_span_end(
            span_id,
            status="COMPLETED",
            attributes=attrs,
            payload=payload,
            payload_mime="application/json",
            payload_role="output",
        )
        # LLM span is done — the current-LLM pointer is cleared so
        # subsequent tool calls link to the INVOCATION again if no new
        # LLM_CALL is open.
        with self._lock:
            if self._llm_by_invocation.get(inv_id) == span_id:
                self._llm_by_invocation.pop(inv_id, None)

    # ------------------------------------------------------------------
    # Tool
    # ------------------------------------------------------------------

    def on_tool_start(self, tool: Any, tool_args: dict[str, Any], tool_context: Any) -> None:
        call_id = _safe_attr(tool_context, "function_call_id", "") or _safe_attr(tool, "name", "tool")
        inv_id = _invocation_id_from_callback(tool_context)
        parent = self._current_llm_span(inv_id) or self._get_invocation_span(inv_id)
        is_long_running = bool(_safe_attr(tool, "is_long_running", False))
        name = _safe_attr(tool, "name", "tool") or "tool"
        payload = _safe_json(tool_args)
        span_id = self._client.emit_span_start(
            kind="TOOL_CALL",
            name=name,
            parent_span_id=parent,
            attributes={"is_long_running": is_long_running},
            payload=payload,
            payload_mime="application/json",
            payload_role="args",
        )
        with self._lock:
            self._tools[call_id] = span_id
            if is_long_running:
                self._long_running.add(call_id)
        if is_long_running:
            self._client.emit_span_update(span_id, status="AWAITING_HUMAN")

    def on_tool_end(
        self,
        tool: Any,
        tool_context: Any,
        *,
        result: Optional[dict[str, Any]],
        error: Optional[BaseException],
    ) -> None:
        call_id = _safe_attr(tool_context, "function_call_id", "") or _safe_attr(tool, "name", "tool")
        with self._lock:
            span_id = self._tools.pop(call_id, None)
            self._long_running.discard(call_id)
        if not span_id:
            return
        if error is not None:
            self._client.emit_span_end(
                span_id,
                status="FAILED",
                error={"type": type(error).__name__, "message": str(error)},
            )
            return
        payload = _safe_json(result) if result is not None else None
        self._client.emit_span_end(
            span_id,
            status="COMPLETED",
            payload=payload,
            payload_mime="application/json",
            payload_role="result",
        )

    # ------------------------------------------------------------------
    # Events (state_delta + transfers)
    # ------------------------------------------------------------------

    def on_event(self, ic: Any, event: Any) -> None:
        inv_id = _safe_attr(ic, "invocation_id", "")
        actions = _safe_attr(event, "actions", None)
        if actions is None:
            return

        # state_delta → attributes on the enclosing span.
        state_delta = _safe_attr(actions, "state_delta", None)
        if state_delta:
            target_span = (
                self._current_llm_span(inv_id)
                or self._get_invocation_span(inv_id)
            )
            if target_span:
                attrs = {f"state_delta.{k}": _stringify(v) for k, v in state_delta.items()}
                if attrs:
                    self._client.emit_span_update(target_span, attributes=attrs)

        # Transfer → emit a TRANSFER span with a SpanLink to the target
        # agent (target_span_id unknown, so empty).
        transfer_to = _safe_attr(actions, "transfer_to_agent", None)
        if transfer_to:
            parent = self._get_invocation_span(inv_id)
            transfer_sid = self._client.emit_span_start(
                kind="TRANSFER",
                name=f"transfer_to_{transfer_to}",
                parent_span_id=parent,
                attributes={"target_agent": transfer_to},
            )
            self._client.emit_span_end(transfer_sid, status="COMPLETED")

    # ------------------------------------------------------------------
    # Control → session mutations
    # ------------------------------------------------------------------

    def queue_session_mutation(self, key: str, value: str) -> None:
        with self._lock:
            self._pending_mutations.append((key, value))

    def pending_session_mutations(self) -> list[tuple[str, str]]:
        with self._lock:
            out = list(self._pending_mutations)
            self._pending_mutations.clear()
            return out

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_invocation_span(self, inv_id: str) -> Optional[str]:
        with self._lock:
            return self._invocations.get(inv_id)

    def _current_llm_span(self, inv_id: str) -> Optional[str]:
        with self._lock:
            return self._llm_by_invocation.get(inv_id)


# ---------------------------------------------------------------------------
# Module-level helpers — defensive against ADK internals moving.
# ---------------------------------------------------------------------------


def _safe_attr(obj: Any, name: str, default: Any) -> Any:
    if obj is None:
        return default
    try:
        val = getattr(obj, name, default)
    except Exception:
        return default
    return val if val is not None else default


def _invocation_id_from_callback(cc: Any) -> str:
    # CallbackContext exposes invocation_context via private attr in
    # current ADK; fall back to invocation_id if present directly.
    inv_id = _safe_attr(cc, "invocation_id", "")
    if inv_id:
        return inv_id
    ic = _safe_attr(cc, "_invocation_context", None) or _safe_attr(cc, "invocation_context", None)
    return _safe_attr(ic, "invocation_id", "")


def _safe_json(obj: Any) -> Optional[bytes]:
    try:
        return json.dumps(obj, default=str, ensure_ascii=False).encode("utf-8")
    except Exception:
        return None


def _stringify(v: Any) -> str:
    try:
        return json.dumps(v, default=str, ensure_ascii=False)
    except Exception:
        return str(v)


def _safe_llm_request_payload(req: Any) -> Optional[bytes]:
    try:
        contents = _safe_attr(req, "contents", None)
        if contents is None:
            return None
        # Best-effort serialization: pydantic models expose model_dump.
        dumps = []
        for item in contents:
            dump = getattr(item, "model_dump", None)
            dumps.append(dump(mode="json") if callable(dump) else str(item))
        return _safe_json({"contents": dumps})
    except Exception:
        return None


def _safe_llm_response_attrs(resp: Any) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    usage = _safe_attr(resp, "usage_metadata", None)
    if usage is not None:
        for k in ("prompt_token_count", "candidates_token_count", "total_token_count"):
            v = _safe_attr(usage, k, None)
            if v is not None:
                attrs[k] = int(v) if isinstance(v, (int, float)) else str(v)
    return attrs


def _safe_llm_response_payload(resp: Any) -> Optional[bytes]:
    try:
        content = _safe_attr(resp, "content", None)
        if content is None:
            return None
        dump = getattr(content, "model_dump", None)
        if callable(dump):
            return _safe_json({"content": dump(mode="json")})
        return _safe_json({"content": str(content)})
    except Exception:
        return None
