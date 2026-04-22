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
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .client import Client
from .enums import SpanKind, SpanStatus


@dataclass
class _ModelSpanSlot:
    """Per-LLM-call bookkeeping kept in ``_model_spans[invocation_id]``.

    ``reasoning_chunks`` accumulates partial reasoning text yielded by
    streaming providers (LiteLlm emits one ``LlmResponse`` per chunk in
    SSE mode); the non-partial finalize stitches them together and
    attaches the result as ``llm.reasoning`` on the span.
    """

    span_id: str
    reasoning_chunks: list[str] = field(default_factory=list)


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


def _adk_session_id(ctx: Any) -> str:
    """Extract the ADK session id from any ADK callback context.

    ADK's ``InvocationContext``, ``CallbackContext``, and ``ToolContext``
    all expose a ``session`` attribute that resolves to the
    ``sessions.Session`` row carrying a stable per-ADK-session ``id``.
    Without stamping this on every harmonograf span, a long-lived
    module-level :class:`Client` (the usual presentation_agent pattern)
    collapses every ADK session from the same Python process into
    whatever ``sess_<date>_<nnnn>`` the server generated on first
    Hello — see the issue description for the full pathology.

    Returns the session id as a ``str``, or ``""`` when the context is
    malformed or ADK is running without a session service. Empty
    strings cause :class:`Client` to fall back to its default
    ``session_id`` — the legacy pre-fix behaviour — which is the right
    failure mode when the context shape is unexpected.
    """
    session = _safe_attr(ctx, "session", None)
    if session is None:
        return ""
    sid = _safe_attr(session, "id", "") or ""
    return str(sid)


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


def _join_reasoning(chunks: list[str]) -> str:
    """Deduplicate and stitch reasoning fragments collected across partials.

    LiteLlm streaming yields one ``LlmResponse`` per reasoning delta, so
    the plugin sees ``["Let me ", "think step by ", "step."]``. The
    finalize chunk also carries the aggregated reasoning as a thought
    part, which would otherwise appear twice if we concatenated
    naively; we drop the finalize duplicate when it is a prefix-equal
    superstring of the stitched partials (LiteLlm's finalize path
    always reproduces the full running buffer).
    """
    if not chunks:
        return ""
    if len(chunks) == 1:
        return chunks[0]
    stitched_partials = "".join(chunks[:-1])
    last = chunks[-1]
    if last == stitched_partials:
        return stitched_partials
    # Finalize may include the aggregated reasoning plus a trailing
    # newline/punct; collapse the duplicate prefix when present.
    if last.startswith(stitched_partials):
        return last
    return stitched_partials + last


