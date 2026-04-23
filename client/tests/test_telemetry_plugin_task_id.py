"""Tests for ``hgraf.task_id`` SpanStart stamping (harmonograf#3).

Goldfive mirrors ``goldfive.current_task_id`` into ADK's
``session.state`` on every ``before_run_callback`` via
``_adk_state_protocol``. The harmonograf server's ingest and the
frontend's Task tab / Trajectory subtab, TaskRegistry.boundSpanId, and
Gantt/Graph/Timeline dependency arrows ALL key off a ``hgraf.task_id``
string attribute on SpanStart frames — but pre-fix nobody emitted it
(verified: 0/44 spans in a fresh e2e session carried the attribute).

This module verifies:

* Every SpanStart (INVOCATION / LLM_CALL / TOOL_CALL) carries
  ``hgraf.task_id`` when ``session.state['goldfive.current_task_id']``
  is populated.
* Absent / empty state key → no attribute stamped (non-goldfive runs,
  pre-plan spans must not carry an invented id).
* Task id flips between turns: the follow-up SpanStart carries the
  NEW task id, not the cached first-turn value.
"""

from __future__ import annotations

from typing import Any

import pytest

from harmonograf_client.buffer import EnvelopeKind
from harmonograf_client.client import Client
from harmonograf_client.telemetry_plugin import HarmonografTelemetryPlugin

from tests._fixtures import FakeTransport, make_factory


ROOT_SESSION = "root-session-task-id"
CLIENT_AGENT_ID = "client-root-agent-task-id"


# ---------------------------------------------------------------------------
# ADK-shaped stand-ins with session.state (mirrored by _adk_state_protocol)
# ---------------------------------------------------------------------------


class _Session:
    def __init__(self, sid: str, state: dict[str, Any] | None = None) -> None:
        self.id = sid
        # ADK's ``Session.state`` is a MutableMapping; a plain dict
        # duck-types for ``.get()`` which is what the plugin reads.
        self.state = dict(state or {})


class _BaseAgent:
    def __init__(self, name: str, parent: Any = None) -> None:
        self.name = name
        self.parent_agent = parent


class Agent(_BaseAgent):
    """Class-name matches ADK's ``Agent``/``LlmAgent`` mapping."""


class _InvocationContext:
    def __init__(
        self,
        invocation_id: str,
        session_id: str,
        agent: Any,
        state: dict[str, Any] | None = None,
        branch: str = "",
    ) -> None:
        self.invocation_id = invocation_id
        self.session = _Session(session_id, state)
        self.agent = agent
        self.branch = branch


class _CallbackContext:
    def __init__(
        self,
        invocation_id: str,
        session_id: str,
        agent: Any,
        state: dict[str, Any] | None = None,
        branch: str = "",
    ) -> None:
        self.invocation_id = invocation_id
        # CallbackContext carries a *shallow copy* of the session.state
        # in real ADK (see the memory pitfall
        # "Verify plugin callback state handoff is read-readable"). For
        # a read-only stamp we only need the snapshot to contain the
        # key — which goldfive's mirror has populated by the time any
        # before_* callback fires.
        self.session = _Session(session_id, state)
        self.branch = branch
        self._invocation_context = _InvocationContext(
            invocation_id, session_id, agent, state=state, branch=branch
        )


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class _LlmRequest:
    def __init__(self, model: str = "mock/llm") -> None:
        self.model = model


@pytest.fixture
def made() -> list[FakeTransport]:
    return []


@pytest.fixture
def client(made: list[FakeTransport]) -> Client:
    return Client(
        name="task-id-test",
        agent_id=CLIENT_AGENT_ID,
        session_id="home",
        buffer_size=256,
        _transport_factory=make_factory(made),
    )


@pytest.fixture
def plugin(client: Client) -> HarmonografTelemetryPlugin:
    return HarmonografTelemetryPlugin(client)


def _span_starts(client: Client) -> list[Any]:
    out: list[Any] = []
    for env in client._events.drain():
        if env.kind is EnvelopeKind.SPAN_START:
            out.append(env.payload.span)
    return out


def _attr_string(span: Any, key: str) -> str | None:
    attrs = dict(span.attributes or {})
    val = attrs.get(key)
    if val is None:
        return None
    return val.string_value


# ---------------------------------------------------------------------------
# Populated state → every SpanStart carries hgraf.task_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invocation_span_stamps_hgraf_task_id(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """before_run_callback INVOCATION SpanStart carries hgraf.task_id."""
    coord = Agent("coordinator")
    state = {"goldfive.current_task_id": "t1"}
    await plugin.before_run_callback(
        invocation_context=_InvocationContext(
            "inv-1", ROOT_SESSION, coord, state=state
        )
    )
    spans = _span_starts(client)
    assert len(spans) == 1
    assert _attr_string(spans[0], "hgraf.task_id") == "t1"


