"""End-to-end: lazy Hello produces exactly one session per run.

Regression test for harmonograf#83. Before lazy Hello, every ADK run
produced two sessions on the server: a ``sess_YYYY-MM-DD_NNNN`` ghost
(created by the client's Hello handshake at construction time, before
the ADK plugin had cached the outer adk-web session id) and the real
ADK session (created later, when the plugin stamped its id onto
spans + goldfive events). PR #78 hid the ghost from the picker with a
server-side filter; #83 eliminates it by deferring Hello to first emit.

These tests exercise the real harmonograf server (not the mock) through
the real client and verify the three lazy-Hello contracts:

1. **ADK-style first emit** — span with explicit ``session_id`` → server
   creates exactly one session, with that id. No ghost.
2. **Non-ADK first emit** — span with no ``session_id`` → server
   auto-creates a home session (``sess_YYYY-MM-DD_NNNN``). The
   harmonograf#62 home-session rollup contract is preserved.
3. **Idle client** — constructed and shut down without emitting → server
   creates zero sessions.

If any of these regresses, ghost sessions will reappear in the picker
and #78's server-side filter will not save us — that filter was
reverted with #82.
"""

from __future__ import annotations

import asyncio

import pytest

from harmonograf_client import Client, SpanKind, SpanStatus


async def _wait_for_sessions(store, expected: int, *, timeout: float = 3.0) -> list:
    """Poll ``store.list_sessions()`` until it contains at least
    ``expected`` rows or ``timeout`` elapses. Returns the last snapshot.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    sessions: list = []
    while asyncio.get_event_loop().time() < deadline:
        sessions = list(await store.list_sessions() or [])
        if len(sessions) >= expected:
            return sessions
        await asyncio.sleep(0.05)
    return sessions


class TestLazyHelloAgainstRealServer:
    @pytest.mark.asyncio
    async def test_adk_style_first_emit_creates_one_session_no_ghost(
        self,
        harmonograf_server: dict,
    ) -> None:
        """An ADK-style client stamps the outer adk-web ``session_id``
        onto the first span's envelope. Lazy Hello picks that id up and
        the server creates exactly one session — the ADK session. The
        ``sess_YYYY-MM-DD_NNNN`` ghost that used to accompany every run
        (harmonograf#77) never appears.
        """
        client = Client(
            name="adk-sim-agent",
            server_addr=harmonograf_server["addr"],
        )
        adk_session_id = "sess_adk_outer_test_xyz"
        try:
            # First emit carries an explicit session_id, simulating
            # what HarmonografTelemetryPlugin._stamp_session_id does on
            # a real ADK invocation.
            sid = client.emit_span_start(
                kind=SpanKind.LLM_CALL,
                name="echo",
                session_id=adk_session_id,
            )
            client.emit_span_end(sid, status=SpanStatus.COMPLETED)
        finally:
            client.shutdown(flush_timeout=2.0)

        sessions = await _wait_for_sessions(
            harmonograf_server["store"], expected=1, timeout=3.0
        )
        session_ids = sorted(s.id for s in sessions)
        assert session_ids == [
            adk_session_id
        ], f"expected exactly the ADK session, got {session_ids!r}"

    @pytest.mark.asyncio
    async def test_non_adk_first_emit_auto_creates_home_session(
        self,
        harmonograf_server: dict,
    ) -> None:
        """Non-ADK flow: the client emits a span with no ``session_id``
        override. Hello fires on first emit with an empty session id;
        the server auto-creates a ``sess_YYYY-MM-DD_NNNN`` home session
        and all subsequent spans land on it. This is the
        harmonograf#62 rollup contract — explicitly preserved by lazy
        Hello.
        """
        client = Client(
            name="non-adk-agent",
            server_addr=harmonograf_server["addr"],
        )
        try:
            sid = client.emit_span_start(kind=SpanKind.LLM_CALL, name="echo")
            client.emit_span_end(sid, status=SpanStatus.COMPLETED)
        finally:
            client.shutdown(flush_timeout=2.0)

        sessions = await _wait_for_sessions(
            harmonograf_server["store"], expected=1, timeout=3.0
        )
        assert len(sessions) == 1, (
            f"expected exactly one auto-created home session, got "
            f"{[s.id for s in sessions]!r}"
        )
        # The server mints an id of the form ``sess_YYYY-MM-DD_NNNN``
        # for Hellos that arrive with no session_id.
        assert sessions[0].id.startswith("sess_"), sessions[0].id

    @pytest.mark.asyncio
    async def test_idle_client_shutdown_creates_no_session(
        self,
        harmonograf_server: dict,
    ) -> None:
        """A client that constructs and shuts down without emitting
        anything produces zero Hellos, so the server never creates any
        session row. This is the fundamental guarantee lazy Hello adds
        — no ghost, no orphan stream, no DB row.
        """
        client = Client(
            name="idle-agent",
            server_addr=harmonograf_server["addr"],
        )
        # Let the client connect to the server so the stream is actually
        # open. Without lazy Hello, Welcome would already have arrived
        # by this point and a session row would exist.
        await asyncio.sleep(0.3)
        client.shutdown(flush_timeout=1.0)
        # Let the server side observe stream close.
        await asyncio.sleep(0.3)

        sessions = list(
            await harmonograf_server["store"].list_sessions() or []
        )
        assert sessions == [], (
            f"idle client produced {len(sessions)} session(s): "
            f"{[s.id for s in sessions]!r}"
        )
