"""ADK plugin that emits harmonograf spans for ADK lifecycle callbacks.

Observability-only: this plugin never makes orchestration decisions. All
plan, task, drift, and steering logic now lives in goldfive (see issue
#2); harmonograf just emits spans so the server's timeline /
Gantt renders per-invocation, per-model-call, and per-tool-call bars.

Per-ADK-agent attribution (harmonograf#74). A single ``goldfive.wrap``
run drives a tree of ADK agents — coordinator, specialists, AgentTool
wrappers, sequential/parallel/loop containers. The plugin stamps every
span with a per-agent harmonograf ``agent_id`` derived from the client's
root id and the ADK agent's name (``<client_agent_id>:<agent.name>``),
so the harmonograf Gantt renders one row per ADK agent instead of
collapsing the whole tree onto the client root. Parent-agent and kind
hints ride on the first span emitted by each agent via
``hgraf.agent.*`` attributes — the server harvests them into the
agent's ``metadata`` the first time it auto-registers the agent row.
See :meth:`HarmonografTelemetryPlugin.before_agent_callback`.

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

Duplicate-install safety: if two ``HarmonografTelemetryPlugin``
instances end up on the same ADK ``PluginManager`` (easy to do under
``goldfive.wrap`` + ``adk web`` — one comes from ``App.plugins``, one
from ``observe()`` or ``add_plugin``), the later instance detects the
earlier one on its first callback and silently disables itself. See
harmonograf #68 / goldfive #166.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

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


def _extract_current_task_id(ctx: Any) -> str:
    """Read ``goldfive.current_task_id`` out of an ADK callback context's session.state.

    Goldfive's ADK state-protocol mirror (``_adk_state_protocol``) stamps
    ``goldfive.current_task_id`` onto ``session.state`` on every
    ``before_run_callback`` — see goldfive issue #3. The server's ingest
    and the frontend's Task tab both key Trajectory / dependency
    rendering off a ``hgraf.task_id`` attribute on SpanStart frames; this
    helper pulls the goldfive-side value out of whichever shape the
    callback gives us (CallbackContext exposes ``session`` directly;
    ToolContext does too; InvocationContext does as well).

    ADK shallow-copies ``session.state`` into ``CallbackContext`` so
    *writes* on the callback side don't propagate back — this is a
    **read-only** use and the mirror has fired on ``before_run_callback``
    by the time any before-agent/tool/model callback runs, so the read
    sees the latest task id.

    Returns ``""`` when the context is non-goldfive (no state key present),
    malformed, or pre-plan — callers must treat that as a no-op and not
    invent a task id.
    """
    if ctx is None:
        return ""
    session = _safe_attr(ctx, "session", None)
    if session is None:
        inv = _safe_attr(ctx, "_invocation_context", None)
        if inv is not None:
            session = _safe_attr(inv, "session", None)
    if session is None:
        return ""
    state = _safe_attr(session, "state", None)
    if state is None:
        return ""
    try:
        value = state.get("goldfive.current_task_id", "")
    except Exception:  # noqa: BLE001 — defensive: telemetry must not raise
        return ""
    if not value:
        return ""
    return str(value)


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


# The ``llm.reasoning_trail`` aggregate on an INVOCATION span concatenates
# reasoning from every LLM_CALL child. We cap the inline version at 16 KiB
# — the UI collapses anything longer into a payload_ref role="reasoning".
# A coordinator that burned 100 LLM turns (e.g. a tight tool-loop) would
# otherwise shove tens of KiB onto one span attribute, which inflates every
# span-stream frame that carries the INVOCATION's SpanEnd.
REASONING_TRAIL_INLINE_MAX_BYTES: int = 16 * 1024


def _format_reasoning_trail(chunks: list[str]) -> str:
    """Concatenate per-LLM-call reasoning fragments with clear separators.

    Returns a single string suitable for the ``llm.reasoning_trail``
    attribute on an INVOCATION span. Empty input returns ``""``. Each
    chunk gets a ``[LLM call N]`` header so a reader scanning the
    aggregate can tell where one model turn ends and the next begins;
    separators make the trail scannable even when reasoning is
    paragraph-dense. Callers pass only *non-empty* chunks — the plugin
    filters empties before appending.
    """
    if not chunks:
        return ""
    parts: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        parts.append(f"[LLM call {i}]\n{chunk}")
    return "\n\n---\n\n".join(parts)


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
        # Parallel map from ``id(tool_context)`` -> ``invocation_id`` used
        # only by the cancellation cleanup path
        # (:meth:`_close_stale_spans_for_invocation`). Lets us flush tool
        # spans belonging to the cancelled invocation while leaving any
        # concurrent sibling invocation's tool spans intact. Kept as a
        # separate dict so the hot ``before_tool`` / ``after_tool`` path
        # pays a single extra dict write and nothing else. Entries are
        # removed by ``after_tool_callback`` / ``on_tool_error_callback``
        # on the normal path and by the cancellation helper on the
        # error path.
        self._tool_span_invocations: dict[int, str] = {}
        # Duplicate-install guard (harmonograf #68 / goldfive #166). Under
        # ``goldfive.wrap`` + ``adk web`` + ``App(plugins=[...])`` it is
        # easy to end up with two ``HarmonografTelemetryPlugin`` instances
        # attached to the same ADK ``PluginManager``: one from
        # ``App.plugins`` and one from a downstream ``observe()`` /
        # ``add_plugin`` call. Each instance fires every callback, so
        # every span appears twice in harmonograf's Gantt.
        #
        # On the first callback we inspect
        # ``invocation_context.plugin_manager.plugins`` and, if another
        # plugin named ``"harmonograf-telemetry"`` appears earlier in the
        # list, flip this flag and short-circuit every callback from then
        # on. The earliest instance is the authoritative emitter; later
        # duplicates go silent.
        #
        # Semantics: silent-by-default (dedup should not make noise on
        # healthy runs). We log at INFO exactly once per deduped
        # instance so operators can see what happened if they wonder why
        # their callbacks appear to be missing.
        self._disabled_as_duplicate: bool = False
        self._duplicate_log_emitted: bool = False

        # Per-ADK-agent attribution (harmonograf#74).
        #
        # ``_agent_stash`` maps ``invocation_id -> [per_agent_id, ...]`` as
        # a stack so nested sub-agent invocations (e.g. coordinator calls
        # an AgentTool which runs a specialist sub-Runner) pop back to the
        # caller's id when the callee's ``after_agent_callback`` fires.
        # Using ``invocation_id`` as the key handles ADK's
        # CallbackContext-rebuild pattern (plugins rebuild Context objects
        # between before/after hooks, so ``id(ctx)`` is unstable) and
        # co-exists with the existing ``_model_spans`` keying by
        # ``invocation_id``. Sub-Runners spawned by AgentTool get a fresh
        # ``invocation_id``, so they get their own stack slot — the parent
        # agent's stash is untouched. Both resolve via
        # :meth:`_resolve_agent_id`.
        #
        # ``_seen_agents`` is a set of ``(session_id, per_agent_id)``
        # tuples the plugin has already seen on this session. We stamp
        # ``hgraf.agent.*`` attributes on each agent's FIRST span only;
        # subsequent spans from the same agent skip those attributes so
        # every span doesn't pay the metadata cost. The server uses them
        # to auto-register the agent row on first-sight. Bounded by the
        # session lifetime — a long-running session with many agents will
        # accumulate entries but at ~tens-of-bytes per entry this is
        # negligible.
        self._agent_stash: dict[str, list[str]] = {}
        self._seen_agents: set[tuple[str, str]] = set()

        # harmonograf#113: nested-Runner invocation aliasing.
        #
        # ``goldfive.wrap`` produces a :class:`GoldfiveADKAgent` whose
        # ``_run_async_impl`` drives an internal :class:`InMemoryRunner`
        # around the user's real root agent. Under adk-web, ADK fires
        # ``before_run_callback`` TWICE for the SAME root agent — once
        # for the outer adk-web Runner (over ``GoldfiveADKAgent``),
        # once for goldfive's inner Runner (over the actual
        # coordinator). ``GoldfiveADKAgent`` copies its inner's
        # ``name`` through (so both invocations report the same
        # per-agent id from the plugin's POV) but the OUTER
        # invocation produces no LLM calls of its own — only the
        # inner does — so the outer INVOCATION span is a pure duplicate
        # wrapper that:
        #   * fragments the Gantt coordinator row into two identical
        #     bars,
        #   * confuses Drawer span selection (outer has no reasoning
        #     trail; inner carries the full chain-of-thought),
        #   * and on user cancel mid-run leaks as ``status=RUNNING``
        #     because ``on_cancellation`` closes only one of the two
        #     invocation ids.
        #
        # We detect the wrapper agent by class name
        # (``GoldfiveADKAgent`` or any subclass whose bases include
        # it) and short-circuit: no span opens, ``_invocation_spans``
        # skips the entry, and LLM calls — which all arrive from the
        # inner invocation_id — route to the inner's span unchanged.
        # Cancellation on the outer invocation_id becomes a harmless
        # no-op (no span to close); cancellation on the inner closes
        # the real span normally.
        #
        # Class-name detection instead of ``isinstance`` avoids a hard
        # dependency on goldfive from the client — the plugin stays
        # installable in an environment that doesn't ship goldfive at
        # all.
        self._goldfive_wrapper_invocations: set[str] = set()
        # Reasoning aggregation for the INVOCATION span (harmonograf#108).
        #
        # Every LLM_CALL finalize inside an invocation appends its
        # reasoning to ``_reasoning_trails[invocation_id]``. When the
        # invocation's ``after_run_callback`` fires (or the on-cancel
        # cleanup / on_run_end sweep runs), the buffered chunks are
        # formatted into a single ``llm.reasoning_trail`` attribute and
        # stamped onto the INVOCATION span's ``SpanEnd`` alongside
        # ``has_reasoning=True`` and ``reasoning_call_count=N``.
        #
        # Why this matters: the Drawer opens on an INVOCATION span when
        # a user clicks an agent row on the Gantt. Before the aggregate,
        # reasoning only lived on LLM_CALL children so a click on
        # ``coordinator_agent`` surfaced no reasoning at all — users had
        # to hunt each LLM_CALL child separately. The aggregate lets the
        # Drawer render the full agent-level chain-of-thought from the
        # span the user actually selected.
        #
        # Cleared on ``after_run_callback`` / cancel / on_run_end so the
        # buffer doesn't leak across concurrent or sequential runs.
        self._reasoning_trails: dict[str, list[str]] = {}
        # Lookup: per_agent_id -> (adk_name, parent_agent_id, kind, branch)
        # so we can re-stamp metadata attributes on the first span after
        # an ``after_agent_callback`` clears the stash but a span from
        # that agent still arrives (edge case: spans enqueued during
        # agent teardown). Populated by
        # :meth:`_register_agent_for_ctx`.
        self._agent_metadata: dict[str, dict[str, str]] = {}

    @property
    def client(self) -> Client:
        return self._client

    # ------------------------------------------------------------------
    # Per-agent attribution (harmonograf#74)
    # ------------------------------------------------------------------

    def _derive_agent_id(self, agent_name: str) -> str:
        """Return the stable per-ADK-agent harmonograf id.

        Format: ``<client.agent_id>:<adk_agent_name>``. Embedding the
        client's own agent_id as a prefix keeps the id globally unique
        across concurrent runs (the client's agent_id is already a
        UUID) while a suffix of the ADK agent.name gives operators a
        human-readable trailing token in the Gantt gutter.

        Returns the client's bare ``agent_id`` when ``agent_name`` is
        empty, preserving the pre-#74 attribution (everything collapsed
        onto the client root) as a safe degraded mode.
        """
        if not agent_name:
            return str(self._client.agent_id)
        return f"{self._client.agent_id}:{agent_name}"

    def _agent_kind_hint(self, agent: Any) -> str:
        """Best-effort ADK-class → harmonograf ``kind`` hint.

        The server does not currently do anything authoritative with the
        kind hint — it rides as metadata so operators can tell at a
        glance whether a row is a worker (``llm``), an AgentTool wrapper
        (``tool_wrapped``), a workflow container (``workflow``), or the
        tree root when the agent object is unavailable (``unknown``).
        New ADK agent classes fall through to ``unknown`` rather than
        crashing the callback — keep this robust: telemetry must never
        raise.
        """
        if agent is None:
            return "unknown"
        # Walk the MRO to handle user subclasses of LlmAgent /
        # SequentialAgent / etc. without having to import each class.
        for cls in type(agent).__mro__:
            n = cls.__name__
            if n in ("LlmAgent", "Agent"):
                return "llm"
            if n in ("SequentialAgent", "ParallelAgent", "LoopAgent"):
                return "workflow"
            if n in ("AgentTool",):
                # AgentTool wraps another agent — it's a tool from the
                # caller's perspective but harmonograf attributes work
                # done inside the wrapped agent directly to that agent's
                # row, not the AgentTool's row. Kept for completeness in
                # case ADK ever fires agent callbacks on AgentTool
                # itself (it currently doesn't).
                return "tool_wrapped"
        return "unknown"

    def _register_agent_for_ctx(self, ctx: Any) -> str:
        """Compute the per-agent id for ``ctx`` and prime the metadata cache.

        Called from ``before_agent_callback`` on every agent entry
        (including AgentTool sub-Runner roots — ADK propagates the
        plugin manager and fires agent callbacks inside sub-Runners
        too). Returns the per-agent id the caller should stash.

        Also populates ``_agent_metadata[per_agent_id]`` with the
        hgraf.agent.* payload the next emitting span from this agent
        will stamp on its first arrival at the server. The payload is
        intentionally minimal (name, parent, kind, branch) — the server
        parses strings into ``Agent.metadata`` without needing a proto
        schema change or a new wire event.
        """
        agent = _safe_attr(ctx, "agent", None)
        # Different ADK versions expose the agent under either
        # ``ctx.agent`` (InvocationContext) or
        # ``ctx._invocation_context.agent`` (CallbackContext). Fall back
        # through both. ``agent_name`` is the ReadonlyContext property.
        if agent is None:
            inv = _safe_attr(ctx, "_invocation_context", None)
            if inv is not None:
                agent = _safe_attr(inv, "agent", None)
        agent_name = ""
        if agent is not None:
            agent_name = str(_safe_attr(agent, "name", "") or "")
        if not agent_name:
            agent_name = str(_safe_attr(ctx, "agent_name", "") or "")
        per_agent_id = self._derive_agent_id(agent_name)
        # Parent: prefer agent.parent_agent.name (ADK BaseAgent exposes
        # this); fall back to parsing the InvocationContext.branch
        # (dot-delimited ancestry).
        parent_name = ""
        if agent is not None:
            parent = _safe_attr(agent, "parent_agent", None)
            if parent is not None:
                parent_name = str(_safe_attr(parent, "name", "") or "")
        if not parent_name:
            branch = _safe_attr(ctx, "branch", None)
            if branch is None:
                inv = _safe_attr(ctx, "_invocation_context", None)
                if inv is not None:
                    branch = _safe_attr(inv, "branch", None)
            if isinstance(branch, str) and branch:
                # Branch format: ``root.child.grandchild``. The current
                # agent sits at the tail; its immediate parent is
                # second-to-last. When branch has one segment the current
                # agent IS the root and there is no parent.
                segments = branch.split(".")
                if len(segments) >= 2:
                    parent_name = segments[-2]
        parent_id = self._derive_agent_id(parent_name) if parent_name else ""
        meta = {
            "hgraf.agent.name": agent_name,
            "hgraf.agent.kind": self._agent_kind_hint(agent),
        }
        if parent_id:
            meta["hgraf.agent.parent_id"] = parent_id
        # Also carry the ADK branch for forensic debugging — operators
        # can read it from the agent metadata to reconstruct the exact
        # dispatch path that produced an agent row.
        branch_val = _safe_attr(ctx, "branch", None)
        if branch_val is None:
            inv = _safe_attr(ctx, "_invocation_context", None)
            if inv is not None:
                branch_val = _safe_attr(inv, "branch", None)
        if isinstance(branch_val, str) and branch_val:
            meta["hgraf.agent.branch"] = branch_val
        self._agent_metadata[per_agent_id] = meta
        return per_agent_id

    def _resolve_agent_id(self, ctx: Any) -> str:
        """Return the per-agent id to stamp on a span emitted from ``ctx``.

        Lookup order:

        1. The top of the invocation's agent stack
           (``_agent_stash[invocation_id][-1]``) when ``ctx`` carries
           an invocation id and the stack is non-empty. This is the
           hot path — every span from within a before/after_agent
           window lands here.
        2. Fall back to the client's root ``agent_id`` otherwise —
           preserves pre-#74 behaviour for spans emitted outside any
           agent window (startup, teardown, unit tests driving one
           callback without a before_agent).
        """
        inv_id = str(_safe_attr(ctx, "invocation_id", "") or "")
        if inv_id:
            stack = self._agent_stash.get(inv_id)
            if stack:
                return stack[-1]
        return str(self._client.agent_id)

    def _stamp_agent_attrs(
        self, per_agent_id: str, attrs: Optional[dict[str, Any]] = None
    ) -> Optional[dict[str, Any]]:
        """Return span attributes augmented with ``hgraf.agent.*``.

        Contract (harmonograf#6):

        * ``hgraf.agent.name`` and ``hgraf.agent.kind`` are stamped on
          EVERY SpanStart emitted for a registered per-agent id. The
          frontend's Gantt/Graph/Timeline boundSpanId binding and the
          Task tab's Trajectory subtab key off these, and follow-up
          user turns reuse an already-registered per-agent id — so
          gating on first-sight caused the second INVOCATION span for
          the same agent to ship without ``hgraf.agent.kind`` and the
          row rendered as ``unknown`` (verified against session
          ``9fa8d4cb-...``).
        * ``hgraf.agent.parent_id`` and ``hgraf.agent.branch`` remain
          first-sight only — the server harvests them into ``Agent.metadata``
          on auto-register, and re-shipping them on every span would
          bloat span-stream bytes without changing behavior.

        The ``_seen_agents`` set still gates first-sight so parent/branch
        ride only on the first span per ``(session_id, per_agent_id)``
        pair.

        Returns a *new* dict when any stamping happened so the caller's
        ``attrs`` reference stays pristine; returns the input unchanged
        (possibly ``None``) when ``per_agent_id`` is the client root,
        which is registered via the Hello frame and must not carry
        agent-row metadata.
        """
        if per_agent_id == str(self._client.agent_id):
            # The client root id is registered by the Hello frame —
            # never stamp hgraf.agent.* on spans landing on the root.
            return attrs
        meta = self._agent_metadata.get(per_agent_id, {})
        if not meta:
            # Degraded path: span was emitted before ``before_agent_callback``
            # populated the metadata cache. Stamp a name-only entry so the
            # server still creates a row. Kind is left off — the server's
            # auto-register defaults it to ``unknown`` until a subsequent
            # span carries the real metadata.
            meta = {"hgraf.agent.name": per_agent_id.split(":", 1)[-1]}
        merged: dict[str, Any] = dict(attrs or {})
        # Always-stamp keys: name + kind land on every SpanStart so the
        # Task tab / boundSpanId pipeline works on follow-up turns.
        for k in ("hgraf.agent.name", "hgraf.agent.kind"):
            v = meta.get(k)
            if v is not None:
                merged.setdefault(k, v)
        # First-sight keys: parent_id + branch ride only on the first
        # span per (session_id, per_agent_id) pair.
        session_id = self._root_session_id or str(self._client.session_id or "")
        key = (session_id, per_agent_id)
        if key not in self._seen_agents:
            self._seen_agents.add(key)
            for k in ("hgraf.agent.parent_id", "hgraf.agent.branch"):
                v = meta.get(k)
                if v is not None:
                    merged.setdefault(k, v)
        return merged or None

    def _stamp_task_id(
        self, ctx: Any, attrs: Optional[dict[str, Any]] = None
    ) -> Optional[dict[str, Any]]:
        """Augment ``attrs`` with ``hgraf.task_id`` when ``ctx`` is in a goldfive run.

        Goldfive's ``_adk_state_protocol`` mirror writes
        ``goldfive.current_task_id`` into ADK ``session.state`` on each
        ``before_run_callback``. The server's ingest and the frontend's
        Task tab / Gantt / Timeline / Graph views all key task binding
        off a ``hgraf.task_id`` string attribute on SpanStart frames
        (``hgraf.task_id`` drives ``collectThinkingForTask`` and
        ``TaskRegistry.boundSpanId``). Nobody emits it today — fixing
        that is harmonograf#3.

        No-op for non-goldfive runs or pre-plan spans (state key absent
        or empty). Never invents a value.
        """
        task_id = _extract_current_task_id(ctx)
        if not task_id:
            return attrs
        merged: dict[str, Any] = dict(attrs or {})
        # Caller-provided attrs win — tests / future stamps can override.
        merged.setdefault("hgraf.task_id", task_id)
        return merged

    # ------------------------------------------------------------------
    # Reasoning-trail aggregation (harmonograf#108)
    # ------------------------------------------------------------------

    def _build_reasoning_trail_attrs(
        self, invocation_id: str
    ) -> tuple[dict[str, Any] | None, bytes | None, str, str]:
        """Drain the per-invocation reasoning buffer into span attributes.

        Returns ``(attributes, payload, payload_mime, payload_role)``
        ready to merge into a ``client.emit_span_end`` call. Attributes
        are ``None`` when no reasoning was captured during the
        invocation (the hot path for short tool-call runs that never
        surface chain-of-thought).

        When the aggregated trail fits within
        :data:`REASONING_TRAIL_INLINE_MAX_BYTES`, it rides inline as
        ``llm.reasoning_trail`` so the Drawer renders on open without a
        blob round-trip. Larger aggregates spill to a payload_ref with
        ``role="reasoning"`` — the Drawer already handles this shape for
        per-LLM_CALL reasoning, so no UI change is needed to surface
        jumbo trails.

        Always attaches ``has_reasoning=True`` and
        ``reasoning_call_count=N`` so the Drawer's toggle renders with a
        call count even when the trail itself is off-heap.

        Invocation ids without buffered reasoning are popped and return
        ``(None, None, ...)`` so the cleanup paths can call this
        unconditionally.
        """
        chunks = self._reasoning_trails.pop(invocation_id, None)
        if not chunks:
            return None, None, "application/json", "output"
        trail = _format_reasoning_trail(chunks)
        if not trail:
            return None, None, "application/json", "output"
        attrs: dict[str, Any] = {
            "has_reasoning": True,
            "reasoning_call_count": len(chunks),
        }
        encoded = trail.encode("utf-8")
        payload: bytes | None = None
        payload_mime = "application/json"
        payload_role = "output"
        if len(encoded) <= REASONING_TRAIL_INLINE_MAX_BYTES:
            attrs["llm.reasoning_trail"] = trail
        else:
            # Large trail: spill to a payload_ref. The Drawer's
            # ReasoningSection already resolves payload_refs with
            # role="reasoning" on open, so the wire shape matches the
            # per-LLM_CALL fallback path already in place.
            payload = encoded
            payload_mime = "text/plain"
            payload_role = "reasoning"
        return attrs, payload, payload_mime, payload_role

    # ------------------------------------------------------------------
    # Duplicate-install detection
    # ------------------------------------------------------------------

    def _maybe_disable_as_duplicate(self, ctx: Any) -> bool:
        """Return True when this plugin instance should stay silent.

        Inspects ``ctx.plugin_manager.plugins`` (ADK places the plugin
        manager on the invocation / callback / tool context) and checks
        whether another plugin with the same ``name`` appears *earlier*
        in the list than ``self``. When it does, this instance is a
        duplicate: the earlier instance will handle the callback and we
        should no-op.

        Idempotent — sets ``self._disabled_as_duplicate = True`` on the
        first detection and returns the cached flag on subsequent calls
        so the hot callback path doesn't walk the plugin list twice.
        Logs at INFO exactly once per deduped instance.

        Never raises: on any error (missing ``plugin_manager``, unusual
        ``plugins`` shape) falls through to "enabled" so the plugin's
        normal behaviour is preserved. The fix is an optimistic dedup,
        not a hard gate.
        """
        if self._disabled_as_duplicate:
            return True
        pm = _safe_attr(ctx, "plugin_manager", None)
        if pm is None:
            return False
        plugins = _safe_attr(pm, "plugins", None)
        if not isinstance(plugins, list) or not plugins:
            return False
        own_name = getattr(self, "name", "harmonograf-telemetry")
        # Find the first plugin with the same name. If that plugin is
        # not ``self``, we are a duplicate.
        for other in plugins:
            if _safe_attr(other, "name", None) != own_name:
                continue
            if other is self:
                return False
            # Earlier instance with the same name: we are the dupe.
            self._disabled_as_duplicate = True
            if not self._duplicate_log_emitted:
                self._duplicate_log_emitted = True
                log.info(
                    "telemetry_plugin: duplicate HarmonografTelemetryPlugin "
                    "instance detected on plugin_manager; this instance will "
                    "stay silent (earliest instance remains the authoritative "
                    "emitter). See harmonograf #68 / goldfive #166.",
                )
            return True
        return False

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
    # Cancellation cleanup
    # ------------------------------------------------------------------

    def _close_stale_spans_for_invocation(
        self,
        invocation_id: str,
        *,
        status: SpanStatus = SpanStatus.CANCELLED,
    ) -> None:
        """Close any open spans whose invocation context matches ``invocation_id``.

        Called when an ADK invocation is cancelled mid-flight (the asyncio
        task driving ``runner.run_async`` raises ``CancelledError``). In
        that path ADK's ``after_run_callback`` — and the
        ``after_model_callback`` / ``after_tool_callback`` for any
        in-flight sub-call — does not fire, because ADK places those
        plugin hooks after the ``async with Aclosing(execute_fn(...))``
        block in :meth:`google.adk.runners.Runner._exec_with_plugin`, not
        inside a ``finally``. Spans the plugin already ``emit_span_start``-ed
        for this invocation would otherwise stay ``status=RUNNING`` in the
        harmonograf DB forever (goldfive#167).

        Idempotent: safe to call after normal completion (the usual
        ``after_*`` callbacks already pop their entries, so this is a
        no-op in that case) and safe to call multiple times for the same
        ``invocation_id``.

        Scoped to a single invocation id — concurrent invocations running
        on other asyncio tasks are not touched. Tool-span cleanup is
        limited to tool spans opened *during* this invocation; ADK's
        ``ToolContext`` carries the ``invocation_id`` so we only pop the
        entries that actually belong to the cancelled invocation.

        Never raises: observability must not corrupt the main cancel
        path. Any per-emit failure is swallowed with a debug log.
        """
        if not invocation_id:
            return

        # Close model-call spans for this invocation.
        leftover = self._model_spans.pop(invocation_id, None)
        if leftover:
            for slot in leftover:
                reasoning = _join_reasoning(slot.reasoning_chunks)
                attributes = (
                    {"has_reasoning": True, "llm.reasoning": reasoning}
                    if reasoning
                    and len(reasoning.encode("utf-8")) <= REASONING_INLINE_MAX_BYTES
                    else ({"has_reasoning": True} if reasoning else None)
                )
                try:
                    self._client.emit_span_end(
                        slot.span_id,
                        status=status,
                        attributes=attributes,
                    )
                except Exception:  # noqa: BLE001 — defensive: telemetry must not raise
                    log.debug(
                        "telemetry_plugin: emit_span_end for model span %s raised",
                        slot.span_id,
                        exc_info=True,
                    )

        # Close tool-call spans opened by this invocation. ``_tool_spans``
        # is keyed by ``id(tool_context)``; we track the invocation id
        # alongside in ``_tool_span_invocations`` so cancellation can
        # filter to just the cancelled invocation's entries without
        # disturbing tool spans from a concurrent sibling invocation.
        stale_tool_keys = [
            key
            for key, inv in self._tool_span_invocations.items()
            if inv == invocation_id
        ]
        for key in stale_tool_keys:
            sid = self._tool_spans.pop(key, None)
            self._tool_span_invocations.pop(key, None)
            if sid is None:
                continue
            try:
                self._client.emit_span_end(sid, status=status)
            except Exception:  # noqa: BLE001 — defensive
                log.debug(
                    "telemetry_plugin: emit_span_end for tool span %s raised",
                    sid,
                    exc_info=True,
                )

        # Close the run span itself. Stamp any buffered reasoning trail
        # (harmonograf#108) so a user-cancelled invocation still shows
        # whatever chain-of-thought the coordinator produced up to the
        # cancel point — same Drawer surface as a normal completion.
        sid = self._invocation_spans.pop(invocation_id, None)
        if sid is not None:
            trail_attrs, trail_payload, trail_mime, trail_role = (
                self._build_reasoning_trail_attrs(invocation_id)
            )
            try:
                if trail_attrs is None:
                    self._client.emit_span_end(sid, status=status)
                elif trail_payload is not None:
                    self._client.emit_span_end(
                        sid,
                        status=status,
                        attributes=trail_attrs,
                        payload=trail_payload,
                        payload_mime=trail_mime,
                        payload_role=trail_role,
                    )
                else:
                    self._client.emit_span_end(
                        sid,
                        status=status,
                        attributes=trail_attrs,
                    )
            except Exception:  # noqa: BLE001 — defensive
                log.debug(
                    "telemetry_plugin: emit_span_end for run span %s raised",
                    sid,
                    exc_info=True,
                )

        # Drop any leftover agent-stack entries for this invocation
        # (harmonograf#74). On cancel, after_agent_callback may not fire
        # on the in-flight agent, leaving a stale stack entry keyed by
        # ``invocation_id``. Clear the whole entry — ``_agent_stash``
        # is keyed per-invocation so the cleanup is scoped to the
        # cancelled invocation without touching concurrent siblings.
        self._agent_stash.pop(invocation_id, None)

        # If the cancelled invocation was the cached ROOT, release the
        # root-session cache and tear down the per-session control sub.
        # Without this, a subsequent run after a user cancel would still
        # stamp spans onto the prior root's session id.
        if (
            self._root_invocation_id is not None
            and invocation_id == self._root_invocation_id
        ):
            cached = self._root_session_id
            self._root_session_id = None
            self._root_invocation_id = None
            close_sub = getattr(
                self._client, "close_additional_control_subscription", None
            )
            if callable(close_sub) and cached:
                try:
                    close_sub(cached)
                except Exception:  # noqa: BLE001 — defensive
                    log.debug(
                        "telemetry_plugin: close_additional_control_subscription "
                        "raised for %s during cancel",
                        cached,
                        exc_info=True,
                    )

    def on_cancellation(self, invocation_id: str) -> None:
        """Public hook invoked by goldfive's ADKAdapter on cancel.

        When :class:`goldfive.adapters.adk.ADKAdapter.invoke` catches
        ``asyncio.CancelledError`` (USER_STEER / USER_CANCEL /
        upstream cancel), it iterates the caller-supplied plugin list
        and calls ``plugin.on_cancellation(invocation_id)`` on every
        plugin that defines one. This plugin delegates to
        :meth:`_close_stale_spans_for_invocation` so any still-open
        run / LLM / tool spans are flushed with
        ``status=CANCELLED`` instead of leaking as
        ``status=RUNNING`` forever (goldfive#167).

        Safe to call from any context — never raises.
        """
        self._close_stale_spans_for_invocation(
            invocation_id, status=SpanStatus.CANCELLED
        )

    def on_run_end(self) -> None:
        """Public hook invoked by ``GoldfiveADKAgent._run_async_impl``'s
        ``finally`` block when an outer run exits (normal completion,
        cancellation, or early generator-close).

        Closes EVERY INVOCATION span this plugin opened during the run
        that is still in the open map (``_invocation_spans``). This is
        the broader sweep that complements :meth:`on_cancellation`
        (which is scoped to a single invocation_id): sub-Runner
        invocations spawned by ``AgentTool`` have their OWN
        ``invocation_id`` so the outer cancel path doesn't reach them,
        and ADK places ``after_run_callback`` outside a ``finally`` in
        :meth:`Runner._exec_with_plugin` — so a cancelled or
        early-closed sub-Runner leaks its INVOCATION span forever.

        Cleanup covers:
          * open INVOCATION spans (keyed by invocation_id)
          * any leftover model spans
          * any leftover tool spans

        Status is ``COMPLETED`` — the goldfive adapter also runs the
        targeted ``on_cancellation(invocation_id)`` with
        ``status=CANCELLED`` on the specific cancel path, so by the
        time we sweep here the "was this cancelled?" signal has
        already been applied to the spans that belong to that path.
        What remains are orphans whose after-callbacks ADK simply
        never fired — ``COMPLETED`` matches the "clean exit after the
        outer run is done" semantics for those.

        Safe to call from any context — never raises. Idempotent: a
        second call after a full close is a no-op because the open
        maps are empty.
        """
        # Close every open INVOCATION span. We don't want to filter to
        # "just the root" — sub-Runner invocations keyed by their own
        # ids are exactly the spans that leak through ADK's callback gap.
        # Stamp any buffered reasoning trail (harmonograf#108) on each —
        # same rationale as the normal ``after_run_callback`` path.
        stale_inv_ids = list(self._invocation_spans.keys())
        for inv_key in stale_inv_ids:
            sid = self._invocation_spans.pop(inv_key, None)
            if sid is None:
                continue
            trail_attrs, trail_payload, trail_mime, trail_role = (
                self._build_reasoning_trail_attrs(inv_key)
            )
            try:
                if trail_attrs is None:
                    self._client.emit_span_end(sid, status=SpanStatus.COMPLETED)
                elif trail_payload is not None:
                    self._client.emit_span_end(
                        sid,
                        status=SpanStatus.COMPLETED,
                        attributes=trail_attrs,
                        payload=trail_payload,
                        payload_mime=trail_mime,
                        payload_role=trail_role,
                    )
                else:
                    self._client.emit_span_end(
                        sid,
                        status=SpanStatus.COMPLETED,
                        attributes=trail_attrs,
                    )
            except Exception:  # noqa: BLE001 — defensive: telemetry must not raise
                log.debug(
                    "telemetry_plugin: on_run_end emit_span_end for "
                    "invocation span %s raised",
                    sid,
                    exc_info=True,
                )

        # Sweep any leftover model spans still open. Keyed by invocation_id.
        stale_model_keys = list(self._model_spans.keys())
        for inv_id in stale_model_keys:
            slots = self._model_spans.pop(inv_id, None) or []
            for slot in slots:
                try:
                    self._client.emit_span_end(
                        slot.span_id, status=SpanStatus.COMPLETED
                    )
                except Exception:  # noqa: BLE001 — defensive
                    log.debug(
                        "telemetry_plugin: on_run_end emit_span_end for "
                        "model span %s raised",
                        slot.span_id,
                        exc_info=True,
                    )

        # Sweep any leftover tool spans still open.
        stale_tool_keys = list(self._tool_spans.keys())
        for key in stale_tool_keys:
            sid = self._tool_spans.pop(key, None)
            self._tool_span_invocations.pop(key, None)
            if sid is None:
                continue
            try:
                self._client.emit_span_end(sid, status=SpanStatus.COMPLETED)
            except Exception:  # noqa: BLE001 — defensive
                log.debug(
                    "telemetry_plugin: on_run_end emit_span_end for "
                    "tool span %s raised",
                    sid,
                    exc_info=True,
                )

        # Drop any orphan reasoning-trail buffers. Normal path drained
        # these via ``_build_reasoning_trail_attrs`` during the
        # INVOCATION span_end above; anything left here came from an
        # after_model_callback that fired without a matching
        # before_run_callback (unit-test harnesses, partial ADK
        # contexts). Clearing prevents cross-run bleed on long-lived
        # plugin instances.
        self._reasoning_trails.clear()

    # ------------------------------------------------------------------
    # Invocation lifecycle
    # ------------------------------------------------------------------

    def _is_goldfive_wrapper_agent(self, agent: Any) -> bool:
        """Return True when ``agent`` is a goldfive.wrap wrapper agent.

        Walks ``type(agent).__mro__`` and looks for a class named
        ``GoldfiveADKAgent``. Matches the runtime class under
        ``goldfive.adapters.adk_wrap`` without importing goldfive — the
        client must stay installable in environments that ship only
        ADK (no goldfive). Returns False on any lookup failure so
        telemetry degrades to the pre-fix behaviour (duplicate
        INVOCATION spans) rather than raising.
        """
        if agent is None:
            return False
        try:
            for cls in type(agent).__mro__:
                if cls.__name__ == "GoldfiveADKAgent":
                    return True
        except Exception:  # noqa: BLE001 — defensive
            return False
        return False

    async def before_run_callback(self, *, invocation_context: Any) -> None:
        if self._maybe_disable_as_duplicate(invocation_context):
            return
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

        # Goldfive.wrap nested-Runner detection (harmonograf#113).
        # Skip the wrapper agent's INVOCATION entirely — see the
        # docstring on ``_goldfive_wrapper_invocations`` for why.
        if self._is_goldfive_wrapper_agent(agent):
            if inv_id:
                self._goldfive_wrapper_invocations.add(inv_id)
            return

        attrs: dict[str, Any] = {}
        if inv_id:
            attrs["adk.invocation_id"] = inv_id
        if adk_sid:
            attrs["adk.session_id"] = adk_sid
        # The INVOCATION span is the outermost bar for this agent's run.
        # Register the agent (before_agent_callback may not have fired
        # yet for the root in some ADK flows) and stamp the per-agent
        # id so the Gantt puts this invocation on the right row.
        per_agent_id = self._register_agent_for_ctx(invocation_context)
        augmented_attrs = self._stamp_agent_attrs(per_agent_id, attrs)
        augmented_attrs = self._stamp_task_id(invocation_context, augmented_attrs)
        sid = self._client.emit_span_start(
            kind=SpanKind.INVOCATION,
            name=name,
            attributes=augmented_attrs or None,
            agent_id=per_agent_id,
            session_id=self._stamp_session_id(invocation_context),
        )
        if inv_id:
            self._invocation_spans[inv_id] = sid
        else:
            # Fall back to object id so the end callback still balances.
            self._invocation_spans[str(id(invocation_context))] = sid

    async def after_run_callback(self, *, invocation_context: Any) -> None:
        if self._maybe_disable_as_duplicate(invocation_context):
            return
        inv_id = str(_safe_attr(invocation_context, "invocation_id", "") or "")
        # Goldfive.wrap wrapper short-circuit (harmonograf#113). The
        # matching ``before_run_callback`` opened no span for this
        # invocation_id, so this after_run is a no-op. Dropping the
        # tracking flag keeps the set from growing unbounded across
        # many sequential runs.
        if inv_id and inv_id in self._goldfive_wrapper_invocations:
            self._goldfive_wrapper_invocations.discard(inv_id)
            return
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
            # Stamp the aggregated reasoning trail (harmonograf#108) on
            # the INVOCATION span's SpanEnd so clicking an agent row in
            # the Gantt surfaces the agent's full chain-of-thought. The
            # trail attributes ride on the same frame as the status
            # transition — one SpanEnd, no extra SpanUpdate needed.
            trail_attrs, trail_payload, trail_mime, trail_role = (
                self._build_reasoning_trail_attrs(inv_id) if inv_id else (None, None, "application/json", "output")
            )
            if trail_attrs is None:
                self._client.emit_span_end(sid, status=SpanStatus.COMPLETED)
            elif trail_payload is not None:
                # Large aggregate: spill to a payload_ref with
                # role="reasoning". The Drawer's ReasoningSection
                # already handles this shape for per-LLM_CALL reasoning.
                self._client.emit_span_end(
                    sid,
                    status=SpanStatus.COMPLETED,
                    attributes=trail_attrs,
                    payload=trail_payload,
                    payload_mime=trail_mime,
                    payload_role=trail_role,
                )
            else:
                # Small aggregate: inline as llm.reasoning_trail attr.
                self._client.emit_span_end(
                    sid,
                    status=SpanStatus.COMPLETED,
                    attributes=trail_attrs,
                )

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
    # Per-agent lifecycle (harmonograf#74)
    # ------------------------------------------------------------------

    async def before_agent_callback(
        self, *, agent: Any, callback_context: Any
    ) -> None:
        """Push the per-agent id onto the invocation's agent stack.

        ADK fires this once per agent entry: the root agent, every
        sub-agent it transfers to, and every sub-agent wrapped by an
        AgentTool (AgentTool spawns a sub-Runner whose own root agent
        fires before_agent). The stack structure handles the nested
        case — coordinator pushes 'coordinator', AgentTool sub-Runner
        pushes 'research_agent', research's after_agent pops 'research',
        and the coordinator's subsequent spans stamp 'coordinator'
        again. Sub-Runner sessions get a fresh ``invocation_id`` so
        they have their own stack slot.

        Idempotent: the duplicate-install guard short-circuits. If
        called without a matching after_agent (e.g. a cancelled
        invocation), the stack is cleaned up by
        :meth:`_close_stale_spans_for_invocation` on cancellation.
        """
        if self._maybe_disable_as_duplicate(callback_context):
            return
        per_agent_id = self._register_agent_for_ctx(callback_context)
        inv_id = str(_safe_attr(callback_context, "invocation_id", "") or "")
        if not inv_id:
            # No invocation id: fall back to the ADK agent name so the
            # stack still balances. Rare — ADK always populates
            # invocation_id on a real callback — but a zero-id here
            # would silently collapse every agent onto one stack slot.
            inv_id = f"_agent:{_safe_attr(agent, 'name', 'unknown')}"
        self._agent_stash.setdefault(inv_id, []).append(per_agent_id)

    async def after_agent_callback(
        self, *, agent: Any, callback_context: Any
    ) -> None:
        """Pop the per-agent id off the invocation's agent stack.

        Balances :meth:`before_agent_callback`. When the stack drains
        the entry is removed wholesale so ``_agent_stash`` never grows
        unbounded across many sequential invocations.

        Defensive on underflow: a stray after_agent (e.g. ADK races the
        cancellation cleanup) is a no-op rather than raising. Telemetry
        must not surface internal bookkeeping errors into the
        orchestration control path.
        """
        if self._maybe_disable_as_duplicate(callback_context):
            return
        inv_id = str(_safe_attr(callback_context, "invocation_id", "") or "")
        if not inv_id:
            inv_id = f"_agent:{_safe_attr(agent, 'name', 'unknown')}"
        stack = self._agent_stash.get(inv_id)
        if stack:
            stack.pop()
            if not stack:
                self._agent_stash.pop(inv_id, None)

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
        if self._maybe_disable_as_duplicate(callback_context):
            return
        model = str(_safe_attr(llm_request, "model", "") or "")
        per_agent_id = self._resolve_agent_id(callback_context)
        attrs: dict[str, Any] = {"llm.model": model} if model else {}
        augmented_attrs = self._stamp_agent_attrs(per_agent_id, attrs or None)
        augmented_attrs = self._stamp_task_id(callback_context, augmented_attrs)
        sid = self._client.emit_span_start(
            kind=SpanKind.LLM_CALL,
            name=model or "llm_call",
            attributes=augmented_attrs or None,
            agent_id=per_agent_id,
            session_id=self._stamp_session_id(callback_context),
        )
        key = self._model_span_key(callback_context)
        self._model_spans.setdefault(key, deque()).append(_ModelSpanSlot(span_id=sid))

    async def after_model_callback(
        self, *, callback_context: Any, llm_response: Any
    ) -> None:
        if self._maybe_disable_as_duplicate(callback_context):
            return
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

            # Append to the per-invocation aggregate (harmonograf#108).
            # The INVOCATION span's SpanEnd will stamp the concatenated
            # trail so a click on an agent row surfaces the agent's
            # full chain-of-thought, not an empty section with reasoning
            # hidden on LLM_CALL children. We append the full text
            # regardless of inline-vs-payload_ref routing on the
            # per-call span — the trail itself makes the same
            # inline-or-spill decision at invocation close.
            inv_id_for_trail = str(
                _safe_attr(callback_context, "invocation_id", "") or ""
            )
            if inv_id_for_trail:
                self._reasoning_trails.setdefault(inv_id_for_trail, []).append(
                    reasoning
                )

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
        if self._maybe_disable_as_duplicate(tool_context):
            return
        tool_name = str(_safe_attr(tool, "name", "") or "tool")
        payload = _serialize_args(tool_args)
        per_agent_id = self._resolve_agent_id(tool_context)
        augmented_attrs = self._stamp_agent_attrs(per_agent_id, None)
        augmented_attrs = self._stamp_task_id(tool_context, augmented_attrs)
        sid = self._client.emit_span_start(
            kind=SpanKind.TOOL_CALL,
            name=tool_name,
            attributes=augmented_attrs or None,
            agent_id=per_agent_id,
            payload=payload,
            payload_role="input",
            session_id=self._stamp_session_id(tool_context),
        )
        key = id(tool_context)
        self._tool_spans[key] = sid
        # Track the invocation id so
        # :meth:`_close_stale_spans_for_invocation` can scope its cleanup
        # to just the cancelled invocation's open tool spans. Empty
        # invocation ids are skipped — the cancellation path is a
        # no-op for those and the normal after-tool cleanup path pops
        # on object-id, not invocation-id.
        inv_id = str(_safe_attr(tool_context, "invocation_id", "") or "")
        if inv_id:
            self._tool_span_invocations[key] = inv_id

    async def after_tool_callback(
        self,
        *,
        tool: Any,
        tool_args: Any,
        tool_context: Any,
        result: Any,
    ) -> None:
        if self._maybe_disable_as_duplicate(tool_context):
            return
        key = id(tool_context)
        sid = self._tool_spans.pop(key, None)
        self._tool_span_invocations.pop(key, None)
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
        if self._maybe_disable_as_duplicate(tool_context):
            return
        key = id(tool_context)
        sid = self._tool_spans.pop(key, None)
        self._tool_span_invocations.pop(key, None)
        if sid is None:
            return
        self._client.emit_span_end(
            sid,
            status=SpanStatus.FAILED,
            error={"type": type(error).__name__, "message": str(error)},
        )
