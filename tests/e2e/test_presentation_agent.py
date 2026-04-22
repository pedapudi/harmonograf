"""End-to-end: presentation_agent sample on goldfive + harmonograf.

Split across the two reference-agent packages:

* **Observation mode** — ``tests.reference_agents.presentation_agent`` —
  plain ADK tree with ``HarmonografTelemetryPlugin``. No
  ``goldfive.wrap``. The smoke tests here verify the tree + lazy ``app``
  still assemble now that the tree is sourced from goldfive's
  ``examples/presentation_agent/agent.py``.
* **Orchestration mode** — ``tests.reference_agents.presentation_agent_orchestrated`` —
  the same tree wrapped with ``goldfive.wrap(...)`` before ``App()``
  sees it, plus a programmatic ``build_goldfive_runner`` helper for
  headless runs. The runner tests exercise the full goldfive event
  stream end-to-end.

The legacy scenario suites (scripted multi-turn ADK runs, concurrent
sessions, awaiting-human steering) were tightly coupled to
``attach_adk`` and the old ``HarmonografAgent`` orchestration and were
deleted in the Phase C cut. Restoring a scripted-model e2e against the
new stack is Phase D cleanup work — goldfive's own
``examples/presentation_agent`` covers the same shape with a
non-scripted mock.
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
    """Import observation-mode ``presentation_agent.agent`` as a top-level module.

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


@pytest.fixture
def presentation_agent_orchestrated_module() -> Any:
    """Import orchestration-mode ``presentation_agent_orchestrated.agent``."""
    if str(_PRES_DIR) not in sys.path:
        sys.path.insert(0, str(_PRES_DIR))
    for stale in (
        "presentation_agent_orchestrated",
        "presentation_agent_orchestrated.agent",
    ):
        sys.modules.pop(stale, None)
    import presentation_agent_orchestrated.agent as agent_mod  # type: ignore

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
    """Verify observation-mode ``presentation_agent.agent.app`` still builds."""

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


