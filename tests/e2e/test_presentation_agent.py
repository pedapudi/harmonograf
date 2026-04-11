"""End-to-end: presentation_agent sample under Harmonograf.

Drives the full coordinator → research_agent → web_developer_agent
pipeline with a deterministic mock LLM so the spans land in the
in-process harmonograf server fixture and can be asserted on. This is
the smoke-test sibling of the canonical ``adk web presentation_agent``
entry point — the ``App`` exported by ``presentation_agent.agent``
attaches a harmonograf plugin automatically, and
``TestPresentationAppExport`` below verifies that wiring.

The scenario:
  1. coordinator_agent's LLM issues an AgentTool call to research_agent
  2. research_agent's LLM returns a research-notes text blob
  3. coordinator_agent's LLM issues an AgentTool call to
     web_developer_agent
  4. web_developer_agent's LLM issues a FunctionTool call to
     write_webpage with canned html/css/js
  5. web_developer_agent's LLM returns a confirmation text
  6. coordinator_agent's LLM returns a final-answer text

The mock model is shared across all three agents; each
``generate_content_async`` call advances one cursor so the scripted
sequence above plays out in order regardless of which agent is asking.
"""

from __future__ import annotations

import asyncio
import importlib.util
from typing import Any

import pytest

from harmonograf_client import Client, attach_adk


_ADK_AVAILABLE = importlib.util.find_spec("google.adk") is not None

pytestmark = pytest.mark.skipif(
    not _ADK_AVAILABLE,
    reason="google.adk is not installed — run `make install` to pick up the submodule",
)


# ---------------------------------------------------------------------------
# Scripted mock model
# ---------------------------------------------------------------------------


def _build_scripted_model() -> Any:
    """Return an ADK BaseLlm whose responses step through a fixed script.

    The script covers: coordinator→research dispatch, research result,
    coordinator→web_developer dispatch, write_webpage tool call, web_dev
    final text, coordinator final text. A single response index advances
    across all calls so the same instance can be reused for every agent.
    """
    import contextlib
    from typing import AsyncGenerator

    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types as genai_types

    def _text_response(text: str) -> LlmResponse:
        return LlmResponse(
            content=genai_types.Content(
                role="model", parts=[genai_types.Part(text=text)]
            )
        )

    def _tool_call_response(name: str, args: dict[str, Any]) -> LlmResponse:
        return LlmResponse(
            content=genai_types.Content(
                role="model",
                parts=[
                    genai_types.Part(
                        function_call=genai_types.FunctionCall(name=name, args=args)
                    )
                ],
            )
        )

    research_notes = (
        "- Python is a dynamic, interpreted language.\n"
        "- Created by Guido van Rossum, first released in 1991.\n"
        "- Popular for data science, web, automation."
    )

    html_content = (
        "<!doctype html><html><head>"
        '<link rel="stylesheet" href="styles.css">'
        "</head><body><h1>Python</h1>"
        '<script src="script.js"></script></body></html>'
    )
    css_content = "body { font-family: sans-serif; }"
    js_content = "console.log('slides');"

    # Scripted turn sequence. Any extra calls reuse the last response so
    # stray follow-ups from ADK's orchestrator don't blow up the run.
    script: list[LlmResponse] = [
        # 1. coordinator dispatches to research_agent
        _tool_call_response(
            "research_agent", {"request": "Research: Python programming"}
        ),
        # 2. research_agent returns its notes
        _text_response(research_notes),
        # 3. coordinator dispatches to web_developer_agent
        _tool_call_response(
            "web_developer_agent",
            {"request": f"Build a presentation using: {research_notes}"},
        ),
        # 4. web_developer_agent calls write_webpage
        _tool_call_response(
            "write_webpage",
            {
                "topic": "python_programming_test",
                "html_content": html_content,
                "css_content": css_content,
                "js_content": js_content,
            },
        ),
        # 5. web_developer_agent confirms and returns text
        _text_response("Presentation saved to disk."),
        # 6. coordinator final answer to the user
        _text_response("All done — your presentation is ready."),
    ]

    class _ScriptedModel(BaseLlm):  # type: ignore[misc]
        model: str = "scripted-mock"
        responses: list[LlmResponse] = script
        cursor: int = -1

        @classmethod
        def supported_models(cls) -> list[str]:
            return ["scripted-mock"]

        async def generate_content_async(
            self, llm_request, stream: bool = False
        ) -> "AsyncGenerator[LlmResponse, None]":
            self.cursor += 1
            idx = min(self.cursor, len(self.responses) - 1)
            yield self.responses[idx]

        @contextlib.asynccontextmanager
        async def connect(self, llm_request):
            yield None

    return _ScriptedModel()


