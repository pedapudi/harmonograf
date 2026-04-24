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

LLM-call translation (harmonograf Option X — unify goldfive LLM observability)
-----------------------------------------------------------------------------
Goldfive and ADK emit LLM-call telemetry through two different channels. ADK
agent LLM calls land as proper ``SpanStart`` / ``SpanEnd`` proto frames on
the span transport; goldfive-internal LLM calls (``refine_steer``,
``judge_reasoning``, ``goal_derive``, ``plan_generate``, ...) land as
``GoldfiveLLMCallStart`` / ``GoldfiveLLMCallEnd`` / ``ReasoningJudgeInvoked``
goldfive-event variants that require per-event-kind synthesizers in the
frontend. Every new goldfive-internal LLM call therefore needed a new
frontend handler — and when goldfive#244 shipped the start/end pair, nothing
rendered until the frontend caught up.

This sink closes the asymmetry at the client seam: when a goldfive Event
carries one of those three LLM-call oneof variants, it is translated to the
equivalent ``SpanStart`` / ``SpanEnd`` frame and pushed via the existing
span transport — so the server stores the result in the ``spans`` table
like any other span and the frontend renders it uniformly. The original
``goldfive_event`` envelope is NOT forwarded for these variants; the span
pair is the authoritative wire surface.