class TestOrchestratedAppExport:
    """Verify orchestration-mode ``presentation_agent_orchestrated.agent.app`` wraps the tree."""

    def test_orchestrated_app_builds(
        self,
        presentation_agent_orchestrated_module: Any,
        harmonograf_server: dict,
    ) -> None:
        from goldfive.adapters.adk_wrap import GoldfiveADKAgent
        from google.adk.agents.base_agent import BaseAgent

        # Force mock-mode (no OPENAI_API_KEY) so the lazy ``app`` build
        # stays offline. The fixture still exercises live-mode indirectly
        # via other tests.
        prev = os.environ.pop("OPENAI_API_KEY", None)
        os.environ["HARMONOGRAF_SERVER"] = harmonograf_server["addr"]
        try:
            app = presentation_agent_orchestrated_module.app
        finally:
            os.environ.pop("HARMONOGRAF_SERVER", None)
            if prev is not None:
                os.environ["OPENAI_API_KEY"] = prev

        assert app is not None
        assert app.name == "presentation_agent_orchestrated"
        # ``goldfive.wrap`` of an ADK BaseAgent returns a GoldfiveADKAgent
        # (itself a BaseAgent subclass); confirm both invariants.
        assert isinstance(app.root_agent, BaseAgent)
        assert isinstance(app.root_agent, GoldfiveADKAgent)
        plugin_names = {getattr(p, "name", "") for p in (app.plugins or [])}
        assert "harmonograf-telemetry" in plugin_names

    @pytest.mark.asyncio
    async def test_orchestrated_app_wires_full_observability(
        self,
        presentation_agent_orchestrated_module: Any,
        harmonograf_server: dict,
    ) -> None:
        """``_build_app`` must wire plugin + sink + control onto the Runner.

        Under single-Runner-per-wrap (goldfive#130), all three harmonograf
        hookups attach to the one runner that backs the wrapped tree:

        * :class:`HarmonografTelemetryPlugin` on the ADK app / wrap
          plugins (so lifecycle spans flow out — harmonograf#48 shape).
        * :class:`HarmonografSink` on ``runner.sinks`` (so goldfive's
          ``plan_submitted`` / ``plan_revised`` / ``drift_detected`` /
          ``task_*`` events reach harmonograf — the hookup that
          harmonograf#57 was bug-tracking under N-runners).
        * A live :class:`ControlBridge`-backed :class:`ControlChannel`
          on ``runner.control`` so STEER / PAUSE / CANCEL events from
          the UI route back into the goldfive steerer. Without it,
          PostAnnotation returns ``delivery=FAILURE``.

        The test runs inside a loop — the same shape ``adk web`` uses —
        so the ControlBridge is reachable (it needs a running loop).
        """
        from harmonograf_client import HarmonografSink
        from harmonograf_client._control_bridge import ControlBridge

        prev = os.environ.pop("OPENAI_API_KEY", None)
        os.environ["HARMONOGRAF_SERVER"] = harmonograf_server["addr"]
        try:
            # First-access is lazy — run on this asyncio loop so the
            # ControlBridge construction in ``_build_app`` finds it.
            app = presentation_agent_orchestrated_module.app
        finally:
            os.environ.pop("HARMONOGRAF_SERVER", None)
            if prev is not None:
                os.environ["OPENAI_API_KEY"] = prev

        root = app.root_agent
        # ``goldfive.wrap`` returns a ``GoldfiveADKAgent`` that keeps the
        # backing Runner under ``_runner``.
        runner = getattr(root, "_runner", None) or getattr(root, "runner", None)
        assert runner is not None, f"no runner on {type(root).__name__}"

        # Control: live, bridge-backed channel.
        assert runner.control is not None, (
            "runner.control is None — ControlBridge did not wire; "
            "steers will return delivery=FAILURE"
        )
        bridge = getattr(runner.control, "_harmonograf_control_bridge", None)
        assert isinstance(bridge, ControlBridge), (
            "runner.control is not a bridge-backed channel; the UI control "
            "path will not reach the goldfive steerer"
        )

        # Sink: HarmonografSink on runner.sinks so goldfive plan / task
        # events reach harmonograf.
        sinks = list(getattr(runner, "sinks", None) or [])
        assert any(isinstance(s, HarmonografSink) for s in sinks), (
            "no HarmonografSink on runner.sinks — goldfive plan / task "
            "events will not reach harmonograf. "
            f"sinks={[type(s).__name__ for s in sinks]}"
        )

        # Plugin: HarmonografTelemetryPlugin on the ADK App plugins so
        # ADK lifecycle callbacks turn into harmonograf spans.
        plugin_names = {getattr(p, "name", "") for p in (app.plugins or [])}
        assert "harmonograf-telemetry" in plugin_names, (
            f"HarmonografTelemetryPlugin not on app.plugins: {plugin_names}"
        )


