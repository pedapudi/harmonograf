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

Agent-id canonicalization (harmonograf#125)
------------------------------------------
Goldfive emits two forms of agent identity:

* **Bare**: the ADK ``agent.name`` string, e.g. ``coordinator_agent`` —
  present on ``DelegationObserved.from_agent`` / ``.to_agent``,
  ``AgentInvocationStarted.agent_name``, ``DriftDetected.current_agent_id``,
  and ``Task.assignee_agent_id`` inside plan events. Goldfive has no
  knowledge of who is wrapping it, so it cannot emit anything richer.
* **Compound**: ``<client_id>:<bare_name>``, e.g.
  ``presentation-orchestrated-abc123:coordinator_agent`` — the canonical
  agent-row id the :class:`HarmonografTelemetryPlugin` stamps on every
  ``SpanStart`` as ``span.agent_id``, and the row key the frontend uses for
  Gantt lanes, lifelines, and delegation-arrow endpoints.

Before harmonograf#125 the server did best-effort bare→compound rewrites on
ingest and burst replay (``find_agent_id_by_name``), but a race between a
``DelegationObserved`` landing and the target agent's first span registering
leaked bare names onto live subscribers — producing the "transfer arrows
missing until refresh" symptom (#111, #117).

The fix: canonicalize at the source, before the event leaves the client
process. After this sink runs, every event on the wire carries compound ids
only; the server stores them verbatim and the frontend renders them
verbatim.

Rewrite rules:
  * non-empty + no ``:`` already present (idempotent — already-compound ids
    pass through untouched).
  * applies to every agent-identity field across the event oneof; fields
    not present on a particular payload are simply skipped.
"""

from __future__ import annotations

from typing import Any

from .client import Client


class HarmonografSink:
    """``goldfive.EventSink`` adapter that ships events to a harmonograf server.

    Usage::

        from goldfive import Runner
        from harmonograf_client import Client, HarmonografSink

        client = Client(name="research", server_addr="127.0.0.1:7531")
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

        Before push, every agent-identity field on ``event_pb`` is rewritten
        bare→compound (see module docstring).
        """
        if self._closed:
            return
        self._canonicalize_agent_ids(event_pb)
        self._client.emit_goldfive_event(event_pb)

    async def close(self) -> None:
        """Mark the sink as closed. Does *not* shut down the underlying client.

        The caller owns the :class:`Client` lifecycle; call ``client.shutdown()``
        separately to flush and join the transport. Subsequent ``emit`` calls
        after ``close`` are silently dropped so late events from a tearing-down
        runner do not raise.
        """
        self._closed = True

    # ------------------------------------------------------------------
    # Canonicalization
    # ------------------------------------------------------------------

    def _compound(self, bare: str) -> str:
        """Return the compound id for ``bare``, or ``bare`` itself if it's
        empty or already compound.

        A value is considered "already compound" if it contains a ``:`` —
        this keeps the rewrite idempotent so re-emitting an event (e.g. on
        a sink that wraps another sink) doesn't produce
        ``client:client:agent`` double-prefixing.
        """
        if not bare:
            return bare
        if ":" in bare:
            return bare
        return f"{self._client.agent_id}:{bare}"

    def _rewrite_field(self, msg: Any, field_name: str) -> None:
        """Canonicalize ``msg.<field_name>`` in-place if the field exists
        and is non-empty. Silently no-ops on messages that don't carry the
        field — keeps the dispatch table declarative across event types."""
        current = getattr(msg, field_name, None)
        if not isinstance(current, str) or not current:
            return
        canonical = self._compound(current)
        if canonical != current:
            setattr(msg, field_name, canonical)

    def _canonicalize_agent_ids(self, event_pb: Any) -> None:
        """Rewrite every agent-identity field on ``event_pb`` bare→compound.

        Dispatches on the oneof payload case. Fields covered:

        * ``DelegationObserved.from_agent`` / ``.to_agent``
        * ``AgentInvocationStarted.agent_name``
        * ``AgentInvocationCompleted.agent_name``
        * ``DriftDetected.current_agent_id``
        * ``PlanSubmitted.plan.tasks[*].assignee_agent_id``
        * ``PlanRevised.plan.tasks[*].assignee_agent_id``

        Unknown / unset oneof cases are a no-op — the wire is unchanged.
        """
        which = event_pb.WhichOneof("payload") if hasattr(event_pb, "WhichOneof") else None
        if not which:
            return

        if which == "delegation_observed":
            d = event_pb.delegation_observed
            self._rewrite_field(d, "from_agent")
            self._rewrite_field(d, "to_agent")
        elif which == "agent_invocation_started":
            self._rewrite_field(event_pb.agent_invocation_started, "agent_name")
        elif which == "agent_invocation_completed":
            self._rewrite_field(event_pb.agent_invocation_completed, "agent_name")
        elif which == "drift_detected":
            self._rewrite_field(event_pb.drift_detected, "current_agent_id")
        elif which == "plan_submitted":
            self._canonicalize_plan(event_pb.plan_submitted.plan)
        elif which == "plan_revised":
            self._canonicalize_plan(event_pb.plan_revised.plan)
        # RunStarted, GoalDerived, Task{Started,Progress,Completed,Failed,
        # Blocked,Cancelled}, RunCompleted, RunAborted, Conversation*,
        # Approval* — none carry an agent-identity string field on the proto
        # as of goldfive v1 (events.proto at 7b8ab49). They pass through
        # untouched.

    def _canonicalize_plan(self, plan: Any) -> None:
        """Rewrite every ``task.assignee_agent_id`` on ``plan`` bare→compound."""
        if plan is None:
            return
        for task in plan.tasks:
            self._rewrite_field(task, "assignee_agent_id")
