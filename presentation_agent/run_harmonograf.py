"""CLI entry point: run the presentation_agent sample under Harmonograf.

Usage::

    uv run --extra e2e python -m presentation_agent.run_harmonograf \
        --topic "Python programming" --server 127.0.0.1:7531

Attaches a real :class:`harmonograf_client.Client` to the running
coordinator → research → web_developer pipeline via :func:`attach_adk`
and drives one invocation through ``InMemoryRunner.run_async``. Prints
the assigned Harmonograf session id at start so an operator can open
the UI and watch the run materialize.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time

from harmonograf_client import Client, attach_adk


DEFAULT_SERVER = os.environ.get("HARMONOGRAF_SERVER", "127.0.0.1:7531")


async def _main_async(topic: str, server_addr: str) -> int:
    from google.adk.runners import InMemoryRunner
    from google.genai import types as genai_types

    from presentation_agent.agent import root_agent

    client = Client(
        name="presentation",
        server_addr=server_addr,
        framework="ADK",
        capabilities=["HUMAN_IN_LOOP", "STEERING"],
    )

    runner = InMemoryRunner(agent=root_agent, app_name="presentation_agent")
    handle = attach_adk(runner, client)

    # Wait up to a few seconds for the server to assign a session id so we
    # can print a stable link before the invocation starts emitting spans.
    deadline = time.monotonic() + 5.0
    while not client.session_id and time.monotonic() < deadline:
        await asyncio.sleep(0.05)

    session_id = client.session_id or "(pending)"
    print(f"[harmonograf] session_id={session_id}")
    print(f"[harmonograf] server={server_addr}  topic={topic!r}")

    try:
        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id="presentation_user"
        )
        async for event in runner.run_async(
            user_id="presentation_user",
            session_id=session.id,
            new_message=genai_types.Content(
                role="user",
                parts=[genai_types.Part(text=f"Create a presentation about: {topic}")],
            ),
        ):
            # Surface a compact per-event line so CLI users see progress.
            author = getattr(event, "author", "")
            print(f"[event] from={author}")
    finally:
        handle.detach()
        client.shutdown(flush_timeout=5.0)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="presentation_agent.run_harmonograf",
        description="Run the presentation_agent sample wired to a Harmonograf server.",
    )
    parser.add_argument(
        "--topic",
        default="Python programming",
        help="Topic for the generated presentation.",
    )
    parser.add_argument(
        "--server",
        default=DEFAULT_SERVER,
        help="Harmonograf server address host:port (default from $HARMONOGRAF_SERVER or 127.0.0.1:7531).",
    )
    args = parser.parse_args()
    return asyncio.run(_main_async(args.topic, args.server))


if __name__ == "__main__":
    raise SystemExit(main())
