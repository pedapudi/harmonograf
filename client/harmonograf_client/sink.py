"""HarmonografSink — goldfive.EventSink that forwards events to harmonograf.

Each goldfive :class:`goldfive.v1.Event` received via ``emit`` is pushed onto
the client's existing buffer/transport pipeline, where the send loop wraps it
in a ``TelemetryUp(goldfive_event=...)`` frame. The sink reuses the span
transport's backpressure, reconnect, and heartbeat semantics — nothing new on
the wire except the ``goldfive_event`` variant introduced in the Phase A proto
migration (issue #2).

Module identity: harmonograf's generated ``telemetry_pb2`` imports
``goldfive.v1.events_pb2`` via the same module grafted onto ``goldfive.pb``,
so ``TelemetryUp.goldfive_event`` shares its class with whatever goldfive's
runner produces. No serialize/parse round-trip is required.
"""

from __future__ import annotations

from typing import Any

from .client import Client


class HarmonografSink:
    """``goldfive.EventSink`` adapter that ships events to a harmonograf server.

    Usage::

        from goldfive import Runner
        from harmonograf_client import Client, HarmonografSink

        client = Client(name="research", server_addr="localhost:50431")
        sink = HarmonografSink(client)
        runner = Runner(..., sinks=[sink])
        await runner.run(user_request)
        await sink.close()
        client.shutdown()
    """

    def __init__(self, client: Client) -> None:
        self._client = client
        self._closed = False

    @property
    def client(self) -> Client:
        return self._client

    async def emit(self, event_pb: Any) -> None:
        """Push ``event_pb`` onto the client's transport buffer.

        ``emit`` is declared async to satisfy ``goldfive.EventSink`` but does
        no IO itself — the push is a constant-time, thread-safe buffer append
        that the transport's send loop drains.
        """
        if self._closed:
            return
        self._client.emit_goldfive_event(event_pb)

    async def close(self) -> None:
        """Mark the sink as closed. Does *not* shut down the underlying client.

        The caller owns the :class:`Client` lifecycle; call ``client.shutdown()``
        separately to flush and join the transport. Subsequent ``emit`` calls
        after ``close`` are silently dropped so late events from a tearing-down
        runner do not raise.
        """
        self._closed = True
