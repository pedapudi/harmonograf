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


write_webpage_tool = FunctionTool(write_webpage)


# Users can specify the model via environment variable.
# For OpenAI-compatible endpoints, provide a LiteLLM-compliant string
# (e.g. ``openai/my-model``) and set ``OPENAI_API_BASE``.
MODEL_NAME = os.environ.get("USER_MODEL_NAME", "gemini-2.5-flash")


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


research_agent = Agent(
    name="research_agent",
    model=MODEL_NAME,
    instruction=(
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
    instruction=(
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

root_agent = Agent(
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
        "generate and save the presentation codebase.\nReport back to the "
        "user when the task is complete."
    ),
    description=(
        "The main coordinator agent that drives the overall process of "
        "creating an interactive slideshow generation."
    ),
    tools=[AgentTool(research_agent), AgentTool(web_developer_agent)],
)


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
        plugins.append(make_adk_plugin(_CLIENT))
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
