"""Standalone steering scenario: make the zicato console render goldfive
STEERING ARROWS (and 🧠 reasoning glyphs).

WHAT THIS PRODUCES
------------------
One harmonograf session whose data satisfies the ``buildSteers`` contract in
``frontend/src/components/zicato/adapter.ts``. A ``ZSteer`` (→ one arrow on the
gantt, from the goldfive lane to a steered work agent) is produced when BOTH
the ``from`` (the goldfive lane) and the ``to`` (a registered NON-goldfive
agent) exist as lanes, AND a drift→revision chain ties them together.

To make all of that true in a single, deterministic, LLM-free run we emit:

  1. A multi-agent plan (PlanSubmitted, rev0) assigning tasks to ``coordinator``,
     ``researcher``, ``writer``, ``reviewer`` — so those work-agent lanes exist.
  2. Real INVOCATION / LLM_CALL / TOOL_CALL spans on each work agent (so the
     lanes carry bars), with 1–2 LLM spans stamped ``llm.reasoning`` +
     ``has_reasoning`` so ``lib/thinking.hasThinking`` flags them → 🧠 glyph.
  3. Goldfive-internal ``refine_steer`` LLM-call spans on the ``<client>:goldfive``
     lane (GoldfiveLLMCallStart/End, proto fields 32/33). These give the goldfive
     LANE real bars (the lane must exist for an arrow to originate from it).
  4. Three DriftDetected events (INFO, WARNING, CRITICAL) whose ``current_agent_id``
     is a real work agent, each carrying a unique ``id``.
  5. Three PlanRevised events (rev1/2/3) whose ``trigger_event_id`` == the matching
     drift's ``id`` (so the frontend's ``revisionByDrift`` map links them), and whose
     ``target_agent_id`` is the steered work agent. The frontend synthesizes a
     ``refine:<n>`` span on the goldfive lane from each PlanRevised, stamped with
     ``refine.index`` (= revision number) and ``refine.target_agent_id`` — the
     PRIMARY path of ``buildSteers``. The drift→revision chain is also the FALLBACK
     path (drift.current_agent_id is the steer target), so steers render either way.

WHY THE SINK
------------
We drive everything through :class:`harmonograf_client.HarmonografSink`, not raw
``Client.emit_goldfive_event``, for two reasons the server / frontend rely on:

  * ``GoldfiveLLMCallStart`` / ``GoldfiveLLMCallEnd`` are NOT ingested by the
    server as goldfive events — the SINK translates them into real
    ``SpanStart`` / ``SpanEnd`` on the ``<client>:goldfive`` row. Emitting them
    raw would persist an inert event with no span (no goldfive lane bars).
  * The sink canonicalizes every agent-identity field bare→compound
    (``DriftDetected.current_agent_id`` and ``Plan.tasks[*].assignee_agent_id``),
    so the drift's agent, the plan assignee, and our explicitly-compound work-agent
    spans all land on the SAME lane id — which is what lets the steer's ``to``
    resolve to a registered, non-goldfive lane.

The one field the sink does NOT canonicalize is ``PlanRevised.target_agent_id``;
we therefore set it to the ALREADY-compound ``<client>:<bare>`` form ourselves
(``_compound`` is idempotent on ids containing ``:``), so the synthesized
``refine.target_agent_id`` matches the work-agent lane for the PRIMARY path.

Run:
    export HARMONOGRAF_SERVER=127.0.0.1:7531
    uv run --extra orchestration python \\
        examples/standalone_observability/steering_scenario.py

Environment:
    HARMONOGRAF_SERVER  (default 127.0.0.1:7531)
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

# Importing goldfive grafts ``goldfive.v1`` onto the package namespace so the
# generated proto stubs resolve. We pull ONLY message classes from it — no
# Runner, no planner, no orchestration. ``goldfive.pb`` triggers the graft.
import goldfive  # noqa: F401  (import for namespace side effect)
import goldfive.pb  # noqa: F401  (extends goldfive.__path__ to the pb subtree)
from goldfive.v1 import events_pb2, types_pb2
from google.protobuf import timestamp_pb2

from harmonograf_client import Client, HarmonografSink, SpanKind, SpanStatus

PLAN_ID = "steering-demo"

# Bare work-agent names. Tasks are assigned to these; spans land on the
# compound ``<client>:<bare>`` form so the lanes line up with the canonicalized
# plan assignees + drift agents.
COORDINATOR = "coordinator"
RESEARCHER = "researcher"
WRITER = "writer"
REVIEWER = "reviewer"


def _now_ns(offset_s: float = 0.0) -> int:
    return int((time.time() + offset_s) * 1_000_000_000)


def _ts(offset_s: float = 0.0) -> timestamp_pb2.Timestamp:
    t = timestamp_pb2.Timestamp()
    t.FromNanoseconds(_now_ns(offset_s))
    return t


def _task(
    task_id: str,
    title: str,
    *,
    assignee: str,
    status: int = types_pb2.TASK_STATUS_PENDING,
    description: str = "",
) -> types_pb2.Task:
    return types_pb2.Task(
        id=task_id,
        title=title,
        description=description,
        assignee_agent_id=assignee,
        status=status,
    )


def _edge(from_id: str, to_id: str) -> types_pb2.TaskEdge:
    return types_pb2.TaskEdge(from_task_id=from_id, to_task_id=to_id)


def _plan(
    *,
    run_id: str,
    revision_index: int,
    tasks: list[types_pb2.Task],
    edges: list[types_pb2.TaskEdge],
    summary: str,
    revision_reason: str = "",
    revision_kind: int = types_pb2.DRIFT_KIND_UNSPECIFIED,
    revision_severity: int = types_pb2.DRIFT_SEVERITY_UNSPECIFIED,
    trigger_event_id: str = "",
    created_offset_s: float = 0.0,
) -> types_pb2.Plan:
    return types_pb2.Plan(
        id=PLAN_ID,
        run_id=run_id,
        goal_ids=["g-research-summary"],
        summary=summary,
        tasks=tasks,
        edges=edges,
        revision_reason=revision_reason,
        revision_kind=revision_kind,
        revision_severity=revision_severity,
        revision_index=revision_index,
        created_at=_ts(created_offset_s),
        revision_trigger_event_id=trigger_event_id,
    )


def _event(
    *,
    run_id: str,
    session_id: str,
    sequence: int,
    payload_field: str,
    payload_msg,
    emitted_offset_s: float = 0.0,
) -> events_pb2.Event:
    evt = events_pb2.Event(
        event_id=str(uuid.uuid4()),
        run_id=run_id,
        sequence=sequence,
        session_id=session_id,
    )
    evt.emitted_at.FromNanoseconds(_now_ns(emitted_offset_s))
    getattr(evt, payload_field).CopyFrom(payload_msg)
    return evt


def _wait_for_session(client: Client, timeout_s: float = 10.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        sid = client.session_id
        if sid:
            return sid
        time.sleep(0.05)
    return client.session_id


def main() -> None:
    server = os.environ.get("HARMONOGRAF_SERVER", "127.0.0.1:7531")
    run_id = f"steering-run-{uuid.uuid4().hex[:12]}"

    client = Client(
        name="steering-scenario",
        framework="CUSTOM",
        server_addr=server,
        session_title="Steering scenario (drift → refine → arrows)",
    )
    sink = HarmonografSink(client)

    # The compound prefix every work-agent lane / canonicalized id uses.
    root = client.agent_id

    def compound(bare: str) -> str:
        return f"{root}:{bare}"

    try:
        # ── 1. Root invocation + work-agent spans (lanes + 🧠) ──────────────
        # A top-level INVOCATION so the Hello lands and the session row exists
        # before the plan / drift events FK to it.
        invocation = client.emit_span_start(
            kind=SpanKind.INVOCATION,
            name="steering_scenario",
            attributes={"scenario": "steering"},
        )

        session_id = _wait_for_session(client)

        def work_span_pair(
            bare_agent: str,
            *,
            kind: SpanKind,
            name: str,
            offset_s: float,
            duration_s: float,
            reasoning: str | None = None,
            attributes: dict | None = None,
        ) -> None:
            """Emit a start/end pair on a work agent's compound lane."""
            attrs = dict(attributes or {})
            if reasoning is not None:
                # The two carriers lib/thinking.hasThinking checks: the
                # boolean flag and the llm.reasoning text attribute. Setting
                # both drives the 🧠 glyph + the reasoning disclosure.
                attrs["llm.reasoning"] = reasoning
                attrs["has_reasoning"] = True
            span_id = client.emit_span_start(
                kind=kind,
                name=name,
                parent_span_id=invocation,
                agent_id=compound(bare_agent),
                attributes=attrs,
                start_time=_ts(offset_s),
            )
            client.emit_span_end(
                span_id, status=SpanStatus.COMPLETED, end_time=_ts(offset_s + duration_s)
            )

        # coordinator plans the work (carries reasoning → 🧠).
        work_span_pair(
            COORDINATOR,
            kind=SpanKind.LLM_CALL,
            name="coordinator.plan",
            offset_s=0.1,
            duration_s=0.4,
            reasoning=(
                "The user wants a sourced research brief. I'll split this into "
                "research, drafting, and review, and assign a specialist to each. "
                "Researcher first so the writer has material to work from."
            ),
            attributes={"model": "claude-opus-4", "temperature": 0.2},
        )
        # researcher gathers material (carries reasoning → 🧠).
        work_span_pair(
            RESEARCHER,
            kind=SpanKind.LLM_CALL,
            name="researcher.gather",
            offset_s=0.6,
            duration_s=0.6,
            reasoning=(
                "I should prioritize primary sources over secondary commentary. "
                "Starting with the canonical references, then filling gaps with "
                "recent surveys. I'll flag anything I can't corroborate."
            ),
            attributes={"model": "claude-opus-4", "temperature": 0.3},
        )
        work_span_pair(
            RESEARCHER,
            kind=SpanKind.TOOL_CALL,
            name="web_search",
            offset_s=1.3,
            duration_s=0.3,
            attributes={"tool": "web_search", "query": "primary sources"},
        )
        # writer drafts.
        work_span_pair(
            WRITER,
            kind=SpanKind.LLM_CALL,
            name="writer.draft",
            offset_s=1.7,
            duration_s=0.7,
            attributes={"model": "claude-opus-4", "temperature": 0.5},
        )
        # reviewer reviews.
        work_span_pair(
            REVIEWER,
            kind=SpanKind.LLM_CALL,
            name="reviewer.review",
            offset_s=2.5,
            duration_s=0.5,
            attributes={"model": "claude-opus-4", "temperature": 0.1},
        )

        # ── 2. Initial plan (rev0) — creates the work-agent lanes ───────────
        # Assignees are bare; the sink canonicalizes them to <client>:<bare>.
        rev0_tasks = [
            _task("coordinate", "Coordinate the brief", assignee=COORDINATOR,
                  status=types_pb2.TASK_STATUS_RUNNING),
            _task("research", "Gather research notes", assignee=RESEARCHER,
                  status=types_pb2.TASK_STATUS_RUNNING),
            _task("draft", "Draft the summary", assignee=WRITER),
            _task("review", "Review the draft", assignee=REVIEWER),
        ]
        rev0_edges = [
            _edge("coordinate", "research"),
            _edge("research", "draft"),
            _edge("draft", "review"),
        ]
        async def emit(event_pb) -> None:
            await sink.emit(event_pb)

        loop = asyncio.new_event_loop()

        loop.run_until_complete(
            emit(
                _event(
                    run_id=run_id,
                    session_id=session_id,
                    sequence=0,
                    payload_field="plan_submitted",
                    payload_msg=events_pb2.PlanSubmitted(
                        plan=_plan(
                            run_id=run_id,
                            revision_index=0,
                            tasks=rev0_tasks,
                            edges=rev0_edges,
                            summary="Coordinate, research, draft, review.",
                            revision_reason="initial plan install",
                            created_offset_s=0.0,
                        )
                    ),
                    emitted_offset_s=0.0,
                )
            )
        )

        # ── 3. Goldfive-internal refine_steer LLM spans (goldfive lane) ─────
        # These translate (at the sink) into real bars on <client>:goldfive so
        # the goldfive lane exists for arrows to originate from. One per steer.
        def goldfive_refine_call(
            target_bare: str,
            target_task: str,
            *,
            offset_s: float,
            input_preview: str,
            output_preview: str,
        ) -> None:
            span_id = uuid.uuid4().hex
            start = events_pb2.GoldfiveLLMCallStart(
                span_id=span_id,
                name="refine_steer",
                model="claude-opus-4",
                task_id=target_task,
                start_time_ns=_now_ns(offset_s),
                input_preview=input_preview,
                target_agent_id=target_bare,
                target_task_id=target_task,
            )
            end = events_pb2.GoldfiveLLMCallEnd(
                span_id=span_id,
                name="refine_steer",
                end_time_ns=_now_ns(offset_s + 0.2),
                status="completed",
                input_preview=input_preview,
                output_preview=output_preview,
                target_agent_id=target_bare,
                target_task_id=target_task,
            )
            loop.run_until_complete(
                emit(
                    events_pb2.Event(
                        event_id=str(uuid.uuid4()),
                        run_id=run_id,
                        session_id=session_id,
                        goldfive_llm_call_start=start,
                    )
                )
            )
            loop.run_until_complete(
                emit(
                    events_pb2.Event(
                        event_id=str(uuid.uuid4()),
                        run_id=run_id,
                        session_id=session_id,
                        goldfive_llm_call_end=end,
                    )
                )
            )

        # ── 4 + 5. Three drift → refine chains (the steering arrows) ────────
        # Each entry: (drift_kind, drift_severity, steered bare agent, task,
        #              revision_index, reason).
        steers = [
            (
                types_pb2.DRIFT_KIND_NEW_WORK_DISCOVERED,
                types_pb2.DRIFT_SEVERITY_INFO,
                RESEARCHER,
                "research",
                1,
                "researcher needs to gather primary sources before drafting",
            ),
            (
                types_pb2.DRIFT_KIND_PLAN_DIVERGENCE,
                types_pb2.DRIFT_SEVERITY_WARNING,
                WRITER,
                "draft",
                2,
                "draft drifted off the agreed outline; steering the writer back on-scope",
            ),
            (
                types_pb2.DRIFT_KIND_OFF_TOPIC,
                types_pb2.DRIFT_SEVERITY_CRITICAL,
                REVIEWER,
                "review",
                3,
                "review went off-topic and missed the success criteria; hard re-steer",
            ),
        ]

        seq = 1
        for (kind, severity, bare_agent, task_id, rev_idx, reason) in steers:
            drift_id = f"drift-{uuid.uuid4().hex[:12]}"

            # 4. DriftDetected — current_agent_id bare (sink canonicalizes it).
            drift = events_pb2.DriftDetected(
                kind=kind,
                severity=severity,
                detail=reason,
                current_task_id=task_id,
                current_agent_id=bare_agent,
                id=drift_id,
                authored_by="goldfive",
            )
            loop.run_until_complete(
                emit(
                    _event(
                        run_id=run_id,
                        session_id=session_id,
                        sequence=seq,
                        payload_field="drift_detected",
                        payload_msg=drift,
                        emitted_offset_s=2.8 + rev_idx * 0.3,
                    )
                )
            )
            seq += 1

            # Goldfive's refine_steer LLM call (goldfive-lane bar for this steer).
            goldfive_refine_call(
                bare_agent,
                task_id,
                offset_s=2.85 + rev_idx * 0.3,
                input_preview=f"drift {kind} on {task_id}; prior plan summary",
                output_preview=f"refined plan: re-steer {bare_agent} on {task_id}",
            )

            # 5. PlanRevised — trigger_event_id == drift.id (links the chain).
            # target_agent_id is set ALREADY-compound so the synthesized
            # refine.target_agent_id matches the work-agent lane (PRIMARY path).
            revised_tasks = [
                _task("coordinate", "Coordinate the brief", assignee=COORDINATOR,
                      status=types_pb2.TASK_STATUS_RUNNING),
                _task("research", "Gather research notes", assignee=RESEARCHER,
                      status=types_pb2.TASK_STATUS_COMPLETED if rev_idx >= 2
                      else types_pb2.TASK_STATUS_RUNNING),
                _task("draft", "Draft the summary", assignee=WRITER,
                      status=types_pb2.TASK_STATUS_RUNNING if rev_idx >= 2
                      else types_pb2.TASK_STATUS_PENDING),
                _task("review", "Review the draft", assignee=REVIEWER,
                      status=types_pb2.TASK_STATUS_PENDING),
            ]
            revised = events_pb2.PlanRevised(
                plan=_plan(
                    run_id=run_id,
                    revision_index=rev_idx,
                    tasks=revised_tasks,
                    edges=rev0_edges,
                    summary=f"Revision {rev_idx}: re-steer {bare_agent}.",
                    revision_reason=reason,
                    revision_kind=kind,
                    revision_severity=severity,
                    trigger_event_id=drift_id,
                    created_offset_s=2.9 + rev_idx * 0.3,
                ),
                drift_kind=kind,
                severity=severity,
                reason=reason,
                revision_index=rev_idx,
                trigger_event_id=drift_id,
                target_agent_id=compound(bare_agent),
                refine_input_summary=f"rev{rev_idx - 1} plan; {reason}",
                refine_output_summary=f"rev{rev_idx} re-steers {bare_agent} on {task_id}",
            )
            loop.run_until_complete(
                emit(
                    _event(
                        run_id=run_id,
                        session_id=session_id,
                        sequence=seq,
                        payload_field="plan_revised",
                        payload_msg=revised,
                        emitted_offset_s=2.95 + rev_idx * 0.3,
                    )
                )
            )
            seq += 1

        loop.run_until_complete(sink.close())
        loop.close()

        # Close the root invocation.
        client.emit_span_end(invocation, status=SpanStatus.COMPLETED)

        print(f"session_id={session_id}")
        print(f"run_id={run_id}")
        print(f"plan_id={PLAN_ID}")
        print(f"client_root_agent_id={root}")
        print("emitted steering chains:")
        for (kind, severity, bare_agent, task_id, rev_idx, _reason) in steers:
            sev = types_pb2.DriftSeverity.Name(severity).replace(
                "DRIFT_SEVERITY_", "").lower()
            print(f"  rev{rev_idx}  {sev:<9} steer goldfive -> {bare_agent} (task {task_id})")
        print(f"deep_link=http://127.0.0.1:7532/#/session/{session_id}")
    finally:
        client.shutdown(flush_timeout=10.0)

    print(f"DONE. session_id={session_id}")


if __name__ == "__main__":
    main()
