"""HarmonografRunner — convenience factory around an ADK Runner.

Historically this class owned a re-invocation loop that enforced the
planner's plan by wrapping an ADK Runner and overriding ``run_async``.
That responsibility has moved to :class:`HarmonografAgent` (see
``client/harmonograf_client/agent.py``), which slots plan-driven
orchestration into ADK's native ``BaseAgent._run_async_impl`` seam.
Because HarmonografAgent is a regular ADK agent, ``adk web`` / ``adk
run`` / ``adk api_server`` / ``make demo`` pick it up transparently
without any Runner monkey-patching.

What remains here is a thin convenience wrapper that constructs an
:class:`InMemoryRunner` around a HarmonografAgent pre-built on top of
``agent``. Callers who want a one-shot "wrap this agent in enforcement
and hand me back something that has ``run_async``" can reach for
:func:`make_harmonograf_runner`. Everyone else should use
:class:`HarmonografAgent` directly and let ADK construct the Runner.

The class also supports a legacy ``runner=`` composition-mode
constructor used by tests in ``client/tests/test_runner.py``: when
supplied, the instance adopts that inner runner verbatim and delegates
``run_async`` to it. This keeps the prior test surface viable during
the HarmonografAgent pivot.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Optional

from .adk import AdkAdapter, make_adk_plugin
from .agent import HarmonografAgent
from .client import Client

log = logging.getLogger("harmonograf_client.runner")


_DEFAULT_MAX_PLAN_REINVOCATIONS = 3


class HarmonografRunner:
    """Convenience wrapper: build an InMemoryRunner around a
    HarmonografAgent that wraps ``agent``, and attach a
    HarmonografAdkPlugin for telemetry + plan-state tracking.

    Two construction modes:

    - **Agent mode** (default): pass ``agent=``. A HarmonografAgent is
      built automatically, an InMemoryRunner is constructed around it,
      and the harmonograf plugin is attached. ``run_async`` delegates
      to the inner runner.
    - **Composition mode** (tests / legacy): pass ``runner=`` to adopt
      a pre-built or stub inner runner. The wrapper attaches a plugin
      but otherwise delegates ``run_async`` to the supplied runner.
      Plan re-invocation no longer happens here — it lives inside
      HarmonografAgent — so tests that rely on re-invocation should
      drive HarmonografAgent directly.
    """

    def __init__(
        self,
        *,
        agent: Any = None,
        client: Optional[Client] = None,
        harmonograf_client: Optional[Client] = None,
        app_name: Optional[str] = None,
        session_service: Any = None,
        planner: Any = None,
        planner_model: str = "",
        refine_on_events: bool = True,
        enforce_plan: bool = True,
        max_plan_reinvocations: int = _DEFAULT_MAX_PLAN_REINVOCATIONS,
        runner: Any = None,
    ) -> None:
        hg_client = client if client is not None else harmonograf_client

        if runner is None and agent is None:
            raise ValueError(
                "HarmonografRunner: either agent= or runner= is required"
            )

        if runner is None:
            # Build a HarmonografAgent wrapping ``agent`` and spin up an
            # InMemoryRunner around it. This matches what ADK would do
            # at ``adk run`` time if ``agent`` were the project root.
            self._hg_agent: Any = HarmonografAgent(
                name="harmonograf",
                inner_agent=agent,
                harmonograf_client=hg_client,
                planner=planner,
                planner_model=planner_model,
                refine_on_events=refine_on_events,
                enforce_plan=enforce_plan,
                max_plan_reinvocations=max_plan_reinvocations,
            )
            if session_service is not None:
                from google.adk.runners import Runner

                runner = Runner(
                    app_name=app_name or "HarmonografRunner",
                    agent=self._hg_agent,
                    session_service=session_service,
                )
            else:
                from google.adk.runners import InMemoryRunner

                runner = InMemoryRunner(agent=self._hg_agent, app_name=app_name)
        else:
            self._hg_agent = None

        self._runner = runner
        self._client = hg_client

        # Attach the harmonograf telemetry plugin. HarmonografAgent
        # orchestrates via the plugin's _AdkState, so we need both.
        self._plugin: Any = None
        if hg_client is not None:
            self._plugin = make_adk_plugin(
                hg_client,
                planner=planner,
                planner_model=planner_model,
                refine_on_events=refine_on_events,
            )
            self._state = self._plugin._hg_state
            try:
                runner.plugin_manager.plugins.append(self._plugin)
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(
                    f"HarmonografRunner: could not install plugin on runner: {e}"
                ) from e
        else:
            self._state = None

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def runner(self) -> Any:
        return self._runner

    @property
    def agent(self) -> Any:
        return getattr(self._runner, "agent", None)

    @property
    def app_name(self) -> str:
        return getattr(self._runner, "app_name", "") or ""

    @property
    def session_service(self) -> Any:
        return getattr(self._runner, "session_service", None)

    @property
    def client(self) -> Optional[Client]:
        return self._client

    @property
    def plugin(self) -> Any:
        return self._plugin

    def detach(self) -> None:
        """Remove the installed plugin from the inner runner. Used by
        tests that want a clean slate.
        """
        if self._plugin is None:
            return
        try:
            plugins = self._runner.plugin_manager.plugins
            if self._plugin in plugins:
                plugins.remove(self._plugin)
        except Exception:  # noqa: BLE001
            pass

    def as_adapter(self) -> AdkAdapter:
        """Return a legacy :class:`AdkAdapter` handle over the same
        runner+plugin for code that expects the old attach_adk shape.
        """
        return AdkAdapter(
            runner=self._runner, client=self._client, plugin=self._plugin
        )

    # ------------------------------------------------------------------
    # run_async — now a plain delegate to the inner runner.
    # ------------------------------------------------------------------

    async def run_async(self, **kwargs: Any) -> AsyncIterator[Any]:
        async for event in self._runner.run_async(**kwargs):
            yield event


def make_harmonograf_runner(
    *,
    agent: Any,
    client: Client,
    planner: Any = None,
    planner_model: str = "",
    refine_on_events: bool = True,
    max_plan_reinvocations: int = _DEFAULT_MAX_PLAN_REINVOCATIONS,
) -> HarmonografRunner:
    """Factory: wrap ``agent`` in a HarmonografAgent, build an
    InMemoryRunner around it, attach the harmonograf telemetry plugin,
    and return the :class:`HarmonografRunner` handle.

    Thin convenience wrapper over the class constructor.
    """
    return HarmonografRunner(
        agent=agent,
        client=client,
        planner=planner,
        planner_model=planner_model,
        refine_on_events=refine_on_events,
        max_plan_reinvocations=max_plan_reinvocations,
    )


__all__ = ["HarmonografRunner", "make_harmonograf_runner"]