# ---------------------------------------------------------------------------
# Runner construction — imports presentation_agent.agent lazily so the
# skip marker fires cleanly when ADK is absent.
# ---------------------------------------------------------------------------


def _build_presentation_runner(tmp_output_dir: Any) -> Any:
    import os

    # Point the write_webpage tool at tmp_path so the test doesn't scribble
    # into the repo tree. The tool computes output via os.path.dirname of
    # its own module file — we can't easily redirect that without a
    # monkeypatch, so instead we monkeypatch the function to write into
    # tmp_output_dir.
    from google.adk.agents.llm_agent import LlmAgent
    from google.adk.runners import InMemoryRunner
    from google.adk.tools import AgentTool, FunctionTool

    model = _build_scripted_model()

    def write_webpage(
        topic: str, html_content: str, css_content: str, js_content: str
    ) -> str:
        topic_filename = topic.lower().replace(" ", "_").replace("/", "_")
        out_dir = os.path.join(str(tmp_output_dir), topic_filename)
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "index.html"), "w") as f:
            f.write(html_content)
        with open(os.path.join(out_dir, "styles.css"), "w") as f:
            f.write(css_content)
        with open(os.path.join(out_dir, "script.js"), "w") as f:
            f.write(js_content)
        return f"Successfully created presentation on '{topic}' at {out_dir}"

    write_webpage_tool = FunctionTool(write_webpage)

    research_agent = LlmAgent(
        name="research_agent",
        model=model,
        instruction="You are a researcher.",
        description="Researcher.",
        tools=[],
    )
    web_developer_agent = LlmAgent(
        name="web_developer_agent",
        model=model,
        instruction="You are a web developer. Always call write_webpage.",
        description="Web developer.",
        tools=[write_webpage_tool],
    )
    coordinator_agent = LlmAgent(
        name="coordinator_agent",
        model=model,
        instruction=(
            "First dispatch to research_agent, then to web_developer_agent, "
            "then return a final answer."
        ),
        description="Coordinator.",
        tools=[AgentTool(research_agent), AgentTool(web_developer_agent)],
    )
    return InMemoryRunner(agent=coordinator_agent, app_name="presentation_e2e")


