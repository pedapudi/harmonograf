"""End-to-end: presentation_agent sample on goldfive + harmonograf.

Post-migration (issue #4) the presentation_agent module no longer owns
orchestration. The smoke test here verifies the new wiring:

* ``presentation_agent.agent`` imports cleanly.
* ``app`` exports a live :class:`google.adk.apps.app.App` with the
  :class:`HarmonografTelemetryPlugin` installed when a Client is built.
* ``build_goldfive_runner(mock=True)`` assembles a runnable
  :class:`goldfive.Runner` with ``ADKAdapter`` + ``HarmonografSink`` +
  a mocked ADK model + a canned LLMPlanner / LLMGoalDeriver.
* Driving that runner emits the full goldfive event stream
  (``RunStarted`` → ``PlanSubmitted`` → per-task ``TaskStarted`` /
  ``TaskCompleted`` → ``RunCompleted``) into ``InMemorySink``.
* When wired to a real in-process harmonograf server fixture, the
  events land as ``TelemetryUp(goldfive_event=...)`` frames that the
  server's ingest pipeline translates into storage + bus deltas.

The legacy scenario suites (scripted multi-turn ADK runs, concurrent
sessions, awaiting-human steering) were tightly coupled to
``attach_adk`` and the old ``HarmonografAgent`` orchestration and were
deleted in this Phase C cut. Restoring a scripted-model e2e against the
new stack is Phase D cleanup work — goldfive's own
``examples/adk_presentation`` covers the same shape with a non-scripted
mock.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

_ADK_AVAILABLE = importlib.util.find_spec("google.adk") is not None
_GOLDFIVE_AVAILABLE = importlib.util.find_spec("goldfive") is not None

pytestmark = pytest.mark.skipif(
    not (_ADK_AVAILABLE and _GOLDFIVE_AVAILABLE),
    reason="google.adk and goldfive must both be importable",
)


_PRES_DIR = Path(__file__).resolve().parent.parent / "reference_agents"


@pytest.fixture
def presentation_agent_module() -> Any:
    """Import ``presentation_agent.agent`` as a top-level module.

    The module is normally consumed by ``adk web presentation_agent``
    which puts the containing directory on ``sys.path``; here we do the
    same so the bare ``import presentation_agent.agent`` works inside
    the tests.
    """
    if str(_PRES_DIR) not in sys.path:
        sys.path.insert(0, str(_PRES_DIR))
    for stale in ("presentation_agent", "presentation_agent.agent"):
        sys.modules.pop(stale, None)
    import presentation_agent.agent as agent_mod  # type: ignore

    try:
        agent_mod._reset_for_testing()
    except Exception:
        pass
    yield agent_mod
    try:
        agent_mod._reset_for_testing()
    except Exception:
        pass


class TestPresentationAppExport:
    """Verify ``presentation_agent.agent.app`` builds without a server."""

    def test_module_exports_root_agent(self, presentation_agent_module: Any) -> None:
        root = presentation_agent_module.root_agent
        assert root.name == "coordinator_agent"
        sub_names = {
            getattr(t, "agent", t).name for t in root.tools if hasattr(t, "agent")
        }
        assert {
            "research_agent",
            "web_developer_agent",
            "reviewer_agent",
            "debugger_agent",
        }.issubset(sub_names)

    def test_app_builds_with_telemetry_plugin(
        self,
        presentation_agent_module: Any,
        harmonograf_server: dict,
    ) -> None:
        # Point the lazy app at the fixture's server so the Client's
        # transport connects cleanly in the background.
        os.environ["HARMONOGRAF_SERVER"] = harmonograf_server["addr"]
        try:
            app = presentation_agent_module.app
        finally:
            os.environ.pop("HARMONOGRAF_SERVER", None)
        assert app is not None
        assert app.name == "presentation_agent"
        assert app.root_agent.name == "coordinator_agent"
        plugin_names = {getattr(p, "name", "") for p in (app.plugins or [])}
        assert "harmonograf-telemetry" in plugin_names


class TestPresentationGoldfiveRunner:
    """End-to-end: goldfive Runner + HarmonografSink via build_goldfive_runner."""

    @pytest.mark.asyncio
    async def test_mock_run_emits_goldfive_event_stream(
        self,
        presentation_agent_module: Any,
    ) -> None:
        runner, memory_sink, client, _sink = (
            presentation_agent_module.build_goldfive_runner(
                topic="espresso machines",
                mock=True,
                client=None,  # no harmonograf server wiring in this variant
            )
        )
        assert client is None or client is not None  # sanity

        outcome = await runner.run("Create a short slideshow on espresso machines.")
        await runner.close()
        assert outcome.success is True, outcome.reason

        payload_kinds: list[str] = []
        for evt in memory_sink.events:
            if isinstance(evt, dict):
                payload_kinds.append(str(evt.get("kind", "")))
            else:
                kind = getattr(evt, "WhichOneof", lambda _: None)("payload")
                if kind is not None:
                    payload_kinds.append(str(kind))

        assert "run_started" in payload_kinds
        assert "run_completed" in payload_kinds
        assert "plan_submitted" in payload_kinds
        assert any(k == "task_started" for k in payload_kinds)
        assert any(k == "task_completed" for k in payload_kinds)

    @pytest.mark.asyncio
    async def test_mock_run_ships_events_to_harmonograf_server(
        self,
        presentation_agent_module: Any,
        harmonograf_server: dict,
    ) -> None:
        from harmonograf_client import Client, HarmonografSink

        client = Client(
            name="pres-e2e",
            server_addr=harmonograf_server["addr"],
            framework="ADK",
        )
        try:
            runner, memory_sink, bound_client, harmonograf_sink = (
                presentation_agent_module.build_goldfive_runner(
                    topic="espresso machines",
                    mock=True,
                    client=client,
                )
            )
            assert bound_client is client
            assert isinstance(harmonograf_sink, HarmonografSink)

            outcome = await runner.run(
                "Create a short slideshow on espresso machines."
            )
            await runner.close()
            assert outcome.success is True, outcome.reason

            # Wait briefly for the ingest loop to drain the sink's pushes.
            store = harmonograf_server["store"]
            plans = await _wait_for_any_plan(store, timeout=5.0)
            assert plans, (
                "no plan landed on the harmonograf server after mock run"
            )
        finally:
            client.shutdown(flush_timeout=2.0)


async def _wait_for_any_plan(store: Any, *, timeout: float) -> list[Any]:
    """Poll every session in the store until a plan is persisted. The
    presentation agent test boots a single client against a fresh server
    fixture, so any plan that lands belongs to this run.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        sessions = await store.list_sessions()
        for s in sessions or []:
            plans = await store.list_task_plans_for_session(s.id)
            if plans:
                return list(plans)
        await asyncio.sleep(0.1)
    return []
