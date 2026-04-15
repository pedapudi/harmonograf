"""ADK multi-agent sample: coordinator → research → web developer.

The ``root_agent`` below is a plain ADK ``LlmAgent`` hierarchy. The
module also exports ``app``: an :class:`google.adk.apps.app.App` that
wraps ``root_agent`` and attaches a harmonograf plugin so that canonical
ADK entry points (``adk web``, ``adk run``, ``adk api_server``) emit
telemetry automatically — no separate runner script required.

``app`` is materialised lazily via PEP 562 module-level ``__getattr__``
so that importing this module without a running harmonograf server is
still cheap, and tests can override ``HARMONOGRAF_SERVER`` before the
first access.
"""

from __future__ import annotations

import atexit
import logging
import os
from typing import Any, Optional

from google.adk.agents import Agent
from google.adk.tools import AgentTool, FunctionTool

try:
    from harmonograf_client.tools import augment_instruction as _hg_augment
except Exception:  # noqa: BLE001 — keep module importable without the client
    def _hg_augment(existing: str) -> str:  # type: ignore[misc]
        return existing

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def write_webpage(
    topic: str, html_content: str, css_content: str, js_content: str
) -> str:
    """Writes an interactive webpage (HTML, CSS, JS) under ``output/``."""
    try:
        topic_filename = topic.lower().replace(" ", "_").replace("/", "_")
        output_dir = os.path.join(os.path.dirname(__file__), "output", topic_filename)
        os.makedirs(output_dir, exist_ok=True)

        html_path = os.path.join(output_dir, "index.html")
        css_path = os.path.join(output_dir, "styles.css")
        js_path = os.path.join(output_dir, "script.js")

        with open(html_path, "w") as f:
            f.write(html_content)
        with open(css_path, "w") as f:
            f.write(css_content)
        with open(js_path, "w") as f:
            f.write(js_content)

        return f"Successfully created presentation on '{topic}' at {output_dir}"
    except Exception as e:  # noqa: BLE001 — tool result must be serialisable
        return f"Error writing file: {e}"


def read_presentation_files(topic: str) -> dict[str, str]:
    """Reads the generated presentation files for ``topic`` and returns a
    dict mapping filename → file contents. Used by ``reviewer_agent`` to
    critique ``web_developer_agent``'s output.
    """
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
    """Overwrites ``path`` with ``new_content`` in place. Used by
    ``debugger_agent`` to fix issues that ``reviewer_agent`` flagged or
    that caused ``write_webpage`` to fail. Relative paths are resolved
    against the ``output/`` directory so the debugger can't accidentally
    scribble outside the sandbox.
    """
    try:
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(__file__), "output", path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(new_content)
        return f"Successfully patched {path}"
    except Exception as e:  # noqa: BLE001 — tool result must be serialisable
        return f"Error patching file: {e}"


write_webpage_tool = FunctionTool(write_webpage)
read_presentation_files_tool = FunctionTool(read_presentation_files)
patch_file_tool = FunctionTool(patch_file)


# Users can specify the model via environment variable.
# For OpenAI-compatible endpoints, provide a LiteLLM-compliant string
# (e.g. ``openai/my-model``) and set ``OPENAI_API_BASE``. ADK's LLMRegistry
# recognises provider-style strings and dispatches them through LiteLlm
# automatically — no wrapping needed here, as long as litellm is installed
# (the ``demo`` optional-deps group pulls it in).
MODEL_NAME = os.environ.get("USER_MODEL_NAME", "gemini-2.5-flash")


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


research_agent = Agent(
    name="research_agent",
    model=MODEL_NAME,
    instruction=_hg_augment(
        "You are a researcher. Your goal is to gather information about the "
        "topic the user provides.\nThink step-by-step and provide a "
        "comprehensive synthesis of high-quality bullet points and facts "
        "that can be used to generate a presentation slideshow."
    ),
    description=(
        "An agent capable of deeply reasoning and synthesizing a given topic "
        "for presentation notes."
    ),
    tools=[],
)