class TestPresentationGoldfiveRunner:
    """End-to-end: goldfive Runner + HarmonografSink via build_goldfive_runner.

    The helper lives on the orchestration-mode sibling now — that's the
    only module that knows how to drive a goldfive Runner around the
    tree. Observation mode never had a runner.
    """

    @pytest.mark.asyncio
    async def test_orchestrated_mock_run_emits_goldfive_events(
        self,
        presentation_agent_orchestrated_module: Any,
    ) -> None:
        runner, memory_sink, client, _sink = (
            presentation_agent_orchestrated_module.build_goldfive_runner(
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

        # One TaskStarted + TaskCompleted per specialist.
        completed_task_ids = {
            evt.task_completed.task_id
            for evt in memory_sink.events
            if hasattr(evt, "WhichOneof")
            and evt.WhichOneof("payload") == "task_completed"
        }
        assert completed_task_ids == {"research", "build", "review", "debug"}, (
            f"expected TaskCompleted for each specialist, got {completed_task_ids}"
        )

    @pytest.mark.asyncio
    async def test_mock_run_ships_events_to_harmonograf_server(
        self,
        presentation_agent_orchestrated_module: Any,
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
                presentation_agent_orchestrated_module.build_goldfive_runner(
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

    @pytest.mark.asyncio
    async def test_all_task_status_events_persist(
        self,
        presentation_agent_orchestrated_module: Any,
        harmonograf_server: dict,
    ) -> None:
        """Regression for issue #18: every task_completed event the sink
        observes must also land in harmonograf's storage with the matching
        terminal status.

        The original bug was a shutdown race in the Client transport:
        Client.shutdown waited for the ring buffer to empty, but the
        buffered events had only been moved onto an in-process gRPC
        send_queue — not yet serialized to the wire. Cancelling recv_task
        then aborted the bidirectional call and dropped every event in
        flight, leaving research=COMPLETED but build/review still stuck
        in RUNNING/PENDING.
        """
        from harmonograf_client import Client
        from harmonograf_server.storage import TaskStatus

        client = Client(
            name="pres-e2e-regress",
            server_addr=harmonograf_server["addr"],
            framework="ADK",
        )
        try:
            runner, memory_sink, _, _ = (
                presentation_agent_orchestrated_module.build_goldfive_runner(
                    topic="waffles",
                    mock=True,
                    client=client,
                )
            )
            outcome = await runner.run("make a presentation about waffles")
            await runner.close()
            assert outcome.success is True, outcome.reason

            completed_task_ids = {
                evt.task_completed.task_id
                for evt in memory_sink.events
                if evt.WhichOneof("payload") == "task_completed"
            }
            assert completed_task_ids == {"research", "build", "review", "debug"}, (
                f"goldfive sink did not observe all four task_completed events: "
                f"{completed_task_ids}"
            )
        finally:
            # ``Client.shutdown`` is synchronous and internally joins the
            # transport thread. Running it from the event loop would block
            # the same loop that the in-process harmonograf_server fixture
            # uses to drain the wire, so queued events would never reach
            # the servicer before the stream closed. Hand it off to a
            # worker thread so the main loop keeps draining the socket.
            await asyncio.to_thread(client.shutdown, 5.0)

        # After the client flushes, storage must agree with the sink.
        store = harmonograf_server["store"]
        plans = await _wait_for_plan_with_tasks(
            store, expected_task_ids=completed_task_ids, timeout=5.0
        )
        assert plans, "no plan landed on the server after mock run"
        # Only the waffles plan should exist — no phantom plans from past runs.
        assert len(plans) == 1
        plan = plans[0]
        assert "waffles" in plan.summary.lower()
        task_by_id = {t.id: t for t in plan.tasks}
        for tid in completed_task_ids:
            assert task_by_id[tid].status == TaskStatus.COMPLETED, (
                f"task {tid!r} persisted with status {task_by_id[tid].status.value} "
                f"but the sink saw task_completed for it"
            )


class TestOrchestratedPerAgentRows:
    """Regression for harmonograf#74 — per-ADK-agent Gantt rows.

    Before the fix, a goldfive-wrapped presentation tree (coordinator +
    research + web_developer + reviewer + debugger) produced a SINGLE
    ``agents`` row in the harmonograf store — the client-root id — and
    every span attributed to that one id. The Gantt UI therefore
    collapsed the whole tree onto one row.

    After the fix, the :class:`HarmonografTelemetryPlugin` stamps each
    ADK agent with its own derived per-agent id
    (``<client.agent_id>:<adk_name>``) and emits
    ``hgraf.agent.{name,parent_id,kind,branch}`` on the first span of
    each agent, which the server harvests into the auto-registered
    ``Agent.metadata``. The test below runs a mock orchestrated pass
    and asserts the store has one row per ADK agent with the right
    parent-agent links.
    """

    @pytest.mark.asyncio
    async def test_orchestrated_mock_run_registers_one_row_per_adk_agent(
        self,
        presentation_agent_orchestrated_module: Any,
        harmonograf_server: dict,
    ) -> None:
        from harmonograf_client import Client

        client = Client(
            name="pres-per-agent",
            server_addr=harmonograf_server["addr"],
            framework="ADK",
        )
        try:
            runner, _memory_sink, _bound_client, _harmonograf_sink = (
                presentation_agent_orchestrated_module.build_goldfive_runner(
                    topic="waffles",
                    mock=True,
                    client=client,
                )
            )
            outcome = await runner.run("make a presentation about waffles")
            await runner.close()
            assert outcome.success is True, outcome.reason
        finally:
            await asyncio.to_thread(client.shutdown, 5.0)

        store = harmonograf_server["store"]
        # Poll until agents land — spans flow over the wire async.
        deadline = asyncio.get_event_loop().time() + 5.0
        rows: list[Any] = []
        while asyncio.get_event_loop().time() < deadline:
            sessions = await store.list_sessions()
            rows = []
            for s in sessions or []:
                rows.extend(await store.list_agents_for_session(s.id))
            # Expect at least the coordinator + research specialist (the
            # two agents that actually fire callbacks in mock mode).
            coord_hits = [
                r for r in rows if r.metadata.get("adk.agent.name") == "coordinator_agent"
            ]
            if coord_hits:
                break
            await asyncio.sleep(0.1)

        # The client-root id appears as one row (Hello-registered); the
        # per-agent rows appear as additional rows with
        # ``adk.agent.name`` metadata.
        adk_named_rows = [r for r in rows if r.metadata.get("adk.agent.name")]
        adk_names = {r.metadata["adk.agent.name"] for r in adk_named_rows}
        assert "coordinator_agent" in adk_names, (
            f"expected coordinator_agent row; got adk names {adk_names!r} across {len(rows)} rows"
        )
        # Every per-agent row must carry a real display name
        # (``Agent.name``), a kind metadata, and either no parent (for
        # the coordinator / root) or a parent_agent_id that references
        # another per-agent row in this same session.
        for row in adk_named_rows:
            assert row.name, f"agent row missing display name: {row.id}"
            assert row.metadata.get("harmonograf.agent_kind"), (
                f"agent row missing kind metadata: {row.id} / {row.metadata!r}"
            )
        # If the run drove into a sub-agent (mock mode routes through
        # research), confirm the parent link is wired up.
        specialists = [
            r for r in adk_named_rows
            if r.metadata.get("adk.agent.name") != "coordinator_agent"
        ]
        if specialists:
            coord_id = coord_hits[0].id
            for spec in specialists:
                parent = spec.metadata.get("harmonograf.parent_agent_id")
                assert parent == coord_id, (
                    f"specialist {spec.metadata['adk.agent.name']} has parent "
                    f"{parent!r}; expected coordinator id {coord_id!r}"
                )


async def _wait_for_plan_with_tasks(
    store: Any, *, expected_task_ids: set, timeout: float
) -> list[Any]:
    """Poll until a plan whose tasks are all in a terminal state lands."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        sessions = await store.list_sessions()
        for s in sessions or []:
            plans = await store.list_task_plans_for_session(s.id)
            for p in plans:
                task_by_id = {t.id: t for t in p.tasks}
                if expected_task_ids.issubset(task_by_id.keys()) and all(
                    task_by_id[tid].status.value == "COMPLETED"
                    for tid in expected_task_ids
                ):
                    return list(plans)
        await asyncio.sleep(0.1)
    # Timed out — return whatever we have so the assertion reports the
    # actual persisted state.
    sessions = await store.list_sessions()
    plans: list[Any] = []
    for s in sessions or []:
        plans.extend(await store.list_task_plans_for_session(s.id))
    return plans


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
