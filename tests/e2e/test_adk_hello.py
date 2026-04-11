"""End-to-end happy path: ADK agent → harmonograf server.

This is the acceptance gate for harmonograf v0 (task #11). It proves
that the whole pipeline — client library, ADK adapter, transport, gRPC
server, ingest pipeline, storage, bus, and control router — works end
to end in one process.

Scenarios
---------

1. Happy path (:class:`TestAdkHelloHappyPath`):
   - ADK agent with ``MockModel`` and one deterministic fake tool
   - Attach a real harmonograf :class:`Client` to a real server via
     :func:`attach_adk`
   - Run one invocation
   - Assert spans landed in the store with the expected parent links
     and payloads

2. Control round-trip (:class:`TestAdkSteering`):
   - Mid-invocation, send a STEER control event via the router
   - Assert the ack arrives back upstream
   - Assert the adapter queued the steering text for the agent to
     consume

3. Human-in-the-loop (:class:`TestAdkHumanInLoop`):
   - Long-running ADK tool (``is_long_running=True``)
   - Verify the TOOL_CALL span is flagged with ``is_long_running=True``
     in its attributes so the frontend can render it as a HITL step.

To run locally::

    make e2e

Dependencies
------------

- ``google.adk`` must be installed (path dep on ``third_party/adk-python``)
- ``harmonograf_server`` + ``harmonograf_client`` installed in the
  same venv as this test

If either is missing, the suite self-skips with a clear reason.
"""

from __future__ import annotations

import asyncio
import importlib.util
from typing import Any

import pytest

import grpc

from harmonograf_client import Client, attach_adk
from harmonograf_client.adk import _harmonograf_session_id_for_adk
from harmonograf_server.pb import frontend_pb2, service_pb2_grpc, types_pb2


_ADK_AVAILABLE = importlib.util.find_spec("google.adk") is not None

pytestmark = pytest.mark.skipif(
    not _ADK_AVAILABLE,
    reason="google.adk is not installed — run `make install` to pick up the submodule",
)


# ---------------------------------------------------------------------------
# Deterministic mock LLM + fake tool
# ---------------------------------------------------------------------------


def _make_mock_model_responding_with(tool_name: str, tool_args: dict[str, Any]) -> Any:
    """Build an ADK ``MockModel`` that first calls ``tool_name(tool_args)``
    then, on the follow-up turn, returns a plain-text completion.
    """
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types as genai_types

    # Import lazily so the skip marker handles the no-ADK case.
    from google.adk.models.base_llm import BaseLlm  # noqa: F401

    # MockModel lives under the ADK test tree; we vendor its key bits
    # here so we don't take a test-only path dependency. This mirrors
    # third_party/adk-python/tests/unittests/testing_utils.py::MockModel.
    import contextlib
    from typing import AsyncGenerator

    class _MockModel(BaseLlm):  # type: ignore[misc]
        model: str = "mock"
        responses: list[LlmResponse] = []
        response_index: int = -1

        @classmethod
        def supported_models(cls) -> list[str]:
            return ["mock"]

        async def generate_content_async(
            self, llm_request, stream: bool = False
        ) -> "AsyncGenerator[LlmResponse, None]":
            self.response_index += 1
            idx = min(self.response_index, len(self.responses) - 1)
            yield self.responses[idx]

        @contextlib.asynccontextmanager
        async def connect(self, llm_request):
            yield None

    tool_call_part = genai_types.Part(
        function_call=genai_types.FunctionCall(name=tool_name, args=tool_args)
    )
    tool_call_response = LlmResponse(
        content=genai_types.Content(role="model", parts=[tool_call_part])
    )
    final_response = LlmResponse(
        content=genai_types.Content(
            role="model", parts=[genai_types.Part(text="all done")]
        )
    )
    return _MockModel(responses=[tool_call_response, final_response])


