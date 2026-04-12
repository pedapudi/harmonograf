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

import contextvars
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


def make_adk_plugin(client: Client) -> Any:
    """Build a harmonograf ADK ``BasePlugin`` bound to ``client``.

    This is the same plugin that :func:`attach_adk` installs, but
    returned standalone so callers who own an ADK ``App`` — for example
    the ``adk web`` CLI, which constructs its own ``Runner`` — can pass
    the plugin to the ``App(plugins=...)`` constructor and have it
    attached automatically. Also wires the STEER / INJECT_MESSAGE
    control handlers on ``client``.
    """
    from google.adk.plugins.base_plugin import BasePlugin

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

    def _handle_steer(event: Any) -> ControlAckSpec:
        body = event.payload.decode("utf-8", errors="replace") if event.payload else ""
        state.queue_session_mutation(STEERING_STATE_KEY, body)
        return ControlAckSpec(result="success")

    def _handle_inject(event: Any) -> ControlAckSpec:
        body = event.payload.decode("utf-8", errors="replace") if event.payload else ""
        state.queue_session_mutation(INJECT_STATE_KEY, body)
        return ControlAckSpec(result="success")

    def _handle_status_query(event: Any) -> ControlAckSpec:
        """Respond to STATUS_QUERY with a plain-text description of current activity."""
        parts: list[str] = []

        # Current activity reported via heartbeat tracking.
        activity = client._current_activity
        if activity:
            parts.append(activity)

        # Latest accumulated LLM streaming text across all in-flight LLM spans.
        with state._lock:
            streaming_texts = list(state._llm_streaming_text.values())
        if streaming_texts:
            # Pick the longest accumulated text (most informative).
            latest = max(streaming_texts, key=len)
            if len(latest) > 10:
                snippet = latest[:120].replace("\n", " ")
                if len(latest) > 120:
                    parts.append(f"LLM thinking: {snippet}\u2026")
                else:
                    parts.append(f"LLM: {snippet}")

        # Active tool calls: report by span id lookup isn't straightforward
        # (tool span ids are values in _tools), so report the count.
        with state._lock:
            active_tool_count = len(state._tools)
        if active_tool_count:
            parts.append(f"{active_tool_count} tool call(s) in flight")

        report = " | ".join(parts) if parts else "No active task."
        return ControlAckSpec(result="success", detail=report)

    client.on_control("STEER", _handle_steer)
    client.on_control("INJECT_MESSAGE", _handle_inject)
    client.on_control("STATUS_QUERY", _handle_status_query)

    return plugin