class HarmonografTelemetryPlugin(BasePlugin):  # type: ignore[misc]
    """Emit one harmonograf span per ADK lifecycle boundary.

    Three span kinds are produced:

    * ``INVOCATION`` — spans an entire ``runner.run_async`` call.
    * ``LLM_CALL`` — one per ``before_model`` / ``after_model`` pair.
    * ``TOOL_CALL`` — one per ``before_tool`` / ``after_tool`` or
      ``on_tool_error`` pair.

    Invocation and tool spans are balanced via the Python object id
    of the shared ADK context (``invocation_context`` / ``tool_context``).
    Model-call spans are balanced via a per-``invocation_id`` FIFO
    queue because ADK rebuilds the ``CallbackContext`` between
    ``before_model`` and ``after_model``, making object identity
    unreliable for that lifecycle pair.
    """

    def __init__(self, client: Client) -> None:
        try:
            super().__init__(name="harmonograf-telemetry")
        except TypeError:
            # BasePlugin fallback when ADK is not installed; the plugin
            # is never actually invoked in that case.
            pass
        self._client = client
        # Outer adk-web ``ctx.session.id`` cached from the ROOT
        # ``before_run_callback`` (harmonograf #65 / goldfive #161).
        # Under overlay + ``AgentTool``, ADK rebuilds ``CallbackContext``
        # per sub-invocation and the ``AgentTool`` sub-Runners mint
        # their own ``InMemorySessionService`` session ids. If we stamp
        # the per-ctx id on every span, a single adk-web run fans out
        # across N harmonograf sessions — one per sub-Runner — and the
        # plan view (which carries the goldfive ``Session.id``, itself
        # pinned to the outer adk-web session via goldfive #161) sits
        # alone on the outer session with no execution spans.
        #
        # Caching the ROOT session id and stamping it on every
        # subsequent span (root + sub-Runner) collapses everything onto
        # one session, matching where the goldfive events land. The
        # ``adk.session_id`` attribute still carries the per-ctx
        # sub-Runner id for forensic debugging (harmonograf#62).
        #
        # ``None`` between runs; populated by the first
        # ``before_run_callback`` and cleared by the matching
        # ``after_run_callback`` so the next adk-web invocation picks
        # up its own root id.
        self._root_session_id: str | None = None
        # Invocation id of the root invocation whose ``after_run`` will
        # clear ``_root_session_id``. Necessary because ADK fires one
        # ``before_run`` / ``after_run`` pair per ``AgentTool``
        # sub-Runner invocation too — we must not drop the cache on the
        # sub-Runner's after_run.
        self._root_invocation_id: str | None = None
        self._invocation_spans: dict[str, str] = {}
        # Model-call spans are keyed by ``invocation_id`` and tracked as
        # FIFO queues: ADK rebuilds the ``CallbackContext`` between
        # ``before_model`` and ``after_model`` (see
        # ``flows/llm_flows/base_llm_flow.py::_handle_after_model_callback``),
        # so ``id(callback_context)`` is never stable across a single LLM
        # call. An agent invocation never overlaps LLM calls on its own
        # flow, so a per-invocation FIFO balances before/after pairs
        # correctly while still supporting concurrent invocations.
        #
        # Each queue entry also carries a reasoning accumulator so
        # streaming partials (``LlmResponse.partial=True``) can be
        # stitched into a single ``llm.reasoning`` attribute at the
        # span's non-partial finalize.
        self._model_spans: dict[str, deque[_ModelSpanSlot]] = {}
        self._tool_spans: dict[int, str] = {}

    @property
    def client(self) -> Client:
        return self._client

    def _stamp_session_id(self, ctx: Any) -> str:
        """Return the harmonograf session id to stamp on a span.

        Prefers the ROOT adk-web session id cached by
        :meth:`before_run_callback` on the first invocation
        (harmonograf#65 / goldfive#161). This collapses spans from the
        root + every ``AgentTool`` sub-Runner onto one harmonograf
        session, matching where the goldfive events land (goldfive's
        ``Session.id`` is also pinned to the outer adk-web session id
        per goldfive#161). Without this rollup the span view fans out
        across N harmonograf sessions while the plan view sits alone
        on the root.

        When no root is cached (pre-connect, offline replay, unit-test
        harnesses that drive a single callback without a
        ``before_run_callback``), falls back to the per-ctx ADK session
        id, then to the Client's home session.

        The ``adk.session_id`` span attribute (stamped in
        :meth:`before_run_callback`) still carries the per-ctx
        sub-Runner session id for forensic debugging —
        harmonograf#62's hook is preserved.

        Returns ``""`` only when none of (cached root / ctx / home) is
        populated — matches the plugin's pre-connect degraded path.
        """
        if self._root_session_id:
            return self._root_session_id
        adk_sid = _adk_session_id(ctx)
        if adk_sid:
            return adk_sid
        return str(self._client.session_id or "")

    # ------------------------------------------------------------------
    # Invocation lifecycle
    # ------------------------------------------------------------------

    async def before_run_callback(self, *, invocation_context: Any) -> None:
        inv_id = str(_safe_attr(invocation_context, "invocation_id", "") or "")
        agent = _safe_attr(invocation_context, "agent")
        name = str(_safe_attr(agent, "name", "") or "invocation")
        adk_sid = _adk_session_id(invocation_context)

        # Root-invocation detection (harmonograf#65 / goldfive#161).
        # The first ``before_run_callback`` we see with no cached root
        # is treated as the adk-web top-level invocation. Cache its
        # ``ctx.session.id`` and drive every subsequent span stamp
        # through it so ``AgentTool`` sub-Runners (which rebuild the
        # CallbackContext and mint their own InMemorySessionService
        # session id) don't fan out into separate harmonograf sessions.
        # Also opens an additional control subscription scoped to the
        # cached session id so STEER targeting the outer adk-web
        # session finds a matching sub on the server (goldfive#162 /
        # harmonograf#54's pattern reintroduced for this specific
        # purpose — exactly ONE extra sub per run, not per sub-Runner).
        if self._root_session_id is None and adk_sid:
            self._root_session_id = adk_sid
            self._root_invocation_id = inv_id or None
            # Best-effort: open a per-session control subscription. The
            # Client's home sub is always live; this is additive and
            # makes the server's router prefer this sub when a STEER
            # arrives with session_id matching the outer adk-web
            # session. Shielded with hasattr so older Client builds
            # without the helper degrade gracefully (spans still stamp
            # the cached id — control routing falls back to home sub
            # + all-live-subs fan-out).
            open_sub = getattr(self._client, "open_additional_control_subscription", None)
            if callable(open_sub):
                try:
                    open_sub(adk_sid)
                except Exception:  # noqa: BLE001 — defensive: telemetry must not raise
                    log.debug(
                        "telemetry_plugin: open_additional_control_subscription "
                        "raised for %s",
                        adk_sid,
                        exc_info=True,
                    )

        attrs: dict[str, Any] = {}
        if inv_id:
            attrs["adk.invocation_id"] = inv_id
        if adk_sid:
            attrs["adk.session_id"] = adk_sid
        sid = self._client.emit_span_start(
            kind=SpanKind.INVOCATION,
            name=name,
            attributes=attrs or None,
            session_id=self._stamp_session_id(invocation_context),
        )
        if inv_id:
            self._invocation_spans[inv_id] = sid
        else:
            # Fall back to object id so the end callback still balances.
            self._invocation_spans[str(id(invocation_context))] = sid

    async def after_run_callback(self, *, invocation_context: Any) -> None:
        inv_id = str(_safe_attr(invocation_context, "invocation_id", "") or "")
        # Close any model-call spans that never saw a non-partial
        # finalize (error paths, client disconnect mid-stream). Without
        # this sweep such spans would leak forever.
        if inv_id:
            leftover = self._model_spans.pop(inv_id, None)
            if leftover:
                for slot in leftover:
                    reasoning = _join_reasoning(slot.reasoning_chunks)
                    attributes = (
                        {"has_reasoning": True, "llm.reasoning": reasoning}
                        if reasoning
                        and len(reasoning.encode("utf-8")) <= REASONING_INLINE_MAX_BYTES
                        else ({"has_reasoning": True} if reasoning else None)
                    )
                    self._client.emit_span_end(
                        slot.span_id,
                        status=SpanStatus.COMPLETED,
                        attributes=attributes,
                    )
        key = (
            inv_id if inv_id in self._invocation_spans else str(id(invocation_context))
        )
        sid = self._invocation_spans.pop(key, None)
        if sid is not None:
            self._client.emit_span_end(sid, status=SpanStatus.COMPLETED)

        # Clear the cached root when the ROOT invocation ends so the
        # next adk-web invocation picks up its own root session id.
        # Match on ``_root_invocation_id`` to avoid clearing on
        # ``AgentTool`` sub-Runner ``after_run_callback`` s — those
        # also fire against this plugin because ADK propagates the
        # plugin manager into sub-Runners. If we clear on the first
        # sub-Runner's after_run, the remaining sub-Runners and the
        # root would re-cache a different id, shattering the rollup.
        if (
            self._root_invocation_id is not None
            and inv_id
            and inv_id == self._root_invocation_id
        ):
            cached = self._root_session_id
            self._root_session_id = None
            self._root_invocation_id = None
            # Tear down the additional control subscription opened on
            # ``before_run_callback``. Best-effort: the Client may not
            # expose the helper (older build) or the sub may already
            # be gone from a reconnect cycle — either way we must not
            # raise from telemetry.
            close_sub = getattr(
                self._client, "close_additional_control_subscription", None
            )
            if callable(close_sub) and cached:
                try:
                    close_sub(cached)
                except Exception:  # noqa: BLE001 — defensive
                    log.debug(
                        "telemetry_plugin: close_additional_control_subscription "
                        "raised for %s",
                        cached,
                        exc_info=True,
                    )

    # ------------------------------------------------------------------
    # LLM call lifecycle
    # ------------------------------------------------------------------

    def _model_span_key(self, callback_context: Any) -> str:
        """Stable key for pairing ``before_model`` / ``after_model``.

        ``CallbackContext`` is rebuilt between the two callbacks
        (``_handle_after_model_callback`` in ADK's base flow constructs
        a fresh ``CallbackContext(invocation_context)`` before running
        plugin callbacks), so ``id(callback_context)`` changes. We fall
        back to the invocation id — stable for the lifetime of an ADK
        invocation — and multiplex via a FIFO queue so sequential LLM
        calls within an invocation balance correctly.
        """
        inv_id = str(_safe_attr(callback_context, "invocation_id", "") or "")
        if inv_id:
            return inv_id
        # Last-ditch fallback: use object id so we still *start* a span
        # and will eventually close it on teardown. This path only
        # triggers if ADK breaks the invocation_id contract.
        return f"_ctx:{id(callback_context)}"

    async def before_model_callback(
        self, *, callback_context: Any, llm_request: Any
    ) -> None:
        model = str(_safe_attr(llm_request, "model", "") or "")
        sid = self._client.emit_span_start(
            kind=SpanKind.LLM_CALL,
            name=model or "llm_call",
            attributes={"llm.model": model} if model else None,
            session_id=self._stamp_session_id(callback_context),
        )
        key = self._model_span_key(callback_context)
        self._model_spans.setdefault(key, deque()).append(_ModelSpanSlot(span_id=sid))

    async def after_model_callback(
        self, *, callback_context: Any, llm_response: Any
    ) -> None:
        key = self._model_span_key(callback_context)
        queue = self._model_spans.get(key)
        if not queue:
            return
        slot = queue[0]
        # Streaming providers (LiteLlm SSE) deliver partials first and a
        # non-partial finalize last. Accumulate reasoning from every
        # partial so the finalize can attach the full chain-of-thought,
        # but only close the span on the finalize.
        chunk = _extract_reasoning(llm_response)
        if chunk:
            slot.reasoning_chunks.append(chunk)

        partial = bool(_safe_attr(llm_response, "partial", False))
        if partial:
            return

        # Non-partial: close the span. Pop the slot from the queue so
        # the next before_model_callback within the same invocation
        # gets its own entry.
        queue.popleft()
        if not queue:
            self._model_spans.pop(key, None)

        error_info = _safe_attr(llm_response, "error_message")
        if error_info:
            self._client.emit_span_end(
                slot.span_id,
                status=SpanStatus.FAILED,
                error={"type": "LlmError", "message": str(error_info)},
            )
            return

        reasoning = _join_reasoning(slot.reasoning_chunks)
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
            slot.span_id,
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
            session_id=self._stamp_session_id(tool_context),
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
