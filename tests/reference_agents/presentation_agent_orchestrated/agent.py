"""ADK multi-agent presentation demo — orchestration mode.

This is the sibling of
``tests/reference_agents/presentation_agent/agent.py``. Both packages
share the same coordinator + research + web_developer + reviewer +
debugger tree (loaded by absolute file path from
``third_party/goldfive/examples/presentation_agent/agent.py``), so the
only intentional behavioural difference is the goldfive wrapping.

* **Observation mode** (``presentation_agent``) — plain ADK; the
  coordinator routes via instruction text. Harmonograf passively
  observes via :class:`HarmonografTelemetryPlugin`.
* **Orchestration mode** (this module) — the tree is handed to
  :func:`goldfive.wrap` before ``App()`` sees it. Goldfive derives a
  goal, plans the specialists, dispatches them, fires drift when the
  adapter return doesn't match, and surfaces steering / HITL seams
  into harmonograf. This is where the full goldfive behaviour lands in
  ``adk web``.

Planner / goal deriver selection mirrors goldfive's own
``examples/presentation_agent/agent.py`` live-mode branch:

* ``OPENAI_API_KEY`` set → live mode, ``openai`` SDK behind
  :class:`goldfive.LLMPlanner` / :class:`goldfive.LLMGoalDeriver`.
* ``OPENAI_API_KEY`` unset → mock mode, so ``adk web`` loads offline.
  The mock planner emits one task per specialist so the full drift /
  dispatch stream still fires end-to-end.
"""

from __future__ import annotations

import atexit
import importlib.util
import logging
import os
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared tree + mock planner / goal deriver / _MockLlm loaded from
# goldfive's example by absolute file path. Same strategy as the sibling
# observation-mode module — see that file for the rationale.
# ---------------------------------------------------------------------------


def _load_goldfive_presentation_module() -> Any:
    here = Path(__file__).resolve()
    candidates = [
        here.parents[3]
        / "third_party"
        / "goldfive"
        / "examples"
        / "presentation_agent"
        / "agent.py",
    ]
    override = os.environ.get("GOLDFIVE_PRESENTATION_AGENT_PATH")
    if override:
        candidates.insert(0, Path(override).expanduser().resolve())
    for candidate in candidates:
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location(
                "_goldfive_presentation_orchestrated", candidate
            )
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise ImportError(
        "Could not locate goldfive's presentation_agent example. Tried: "
        + ", ".join(str(c) for c in candidates)
    )


_goldfive_presentation = _load_goldfive_presentation_module()

_build_agent_tree = _goldfive_presentation._build_agent_tree
_mock_planner_call_llm = _goldfive_presentation._mock_planner_call_llm
_mock_goal_call_llm = _goldfive_presentation._mock_goal_call_llm
_openai_call_llm = _goldfive_presentation._openai_call_llm
_MockLlm = _goldfive_presentation._MockLlm

MODEL_NAME = os.environ.get("USER_MODEL_NAME", "gemini-2.5-flash")

# Build the plain ADK tree so ``root_agent`` is available pre-wrap for
# tests that want to inspect the coordinator + sub-agents directly.
root_agent = _build_agent_tree(MODEL_NAME)


# ---------------------------------------------------------------------------
# Harmonograf telemetry — shared Client across the lazy app builder and
# any future programmatic drivers. Same shape as the observation-mode
# sibling so the two can coexist under one harmonograf server.
# ---------------------------------------------------------------------------


_DEFAULT_SERVER = "127.0.0.1:7531"

_APP: Optional[Any] = None
_CLIENT: Optional[Any] = None
_ATEXIT_REGISTERED: bool = False


def _shutdown_client() -> None:
    global _CLIENT
    if _CLIENT is None:
        return
    try:
        _CLIENT.shutdown(flush_timeout=5.0)
    except Exception as e:  # noqa: BLE001 — atexit must never raise
        log.debug("harmonograf client shutdown raised: %s", e)
    _CLIENT = None