@pytest.mark.asyncio
async def test_tool_span_stamps_hgraf_task_id(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """before_tool_callback TOOL_CALL SpanStart carries hgraf.task_id."""
    research = Agent("research_agent")
    state = {"goldfive.current_task_id": "t2"}
    cb = _CallbackContext("inv-1", ROOT_SESSION, research, state=state)
    await plugin.before_agent_callback(agent=research, callback_context=cb)
    await plugin.before_tool_callback(
        tool=_Tool("read_file"), tool_args={"path": "a.md"}, tool_context=cb
    )
    spans = _span_starts(client)
    tool_spans = [s for s in spans if s.name == "read_file"]
    assert len(tool_spans) == 1
    assert _attr_string(tool_spans[0], "hgraf.task_id") == "t2"


@pytest.mark.asyncio
async def test_model_span_stamps_hgraf_task_id(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """before_model_callback LLM_CALL SpanStart carries hgraf.task_id."""
    coord = Agent("coordinator")
    state = {"goldfive.current_task_id": "t3"}
    await plugin.before_run_callback(
        invocation_context=_InvocationContext(
            "inv-1", ROOT_SESSION, coord, state=state
        )
    )
    cb = _CallbackContext("inv-1", ROOT_SESSION, coord, state=state)
    await plugin.before_agent_callback(agent=coord, callback_context=cb)
    await plugin.before_model_callback(
        callback_context=cb, llm_request=_LlmRequest("gpt-test")
    )
    spans = _span_starts(client)
    # INVOCATION + LLM_CALL.
    assert len(spans) == 2
    assert _attr_string(spans[0], "hgraf.task_id") == "t3"
    assert _attr_string(spans[1], "hgraf.task_id") == "t3"


# ---------------------------------------------------------------------------
# Absent / empty state → no attribute (non-goldfive runs, pre-plan spans)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_state_key_does_not_stamp_hgraf_task_id(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """No ``goldfive.current_task_id`` in state → no attribute; don't
    invent a value, don't raise. Non-goldfive ADK apps must emit
    cleanly without the attribute.
    """
    coord = Agent("coordinator")
    await plugin.before_run_callback(
        invocation_context=_InvocationContext(
            "inv-1", ROOT_SESSION, coord, state={}
        )
    )
    spans = _span_starts(client)
    assert len(spans) == 1
    assert _attr_string(spans[0], "hgraf.task_id") is None


@pytest.mark.asyncio
async def test_empty_state_value_does_not_stamp(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Empty-string value is treated identically to absence — pre-plan
    spans (before goldfive has a task id to mirror) must not carry
    ``hgraf.task_id=""``.
    """
    coord = Agent("coordinator")
    state = {"goldfive.current_task_id": ""}
    await plugin.before_run_callback(
        invocation_context=_InvocationContext(
            "inv-1", ROOT_SESSION, coord, state=state
        )
    )
    spans = _span_starts(client)
    assert _attr_string(spans[0], "hgraf.task_id") is None


@pytest.mark.asyncio
async def test_no_session_attr_does_not_raise(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Malformed ctx (no ``session``) must not raise — telemetry is
    observability-only and must fail quietly."""

    class _BareCtx:
        invocation_id = "inv-1"
        agent = Agent("bare")
        branch = ""

    # Real ADK always populates ``session`` on a runtime callback, but
    # defensive paths matter: ``_safe_attr`` + early-return absorb both.
    await plugin.before_run_callback(invocation_context=_BareCtx())
    spans = _span_starts(client)
    assert len(spans) == 1
    assert _attr_string(spans[0], "hgraf.task_id") is None


# ---------------------------------------------------------------------------
# Follow-up turns: task id flips between tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_id_flips_between_turns(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Goldfive's mirror runs on EVERY before_run_callback, so
    subsequent turns see a fresh (possibly different) task id. The
    plugin reads per-callback — never caches — so the follow-up
    SpanStart carries the NEW value.
    """
    coord = Agent("coordinator")
    await plugin.before_run_callback(
        invocation_context=_InvocationContext(
            "inv-1", ROOT_SESSION, coord, state={"goldfive.current_task_id": "t1"}
        )
    )
    await plugin.after_run_callback(
        invocation_context=_InvocationContext(
            "inv-1", ROOT_SESSION, coord, state={"goldfive.current_task_id": "t1"}
        )
    )
    await plugin.before_run_callback(
        invocation_context=_InvocationContext(
            "inv-2", ROOT_SESSION, coord, state={"goldfive.current_task_id": "t2"}
        )
    )
    spans = _span_starts(client)
    assert len(spans) == 2
    assert _attr_string(spans[0], "hgraf.task_id") == "t1"
    assert _attr_string(spans[1], "hgraf.task_id") == "t2"


# ---------------------------------------------------------------------------
# Shallow-copy regression (harmonograf#117)
# ---------------------------------------------------------------------------
#
# Regression: ADK's CallbackContext holds a SHALLOW COPY of
# invocation_context.session.state at the moment the CallbackContext is
# built. Goldfive's _adk_state_protocol mirror writes
# ``goldfive.current_task_id`` onto the LIVE session.state on
# before_run_callback. If the mirror fires AFTER harmonograf's
# before_run_callback (plugin ordering is not contractual) — or if the
# CallbackContext for a child before_model / before_tool was minted
# before the mirror write — the stale snapshot does NOT see the update.
#
# DB-verified symptom on session 0f4959c9-798e-44b8-8cc2-ab049d0bb415:
# 21 spans total, only 1 carried hgraf.task_id. Task-tab Trajectory
# subtab was empty; Gantt task→span edges broken; 6/7 tasks' bound_span_id
# was NULL. The one span that DID carry it was the LLM_CALL that happened
# to fire close enough to the mirror write to see the value.
#
# Fix: cache the LIVE invocation_context.session on before_run_callback
# keyed by invocation_id; every child callback reads task_id from the
# cached live session at CALLBACK TIME so it sees the latest mirror
# write regardless of plugin ordering.


class _LiveSession:
    """Session stand-in shared between an InvocationContext and N
    CallbackContexts so tests can verify the plugin reads from the
    LIVE object — writes to the live session must be visible to
    already-minted CallbackContexts through the plugin's resolver."""

    def __init__(self, sid: str, state: dict[str, Any] | None = None) -> None:
        self.id = sid
        self.state = dict(state or {})


class _SharedSessionInvocationContext:
    def __init__(
        self,
        invocation_id: str,
        agent: Any,
        live_session: _LiveSession,
        branch: str = "",
    ) -> None:
        self.invocation_id = invocation_id
        self.session = live_session  # THE live session — shared
        self.agent = agent
        self.branch = branch


class _StaleCopyCallbackContext:
    """CallbackContext that mimics ADK's real shallow-copy semantics.

    ``self.session.state`` is a snapshot taken at construction time;
    later writes to the LIVE session (held separately by the
    InvocationContext passed in) DO NOT propagate to ``self.session.state``.
    This is the exact shape that broke ``_extract_current_task_id``
    pre-fix.
    """

    def __init__(
        self,
        invocation_context: _SharedSessionInvocationContext,
        live_session: _LiveSession,
    ) -> None:
        self.invocation_id = invocation_context.invocation_id
        self.branch = invocation_context.branch
        # CRITICAL: snapshot the state dict — no shared reference.
        self.session = _Session(
            live_session.id, state=dict(live_session.state)
        )
        # CallbackContext still holds a ref to the live InvocationContext,
        # which in turn holds a ref to the LIVE session. The plugin's
        # resolver walks through this path when the cache misses; when
        # the cache is seeded (normal flow) it takes precedence.
        self._invocation_context = invocation_context


@pytest.mark.asyncio
async def test_live_session_write_after_callback_context_mint_is_visible(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Simulate the exact production pitfall:

    1. before_run_callback fires → plugin caches the live session.
    2. A CallbackContext for a child before_model is minted, snapshotting
       state = {} (the mirror hasn't written yet).
    3. Goldfive's mirror writes ``goldfive.current_task_id = "t-late"``
       onto the LIVE session.state (AFTER the CallbackContext was minted).
    4. before_model_callback fires with the stale CallbackContext.

    Expected: the LLM_CALL SpanStart carries ``hgraf.task_id = "t-late"``
    — read from the cached live session, NOT the stale snapshot on
    ``callback_context.session.state``.

    Pre-fix: the plugin read ``callback_context.session.state`` which
    was empty, so the LLM_CALL SpanStart had no ``hgraf.task_id`` at
    all — the exact DB-verified regression this test locks down.
    """
    coord = Agent("coordinator")
    live = _LiveSession(ROOT_SESSION, state={})  # empty at run-start
    inv_ctx = _SharedSessionInvocationContext("inv-late", coord, live)

    # (1) Seed the plugin's live-session cache.
    await plugin.before_run_callback(invocation_context=inv_ctx)

    # (2) Child CallbackContext is minted here — snapshots empty state.
    cb = _StaleCopyCallbackContext(inv_ctx, live)
    # Sanity-check the harness: the snapshot on the CallbackContext is
    # divorced from subsequent live writes.
    assert cb.session.state.get("goldfive.current_task_id", "") == ""

    # (3) Goldfive's mirror writes LATE on the LIVE session.
    live.state["goldfive.current_task_id"] = "t-late"
    # The stale snapshot still shows empty — this is the pitfall.
    assert cb.session.state.get("goldfive.current_task_id", "") == ""

    # (4) before_model_callback reads the LIVE session via the cache.
    await plugin.before_model_callback(
        callback_context=cb, llm_request=_LlmRequest("gpt-live")
    )
    spans = _span_starts(client)
    llm_spans = [s for s in spans if s.name == "gpt-live"]
    assert len(llm_spans) == 1
    assert _attr_string(llm_spans[0], "hgraf.task_id") == "t-late", (
        "Regression: plugin read CallbackContext shallow-copy instead of "
        "live session. Every child span loses hgraf.task_id when goldfive's "
        "mirror write lands after the CallbackContext is minted."
    )


@pytest.mark.asyncio
async def test_live_session_write_visible_to_before_tool_callback(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """Same pitfall, TOOL_CALL path. TOOL_CALL spans dominate a typical
    run (6/7 cases in the verified regression) so a separate lock-down
    test here guards the tool path explicitly."""
    research = Agent("research_agent")
    live = _LiveSession(ROOT_SESSION, state={})
    inv_ctx = _SharedSessionInvocationContext("inv-tool", research, live)

    await plugin.before_run_callback(invocation_context=inv_ctx)
    cb = _StaleCopyCallbackContext(inv_ctx, live)
    # Late mirror write — AFTER the CallbackContext was minted.
    live.state["goldfive.current_task_id"] = "t-tool"

    await plugin.before_tool_callback(
        tool=_Tool("read_file"), tool_args={"path": "a.md"}, tool_context=cb
    )
    spans = _span_starts(client)
    tool_spans = [s for s in spans if s.name == "read_file"]
    assert len(tool_spans) == 1
    assert _attr_string(tool_spans[0], "hgraf.task_id") == "t-tool"


@pytest.mark.asyncio
async def test_live_session_cache_cleared_after_run(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """The live-session cache must not retain references across runs.
    after_run_callback pops the entry keyed by invocation_id.
    """
    coord = Agent("coordinator")
    live = _LiveSession(ROOT_SESSION, state={"goldfive.current_task_id": "t1"})
    inv_ctx = _SharedSessionInvocationContext("inv-cleanup", coord, live)

    await plugin.before_run_callback(invocation_context=inv_ctx)
    assert "inv-cleanup" in plugin._live_sessions

    await plugin.after_run_callback(invocation_context=inv_ctx)
    assert "inv-cleanup" not in plugin._live_sessions


@pytest.mark.asyncio
async def test_live_session_cache_cleared_on_cancellation(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """on_cancellation clears the live-session cache for the cancelled
    invocation. Without this, the plugin would pin the ADK session
    across cancelled runs."""
    coord = Agent("coordinator")
    live = _LiveSession(ROOT_SESSION, state={"goldfive.current_task_id": "t1"})
    inv_ctx = _SharedSessionInvocationContext("inv-cancel", coord, live)

    await plugin.before_run_callback(invocation_context=inv_ctx)
    assert "inv-cancel" in plugin._live_sessions

    plugin.on_cancellation("inv-cancel")
    assert "inv-cancel" not in plugin._live_sessions


@pytest.mark.asyncio
async def test_live_session_cache_cleared_on_run_end(
    plugin: HarmonografTelemetryPlugin, client: Client
) -> None:
    """on_run_end sweeps any leftover live-session entries so long-lived
    plugin instances don't accumulate references."""
    coord = Agent("coordinator")
    live_a = _LiveSession(ROOT_SESSION, state={"goldfive.current_task_id": "t1"})
    live_b = _LiveSession(ROOT_SESSION, state={"goldfive.current_task_id": "t2"})
    await plugin.before_run_callback(
        invocation_context=_SharedSessionInvocationContext("inv-a", coord, live_a)
    )
    await plugin.before_run_callback(
        invocation_context=_SharedSessionInvocationContext("inv-b", coord, live_b)
    )
    assert len(plugin._live_sessions) == 2

    plugin.on_run_end()
    assert plugin._live_sessions == {}
