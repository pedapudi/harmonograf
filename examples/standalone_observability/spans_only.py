"""Standalone observability: emit spans to a harmonograf server.

No orchestration. No plans. No tasks. No drift. Just spans over a
non-blocking client — the "minimum viable" way to get agent activity
onto the harmonograf Gantt.

Run:
    # In one terminal:
    make server-run

    # In another terminal:
    uv run python examples/standalone_observability/spans_only.py

    # Open the frontend to see the spans render:
    make frontend-dev  # then http://127.0.0.1:5173/

Environment:
    HARMONOGRAF_SERVER  (default 127.0.0.1:7531)

This example deliberately uses ONLY the harmonograf_client public
surface. The `standalone-test` CI job greps this file to verify it
has no references at all to the orchestration extra.
"""

from __future__ import annotations

import json
import os
import time

from harmonograf_client import Client, SpanKind, SpanStatus


def emit_llm_call(client: Client, parent_id: str, prompt: str) -> str:
    """Emit a short LLM_CALL span with input + output payloads."""
    span = client.emit_span_start(
        kind=SpanKind.LLM_CALL,
        name="gpt-4o-mini",
        parent_span_id=parent_id,
        attributes={"model": "gpt-4o-mini", "temperature": 0.2},
        payload=json.dumps({"prompt": prompt}).encode(),
        payload_role="input",
    )
    time.sleep(0.2)
    client.emit_span_end(
        span,
        status=SpanStatus.COMPLETED,
        payload=json.dumps(
            {"text": f"(synthetic reply to: {prompt[:40]!r})"}
        ).encode(),
        payload_role="output",
        attributes={"tokens_in": 42, "tokens_out": 17},
    )
    return span


def emit_tool_call(client: Client, parent_id: str, tool: str, args: dict) -> str:
    """Emit a TOOL_CALL span with args + result payloads."""
    span = client.emit_span_start(
        kind=SpanKind.TOOL_CALL,
        name=tool,
        parent_span_id=parent_id,
        attributes={"tool": tool},
        payload=json.dumps(args).encode(),
        payload_role="input",
    )
    time.sleep(0.15)
    client.emit_span_end(
        span,
        status=SpanStatus.COMPLETED,
        payload=json.dumps({"ok": True, "tool": tool}).encode(),
        payload_role="output",
    )
    return span


def main() -> None:
    server = os.environ.get("HARMONOGRAF_SERVER", "127.0.0.1:7531")
    client = Client(
        name="standalone-demo",
        framework="CUSTOM",
        server_addr=server,
        session_title="Standalone observability demo",
    )
    try:
        invocation = client.emit_span_start(
            kind=SpanKind.INVOCATION,
            name="answer_question",
            attributes={"question": "What is harmonograf?"},
        )

        # Emit the user's message as its own span so the frontend's
        # messages panel has something to show.
        user_msg = client.emit_span_start(
            kind=SpanKind.USER_MESSAGE,
            name="user",
            parent_span_id=invocation,
            payload=json.dumps({"text": "What is harmonograf?"}).encode(),
            payload_role="input",
        )
        client.emit_span_end(user_msg, status=SpanStatus.COMPLETED)

        # Synthetic LLM reasoning step.
        emit_llm_call(
            client,
            parent_id=invocation,
            prompt="Explain harmonograf briefly.",
        )

        # Synthetic tool call.
        emit_tool_call(
            client,
            parent_id=invocation,
            tool="web_search",
            args={"query": "harmonograf github"},
        )

        # Final synthesized answer.
        agent_msg = client.emit_span_start(
            kind=SpanKind.AGENT_MESSAGE,
            name="agent",
            parent_span_id=invocation,
            payload=json.dumps(
                {
                    "text": (
                        "Harmonograf is an observability console for "
                        "multi-agent workflows."
                    )
                }
            ).encode(),
            payload_role="output",
        )
        client.emit_span_end(agent_msg, status=SpanStatus.COMPLETED)

        client.emit_span_end(invocation, status=SpanStatus.COMPLETED)
        print(f"emitted session: {client.session_id}")
    finally:
        client.shutdown(flush_timeout=5.0)


if __name__ == "__main__":
    main()