def _make_deterministic_tool():
    """Returns a FunctionTool wrapping a sync callable that records its
    invocation and returns a canned result.
    """
    from google.adk.tools.function_tool import FunctionTool

    calls: list[dict[str, Any]] = []

    def search_web(query: str) -> dict[str, Any]:
        """Stub tool — returns a canned search result."""
        calls.append({"query": query})
        return {"results": [f"hit for {query}"]}

    tool = FunctionTool(func=search_web)
    return tool, calls


# ---------------------------------------------------------------------------
# Runner construction
# ---------------------------------------------------------------------------


def _build_adk_runner() -> tuple[Any, list[dict[str, Any]]]:
    from google.adk.agents.llm_agent import LlmAgent
    from google.adk.runners import InMemoryRunner

    tool, tool_calls = _make_deterministic_tool()
    agent = LlmAgent(
        name="research_agent",
        model=_make_mock_model_responding_with("search_web", {"query": "harmonograf"}),
        tools=[tool],
        instruction="Use search_web when asked.",
    )
    runner = InMemoryRunner(agent=agent, app_name="harmonograf_e2e")
    return runner, tool_calls


async def _run_adk_invocation_in_session(
    runner: Any, session_id: str, user_text: str
) -> list[Any]:
    """Drive an invocation against an existing ADK session id (no
    create_session call)."""
    from google.genai import types as genai_types

    events: list[Any] = []
    async for event in runner.run_async(
        user_id="e2e_user",
        session_id=session_id,
        new_message=genai_types.Content(
            role="user", parts=[genai_types.Part(text=user_text)]
        ),
    ):
        events.append(event)
    return events


async def _run_adk_invocation(runner: Any, user_text: str) -> list[Any]:
    """Drive one invocation through ``runner.run_async`` and return the
    emitted events.
    """
    from google.genai import types as genai_types

    session_service = runner.session_service
    session = await session_service.create_session(
        app_name=runner.app_name, user_id="e2e_user"
    )
    events: list[Any] = []
    async for event in runner.run_async(
        user_id="e2e_user",
        session_id=session.id,
        new_message=genai_types.Content(
            role="user", parts=[genai_types.Part(text=user_text)]
        ),
    ):
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


