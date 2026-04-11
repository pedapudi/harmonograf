"""gRPC bidirectional transport — SKELETON.

This file is a placeholder. The real implementation is wired up after
task #2 (proto codegen) lands so we can import ``harmonograf.v1`` and
its ``HarmonografStub``.

Planned shape:

* ``Transport`` owns a background thread running an asyncio loop, a
  ``Connect`` bidi stream, an exponential-backoff reconnect, a
  ``resume_token`` kept in sync with the last server-ack'd envelope,
  and a periodic heartbeat timer.
* ``Transport.enqueue_envelope(env)`` is a non-blocking hop into the
  EventRingBuffer. The thread drains the buffer and writes pb messages
  onto the stream.
* Control events arriving on the downstream half are dispatched to a
  caller-registered callback (``on_control``); the callback typically
  lives in the ADK adapter.
"""

from __future__ import annotations


class Transport:
    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError(
            "transport.py is blocked on task #2 (proto codegen); "
            "the pure-python pieces (buffer, identity, heartbeat) "
            "are ready."
        )