def attach_adk(runner: Any, client: Client) -> AdkAdapter:
    """Install a harmonograf plugin on the ADK runner and return the
    adapter handle. Equivalent to :func:`make_adk_plugin` followed by
    appending the plugin to ``runner.plugin_manager.plugins``.
    """
    plugin = make_adk_plugin(client)
    try:
        runner.plugin_manager.plugins.append(plugin)
    except Exception as e:
        raise RuntimeError(f"attach_adk: could not install plugin on runner: {e}") from e
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
        # invocation_id → (agent_id, session_id) the spans were emitted under,
        # so SpanEnd can re-route to the same row even if context routing fails.
        self._invocation_route: dict[str, tuple[str, str]] = {}
        # Session mutations queued by control handlers — surfaced via
        # pending_session_mutations() for agent code to apply.
        self._pending_mutations: list[tuple[str, str]] = []
        # Multi-session routing: ADK sub-runners (AgentTool) create fresh
        # ADK session ids per sub-invocation, but those should land in the
        # SAME harmonograf session as the enclosing root run. A PER-INSTANCE
        # ContextVar gives us two guarantees at once:
        #
        #   * Concurrent top-level /run calls live in independent asyncio
        #     Tasks whose context copies are independent — each sees an
        #     empty CV and mints its own harmonograf session.
        #   * AgentTool sub-runners execute inline (``await`` on the parent
        #     task) and naturally inherit the CV — their fresh ADK
        #     session id aliases back to the parent's harmonograf session.
        #
        # Per-instance (rather than module-level) means two different
        # ``_AdkState`` objects can't leak state into each other, which
        # matters both for tests that share a process and for future
        # callers who attach multiple plugins on one Client.
        self._adk_to_h_session: dict[str, str] = {}
        self._current_root_hsession_var: contextvars.ContextVar[str] = (
            contextvars.ContextVar(
                f"_harmonograf_current_root_hsession_{id(self)}", default=""
            )
        )
        # invocation_id → token from ContextVar.set(), so on_invocation_end
        # can reset the var in LIFO order.
        self._route_tokens: dict[str, Any] = {}
        # LLM span_id → cumulative streaming text length. Partial events bump
        # this so the frontend can render thinking tick marks on the in-flight
        # LLM block. Task #12 (B4 liveness).
        self._llm_stream_len: dict[str, int] = {}
        # LLM span_id → partial-event counter, also used as a monotonic
        # progress pulse so renderers can pulse/tick even when the partial
        # text has no natural length (e.g. tool-call streaming).
        self._llm_stream_ticks: dict[str, int] = {}
        # LLM span_id → accumulated streaming text for popover "thinking".
        self._llm_streaming_text: dict[str, str] = {}
        # agent_id → cumulative invocation count for that agent (iteration attribute).
        self._invocation_count: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------

    def on_invocation_start(self, ic: Any) -> None:
        inv_id = _safe_attr(ic, "invocation_id", "")
        agent_id, hsession_id = self._route_from_context(ic, opening_root=True)
        if hsession_id:
            token = self._current_root_hsession_var.set(hsession_id)
            with self._lock:
                self._route_tokens[inv_id] = token
        name = agent_id or "agent"
        # Track per-agent invocation count for the "iteration" attribute.
        with self._lock:
            iteration = self._invocation_count.get(name, 0) + 1
            self._invocation_count[name] = iteration
        attrs: dict[str, Any] = {
            "invocation_id": inv_id,
            "user_id": _safe_attr(getattr(ic, "user_id", None), "__str__", "") or str(_safe_attr(ic, "user_id", "")),
            "iteration": iteration,
        }
        adk_session_id = _safe_attr(getattr(ic, "session", None), "id", "")
        if adk_session_id:
            attrs["adk_session_id"] = adk_session_id
        # Emit agent description and class if available from the ADK Agent object.
        agent = _safe_attr(ic, "agent", None)
        agent_desc = _safe_attr(agent, "description", "") if agent is not None else ""
        if agent_desc:
            attrs["agent_description"] = str(agent_desc)
        if agent is not None:
            agent_class = type(agent).__name__
            if agent_class and agent_class not in ("NoneType", "object"):
                attrs["agent_class"] = agent_class
        self._client.set_current_activity(f"Starting invocation of {name}")
        attrs["task_report"] = f"Starting: {name}"
        span_id = self._client.emit_span_start(
            kind="INVOCATION",
            name=name,
            attributes=attrs,
            agent_id=agent_id or None,
            session_id=hsession_id or None,
        )
        with self._lock:
            self._invocations[inv_id] = span_id
            self._invocation_route[inv_id] = (agent_id, hsession_id)

    def on_invocation_end(self, ic: Any) -> None:
        inv_id = _safe_attr(ic, "invocation_id", "")
        with self._lock:
            span_id = self._invocations.pop(inv_id, None)
            self._llm_by_invocation.pop(inv_id, None)
            self._invocation_route.pop(inv_id, None)
            token = self._route_tokens.pop(inv_id, None)
        if token is not None:
            try:
                self._current_root_hsession_var.reset(token)
            except (LookupError, ValueError):
                pass
        if span_id:
            self._client.emit_span_end(span_id, status="COMPLETED")

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------

    def on_model_start(self, cc: Any, req: Any) -> None:
        inv_id = _invocation_id_from_callback(cc)
        parent = self._get_invocation_span(inv_id)
        agent_id, hsession_id = self._route_from_callback_or_invocation(cc, inv_id)
        model = _safe_attr(req, "model", "") or "llm"
        attrs: dict[str, Any] = {"model": model}
        if model and model != "llm":
            attrs["model_name"] = str(model)
        # Count messages and build request_preview for popover.
        contents = _safe_attr(req, "contents", None)
        msg_count = len(contents) if contents is not None else 0
        if msg_count:
            attrs["message_count"] = msg_count
        try:
            if contents:
                preview_parts = []
                for item in contents:
                    dump = getattr(item, "model_dump", None)
                    preview_parts.append(str(dump(mode="json") if callable(dump) else item))
                request_preview = " ".join(preview_parts)[:200]
                attrs["request_preview"] = request_preview
        except Exception:
            pass
        self._client.set_current_activity(
            f"Calling {model} with {msg_count} message{'s' if msg_count != 1 else ''}"
        )
        payload = _safe_llm_request_payload(req)
        span_id = self._client.emit_span_start(
            kind="LLM_CALL",
            name=str(model),
            parent_span_id=parent,
            attributes=attrs,
            payload=payload,
            payload_mime="application/json",
            payload_role="input",
            agent_id=agent_id or None,
            session_id=hsession_id or None,
        )
        with self._lock:
            self._llm_by_invocation[inv_id] = span_id

    def on_model_end(self, cc: Any, resp: Any) -> None:
        inv_id = _invocation_id_from_callback(cc)
        with self._lock:
            span_id = self._llm_by_invocation.get(inv_id)
            streaming_text = self._llm_streaming_text.get(span_id, "") if span_id else ""
        if not span_id:
            return
        attrs = _safe_llm_response_attrs(resp)
        # Add response_preview and finish_reason for popover.
        try:
            content = _safe_attr(resp, "content", None)
            if content is not None:
                parts = _safe_attr(content, "parts", None) or []
                response_text = "".join(
                    _safe_attr(p, "text", "") or "" for p in parts
                )
                if response_text:
                    attrs["response_preview"] = response_text[:200]
        except Exception:
            pass
        finish_reason = _safe_attr(resp, "finish_reason", None)
        if finish_reason is not None:
            try:
                attrs["finish_reason"] = str(finish_reason)
            except Exception:
                pass
        # Set activity description from accumulated streaming text if available.
        if streaming_text:
            self._client.set_current_activity(
                f"Received response: {streaming_text[:60]}\u2026"
            )
        else:
            self._client.set_current_activity("Processing model response")
        payload = _safe_llm_response_payload(resp)
        self._client.emit_span_end(
            span_id,
            status="COMPLETED",
            attributes=attrs,
            payload=payload,
            payload_mime="application/json",
            payload_role="output",
        )
        # Emit task_report on the enclosing INVOCATION span: what the LLM
        # just did and what it's planning to do next (tool calls, if any).
        with self._lock:
            invocation_span_id = self._invocations.get(inv_id)
        if invocation_span_id:
            planned: list[str] = []
            try:
                candidates = _safe_attr(resp, "candidates", None)
                if candidates:
                    for candidate in list(candidates)[:1]:
                        cand_content = _safe_attr(candidate, "content", None)
                        if cand_content is not None:
                            cand_parts = _safe_attr(cand_content, "parts", None) or []
                            for part in cand_parts:
                                fc = _safe_attr(part, "function_call", None)
                                if fc is not None:
                                    fc_name = _safe_attr(fc, "name", None)
                                    if fc_name:
                                        planned.append(f"call {fc_name}")
            except Exception:
                pass
            if planned:
                description = f"Planning: {', '.join(planned)}"
            else:
                description = "Processing response"
            if streaming_text:
                snippet = streaming_text[:80].replace("\n", " ")
                if planned:
                    description = f"{snippet}\u2026 \u2192 {description}"
                else:
                    description = snippet
            self._client.emit_span_update(
                invocation_span_id,
                attributes={"task_report": description, "current_task": description},
            )
        # LLM span is done — the current-LLM pointer is cleared so
        # subsequent tool calls link to the INVOCATION again if no new
        # LLM_CALL is open.
        with self._lock:
            if self._llm_by_invocation.get(inv_id) == span_id:
                self._llm_by_invocation.pop(inv_id, None)
            self._llm_stream_len.pop(span_id, None)
            self._llm_stream_ticks.pop(span_id, None)
            self._llm_streaming_text.pop(span_id, None)

    # ------------------------------------------------------------------
    # Tool
    # ------------------------------------------------------------------

    def on_tool_start(self, tool: Any, tool_args: dict[str, Any], tool_context: Any) -> None:
        call_id = _safe_attr(tool_context, "function_call_id", "") or _safe_attr(tool, "name", "tool")
        inv_id = _invocation_id_from_callback(tool_context)
        parent = self._current_llm_span(inv_id) or self._get_invocation_span(inv_id)
        agent_id, hsession_id = self._route_from_callback_or_invocation(tool_context, inv_id)
        is_long_running = bool(_safe_attr(tool, "is_long_running", False))
        name = _safe_attr(tool, "name", "tool") or "tool"
        payload = _safe_json(tool_args)
        tool_attrs: dict[str, Any] = {"is_long_running": is_long_running, "tool_name": name}
        # Emit a preview of tool arguments so the popover can show what the
        # tool is doing (truncated to 300 chars).
        if tool_args:
            try:
                args_preview = json.dumps(tool_args, default=str, ensure_ascii=False)[:300]
                tool_attrs["tool_args_preview"] = args_preview
            except Exception:
                pass
        if _is_agent_tool(tool):
            target_agent_name = (
                _safe_attr(_safe_attr(tool, "agent", None), "name", "") or name
            )
            self._client.set_current_activity(f"Transferring to {target_agent_name}")
        else:
            self._client.set_current_activity(f"Calling tool {name}")
        span_id = self._client.emit_span_start(
            kind="TOOL_CALL",
            name=name,
            parent_span_id=parent,
            attributes=tool_attrs,
            payload=payload,
            payload_mime="application/json",
            payload_role="args",
            agent_id=agent_id or None,
            session_id=hsession_id or None,
        )
        with self._lock:
            self._tools[call_id] = span_id
            if is_long_running:
                self._long_running.add(call_id)
        if is_long_running:
            self._client.emit_span_update(span_id, status="AWAITING_HUMAN")

        # AgentTool dispatch reads as a sub-agent transfer in the Gantt.
        # Emit a TRANSFER span on the PARENT agent's row (coordinator),
        # with LINK_INVOKED back to the child TOOL_CALL so the frontend
        # can draw the cross-row arrow into the sub-agent's lane.
        if _is_agent_tool(tool):
            target_agent_name = (
                _safe_attr(_safe_attr(tool, "agent", None), "name", "") or name
            )
            transfer_sid = self._client.emit_span_start(
                kind="TRANSFER",
                name=f"transfer_to_{target_agent_name}",
                parent_span_id=parent,
                attributes={
                    "target_agent": target_agent_name,
                    "via": "agent_tool",
                },
                links=[
                    {
                        "target_span_id": span_id,
                        "target_agent_id": target_agent_name,
                        "relation": "INVOKED",
                    }
                ],
                agent_id=agent_id or None,
                session_id=hsession_id or None,
            )
            self._client.emit_span_end(transfer_sid, status="COMPLETED")

    def on_tool_end(
        self,
        tool: Any,
        tool_context: Any,
        *,
        result: Optional[dict[str, Any]],
        error: Optional[BaseException],
    ) -> None:
        call_id = _safe_attr(tool_context, "function_call_id", "") or _safe_attr(tool, "name", "tool")
        tool_name = _safe_attr(tool, "name", "tool") or "tool"
        with self._lock:
            span_id = self._tools.pop(call_id, None)
            self._long_running.discard(call_id)
        if not span_id:
            return
        self._client.set_current_activity(f"Completed tool {tool_name}")
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
        agent_id, hsession_id = self._route_from_context(ic)

        # Partial LLM events → liveness ticks on the in-flight LLM span so
        # the frontend can render a thinking progress indicator. ADK emits
        # these with `partial=True` while the underlying model streams; the
        # final event (partial False/None) is handled by after_model_callback.
        # Task #12 (B4 liveness).
        if _safe_attr(event, "partial", False):
            with self._lock:
                llm_span_id = self._llm_by_invocation.get(inv_id)
            if llm_span_id:
                text_len = _event_text_len(event)
                # Accumulate partial text for popover "thinking" display.
                partial_text = _event_text(event)
                with self._lock:
                    if text_len > self._llm_stream_len.get(llm_span_id, 0):
                        self._llm_stream_len[llm_span_id] = text_len
                    ticks = self._llm_stream_ticks.get(llm_span_id, 0) + 1
                    self._llm_stream_ticks[llm_span_id] = ticks
                    cur_len = self._llm_stream_len[llm_span_id]
                    if partial_text:
                        accumulated = (self._llm_streaming_text.get(llm_span_id, "") + partial_text)
                        if len(accumulated) > 500:
                            accumulated = "..." + accumulated[-480:]
                        self._llm_streaming_text[llm_span_id] = accumulated
                    streaming_text = self._llm_streaming_text.get(llm_span_id)
                update_attrs: dict[str, Any] = {
                    "streaming_text_len": cur_len,
                    "streaming_tick": ticks,
                }
                if streaming_text:
                    update_attrs["streaming_text"] = streaming_text
                self._client.emit_span_update(
                    llm_span_id,
                    attributes=update_attrs,
                )
            # Partial events don't carry actions we care about, so bail out
            # before the transfer/state_delta bookkeeping below.
            return

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
                links=[{"target_agent_id": transfer_to, "relation": "INVOKED"}],
                agent_id=agent_id or None,
                session_id=hsession_id or None,
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

    def _route_from_callback_or_invocation(
        self, cc: Any, inv_id: str
    ) -> tuple[str, str]:
        """Resolve (agent_id, harmonograf_session_id) for a callback that
        carries an InvocationContext (CallbackContext, ToolContext, …).
        Falls back to whatever route the invocation was opened under so a
        SpanEnd that can't see the context still lands on the same row.
        """
        ic = _safe_attr(cc, "_invocation_context", None) or _safe_attr(
            cc, "invocation_context", None
        )
        if ic is None and _safe_attr(cc, "agent", None) is not None:
            ic = cc
        agent_id, hsession_id = (
            self._route_from_context(ic) if ic is not None else ("", "")
        )
        if not agent_id or not hsession_id:
            with self._lock:
                fallback = self._invocation_route.get(inv_id, ("", ""))
            agent_id = agent_id or fallback[0]
            hsession_id = hsession_id or fallback[1]
        return agent_id, hsession_id

    def _route_from_context(
        self, ic: Any, *, opening_root: bool = False
    ) -> tuple[str, str]:
        """Resolve (agent_id, harmonograf_session_id) from an
        InvocationContext-shaped object.

        Routing rules, in order:

          1. If the ADK session id is already in the pool, reuse it —
             this covers repeat callbacks on an established session.
          2. Otherwise, consult the ContextVar. If a root hsession is
             already set in this asyncio Task's Context, the current
             invocation is nested under it (AgentTool sub-run, which
             executes inside the parent task), so alias to it.
          3. Otherwise, if ``opening_root`` is set, mint a brand-new
             harmonograf session id for this ADK session id.

        Concurrent top-level /run calls land in independent asyncio
        Tasks, so each sees an empty ContextVar and hits rule 3 — no
        cross-request aliasing. AgentTool sub-invocations run within
        the parent Task, so they see the parent's ContextVar and hit
        rule 2.
        """
        if ic is None:
            return "", ""
        agent = _safe_attr(ic, "agent", None)
        agent_id = _safe_attr(agent, "name", "") if agent is not None else ""
        session = _safe_attr(ic, "session", None)
        adk_session_id = (
            _safe_attr(session, "id", "") if session is not None else ""
        )
        with self._lock:
            mapped = self._adk_to_h_session.get(adk_session_id, "")
        if not mapped:
            parent_hsession = self._current_root_hsession_var.get()
            if parent_hsession:
                mapped = parent_hsession
            elif opening_root:
                mapped = _harmonograf_session_id_for_adk(adk_session_id)
            if adk_session_id and mapped:
                with self._lock:
                    self._adk_to_h_session.setdefault(adk_session_id, mapped)
                    mapped = self._adk_to_h_session[adk_session_id]
        return agent_id or "", mapped