Translation rules (all three event kinds stamp ``agent_id = <client>:goldfive``,
``kind = LLM_CALL``, ``status = COMPLETED`` on end):

  * ``GoldfiveLLMCallStart`` → ``SpanStart`` with span id + name from the
    event, ``start_time_ns`` as the span start time, attributes carrying
    ``goldfive.task_id`` / ``goldfive.model`` / ``goldfive.run_id``.
  * ``GoldfiveLLMCallEnd`` → ``SpanEnd`` with the matching span id,
    ``end_time_ns`` as the end time, status ``COMPLETED`` / ``FAILED``
    derived from the event's ``status`` string, and ``error`` plumbed
    when ``status == "failed"``.
  * ``ReasoningJudgeInvoked`` → a ``SpanStart`` + ``SpanEnd`` pair emitted
    back-to-back (the event is a terminal-only record; the start is
    synthesized at ``emitted_at - elapsed_ms`` so the span has visible
    width on the Gantt). Verdict fields land as ``judge.*`` attributes so
    the existing :class:`JudgeInvocationDetail` click-through panel
    continues to read them verbatim (harmonograf#147).

Ordering: goldfive's ``_llm_span`` context manager emits Start then End
via ``EventSink.emit`` — the sink translates each event in isolation and
pushes in the order it received, so SpanStart always precedes SpanEnd for
the same span id. For the judge pair the order is fixed internally
(start then end, stamped on the same wire push).

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

from google.protobuf import timestamp_pb2

from .client import Client

# Events whose translation-to-span semantics the sink owns. Anything else
# passes through ``emit_goldfive_event`` as before.
_LLM_SPAN_EVENT_KINDS = frozenset(
    {
        "goldfive_llm_call_start",
        "goldfive_llm_call_end",
        "reasoning_judge_invoked",
    }
)

# Truncation ceilings match the goldfive proto's own: ``reasoning_input``
# is truncated by the goldfive emitter to 4096 chars and ``raw_response``
# to 2048 (see ReasoningJudgeInvoked field comments). The sink echoes the
# bytes verbatim — no further truncation — so the ``JudgeInvocationDetail``
# panel sees exactly what the judge returned.


def _ts_from_nanos(ns: int) -> timestamp_pb2.Timestamp | None:
    """Build a Timestamp from a unix-epoch-ns int. Returns ``None`` on
    zero / negative inputs so the caller can fall back to wall-clock
    stamping at the client layer (``Client.emit_span_start`` does that
    when ``start_time`` is ``None``)."""
    if ns is None or ns <= 0:
        return None
    ts = timestamp_pb2.Timestamp()
    ts.FromNanoseconds(int(ns))
    return ts


def _ts_to_nanos(ts: timestamp_pb2.Timestamp) -> int | None:
    """Convert a ``Timestamp`` to unix-epoch nanoseconds, or ``None``
    when the Timestamp is the zero value (unset field on the envelope)."""
    if ts is None:
        return None
    if ts.seconds == 0 and ts.nanos == 0:
        return None
    return ts.seconds * 1_000_000_000 + ts.nanos


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
        which = (
            event_pb.WhichOneof("payload")
            if hasattr(event_pb, "WhichOneof")
            else None
        )
        if which in _LLM_SPAN_EVENT_KINDS:
            # Translate to span transport — do NOT also forward the
            # goldfive_event envelope (the span pair is authoritative).
            self._translate_and_emit_span(event_pb, which)
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

    # ------------------------------------------------------------------
    # LLM-call translation (Option X)
    # ------------------------------------------------------------------

    def _goldfive_agent_id(self) -> str:
        """Compound agent id for goldfive-internal spans.

        Equivalent to ``self._compound("goldfive")`` but stable across
        future goldfive changes — if goldfive starts stamping the
        compound form itself on any of these events, ``_compound`` is
        idempotent so double-emits still land a clean id.
        """
        return self._compound("goldfive")

    def _translate_and_emit_span(self, event_pb: Any, which: str) -> None:
        """Translate one of the three LLM-call oneof variants to a span
        pair (or half, for the start/end cases) and push via the client's
        span transport. Never raises: on an unexpected shape (e.g. a
        future proto bump adds fields) the helper falls back to emitting
        what it can and swallows the rest — the span is non-critical
        observability and must not break orchestration."""
        if which == "goldfive_llm_call_start":
            self._emit_llm_call_start(event_pb)
        elif which == "goldfive_llm_call_end":
            self._emit_llm_call_end(event_pb)
        elif which == "reasoning_judge_invoked":
            self._emit_reasoning_judge_span(event_pb)

    def _emit_llm_call_start(self, event_pb: Any) -> None:
        """``GoldfiveLLMCallStart`` → ``SpanStart``."""
        start = event_pb.goldfive_llm_call_start
        span_id = start.span_id or self._derive_span_id(event_pb)
        self._client.emit_span_start(
            kind="LLM_CALL",
            name=start.name or "goldfive_llm_call",
            span_id=span_id,
            start_time=_ts_from_nanos(start.start_time_ns),
            agent_id=self._goldfive_agent_id(),
            session_id=event_pb.session_id or None,
            attributes=self._llm_call_attributes(event_pb, start),
        )

    def _emit_llm_call_end(self, event_pb: Any) -> None:
        """``GoldfiveLLMCallEnd`` → ``SpanEnd``."""
        end = event_pb.goldfive_llm_call_end
        span_id = end.span_id or self._derive_span_id(event_pb)
        wire_status = (end.status or "").lower()
        status = "FAILED" if wire_status == "failed" else "COMPLETED"
        error = (
            {"type": "goldfive_llm_call", "message": end.error}
            if wire_status == "failed" and end.error
            else None
        )
        attributes: dict[str, Any] = {}
        if end.name:
            # Echo the call name on end so out-of-order End-before-Start
            # consumers can still label the span (mirrors the proto field
            # comment).
            attributes["goldfive.call_name"] = end.name
        if wire_status:
            attributes["goldfive.status"] = wire_status
        self._client.emit_span_end(
            span_id,
            status=status,
            end_time=_ts_from_nanos(end.end_time_ns),
            error=error,
            attributes=attributes or None,
        )

    def _emit_reasoning_judge_span(self, event_pb: Any) -> None:
        """``ReasoningJudgeInvoked`` → ``SpanStart`` + ``SpanEnd``.

        The judge event is terminal-only (the LLM-as-judge classifier
        emits a single Event after the call completes, not a start/end
        pair). Synthesize a start at ``emitted_at - elapsed_ms`` so the
        span has visible width on the Gantt — matches the pre-Option-X
        frontend synthesizer (harmonograf#147) byte for byte.

        Attributes are stamped with the ``judge.*`` namespace that
        :class:`JudgeInvocationDetail` reads back (see
        ``frontend/src/lib/interventionDetail.ts``). Keys preserved:
        ``judge.kind``, ``judge.event_id``, ``judge.verdict``,
        ``judge.on_task``, ``judge.severity``, ``judge.reason``,
        ``judge.reasoning_input``, ``judge.reasoning`` (back-compat
        alias), ``judge.raw_response``, ``judge.elapsed_ms``,
        ``judge.model``, ``judge.subject_agent_id``,
        ``judge.target_agent_id`` (alias), ``judge.target_task_id``.
        """
        ju = event_pb.reasoning_judge_invoked
        on_task = bool(ju.on_task)
        severity = ju.severity or ""
        reason = ju.reason or ""
        verdict = "on_task" if on_task else (severity or ("off_task" if not on_task else ""))
        display = (
            f"judge: {verdict}" + (f" ({severity})" if verdict != "on_task" and severity else "")
            if verdict
            else "judge: on_task"
        )
        elapsed_ms = int(ju.elapsed_ms or 0)
        # emitted_at is a google.protobuf.Timestamp — fall back to "now"
        # when the envelope didn't populate it (shouldn't happen on a
        # well-formed runner, but the span must still be renderable).
        emitted_ts: timestamp_pb2.Timestamp = event_pb.emitted_at
        end_ns = _ts_to_nanos(emitted_ts)
        if end_ns is None:
            # No emitted_at; let the client stamp wall-clock on both.
            start_time: Any = None
            end_time: Any = None
        else:
            start_ns = max(0, end_ns - max(0, elapsed_ms) * 1_000_000)
            start_time = _ts_from_nanos(start_ns)
            end_time = _ts_from_nanos(end_ns)

        subject = ju.subject_agent_id or ""
        task_id = ju.task_id or ""
        attrs: dict[str, Any] = {
            # Discriminator — read by click-through routing. Backed by
            # an explicit attribute (not the span name) so label renames
            # don't accidentally break routing.
            "judge.kind": "judge",
            "judge.event_id": event_pb.event_id or "",
            "judge.verdict": verdict,
            "judge.on_task": on_task,
            "judge.severity": severity,
            "judge.reason": reason,
            "judge.reasoning_input": ju.reasoning_input or "",
            # Back-compat alias: pre-rework detail resolvers and tests
            # still read ``judge.reasoning``. Carry both until nothing
            # reads the alias.
            "judge.reasoning": reason or (ju.reasoning_input or ""),
            "judge.raw_response": ju.raw_response or "",
            "judge.elapsed_ms": str(elapsed_ms),
            "judge.model": ju.model or "",
            "judge.subject_agent_id": subject,
            # Alias used by the detail resolver's fallback read order.
            "judge.target_agent_id": subject,
            "judge.target_task_id": task_id,
            # Run id is handy for debugging cross-session judge lookups.
            "judge.run_id": event_pb.run_id or "",
        }
        session_id = event_pb.session_id or None
        agent_id = self._goldfive_agent_id()
        span_id = self._derive_span_id(event_pb)
        self._client.emit_span_start(
            kind="LLM_CALL",
            name=display,
            span_id=span_id,
            start_time=start_time,
            agent_id=agent_id,
            session_id=session_id,
            attributes=attrs,
        )
        self._client.emit_span_end(
            span_id,
            status="COMPLETED",
            end_time=end_time,
        )

    def _llm_call_attributes(self, event_pb: Any, start: Any) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        if start.model:
            attrs["goldfive.model"] = start.model
        if start.task_id:
            attrs["goldfive.task_id"] = start.task_id
        if event_pb.run_id:
            attrs["goldfive.run_id"] = event_pb.run_id
        if start.name:
            attrs["goldfive.call_name"] = start.name
        return attrs

    def _derive_span_id(self, event_pb: Any) -> str:
        """Fallback span id when the event doesn't carry its own.

        Used only for ``ReasoningJudgeInvoked`` (no ``span_id`` field on
        the proto) and as a defense for malformed start/end events. The
        derivation keys on ``event_id + payload case`` so a Start + End
        pair minted from the same event_id would collide — acceptable
        because that combination is nonsensical (events with a span_id
        use it; the derived path is the exception, not the rule).
        """
        which = (
            event_pb.WhichOneof("payload") if hasattr(event_pb, "WhichOneof") else ""
        )
        return f"goldfive-{which}-{event_pb.event_id}"