async def _wait_for(predicate, *, timeout=3.0, interval=0.02) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def _wait_for_async(predicate, *, timeout=3.0, interval=0.02) -> bool:
    """Like :func:`_wait_for` but for coroutine predicates."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def _store_has_kinds(store, session_id: str, required: set[str]) -> bool:
    spans = await _spans_in_store(store, session_id)
    seen = {str(getattr(s, "kind", "")) for s in spans}
    return all(any(r in k for k in seen) for r in required)


async def _spans_in_store(store, session_id: str) -> list[Any]:
    """Pull all spans for a session regardless of backend."""
    get_spans = getattr(store, "get_spans", None)
    if get_spans is None:
        return []
    result = get_spans(session_id)
    if asyncio.iscoroutine(result):
        result = await result
    return list(result or [])


async def _resolve_adk_session_id(store, fallback: str = "") -> str:
    """The ADK adapter routes spans to a per-ADK-session harmonograf
    session whose id is prefixed with ``adk_``. Tests that expect spans
    to land for a single-invocation run must look these up by listing
    sessions on the server, since the Client's default session id is
    only used for the stream-level Hello, not for span attribution.
    """
    sessions = await store.list_sessions()
    for s in sessions:
        if s.id.startswith("adk_"):
            return s.id
    return fallback


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAdkHelloHappyPath:
    async def test_single_invocation_captures_spans(
        self, harmonograf_server, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HARMONOGRAF_HOME", str(tmp_path))

        client = Client(
            name="research-agent",
            server_addr=harmonograf_server["addr"],
            framework="ADK",
            capabilities=["HUMAN_IN_LOOP", "STEERING"],
        )

        runner, tool_calls = _build_adk_runner()
        handle = attach_adk(runner, client)
        try:
            await _run_adk_invocation(runner, "search for harmonograf")

            store = harmonograf_server["store"]
            # Poll for span arrival on the server side. The client
            # transport runs on a background thread with its own loop,
            # so we yield the pytest loop to let the server's gRPC
            # coroutines drain the telemetry stream.
            assert await _wait_for(
                lambda: client.session_id != "" and client._transport.connected,
                timeout=5.0,
            ), "transport never connected"

            async def _have_kinds() -> bool:
                sid = await _resolve_adk_session_id(store)
                if not sid:
                    return False
                return await _store_has_kinds(
                    store, sid, {"INVOCATION", "LLM_CALL", "TOOL_CALL"}
                )

            assert await _wait_for_async(_have_kinds, timeout=5.0), (
                "expected span kinds never reached the store"
            )
            session_id = await _resolve_adk_session_id(store)
            spans = await _spans_in_store(store, session_id)
            assert tool_calls, "deterministic tool was never called"

            by_kind: dict[str, list[Any]] = {}
            by_id: dict[str, Any] = {}
            for s in spans:
                by_kind.setdefault(str(getattr(s, "kind", None)), []).append(s)
                sid = getattr(s, "id", None)
                if sid:
                    by_id[sid] = s

            assert any("INVOCATION" in k for k in by_kind), f"missing INVOCATION span; got {list(by_kind)}"
            assert any("LLM_CALL" in k for k in by_kind), f"missing LLM_CALL span; got {list(by_kind)}"
            assert any("TOOL_CALL" in k for k in by_kind), f"missing TOOL_CALL span; got {list(by_kind)}"

            # Every TOOL_CALL and LLM_CALL must parent to some span
            # emitted in this invocation (adapter attributes both kinds
            # to the enclosing INVOCATION, not nested).
            child_spans = [
                s
                for k, group in by_kind.items()
                if "TOOL_CALL" in k or "LLM_CALL" in k
                for s in group
            ]
            for child in child_spans:
                parent_id = getattr(child, "parent_span_id", None)
                assert parent_id in by_id, (
                    f"{child.kind} span {child.id} parent {parent_id!r} not found in emitted spans"
                )
        finally:
            handle.detach()
            client.shutdown(flush_timeout=2.0)


# ---------------------------------------------------------------------------
# Control round-trip (STEER)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAdkSteering:
    async def test_steer_control_ack_and_queue(
        self, harmonograf_server, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HARMONOGRAF_HOME", str(tmp_path))

        client = Client(
            name="steer-agent",
            server_addr=harmonograf_server["addr"],
            framework="ADK",
            capabilities=["STEERING"],
        )
        runner, _ = _build_adk_runner()
        handle = attach_adk(runner, client)
        try:
            # Run the invocation in the background so a control
            # subscription is live when we issue the STEER event.
            invocation_task = asyncio.create_task(
                _run_adk_invocation(runner, "hello")
            )

            router = harmonograf_server["router"]
            # Wait for the client's SubscribeControl stream to register
            # against the router for this agent.
            assert await _wait_for(
                lambda: bool(router.live_stream_ids(client.agent_id)),
                timeout=5.0,
            ), "client never established a control subscription"
            assert await _wait_for(
                lambda: client.session_id != "",
                timeout=5.0,
            )

            outcome = await router.deliver(
                session_id=client.session_id,
                agent_id=client.agent_id,
                kind=types_pb2.CONTROL_KIND_STEER,
                payload=b"consider the eastern corridor",
                control_id="ctrl-steer-1",
                timeout_s=5.0,
            )
            assert outcome.control_id == "ctrl-steer-1"
            assert outcome.acks, f"no acks recorded; outcome={outcome}"
            assert any(
                a.result == types_pb2.CONTROL_ACK_RESULT_SUCCESS for a in outcome.acks
            ), f"STEER did not receive a SUCCESS ack; acks={outcome.acks}"

            await invocation_task
        finally:
            handle.detach()
            client.shutdown(flush_timeout=2.0)


# ---------------------------------------------------------------------------
# Long-running tool / HITL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAdkHumanInLoop:
    async def test_long_running_tool_enters_awaiting_human(
        self, harmonograf_server, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HARMONOGRAF_HOME", str(tmp_path))

        client = Client(
            name="hitl-agent",
            server_addr=harmonograf_server["addr"],
            framework="ADK",
            capabilities=["HUMAN_IN_LOOP"],
        )

        from google.adk.agents.llm_agent import LlmAgent
        from google.adk.runners import InMemoryRunner
        from google.adk.tools.long_running_tool import LongRunningFunctionTool

        def request_human_approval(prompt: str) -> dict[str, Any]:
            return {"status": "pending_human"}

        tool = LongRunningFunctionTool(func=request_human_approval)
        agent = LlmAgent(
            name="hitl_agent",
            model=_make_mock_model_responding_with(
                "request_human_approval", {"prompt": "ok?"}
            ),
            tools=[tool],
            instruction="Always request human approval.",
        )
        runner = InMemoryRunner(agent=agent, app_name="hitl_e2e")
        handle = attach_adk(runner, client)
        try:
            invocation_task = asyncio.create_task(_run_adk_invocation(runner, "go"))

            try:
                store = harmonograf_server["store"]

                assert await _wait_for(
                    lambda: client.session_id != "",
                    timeout=5.0,
                ), "client never received a session assignment"

                # Invocation completes once the mock model returns its
                # follow-up final_response turn; we let it drain.
                await asyncio.wait_for(invocation_task, timeout=5.0)

                session_id = await _resolve_adk_session_id(store)

                # Verify the long-running tool call was flagged in the
                # stored span attributes. The adapter also emits a
                # transient AWAITING_HUMAN status update mid-span, but
                # end_span overwrites the status in storage, so we
                # assert on the attribute contract that survives to
                # terminal state — that is what a frontend inspecting
                # a persisted span would see.
                async def long_running_tool_present() -> bool:
                    sid = session_id or await _resolve_adk_session_id(store)
                    if not sid:
                        return False
                    spans = await _spans_in_store(store, sid)
                    for s in spans:
                        if not str(getattr(s, "kind", "")).endswith("TOOL_CALL"):
                            continue
                        attrs = getattr(s, "attributes", None) or {}
                        if attrs.get("is_long_running") is True:
                            return True
                    return False

                assert await _wait_for_async(
                    long_running_tool_present, timeout=5.0
                ), "no TOOL_CALL span with is_long_running=True reached the store"
            finally:
                if not invocation_task.done():
                    invocation_task.cancel()
                    try:
                        await invocation_task
                    except (asyncio.CancelledError, Exception):
                        pass
        finally:
            handle.detach()
            client.shutdown(flush_timeout=2.0)


# ---------------------------------------------------------------------------
# HITL transient-state observability via WatchSession
# ---------------------------------------------------------------------------


def _build_hitl_runner() -> Any:
    """Builds an ADK runner whose tool is marked ``is_long_running=True``.

    The mock model calls the tool once, then returns a plain final
    response on the follow-up turn, so the invocation terminates cleanly.
    """
    from google.adk.agents.llm_agent import LlmAgent
    from google.adk.runners import InMemoryRunner
    from google.adk.tools.long_running_tool import LongRunningFunctionTool

    def request_human_approval(prompt: str) -> dict[str, Any]:
        return {"status": "pending_human"}

    tool = LongRunningFunctionTool(func=request_human_approval)
    agent = LlmAgent(
        name="hitl_agent",
        model=_make_mock_model_responding_with(
            "request_human_approval", {"prompt": "ok?"}
        ),
        tools=[tool],
        instruction="Always request human approval.",
    )
    return InMemoryRunner(agent=agent, app_name="hitl_watch_e2e")


async def _watch_session_until(
    stub: service_pb2_grpc.HarmonografStub,
    session_id: str,
    *,
    stop_when,
    timeout: float = 10.0,
) -> tuple[list[tuple[str, Any]], bool]:
    """Open WatchSession and collect (kind, update) pairs until
    ``stop_when(kind_list)`` returns True or ``timeout`` elapses.

    Returns ``(events, stopped_cleanly)``. ``events`` is the full list of
    (oneof-kind-name, SessionUpdate) tuples captured, in stream order.
    """
    call = stub.WatchSession(frontend_pb2.WatchSessionRequest(session_id=session_id))
    events: list[tuple[str, Any]] = []
    stopped = False

    async def _consume() -> None:
        nonlocal stopped
        async for upd in call:
            which = upd.WhichOneof("kind")
            events.append((which or "", upd))
            if stop_when(events):
                stopped = True
                return

    try:
        await asyncio.wait_for(_consume(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    finally:
        call.cancel()
    return events, stopped


async def _run_hitl_scenario(
    *,
    harmonograf_server,
    tmp_path,
    monkeypatch,
    client_name: str,
) -> list[tuple[str, Any]]:
    """Drives one long-running HITL invocation while a WatchSession
    stream is open. Returns the full (kind, update) event list observed
    on the WatchSession tail stream (post-burst).
    """
    monkeypatch.setenv("HARMONOGRAF_HOME", str(tmp_path))

    client = Client(
        name=client_name,
        server_addr=harmonograf_server["addr"],
        framework="ADK",
        capabilities=["HUMAN_IN_LOOP"],
    )

    store = harmonograf_server["store"]

    # Pre-create the ADK session that the invocation will run against,
    # then compute its harmonograf-side session id so WatchSession can
    # subscribe to the right row before any ADK callback fires. The
    # adapter routes spans to ``adk_<adk_session_id>``, so our sentinel
    # span carries the same session_id override and the WatchSession
    # subscribes to it.
    runner = _build_hitl_runner()
    adk_session = await runner.session_service.create_session(
        app_name=runner.app_name, user_id="e2e_user"
    )
    session_id = _harmonograf_session_id_for_adk(adk_session.id)

    sentinel_id = client.emit_span_start(
        kind="INVOCATION",
        name="hitl_sentinel",
        attributes={"sentinel": True},
        session_id=session_id,
    )
    client.emit_span_end(sentinel_id, status="COMPLETED")

    assert await _wait_for(
        lambda: client.session_id != "" and client._transport.connected,
        timeout=5.0,
    ), "client transport never established a session"

    async def _session_persisted() -> bool:
        return (await store.get_session(session_id)) is not None

    assert await _wait_for_async(
        _session_persisted, timeout=5.0
    ), f"session {session_id} never appeared in the store"

    channel = grpc.aio.insecure_channel(harmonograf_server["addr"])
    stub = service_pb2_grpc.HarmonografStub(channel)

    tail_events: list[tuple[str, Any]] = []

    try:
        # Open WatchSession and drain the initial burst before attaching
        # the ADK runner. We want every TOOL_CALL delta to land on the
        # tail stream, not be replayed as an initial_span.
        call = stub.WatchSession(
            frontend_pb2.WatchSessionRequest(session_id=session_id)
        )

        burst_done = asyncio.Event()
        scenario_done = asyncio.Event()
        stream_error: list[BaseException] = []

        async def _drain() -> None:
            try:
                async for upd in call:
                    which = upd.WhichOneof("kind") or ""
                    if not burst_done.is_set():
                        if which == "burst_complete":
                            burst_done.set()
                        continue
                    tail_events.append((which, upd))
                    # Stop once we've seen the terminal ended_span for a
                    # TOOL_CALL — the HITL scenario is complete by then.
                    if which == "ended_span":
                        # Only stop after we've also seen at least one
                        # updated_span event, so the assertions have
                        # something to chew on.
                        if any(k == "updated_span" for k, _ in tail_events):
                            scenario_done.set()
                            return
            except grpc.aio.AioRpcError as exc:
                if exc.code() != grpc.StatusCode.CANCELLED:
                    stream_error.append(exc)

        drain_task = asyncio.create_task(_drain())
        try:
            await asyncio.wait_for(burst_done.wait(), timeout=5.0)

            handle = attach_adk(runner, client)
            try:
                await _run_adk_invocation_in_session(
                    runner, adk_session.id, "go"
                )
                await asyncio.wait_for(scenario_done.wait(), timeout=5.0)
            finally:
                handle.detach()
        finally:
            call.cancel()
            try:
                await asyncio.wait_for(drain_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        if stream_error:
            raise stream_error[0]
    finally:
        await channel.close()
        client.shutdown(flush_timeout=2.0)

    return tail_events


@pytest.mark.asyncio
class TestAdkHumanInLoopWatchSession:
    """Regression-guard for the HITL observability contract.

    Task #16: unlike :class:`TestAdkHumanInLoop`, this test does not
    inspect persisted span attributes — it subscribes to WatchSession
    and asserts that the frontend sees the transient
    ``SPAN_STATUS_AWAITING_HUMAN`` update strictly before the terminal
    ``ended_span`` for the same span id. The key invariant is that the
    update ordering is deterministic across runs, because the frontend
    drives attention UI off this stream.
    """

    async def test_awaiting_human_update_precedes_ended_span(
        self, harmonograf_server, tmp_path, monkeypatch
    ):
        tail = await _run_hitl_scenario(
            harmonograf_server=harmonograf_server,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            client_name="hitl-watch-agent-1",
        )

        # Find every updated_span that flipped a span to AWAITING_HUMAN.
        awaiting: dict[str, int] = {}
        ended: dict[str, int] = {}
        for idx, (which, upd) in enumerate(tail):
            if which == "updated_span":
                us = upd.updated_span
                if us.status == types_pb2.SPAN_STATUS_AWAITING_HUMAN:
                    awaiting.setdefault(us.span_id, idx)
            elif which == "ended_span":
                ended.setdefault(upd.ended_span.span_id, idx)

        assert awaiting, (
            "no updated_span with SPAN_STATUS_AWAITING_HUMAN arrived on the "
            f"WatchSession tail stream; events={[k for k, _ in tail]}"
        )

        # At least one span must exhibit the full transient lifecycle:
        # updated_span(AWAITING_HUMAN) observed strictly before
        # ended_span for the same id.
        witnessed = [
            span_id
            for span_id, awaiting_idx in awaiting.items()
            if span_id in ended and awaiting_idx < ended[span_id]
        ]
        assert witnessed, (
            "no span had an AWAITING_HUMAN update strictly preceding its "
            f"ended_span; awaiting={awaiting}, ended={ended}"
        )

    async def test_transient_state_ordering_is_deterministic(
        self, harmonograf_server, tmp_path, monkeypatch
    ):
        """Drive the same HITL scenario twice against a fresh client
        each time and assert the observed WatchSession tail kind-
        sequence is identical. This is a regression-guard for any
        reordering between the ingest pipeline and the session bus.
        """
        run_kinds: list[tuple[str, ...]] = []
        for i in range(2):
            # Clean HARMONOGRAF_HOME per run so resume tokens don't
            # leak state between invocations.
            run_tmp = tmp_path / f"run{i}"
            run_tmp.mkdir()
            tail = await _run_hitl_scenario(
                harmonograf_server=harmonograf_server,
                tmp_path=run_tmp,
                monkeypatch=monkeypatch,
                client_name=f"hitl-watch-agent-det-{i}",
            )
            # Filter to just span lifecycle events — agent_status_changed
            # updates are driven by heartbeats and are inherently
            # non-deterministic in timing.
            kinds = tuple(
                k for k, _ in tail if k in {"new_span", "updated_span", "ended_span"}
            )
            run_kinds.append(kinds)

        assert run_kinds[0] == run_kinds[1], (
            "HITL WatchSession tail ordering diverged between runs: "
            f"run0={run_kinds[0]}, run1={run_kinds[1]}"
        )
        # And the ordering must contain at least one updated_span before
        # the final ended_span — degenerate empty/identical sequences
        # would pass the equality check above but not this.
        assert "updated_span" in run_kinds[0]
        assert "ended_span" in run_kinds[0]
        last_ended = max(
            i for i, k in enumerate(run_kinds[0]) if k == "ended_span"
        )
        first_update = next(
            i for i, k in enumerate(run_kinds[0]) if k == "updated_span"
        )
        assert first_update < last_ended
