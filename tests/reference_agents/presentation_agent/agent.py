"""ADK multi-agent presentation demo, driven by goldfive.

Post-migration (issue #4) this module no longer owns any orchestration.
The ADK tree (coordinator → research / web_developer / reviewer /
debugger) is plain ADK. Orchestration — planning, task dispatch, drift
detection, steering — lives in goldfive. Harmonograf observes the run
via :class:`harmonograf_client.HarmonografSink` (plan + task events)
and :class:`harmonograf_client.HarmonografTelemetryPlugin` (per-span
LLM_CALL / TOOL_CALL observability).

The module exports:

* ``root_agent`` — the ADK coordinator agent.
* ``app`` — a lazily-built :class:`google.adk.apps.app.App` that
  installs :class:`HarmonografTelemetryPlugin` so ``adk web`` /
  ``adk run`` emit spans automatically. ``app`` is created on first
  access (PEP 562 module-level ``__getattr__``) so callers that only
  want ``root_agent`` don't pay transport setup cost.
* ``build_goldfive_runner`` — convenience wrapper that assembles a
  :class:`goldfive.Runner` around the coordinator with an
  :class:`ADKAdapter`, :class:`HarmonografSink`, optional mock planner
  / goal deriver, and a logging sink. Mirrors the ``--mock`` pattern
  from ``goldfive/examples/adk_presentation/agent.py`` so the demo
  runs without credentials when needed.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
from collections.abc import AsyncGenerator
from typing import Any, Callable, Optional

from google.adk.agents import Agent
from google.adk.tools import AgentTool, FunctionTool

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def write_webpage(
    topic: str, html_content: str, css_content: str, js_content: str
) -> str:
    """Write an interactive webpage (HTML, CSS, JS) under ``output/``."""
    try:
        topic_filename = topic.lower().replace(" ", "_").replace("/", "_")
        output_dir = os.path.join(os.path.dirname(__file__), "output", topic_filename)
        os.makedirs(output_dir, exist_ok=True)

        with open(os.path.join(output_dir, "index.html"), "w") as f:
            f.write(html_content)
        with open(os.path.join(output_dir, "styles.css"), "w") as f:
            f.write(css_content)
        with open(os.path.join(output_dir, "script.js"), "w") as f:
            f.write(js_content)

        return f"Successfully created presentation on '{topic}' at {output_dir}"
    except OSError as e:
        return f"Error writing file: {e}"


def read_presentation_files(topic: str) -> dict[str, str]:
    """Read the generated presentation files and return name → contents."""
    topic_filename = topic.lower().replace(" ", "_").replace("/", "_")
    output_dir = os.path.join(os.path.dirname(__file__), "output", topic_filename)
    files: dict[str, str] = {}
    for name in ("index.html", "styles.css", "script.js"):
        path = os.path.join(output_dir, name)
        try:
            with open(path, "r") as f:
                files[name] = f.read()
        except OSError as e:
            files[name] = f"<error reading {path}: {e}>"
    return files


def patch_file(path: str, new_content: str) -> str:
    """Overwrite ``path`` with ``new_content`` in place.

    Relative paths resolve against ``output/`` so the debugger cannot
    scribble outside the sandbox.
    """
    try:
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(__file__), "output", path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(new_content)
        return f"Successfully patched {path}"
    except OSError as e:
        return f"Error patching file: {e}"


write_webpage_tool = FunctionTool(write_webpage)
read_presentation_files_tool = FunctionTool(read_presentation_files)
patch_file_tool = FunctionTool(patch_file)


MODEL_NAME = os.environ.get("USER_MODEL_NAME", "gemini-2.5-flash")


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


def _build_agent_tree(model: Any) -> Agent:
    research_agent = Agent(
        name="research_agent",
        model=model,
        instruction=(
            "You are a researcher. Your goal is to gather information about "
            "the topic the user provides.\nThink step-by-step and provide a "
            "comprehensive synthesis of high-quality bullet points and facts "
            "that can be used to generate a presentation slideshow."
        ),
        description=(
            "An agent capable of deeply reasoning and synthesizing a given "
            "topic for presentation notes."
        ),
        tools=[],
    )

    web_developer_agent = Agent(
        name="web_developer_agent",
        model=model,
        instruction=(
            "You are an expert Frontend Web Developer. Your goal is to take "
            "research on a topic and generate a stunning, interactive, "
            "single-page presentation slideshow.\nGenerate beautiful semantic "
            "HTML structure, elegant CSS with modern design trends, "
            "animations, and transitions, and JavaScript for slideshow "
            "navigation (next/prev slides).\nThe HTML MUST include "
            '`<link rel="stylesheet" href="styles.css">` and '
            '`<script src="script.js"></script>` so the files are connected '
            "properly.\nRemember to output the absolute final HTML, CSS, and "
            "JS using the `write_webpage` tool! Do not just print the code "
            "out, you must invoke the tool once everything is ready."
        ),
        description=(
            "An expert frontend developer agent that generates interactive "
            "HTML, CSS, and JS slideshow presentations and saves them to disk."
        ),
        tools=[write_webpage_tool],
    )

    reviewer_agent = Agent(
        name="reviewer_agent",
        model=model,
        instruction=(
            "You are a senior frontend code reviewer. You will be given the "
            "topic of a presentation that ``web_developer_agent`` just "
            "generated. Call the ``read_presentation_files`` tool with the "
            "topic to fetch the generated HTML, CSS, and JS, then produce a "
            "structured critique as a list of issues. Each issue must "
            "include a short description and a severity of 'critical', "
            "'major', or 'minor'. If there are no issues, return an empty "
            "list and say so explicitly so the coordinator knows to skip "
            "debugging."
        ),
        description=(
            "A reviewer agent that reads the generated presentation files "
            "and produces a structured critique of issues and their severity."
        ),
        tools=[read_presentation_files_tool],
    )

    debugger_agent = Agent(
        name="debugger_agent",
        model=model,
        instruction=(
            "You are a debugging agent. You are invoked when "
            "``write_webpage`` failed or when ``reviewer_agent`` flagged "
            "critical issues in the generated presentation. Read the issues "
            "and their file paths, then call the ``patch_file`` tool with "
            "the full corrected content of each file that needs to change. "
            "Report which files you patched when you are done."
        ),
        description=(
            "A debugger agent that patches generated presentation files in "
            "place to resolve critical issues flagged by the reviewer or by "
            "a failing write_webpage call."
        ),
        tools=[patch_file_tool],
    )

    return Agent(
        name="coordinator_agent",
        model=model,
        instruction=(
            "You are the Coordinator Agent. Your task is to work with the "
            "user to pick a topic for an interactive slideshow "
            "presentation.\nFirst, get a topic from the user.\nSecond, "
            "transfer control to the 'research_agent' to gather "
            "comprehensive context and facts about the topic. Make sure to "
            "provide it with the topic!\nThird, after researching, transfer "
            "control to the 'web_developer_agent' and provide it with all "
            "the researched materials. Instruct it to generate and save the "
            "presentation codebase.\nFourth, transfer control to the "
            "'reviewer_agent' with the topic so it can read the generated "
            "files and produce a structured critique.\nFifth, if "
            "``write_webpage`` failed or the reviewer reported any critical "
            "issues, transfer control to the 'debugger_agent' with the "
            "reviewer's critique and have it patch the affected files. Skip "
            "this step when the reviewer reports no critical issues.\n"
            "Finally, report back to the user when the task is complete.\n"
            "Flow: research → web_developer → reviewer → (if critical "
            "issues) debugger → report."
        ),
        description=(
            "The main coordinator agent that drives the overall process of "
            "creating an interactive slideshow generation."
        ),
        tools=[
            AgentTool(research_agent),
            AgentTool(web_developer_agent),
            AgentTool(reviewer_agent),
            AgentTool(debugger_agent),
        ],
    )


root_agent = _build_agent_tree(MODEL_NAME)


# ---------------------------------------------------------------------------
# Harmonograf instrumentation — lazy ``app`` export
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
    except Exception as e:  # noqa: BLE001 — keep module importable
        log.warning("harmonograf_client unavailable (%s); running without telemetry", e)
        return None
    server_addr = os.environ.get("HARMONOGRAF_SERVER", _DEFAULT_SERVER)
    _CLIENT = Client(
        name="presentation",
        server_addr=server_addr,
        framework="ADK",
        capabilities=["HUMAN_IN_LOOP", "STEERING"],
    )
    if not _ATEXIT_REGISTERED:
        atexit.register(_shutdown_client)
        _ATEXIT_REGISTERED = True
    log.info(
        "harmonograf: presentation_agent client → %s (agent_id=%s)",
        server_addr,
        _CLIENT.agent_id,
    )
    return _CLIENT


def _build_app() -> Any:
    """Construct the ADK ``App`` wrapping ``root_agent``.

    Installs :class:`HarmonografTelemetryPlugin` when harmonograf_client
    is importable so ``adk web`` / ``adk run`` emit per-span telemetry.
    Safe to construct even when no server is reachable — the Client
    buffers locally and retries in the background.
    """
    from google.adk.apps.app import App

    plugins: list[Any] = []
    client = _get_or_create_client()
    if client is not None:
        try:
            from harmonograf_client import HarmonografTelemetryPlugin
        except Exception as e:  # noqa: BLE001
            log.warning(
                "HarmonografTelemetryPlugin unavailable (%s); running without spans",
                e,
            )
        else:
            plugins.append(HarmonografTelemetryPlugin(client))

    return App(
        name="presentation_agent",
        root_agent=root_agent,
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
# goldfive Runner wiring — mirrors ``goldfive/examples/adk_presentation``
# ---------------------------------------------------------------------------


def _mock_planner_call_llm(topic: str) -> Callable[[str, str, str], Any]:
    """Return an async ``call_llm`` that produces a canned plan JSON.

    Matches ``goldfive.LLMPlanner``'s ``(system_prompt, user_prompt,
    model) -> str`` signature. Emits one task per specialist subagent
    so the executor walks ``TaskStarted`` / ``TaskCompleted`` for each.
    """

    plan_json = {
        "summary": f"Build a slideshow presentation on '{topic}'.",
        "tasks": [
            {
                "id": "research",
                "title": "Gather research bullet points on the topic",
                "description": "Summarise key facts about the topic.",
                "assignee_agent_id": "research_agent",
            },
            {
                "id": "build",
                "title": "Generate HTML/CSS/JS slideshow",
                "description": "Produce the presentation files and save them.",
                "assignee_agent_id": "web_developer_agent",
            },
            {
                "id": "review",
                "title": "Review the generated presentation",
                "description": "Critique the generated slideshow for issues.",
                "assignee_agent_id": "reviewer_agent",
            },
            {
                "id": "debug",
                "title": "Patch any critical issues the reviewer flagged",
                "description": "Apply fixes to the presentation files.",
                "assignee_agent_id": "debugger_agent",
            },
        ],
        "edges": [
            {"from_task_id": "research", "to_task_id": "build"},
            {"from_task_id": "build", "to_task_id": "review"},
            {"from_task_id": "review", "to_task_id": "debug"},
        ],
    }

    async def _call(system: str, prompt: str, model: str) -> str:
        return json.dumps(plan_json)

    return _call


def _mock_goal_call_llm(topic: str) -> Callable[[str, str, str], Any]:
    """Return an async ``call_llm`` that produces a single canned goal."""

    goals_json = {
        "goals": [
            {
                "id": "g1",
                "summary": f"Produce an interactive slideshow on '{topic}'.",
            }
        ]
    }

    async def _call(system: str, prompt: str, model: str) -> str:
        return json.dumps(goals_json)

    return _call


def _make_mock_adk_model() -> Any:
    """Return a BaseLlm that short-circuits every ADK model call.

    Borrowed from goldfive's adk_presentation example. Each subagent
    produces a deterministic reply so the executor's auto-complete
    marks each task COMPLETED on a clean adapter return.
    """
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types as genai_types

    class _MockLlm(BaseLlm):
        @classmethod
        def supported_models(cls) -> list[str]:
            return [r"mock/.*"]

        async def generate_content_async(
            self, llm_request: LlmRequest, stream: bool = False
        ) -> AsyncGenerator[LlmResponse, None]:
            text = (
                f"[mock:{self.model}] acknowledged task and deferred real "
                "work to a production run."
            )
            yield LlmResponse(
                content=genai_types.Content(
                    role="model",
                    parts=[genai_types.Part(text=text)],
                ),
                partial=False,
                turn_complete=True,
            )

    return _MockLlm(model="mock/presentation-agent")


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
    completes offline. This is the integration-oracle path used by
    ``tests/e2e/test_presentation_agent.py``.
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
        tree = _build_agent_tree(_make_mock_adk_model())
        planner_call_llm = _mock_planner_call_llm(topic)
        goal_call_llm = _mock_goal_call_llm(topic)
        planner_model = "mock/planner"
        goal_model = "mock/goal-deriver"
    else:
        tree = _build_agent_tree(MODEL_NAME)
        raise SystemExit(
            "non-mock mode not wired here; use goldfive/examples/adk_presentation "
            "for a real OpenAI-backed run"
        )

    adapter = ADKAdapter(tree)

    sinks: list[Any] = []
    memory_sink = InMemorySink()
    sinks.append(memory_sink)

    explicit_client = client
    if explicit_client is None:
        explicit_client = _get_or_create_client()

    harmonograf_sink = None
    if explicit_client is not None:
        try:
            from harmonograf_client import HarmonografSink

            harmonograf_sink = HarmonografSink(explicit_client)
            sinks.append(harmonograf_sink)
        except Exception as e:  # noqa: BLE001
            log.warning("HarmonografSink unavailable (%s)", e)

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
        executor=SequentialExecutor(max_plan_reinvocations=8),
        goal_deriver=LLMGoalDeriver(call_llm=goal_call_llm, model=goal_model),
        sinks=sinks,
        max_plan_reinvocations=8,
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
