"""Synthetic harmonograf client for the golden-path Playwright smoke.

Emits one session worth of spans (INVOCATION parent with LLM_CALL and
TOOL_CALL children), then flushes and exits. Kept intentionally tiny —
the only consumer is tests/integration/tests/golden-path.spec.ts, which
spawns this as a subprocess via harness/processes.ts.
"""

from __future__ import annotations

import argparse
import sys
import time

from harmonograf_client.client import Client


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-addr", required=True, help="host:port of the gRPC server")
    parser.add_argument("--session-title", default="smoke-golden-path")
    parser.add_argument("--identity-root", default=None)
    args = parser.parse_args()

    client = Client(
        name="golden-path-synth",
        framework="CUSTOM",
        session_title=args.session_title,
        server_addr=args.server_addr,
        identity_root=args.identity_root,
    )
    try:
        # Space spans ~150ms apart so the Gantt renders them as visually
        # distinct rectangles instead of stacking three sub-millisecond
        # spans on top of each other (which makes proxy/canvas hit-testing
        # ambiguous in the Playwright smoke).
        inv = client.emit_span_start(kind="INVOCATION", name="golden-path-invocation")
        time.sleep(0.15)
        llm = client.emit_span_start(
            kind="LLM_CALL",
            name="llm gpt-4o",
            parent_span_id=inv,
            payload=b'{"prompt": "hello harmonograf"}',
            payload_mime="application/json",
            payload_role="input",
        )
        time.sleep(0.15)
        client.emit_span_end(
            llm,
            status="COMPLETED",
            payload=b'{"response": "hi from synth"}',
            payload_mime="application/json",
            payload_role="output",
        )
        time.sleep(0.15)
        tool = client.emit_span_start(
            kind="TOOL_CALL",
            name="tool search",
            parent_span_id=inv,
            payload=b'{"query": "harmonograf"}',
            payload_mime="application/json",
            payload_role="input",
        )
        time.sleep(0.15)
        client.emit_span_end(tool, status="COMPLETED")
        time.sleep(0.05)
        client.emit_span_end(inv, status="COMPLETED")
        # Give the transport thread a moment to flush before we tear down.
        time.sleep(0.3)
    finally:
        client.shutdown(flush_timeout=5.0)
    sys.stderr.write(f"synth-session-id: {client.session_id}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
