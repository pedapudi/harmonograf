"""ADK multi-agent presentation demo — observation mode.

Post-migration (issue #4) this module no longer owns any orchestration,
and as of the split-out of the orchestrated sibling it no longer even
owns the agent tree: the coordinator + research + web_developer +
reviewer + debugger tree (along with the ``write_webpage`` /
``read_presentation_files`` / ``patch_file`` tools) is canonicalised in
``goldfive/examples/presentation_agent/`` and loaded here by absolute
file path so the two stay byte-identical.

This module is the **observation-mode** variant: the ADK coordinator
agent does its own routing via instruction text, and harmonograf
observes via :class:`harmonograf_client.HarmonografTelemetryPlugin`.
No ``goldfive.wrap`` — the runtime plan / drift / refine behaviour
lives in the sibling ``presentation_agent_orchestrated`` package.

The module exports:

* ``root_agent`` — the ADK coordinator agent, shared with goldfive's
  example.
* ``app`` — a lazily-built :class:`google.adk.apps.app.App` with
  :class:`HarmonografTelemetryPlugin` installed so ``adk web`` /
  ``adk run`` emit spans automatically. Construction is lazy (PEP 562)
  so importing the module is side-effect free.
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
# Shared tree — loaded from goldfive's example by absolute file path so
# the two modules stay byte-identical without relying on
# ``third_party/goldfive`` being on ``sys.path`` at import time.
# ---------------------------------------------------------------------------


def _load_goldfive_presentation_module() -> Any:
    """Return goldfive's ``examples/presentation_agent/agent.py`` as a module.

    Loaded by absolute file path under a private name so we don't collide
    with any ``examples`` package on ``sys.path`` and don't force the
    caller to manipulate ``sys.path`` themselves.
    """
    here = Path(__file__).resolve()
    candidates = [
        # Normal layout: harmonograf/tests/reference_agents/presentation_agent/agent.py
        # climbs three parents to harmonograf root and then into the submodule.
        here.parents[3]
        / "third_party"
        / "goldfive"
        / "examples"
        / "presentation_agent"
        / "agent.py",
    ]
    # Also honour an explicit override so a dev who clones goldfive
    # outside the submodule can still drive this tree.
    override = os.environ.get("GOLDFIVE_PRESENTATION_AGENT_PATH")
    if override:
        candidates.insert(0, Path(override).expanduser().resolve())
    for candidate in candidates:
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location(
                "_goldfive_presentation", candidate
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

# Re-export the shared tree + tools so callers that historically imported
# these symbols from this module keep working unchanged.
_build_agent_tree = _goldfive_presentation._build_agent_tree
write_webpage = _goldfive_presentation.write_webpage
read_presentation_files = _goldfive_presentation.read_presentation_files
patch_file = _goldfive_presentation.patch_file
write_webpage_tool = _goldfive_presentation.write_webpage_tool
read_presentation_files_tool = _goldfive_presentation.read_presentation_files_tool
patch_file_tool = _goldfive_presentation.patch_file_tool

MODEL_NAME = os.environ.get("USER_MODEL_NAME", "gemini-2.5-flash")

# Reconstruct the tree with the harmonograf-preferred model so runs that
# don't set ``USER_MODEL_NAME`` still default to ``gemini-2.5-flash``.
# Goldfive's own ``root_agent`` is built with ``openai/gpt-4o-mini`` at
# import time; we rebuild rather than re-export to keep the historical
# behaviour.
root_agent = _build_agent_tree(MODEL_NAME)


# ---------------------------------------------------------------------------
# Harmonograf instrumentation — lazy ``app`` export. Observation mode only:
# no ``goldfive.wrap`` here. The orchestrated sibling does the wrapping.
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
    """Construct the ADK ``App`` wrapping ``root_agent`` (observation mode).

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
