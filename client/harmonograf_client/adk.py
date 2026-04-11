"""ADK adapter — SKELETON.

Public entry point will be::

    from harmonograf_client import Client, attach_adk
    from google.adk.runners import Runner

    client = Client(name="research-agent", session_id="sess_demo")
    runner = Runner(...)
    attach_adk(runner, client)

Implementation lands under task #9 (blocked on #8, which is blocked on
#2). See docs/design/01 §3 for the ADK → Span mapping.
"""

from __future__ import annotations


def attach_adk(runner, client) -> None:
    raise NotImplementedError("attach_adk is blocked on tasks #8/#9.")