# ---------------------------------------------------------------------------
# Module-level helpers — defensive against ADK internals moving.
# ---------------------------------------------------------------------------


_HSESSION_SAFE = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


def _harmonograf_session_id_for_adk(adk_session_id: str) -> str:
    """Build a harmonograf session_id (regex ^[a-zA-Z0-9_-]{1,128}$) that
    encodes an ADK session id. Non-matching characters are replaced with
    ``_``; oversize ids are truncated to fit the 128-char limit while
    keeping the ``adk_`` prefix readable.
    """
    if not adk_session_id:
        return ""
    safe = "".join(c if c in _HSESSION_SAFE else "_" for c in adk_session_id)
    out = f"adk_{safe}"
    return out[:128]


def _is_agent_tool(tool: Any) -> bool:
    """Structural check — True when ``tool`` is an ADK ``AgentTool``.

    Uses ``isinstance`` (preferred) when ADK is importable, falling back
    to a duck-typed check for a ``.agent`` attribute that itself looks
    like an ADK agent. Name-based matching is deliberately avoided.
    """
    try:
        from google.adk.tools.agent_tool import AgentTool  # type: ignore

        if isinstance(tool, AgentTool):
            return True
    except Exception:
        pass
    agent = getattr(tool, "agent", None)
    if agent is None:
        return False
    return hasattr(agent, "name") and hasattr(agent, "description")


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


def _event_text_len(event: Any) -> int:
    """Cumulative text length across all text parts in an event's content."""
    content = _safe_attr(event, "content", None)
    if content is None:
        return 0
    parts = _safe_attr(content, "parts", None) or []
    total = 0
    for p in parts:
        text = _safe_attr(p, "text", None)
        if isinstance(text, str):
            total += len(text)
    return total


def _event_text(event: Any) -> str:
    """Concatenated text across all text parts in an event's content."""
    content = _safe_attr(event, "content", None)
    if content is None:
        return ""
    parts = _safe_attr(content, "parts", None) or []
    pieces: list[str] = []
    for p in parts:
        text = _safe_attr(p, "text", None)
        if isinstance(text, str):
            pieces.append(text)
    return "".join(pieces)


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