web_developer_agent = Agent(
    name="web_developer_agent",
    model=MODEL_NAME,
    instruction=_hg_augment(
        "You are an expert Frontend Web Developer. Your goal is to take "
        "research on a topic and generate a stunning, interactive, "
        "single-page presentation slideshow.\nGenerate beautiful semantic "
        "HTML structure, elegant CSS with modern design trends, animations, "
        "and transitions, and JavaScript for slideshow navigation "
        "(next/prev slides).\nThe HTML MUST include "
        '`<link rel="stylesheet" href="styles.css">` and '
        '`<script src="script.js"></script>` so the files are connected '
        "properly.\nRemember to output the absolute final HTML, CSS, and JS "
        "using the `write_webpage` tool! Do not just print the code out, "
        "you must invoke the tool once everything is ready."
    ),
    description=(
        "An expert frontend developer agent that generates interactive HTML, "
        "CSS, and JS slideshow presentations and saves them to disk."
    ),
    tools=[write_webpage_tool],
)

reviewer_agent = Agent(
    name="reviewer_agent",
    model=MODEL_NAME,
    instruction=_hg_augment(
        "You are a senior frontend code reviewer. You will be given the "
        "topic of a presentation that ``web_developer_agent`` just "
        "generated. Call the ``read_presentation_files`` tool with the "
        "topic to fetch the generated HTML, CSS, and JS, then produce a "
        "structured critique as a list of issues. Each issue must include "
        "a short description and a severity of 'critical', 'major', or "
        "'minor'. If there are no issues, return an empty list and say so "
        "explicitly so the coordinator knows to skip debugging."
    ),
    description=(
        "A reviewer agent that reads the generated presentation files and "
        "produces a structured critique of issues and their severity."
    ),
    tools=[read_presentation_files_tool],
)

debugger_agent = Agent(
    name="debugger_agent",
    model=MODEL_NAME,
    instruction=_hg_augment(
        "You are a debugging agent. You are invoked when "
        "``write_webpage`` failed or when ``reviewer_agent`` flagged "
        "critical issues in the generated presentation. Read the issues "
        "and their file paths, then call the ``patch_file`` tool with the "
        "full corrected content of each file that needs to change. "
        "Report which files you patched when you are done."
    ),
    description=(
        "A debugger agent that patches generated presentation files in "
        "place to resolve critical issues flagged by the reviewer or by "
        "a failing write_webpage call."
    ),
    tools=[patch_file_tool],
)

