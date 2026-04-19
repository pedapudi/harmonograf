"""Standalone observability with Google ADK: spans only, no goldfive.

Installs :class:`HarmonografTelemetryPlugin` on an ADK ``App`` so ADK's
lifecycle callbacks emit harmonograf spans for every invocation, LLM
call, and tool call. No orchestration: no plans, no tasks, no drift.

This is the "I already have an ADK agent and just want it on the Gantt"
path. If you also want plans/tasks/drift, swap to goldfive's wrap()
(see `with_orchestration.py`).

Run:
    export OPENAI_API_KEY=...        # or your model provider's var
    export HARMONOGRAF_SERVER=127.0.0.1:7531
    uv run --extra e2e python examples/standalone_observability/adk_telemetry.py

Prerequisites:
    - harmonograf server running (make server-run)
    - google-adk installed (`uv sync --extra e2e` brings it in)
    - model credentials for whichever model you wire up below

This file never imports the orchestration API — see README.md for the
directory-wide grep check the CI job runs.
"""

from __future__ import annotations

import asyncio
import os

from google.adk.agents.llm_agent import LlmAgent
from google.adk.apps.app import App
from google.adk.runners import InMemoryRunner
from google.genai import types as genai_types

from harmonograf_client import Client, HarmonografTelemetryPlugin


def build_agent() -> LlmAgent:
    return LlmAgent(
        name="standalone_assistant",
        model="gpt-4o-mini",
        instruction=(
            "You are a concise assistant. Answer briefly in one sentence."
        ),
    )


async def main() -> None:
    server = os.environ.get("HARMONOGRAF_SERVER", "127.0.0.1:7531")
    client = Client(
        name="adk-standalone-demo",
        framework="ADK",
        server_addr=server,
        session_title="ADK standalone telemetry demo",
    )
    try:
        app = App(
            name="standalone_assistant",
            root_agent=build_agent(),
            plugins=[HarmonografTelemetryPlugin(client)],
        )
        runner = InMemoryRunner(app=app)
        session = await runner.session_service.create_session(
            app_name=app.name, user_id="demo-user"
        )
        message = genai_types.Content(
            role="user",
            parts=[genai_types.Part(text="What is harmonograf, in one sentence?")],
        )
        async for event in runner.run_async(
            user_id="demo-user",
            session_id=session.id,
            new_message=message,
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        print(part.text, end="", flush=True)
        print()
        print(f"emitted session: {client.session_id}")
    finally:
        client.shutdown(flush_timeout=5.0)


if __name__ == "__main__":
    asyncio.run(main())