async def _drive_invocation(runner: Any, user_text: str) -> list[Any]:
    from google.genai import types as genai_types

    session = await runner.session_service.create_session(
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


async def _wait_for(predicate, *, timeout=5.0, interval=0.02) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def _wait_for_async(predicate, *, timeout=5.0, interval=0.02) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def _spans_in_store(store, session_id: str) -> list[Any]:
    get_spans = getattr(store, "get_spans", None)
    if get_spans is None:
        return []
    result = get_spans(session_id)
    if asyncio.iscoroutine(result):
        result = await result
    return list(result or [])


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPresentationAgentHarmonograf:
    async def test_full_pipeline_emits_rich_span_set(
        self, harmonograf_server, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HARMONOGRAF_HOME", str(tmp_path))

        client = Client(
            name="presentation",
            server_addr=harmonograf_server["addr"],
            framework="ADK",
            capabilities=["HUMAN_IN_LOOP", "STEERING"],
        )

        runner = _build_presentation_runner(tmp_path / "output")
        handle = attach_adk(runner, client)
        try:
            await _drive_invocation(runner, "Create a presentation about Python programming")

            assert await _wait_for(
                lambda: client.session_id != "" and client._transport.connected,
                timeout=5.0,
            ), "transport never connected"

            store = harmonograf_server["store"]

            async def _resolve_adk_session_id() -> str:
                sessions = await store.list_sessions()
                for s in sessions:
                    if s.id.startswith("adk_"):
                        return s.id
                return ""

            async def _have_core_kinds() -> bool:
                sid = await _resolve_adk_session_id()
                if not sid:
                    return False
                spans = await _spans_in_store(store, sid)
                kinds = {str(getattr(s, "kind", "")) for s in spans}
                have_invocation = any("INVOCATION" in k for k in kinds)
                have_llm = any("LLM_CALL" in k for k in kinds)
                have_tool = any("TOOL_CALL" in k for k in kinds)
                return have_invocation and have_llm and have_tool

            assert await _wait_for_async(
                _have_core_kinds, timeout=10.0
            ), "expected INVOCATION + LLM_CALL + TOOL_CALL spans never reached the store"

            session_id = await _resolve_adk_session_id()
            spans = await _spans_in_store(store, session_id)
            by_kind: dict[str, list[Any]] = {}
            for s in spans:
                by_kind.setdefault(str(getattr(s, "kind", None)), []).append(s)

            invocation_spans = [
                s for k, g in by_kind.items() if "INVOCATION" in k for s in g
            ]
            llm_spans = [
                s for k, g in by_kind.items() if "LLM_CALL" in k for s in g
            ]
            tool_spans = [
                s for k, g in by_kind.items() if "TOOL_CALL" in k for s in g
            ]
            transfer_spans = [
                s for k, g in by_kind.items() if "TRANSFER" in k for s in g
            ]

            assert len(invocation_spans) >= 1, (
                f"expected at least 1 INVOCATION span, got {len(invocation_spans)}"
            )
            # The pipeline fans out across three agents and multiple turns
            # of the coordinator, so we expect comfortably more than one
            # LLM_CALL span to have been emitted.
            assert len(llm_spans) >= 2, (
                f"expected multiple LLM_CALL spans across agents, got {len(llm_spans)}: "
                f"names={[getattr(s, 'name', None) for s in llm_spans]}"
            )

            # At least one TOOL_CALL must be the write_webpage FunctionTool;
            # the others will be AgentTool sub-dispatches.
            tool_names = [getattr(s, "name", "") for s in tool_spans]
            assert any(n == "write_webpage" for n in tool_names), (
                f"no TOOL_CALL span for write_webpage; saw {tool_names}"
            )

            # Agent coverage: every invocation span carries the agent
            # name in its ``name`` field. We want to see all three agents
            # represented somewhere in the emitted span set — either as
            # their own INVOCATION row or via AgentTool TOOL_CALL names.
            invocation_names = {getattr(s, "name", "") for s in invocation_spans}
            all_span_names = invocation_names | set(tool_names)
            for agent_name in (
                "coordinator_agent",
                "research_agent",
                "web_developer_agent",
            ):
                assert agent_name in all_span_names, (
                    f"agent {agent_name!r} missing from emitted span set; "
                    f"invocation_names={invocation_names}, tool_names={tool_names}"
                )

            # A5 / task #12: AgentTool sub-dispatch must read as a
            # transfer in the Gantt. The coordinator fans out to two
            # sub-agents via AgentTool so we expect ≥2 TRANSFER spans,
            # each attributed to the correct target agent and carrying
            # a LINK_RELATION_INVOKED edge back to the paired TOOL_CALL.
            assert len(transfer_spans) >= 2, (
                f"expected ≥2 TRANSFER spans for AgentTool dispatch, "
                f"got {len(transfer_spans)}: "
                f"names={[getattr(s, 'name', None) for s in transfer_spans]}"
            )
            transfer_targets = {
                getattr(s, "attributes", {}).get("target_agent")
                if isinstance(getattr(s, "attributes", None), dict)
                else None
                for s in transfer_spans
            }
            # Attributes may be a protobuf map — fall back to reading
            # via getattr lookup if dict access returned None.
            if None in transfer_targets:
                transfer_targets = set()
                for s in transfer_spans:
                    attrs = getattr(s, "attributes", None)
                    if attrs is None:
                        continue
                    try:
                        val = attrs["target_agent"]
                    except Exception:
                        val = None
                    if hasattr(val, "string_value"):
                        val = val.string_value
                    if val:
                        transfer_targets.add(val)
            assert {"research_agent", "web_developer_agent"} <= transfer_targets, (
                f"TRANSFER spans missing expected sub-agents; "
                f"saw targets={transfer_targets}"
            )
            for s in transfer_spans:
                links = list(getattr(s, "links", []) or [])
                assert links, (
                    f"TRANSFER span {getattr(s, 'name', '?')} has no links; "
                    "expected LINK_RELATION_INVOKED edge to the TOOL_CALL"
                )

            # Task #1: every sub-agent should own its own row in the
            # Gantt — i.e. spans should be attributed to ≥3 distinct
            # agent_ids (coordinator + research + web_developer).
            agent_ids = {
                getattr(s, "agent_id", "") for s in spans if getattr(s, "agent_id", "")
            }
            assert {"coordinator_agent", "research_agent", "web_developer_agent"} <= agent_ids, (
                f"expected per-sub-agent rows; saw agent_ids={agent_ids}"
            )
        finally:
            handle.detach()
            client.shutdown(flush_timeout=5.0)


@pytest.mark.asyncio
class TestPresentationMultiSession:
    """Task #2: drive two ADK sessions through one Client and assert two
    independent harmonograf sessions are created on the server."""

    async def test_two_adk_sessions_become_two_harmonograf_sessions(
        self, harmonograf_server, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HARMONOGRAF_HOME", str(tmp_path))

        client = Client(
            name="presentation",
            server_addr=harmonograf_server["addr"],
            framework="ADK",
            capabilities=["HUMAN_IN_LOOP", "STEERING"],
        )

        runner = _build_presentation_runner(tmp_path / "output")
        handle = attach_adk(runner, client)
        try:
            # Drive two distinct ADK sessions through the same runner.
            # _drive_invocation creates a fresh ADK session each call.
            await _drive_invocation(runner, "Create a presentation about Python programming")
            await _drive_invocation(runner, "Create a presentation about Rust programming")

            assert await _wait_for(
                lambda: client._transport.connected,
                timeout=5.0,
            ), "transport never connected"

            store = harmonograf_server["store"]

            async def _have_two_sessions() -> bool:
                sessions = await store.list_sessions()
                # Filter to harmonograf sessions our adapter would create —
                # those whose ids start with "adk_". The transport's own
                # auto-generated session is also present but unused for
                # spans, so requiring ≥2 adk_-prefixed sessions is the
                # acceptance criterion for task #2.
                adk_sessions = [s for s in sessions if s.id.startswith("adk_")]
                return len(adk_sessions) >= 2

            assert await _wait_for_async(
                _have_two_sessions, timeout=10.0
            ), "expected two harmonograf sessions for two ADK sessions"

            sessions = await store.list_sessions()
            adk_sessions = [s for s in sessions if s.id.startswith("adk_")]
            assert len(adk_sessions) >= 2, (
                f"expected ≥2 adk_-prefixed sessions, got {[s.id for s in sessions]}"
            )

            # Each session must contain its own coordinator INVOCATION span.
            for s in adk_sessions[:2]:
                spans = await _spans_in_store(store, s.id)
                kinds = {str(getattr(sp, "kind", "")) for sp in spans}
                assert any("INVOCATION" in k for k in kinds), (
                    f"session {s.id} has no INVOCATION span"
                )
        finally:
            handle.detach()
            client.shutdown(flush_timeout=5.0)


# ---------------------------------------------------------------------------
# Canonical `adk web` path: module-level `app` attaches the harmonograf
# plugin automatically. This test imports presentation_agent.agent under a
# fixture-scoped HARMONOGRAF_SERVER env, drives one invocation via an
# InMemoryRunner constructed around ``app``, and asserts spans land in
# the server — proving that a user running `adk web presentation_agent`
# gets telemetry without any glue code in their runner.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPresentationAppExport:
    async def test_module_app_attaches_harmonograf_plugin(
        self, harmonograf_server, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HARMONOGRAF_HOME", str(tmp_path))
        monkeypatch.setenv("HARMONOGRAF_SERVER", harmonograf_server["addr"])

        import importlib
        import sys
        from pathlib import Path

        # presentation_agent lives at repo root and has no pyproject.toml
        # of its own, so ensure the repo root is on sys.path when this
        # test runs under `uv run pytest` from any working directory.
        repo_root = Path(__file__).resolve().parents[2]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))

        import presentation_agent.agent as agent_mod

        # Drop any cached Client/App from a prior test so we rebind to
        # the fixture's server addr.
        agent_mod._reset_for_testing()
        importlib.reload(agent_mod)

        app = agent_mod.app
        assert app is not None, "presentation_agent.agent.app failed to build"

        plugin_names = [getattr(p, "name", "") for p in app.plugins]
        assert "harmonograf" in plugin_names, (
            f"expected harmonograf plugin in app.plugins; got {plugin_names}"
        )

        # Swap every agent model for the scripted mock so we don't call
        # a real LLM. LlmAgent.model is a pydantic field — reassigning
        # is supported because the config isn't frozen.
        scripted = _build_scripted_model()
        agent_mod.research_agent.model = scripted
        agent_mod.web_developer_agent.model = scripted
        agent_mod.root_agent.model = scripted

        from google.adk.runners import InMemoryRunner

        runner = InMemoryRunner(app=app, app_name="presentation_agent")
        try:
            await _drive_invocation(
                runner, "Create a presentation about Python programming"
            )

            client = agent_mod._CLIENT
            assert client is not None, (
                "agent module did not construct a harmonograf client"
            )
            assert await _wait_for(
                lambda: client.session_id != "" and client._transport.connected,
                timeout=5.0,
            ), "transport never connected"

            store = harmonograf_server["store"]

            async def _resolve_adk_session_id() -> str:
                sessions = await store.list_sessions()
                for s in sessions:
                    if s.id.startswith("adk_"):
                        return s.id
                return ""

            async def _have_core_kinds() -> bool:
                sid = await _resolve_adk_session_id()
                if not sid:
                    return False
                spans = await _spans_in_store(store, sid)
                kinds = {str(getattr(s, "kind", "")) for s in spans}
                return (
                    any("INVOCATION" in k for k in kinds)
                    and any("LLM_CALL" in k for k in kinds)
                    and any("TOOL_CALL" in k for k in kinds)
                )

            assert await _wait_for_async(
                _have_core_kinds, timeout=10.0
            ), "core spans never reached the store via the app-exported plugin"

            session_id = await _resolve_adk_session_id()
            spans = await _spans_in_store(store, session_id)
            by_kind: dict[str, list[Any]] = {}
            for s in spans:
                by_kind.setdefault(str(getattr(s, "kind", None)), []).append(s)
            tool_names = [
                getattr(s, "name", "")
                for k, g in by_kind.items()
                if "TOOL_CALL" in k
                for s in g
            ]
            assert any(n == "write_webpage" for n in tool_names), (
                f"write_webpage TOOL_CALL missing; saw {tool_names}"
            )
        finally:
            agent_mod._reset_for_testing()