_inner_root_agent = Agent(
    name="coordinator_agent",
    model=MODEL_NAME,
    instruction=(
        "You are the Coordinator Agent. Your task is to work with the user "
        "to pick a topic for an interactive slideshow presentation.\n"
        "First, get a topic from the user.\nSecond, transfer control to the "
        "'research_agent' to gather comprehensive context and facts about "
        "the topic. Make sure to provide it with the topic!\nThird, after "
        "researching, transfer control to the 'web_developer_agent' and "
        "provide it with all the researched materials. Instruct it to "
        "generate and save the presentation codebase.\nFourth, transfer "
        "control to the 'reviewer_agent' with the topic so it can read "
        "the generated files and produce a structured critique.\nFifth, "
        "if ``write_webpage`` failed or the reviewer reported any "
        "critical issues, transfer control to the 'debugger_agent' with "
        "the reviewer's critique and have it patch the affected files. "
        "Skip this step when the reviewer reports no critical issues.\n"
        "Finally, report back to the user when the task is complete.\n"
        "Flow: research → web_developer → reviewer → "
        "(if critical issues) debugger → report."
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


# HarmonografAgent wraps the coordinator so ADK's entry points
# (``adk web`` / ``adk run`` / ``adk api_server``) pick up plan-driven
# orchestration transparently. ``harmonograf_client`` is late-bound in
# ``_build_app()`` — the module-level root_agent holds ``None`` until
# the App is constructed, at which point we mutate the attribute in
# place. This keeps module import cheap for callers that only need the
# inner agents.
try:
    from harmonograf_client import HarmonografAgent as _HarmonografAgent
except Exception:  # noqa: BLE001 — keep module importable without the client
    _HarmonografAgent = None  # type: ignore[assignment,misc]

if _HarmonografAgent is not None:
    root_agent = _HarmonografAgent(
        name="harmonograf",
        description="Harmonograf orchestrator wrapping coordinator_agent",
        inner_agent=_inner_root_agent,
        harmonograf_client=None,
        planner=None,
    )
else:
    root_agent = _inner_root_agent  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Harmonograf instrumentation — lazy App export
# ---------------------------------------------------------------------------


_DEFAULT_SERVER = "127.0.0.1:7531"

# Cached singleton so that multiple ``module.app`` accesses during agent
# loading don't construct a second Client (and a second transport thread).
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


def _build_app() -> Any:
    """Construct the ADK ``App`` wrapping ``root_agent`` plus the
    harmonograf plugin. Safe to call with or without a running server.
    """
    global _CLIENT, _ATEXIT_REGISTERED
    from google.adk.apps.app import App

    server_addr = os.environ.get("HARMONOGRAF_SERVER", _DEFAULT_SERVER)
    plugins: list[Any] = []

    try:
        from harmonograf_client import Client, make_adk_plugin
    except Exception as e:  # noqa: BLE001 — keep module importable
        log.warning(
            "harmonograf_client unavailable (%s); running without instrumentation",
            e,
        )
    else:
        _CLIENT = Client(
            name="presentation",
            server_addr=server_addr,
            framework="ADK",
            capabilities=["HUMAN_IN_LOOP", "STEERING"],
        )
        if not _ATEXIT_REGISTERED:
            atexit.register(_shutdown_client)
            _ATEXIT_REGISTERED = True
        # make_adk_plugin auto-wires a default LLMPlanner backed by ADK's
        # own LLM registry when planner= is left unset, so the
        # HUMAN_IN_LOOP / STEERING capabilities advertised above are
        # actually driven by plan generation and mid-run refinement.
        plugins.append(make_adk_plugin(_CLIENT))
        # Late-bind the harmonograf client onto the wrapper agent so its
        # orchestration loop can reach the plugin's shared state.
        if _HarmonografAgent is not None and isinstance(
            root_agent, _HarmonografAgent
        ):
            object.__setattr__(root_agent, "harmonograf_client", _CLIENT)
        log.info(
            "harmonograf: instrumented presentation_agent → %s (agent_id=%s)",
            server_addr,
            _CLIENT.agent_id,
        )

    return App(
        name="presentation_agent",
        root_agent=root_agent,
        plugins=plugins,
    )


def __getattr__(name: str) -> Any:
    """PEP 562 module-level lazy attribute — builds ``app`` on first
    access so importers that only need ``root_agent`` don't pay the cost
    of starting a telemetry transport.
    """
    global _APP
    if name == "app":
        if _APP is None:
            _APP = _build_app()
        return _APP
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def make_harmonograf_runner(**kwargs: Any) -> Any:
    """Build a :class:`HarmonografRunner` wrapping ``root_agent`` with
    plan enforcement enabled. Callers that bypass ``adk run`` / ``adk web``
    (e.g. a custom entrypoint or an integration test) can use this to
    actually drive the agent through the planner-generated DAG rather
    than relying on the softer plugin-only injection path.

    Returns ``None`` when ``harmonograf_client`` is unavailable so the
    demo module stays importable in minimal environments.
    """
    global _CLIENT, _ATEXIT_REGISTERED

    try:
        from harmonograf_client import Client
        from harmonograf_client.runner import HarmonografRunner
    except Exception as e:  # noqa: BLE001
        log.warning(
            "harmonograf_client unavailable (%s); make_harmonograf_runner returning None",
            e,
        )
        return None

    server_addr = os.environ.get("HARMONOGRAF_SERVER", _DEFAULT_SERVER)
    if _CLIENT is None:
        _CLIENT = Client(
            name="presentation",
            server_addr=server_addr,
            framework="ADK",
            capabilities=["HUMAN_IN_LOOP", "STEERING"],
        )
        if not _ATEXIT_REGISTERED:
            atexit.register(_shutdown_client)
            _ATEXIT_REGISTERED = True

    return HarmonografRunner(agent=root_agent, client=_CLIENT, **kwargs)


def _reset_for_testing() -> None:
    """Drop the cached ``App`` and ``Client`` so tests can rebuild under
    a fresh ``HARMONOGRAF_SERVER`` environment. Not part of the public
    API — called only from tests/e2e/test_presentation_agent.py.
    """
    global _APP, _CLIENT
    if _CLIENT is not None:
        try:
            _CLIENT.shutdown(flush_timeout=1.0)
        except Exception:
            pass
    _APP = None
    _CLIENT = None