def _get_or_create_client() -> Optional[Any]:
    global _CLIENT, _ATEXIT_REGISTERED
    if _CLIENT is not None:
        return _CLIENT
    try:
        from harmonograf_client import Client
    except Exception as e:  # noqa: BLE001
        log.warning("harmonograf_client unavailable (%s); running without telemetry", e)
        return None
    server_addr = os.environ.get("HARMONOGRAF_SERVER", _DEFAULT_SERVER)
    _CLIENT = Client(
        name="presentation-orchestrated",
        server_addr=server_addr,
        framework="ADK",
        capabilities=["HUMAN_IN_LOOP", "STEERING"],
    )
    if not _ATEXIT_REGISTERED:
        atexit.register(_shutdown_client)
        _ATEXIT_REGISTERED = True
    log.info(
        "harmonograf: presentation_agent_orchestrated client → %s (agent_id=%s)",
        server_addr,
        _CLIENT.agent_id,
    )
    return _CLIENT


# ---------------------------------------------------------------------------
# ``adk web`` ``App`` export — root agent is ``goldfive.wrap(tree, ...)``.
# Lazy so importing the module offline is side-effect free.
# ---------------------------------------------------------------------------


def _build_app() -> Any:
    """Construct the ``App`` whose root agent is ``goldfive.wrap(...)``.

    Mirrors goldfive's own ``examples/presentation_agent`` planner /
    goal-deriver selection: live when ``OPENAI_API_KEY`` is set, mock
    otherwise so ``adk web`` can load offline.

    Under single-Runner-per-wrap (goldfive#130), one ``goldfive.Runner``
    backs the whole wrapped tree and it shares one ADK session with
    ``adk web``. The three harmonograf hookups the UI needs — span
    plugin, goldfive event sink, control bridge — wire onto the same
    :class:`Client` and the same Runner, so the full observability
    surface is a few lines of explicit composition. No bundle helper
    is needed.
    """
    import asyncio

    import goldfive
    from goldfive import LLMGoalDeriver, LLMPlanner
    from goldfive.control import ControlChannel
    from google.adk.apps.app import App

    live = bool(os.environ.get("OPENAI_API_KEY"))
    topic = os.environ.get("GOLDFIVE_EXAMPLE_TOPIC", "waffles")

    if live:
        call_llm = _openai_call_llm()
        planner_model = os.environ.get(
            "GOLDFIVE_EXAMPLE_PLANNER_MODEL", "gpt-4o-mini"
        )
        tree = _build_agent_tree(MODEL_NAME)
    else:
        log.info(
            "presentation_agent_orchestrated: OPENAI_API_KEY unset; building "
            "App in mock mode so `adk web` can load offline."
        )
        call_llm = _mock_planner_call_llm(topic)
        planner_model = "mock/planner"
        tree = _build_agent_tree(_MockLlm(model="mock/presentation-agent"))

    planner = LLMPlanner(call_llm=call_llm, model=planner_model)
    goal_deriver = LLMGoalDeriver(call_llm=call_llm, model=planner_model)

    plugins: list[Any] = []
    sinks: list[Any] = []
    control: Any = None
    bridge: Any = None
    client = _get_or_create_client()
    if client is not None:
        try:
            from harmonograf_client import HarmonografSink, HarmonografTelemetryPlugin
            from harmonograf_client._control_bridge import ControlBridge
        except Exception as e:  # noqa: BLE001
            log.warning(
                "harmonograf_client unavailable (%s); running without spans / "
                "plan sink / control",
                e,
            )
        else:
            plugins.append(HarmonografTelemetryPlugin(client))
            sinks.append(HarmonografSink(client))
            # Build a ControlChannel bound to a live bridge. The bridge
            # needs a running asyncio loop to consume events; adk web
            # always builds the app inside its loop so the production
            # path is covered. When ``_build_app`` is called outside a
            # loop (synchronous test setup), fall through to
            # ``control=None`` — plugin + sink still flow, but STEER /
            # PAUSE / CANCEL from the UI will return delivery=FAILURE
            # until the caller rebuilds inside a loop.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                log.warning(
                    "presentation_agent_orchestrated: _build_app called "
                    "outside a running loop; ControlBridge skipped"
                )
            else:
                channel = ControlChannel()
                bridge = ControlBridge(client, channel, loop)
                bridge.start()
                # Stash for test/teardown introspection — same pattern
                # observe() uses on runner._harmonograf_control_bridge.
                channel._harmonograf_control_bridge = bridge  # type: ignore[attr-defined]
                control = channel

    wrapped = goldfive.wrap(
        tree,
        planner=planner,
        goal_deriver=goal_deriver,
        plugins=plugins,
        sinks=sinks or None,
        control=control,
    )

    return App(
        name="presentation_agent_orchestrated",
        root_agent=wrapped,
        plugins=plugins,
    )


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute — build ``app`` on first access."""
    global _APP
    if name == "app":
        if _APP is None:
            _APP = _build_app()
        return _APP
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# Programmatic runner — the old ``build_goldfive_runner`` helper, moved
# here so the observation-mode module stays strictly about observation.
# Mirrors ``goldfive/examples/adk_presentation/agent.py`` so the demo
# runs offline when needed. Driven by ``tests/e2e/test_presentation_agent.py``.
# ---------------------------------------------------------------------------


def build_goldfive_runner(
    *,
    topic: str = "the history of the espresso machine",
    mock: bool = True,
    client: Optional[Any] = None,
    extra_sinks: Optional[list[Any]] = None,
) -> tuple[Any, Any, Optional[Any], Optional[Any]]:
    """Assemble a :class:`goldfive.Runner` driving the coordinator tree.

    Returns ``(runner, memory_sink, client, harmonograf_sink)``. The
    harmonograf ``client`` + ``sink`` are ``None`` when
    ``HARMONOGRAF_SERVER`` is unset and no explicit ``client`` is
    passed; otherwise a :class:`HarmonografSink` is attached.

    When ``mock=True`` every network call — ADK model, planner, goal
    deriver — is short-circuited with canned output so the run
    completes offline.
    """
    import goldfive
    from goldfive import (
        InMemorySink,
        LLMGoalDeriver,
        LLMPlanner,
        Runner,
        SequentialExecutor,
    )
    from goldfive.adapters.adk import ADKAdapter

    if mock:
        tree = _build_agent_tree(_MockLlm(model="mock/presentation-agent"))
        planner_call_llm = _mock_planner_call_llm(topic)
        goal_call_llm = _mock_goal_call_llm(topic)
        planner_model = "mock/planner"
        goal_model = "mock/goal-deriver"
    else:
        tree = _build_agent_tree(MODEL_NAME)
        raise SystemExit(
            "non-mock mode not wired here; use goldfive/examples/presentation_agent "
            "for a real OpenAI-backed run"
        )

    sinks: list[Any] = []
    memory_sink = InMemorySink()
    sinks.append(memory_sink)

    explicit_client = client
    if explicit_client is None:
        explicit_client = _get_or_create_client()

    # Install the telemetry plugin on the single backing Runner so per-
    # ADK-agent spans flow (harmonograf#74). Without this, the programmatic
    # runner path drives ADK with no plugins and the per-agent Gantt rows
    # never get populated — the e2e regression test against this helper
    # would then report a false negative.
    plugins: list[Any] = []
    harmonograf_sink = None
    if explicit_client is not None:
        try:
            from harmonograf_client import HarmonografSink, HarmonografTelemetryPlugin

            plugins.append(HarmonografTelemetryPlugin(explicit_client))
            harmonograf_sink = HarmonografSink(explicit_client)
            sinks.append(harmonograf_sink)
        except Exception as e:  # noqa: BLE001
            log.warning("HarmonografSink/Plugin unavailable (%s)", e)

    adapter = ADKAdapter(tree, plugins=plugins or None)

    try:
        logging_sink = goldfive.sinks.LoggingSink()
    except Exception:  # noqa: BLE001 — proto extra may be absent
        logging_sink = None
    if logging_sink is not None:
        sinks.append(logging_sink)

    if extra_sinks:
        sinks.extend(extra_sinks)

    runner = Runner(
        agent=adapter,
        planner=LLMPlanner(call_llm=planner_call_llm, model=planner_model),
        executor=SequentialExecutor(max_task_invocations=8),
        goal_deriver=LLMGoalDeriver(call_llm=goal_call_llm, model=goal_model),
        sinks=sinks,
        max_task_invocations=8,
    )
    return runner, memory_sink, explicit_client, harmonograf_sink


def _reset_for_testing() -> None:
    """Drop the cached ``app`` and ``Client``. Test-only hook."""
    global _APP, _CLIENT
    if _CLIENT is not None:
        try:
            _CLIENT.shutdown(flush_timeout=1.0)
        except Exception:
            pass
    _APP = None
    _CLIENT = None
