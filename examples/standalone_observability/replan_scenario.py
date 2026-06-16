"""Standalone replan scenario: emit a plan + multiple revisions to harmonograf.

This exercises harmonograf's *plan-revision visualization* — the plan reel
and the plan DAG — by driving a single plan id through four revisions so the
console has real evolution to render: tasks that get ADDED, tasks that get
DROPPED (ghosted), tasks that get SUPERSEDED, and the replan-trigger seams
(reason / kind / severity) between each revision.

WHY THIS IS STANDALONE
----------------------
Post the goldfive migration (issue #2/#4) harmonograf_client is an
observability-only library: plans, tasks, and drift now live in goldfive and
ride to the server inside a ``TelemetryUp.goldfive_event`` variant. Crucially,
*nothing requires the goldfive orchestration machinery (Runner / planner /
LLM) to PRODUCE those events*. We build the ``goldfive.v1.Event`` protos by
hand — a ``PlanSubmitted`` for rev0 and a ``PlanRevised`` for each subsequent
revision — and push them through the same ``Client.emit_goldfive_event`` path
the ``HarmonografSink`` uses. The result is fully deterministic: no LLM, no
network beyond the harmonograf gRPC stream, identical every run.

The goldfive proto stubs come in via the ``orchestration`` extra (the
``goldfive`` package grafts ``goldfive.v1`` onto its namespace), but we only
import the generated message classes — we never instantiate a Runner.

WHAT LANDS ON THE SERVER
------------------------
Each emitted event is correlated to this client's session by setting
``event.session_id`` (and the matching Hello session via the Client). The
server's ingest path (``_on_plan_submitted`` / ``_on_plan_revised`` in
``server/harmonograf_server/ingest.py``) upserts each into the
``task_plan_revisions`` table keyed on ``(plan_id, revision_index)``. After
this script runs, that table has FOUR rows for one ``plan_id`` — exactly the
shape the reel diffs across.

Required per the ingest contract:
  * ``event.run_id``        — non-empty, else the event is dropped wholesale.
  * ``plan.id``             — non-empty, the storage primary key; SAME across
                              all four revisions so they form one lineage.
  * ``plan.revision_index`` — 0,1,2,3 monotonic; drives reel ordering.
  * ``event.session_id``    — routes the event to this run's session.

THE SCENARIO (one plan, four revisions)
---------------------------------------
  rev0  PlanSubmitted   {research, draft, review}
                        reason "initial plan install"  (no drift metadata)
  rev1  PlanRevised     + gather-sources               (added before draft)
                        kind NEW_WORK_DISCOVERED / INFO   "scope creep"
  rev2  PlanRevised     draft -> draft-v2 (REPLACE-supersedes),
                        DROP review, + fact-check
                        kind PLAN_DIVERGENCE / WARNING    "goal drift"
  rev3  PlanRevised     mark every surviving task COMPLETED
                        kind UNSPECIFIED / INFO           "run wrap-up"

Run:
    export HARMONOGRAF_SERVER=127.0.0.1:7531
    uv run --extra orchestration python \\
        examples/standalone_observability/replan_scenario.py

Environment:
    HARMONOGRAF_SERVER  (default 127.0.0.1:7531)

Contrast with ``with_orchestration.py``, which produces the same kind of
events the *real* way (a goldfive Runner with a StaticPlanner). This file is
the deterministic, LLM-free shortcut for stress-testing the revision UI.
"""

from __future__ import annotations

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

from harmonograf_client import Client

# Plan id is stable across every revision — the four rows in
# task_plan_revisions share this id and differ only by revision_index.
PLAN_ID = "replan-demo"
PLANNER_AGENT = "planner"
WORKER_AGENT = "worker"


def _now_ts(offset_s: float = 0.0) -> timestamp_pb2.Timestamp:
    """A protobuf Timestamp at wall-clock now + offset (seconds)."""
    ts = timestamp_pb2.Timestamp()
    ts.FromNanoseconds(int((time.time() + offset_s) * 1_000_000_000))
    return ts


def _task(
    task_id: str,
    title: str,
    *,
    status: int = types_pb2.TASK_STATUS_PENDING,
    assignee: str = WORKER_AGENT,
    supersedes: str = "",
    supersedes_kind: int = types_pb2.SUPERSESSION_KIND_UNSPECIFIED,
    description: str = "",
) -> types_pb2.Task:
    """Build a goldfive ``Task`` proto with sane defaults."""
    return types_pb2.Task(
        id=task_id,
        title=title,
        description=description,
        assignee_agent_id=assignee,
        status=status,
        supersedes=supersedes,
        supersedes_kind=supersedes_kind,
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
    """Build a goldfive ``Plan`` proto for one revision of PLAN_ID."""
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
        created_at=_now_ts(created_offset_s),
        revision_trigger_event_id=trigger_event_id,
    )


def _event(
    *,
    run_id: str,
    session_id: str,
    sequence: int,
    payload_field: str,
    payload_msg,  # PlanSubmitted | PlanRevised
    emitted_offset_s: float = 0.0,
) -> events_pb2.Event:
    """Wrap a plan payload in the goldfive ``Event`` envelope.

    ``run_id`` + ``session_id`` are set on the envelope so the harmonograf
    ingest path persists the row under THIS run's session (see module
    docstring — both are part of the persistence contract).
    """
    evt = events_pb2.Event(
        event_id=str(uuid.uuid4()),
        run_id=run_id,
        sequence=sequence,
        session_id=session_id,
        emitted_at=_now_ts(emitted_offset_s),
    )
    getattr(evt, payload_field).CopyFrom(payload_msg)
    return evt


# ---------------------------------------------------------------------------
# The four revisions
# ---------------------------------------------------------------------------


def rev0_submitted(run_id: str) -> types_pb2.Plan:
    """rev0 — the initial plan the planner installs. {research, draft, review}."""
    tasks = [
        _task("research", "Gather research notes", status=types_pb2.TASK_STATUS_PENDING),
        _task("draft", "Draft the summary", status=types_pb2.TASK_STATUS_PENDING),
        _task("review", "Review the draft", status=types_pb2.TASK_STATUS_PENDING),
    ]
    edges = [_edge("research", "draft"), _edge("draft", "review")]
    return _plan(
        run_id=run_id,
        revision_index=0,
        tasks=tasks,
        edges=edges,
        summary="Research, draft, review a short summary.",
        revision_reason="initial plan install",
        created_offset_s=0.0,
    )


def rev1_add_gather_sources(run_id: str, trigger_event_id: str) -> types_pb2.Plan:
    """rev1 — ADD gather-sources before draft. Scope creep (NEW_WORK_DISCOVERED)."""
    tasks = [
        # research has started running by now
        _task("research", "Gather research notes", status=types_pb2.TASK_STATUS_RUNNING),
        _task(
            "gather-sources",
            "Gather primary sources",
            status=types_pb2.TASK_STATUS_PENDING,
            description="Discovered we need cited sources before drafting.",
        ),
        _task("draft", "Draft the summary", status=types_pb2.TASK_STATUS_PENDING),
        _task("review", "Review the draft", status=types_pb2.TASK_STATUS_PENDING),
    ]
    edges = [
        _edge("research", "gather-sources"),
        _edge("gather-sources", "draft"),
        _edge("draft", "review"),
    ]
    return _plan(
        run_id=run_id,
        revision_index=1,
        tasks=tasks,
        edges=edges,
        summary="Research, gather sources, draft, review.",
        revision_reason="reviewer asked for cited sources; adding a sourcing step",
        revision_kind=types_pb2.DRIFT_KIND_NEW_WORK_DISCOVERED,
        revision_severity=types_pb2.DRIFT_SEVERITY_INFO,
        trigger_event_id=trigger_event_id,
        created_offset_s=0.5,
    )


def rev2_supersede_and_drop(run_id: str, trigger_event_id: str) -> types_pb2.Plan:
    """rev2 — SUPERSEDE draft->draft-v2, DROP review, ADD fact-check.

    Goal drift (PLAN_DIVERGENCE / WARNING): the deliverable shape changed, so
    the old draft is replaced and a verification step replaces plain review.
    """
    tasks = [
        _task("research", "Gather research notes", status=types_pb2.TASK_STATUS_COMPLETED),
        _task("gather-sources", "Gather primary sources", status=types_pb2.TASK_STATUS_COMPLETED),
        # old draft kept as a CANCELLED ghost that the new task supersedes
        _task("draft", "Draft the summary", status=types_pb2.TASK_STATUS_CANCELLED),
        _task(
            "draft-v2",
            "Draft the summary (sourced rewrite)",
            status=types_pb2.TASK_STATUS_RUNNING,
            supersedes="draft",
            supersedes_kind=types_pb2.SUPERSESSION_KIND_REPLACE,
            description="Rewrite the draft to weave in the gathered sources.",
        ),
        _task(
            "fact-check",
            "Fact-check claims against sources",
            status=types_pb2.TASK_STATUS_PENDING,
            description="Replaces a plain read-through with a sourced verification.",
        ),
        # NOTE: 'review' is intentionally ABSENT -> dropped/ghost in the reel.
    ]
    edges = [
        _edge("research", "gather-sources"),
        _edge("gather-sources", "draft-v2"),
        _edge("draft-v2", "fact-check"),
    ]
    return _plan(
        run_id=run_id,
        revision_index=2,
        tasks=tasks,
        edges=edges,
        summary="Research, sources, sourced draft, fact-check.",
        revision_reason="deliverable shifted to a sourced brief; rewriting draft and replacing review with fact-check",
        revision_kind=types_pb2.DRIFT_KIND_PLAN_DIVERGENCE,
        revision_severity=types_pb2.DRIFT_SEVERITY_WARNING,
        trigger_event_id=trigger_event_id,
        created_offset_s=1.0,
    )


def rev3_mark_done(run_id: str, trigger_event_id: str) -> types_pb2.Plan:
    """rev3 — terminal sweep: mark every surviving task COMPLETED."""
    tasks = [
        _task("research", "Gather research notes", status=types_pb2.TASK_STATUS_COMPLETED),
        _task("gather-sources", "Gather primary sources", status=types_pb2.TASK_STATUS_COMPLETED),
        _task("draft", "Draft the summary", status=types_pb2.TASK_STATUS_CANCELLED),
        _task(
            "draft-v2",
            "Draft the summary (sourced rewrite)",
            status=types_pb2.TASK_STATUS_COMPLETED,
            supersedes="draft",
            supersedes_kind=types_pb2.SUPERSESSION_KIND_REPLACE,
        ),
        _task("fact-check", "Fact-check claims against sources", status=types_pb2.TASK_STATUS_COMPLETED),
    ]
    edges = [
        _edge("research", "gather-sources"),
        _edge("gather-sources", "draft-v2"),
        _edge("draft-v2", "fact-check"),
    ]
    return _plan(
        run_id=run_id,
        revision_index=3,
        tasks=tasks,
        edges=edges,
        summary="All work complete.",
        revision_reason="run wrap-up: all surviving tasks complete",
        revision_kind=types_pb2.DRIFT_KIND_UNSPECIFIED,
        revision_severity=types_pb2.DRIFT_SEVERITY_INFO,
        trigger_event_id=trigger_event_id,
        created_offset_s=1.5,
    )


def _diff(
    *,
    added: list[str],
    removed: list[str],
    modified: list[str],
    added_edges: list[types_pb2.TaskEdge] | None = None,
    removed_edges: list[types_pb2.TaskEdge] | None = None,
) -> events_pb2.PlanRevisionDiff:
    """Build the advisory cross-revision diff carried on PlanRevised."""
    return events_pb2.PlanRevisionDiff(
        added_task_ids=added,
        removed_task_ids=removed,
        modified_task_ids=modified,
        added_edges=added_edges or [],
        removed_edges=removed_edges or [],
    )


def _wait_for_session(client: Client, timeout_s: float = 10.0) -> str:
    """Block until the server assigns this client a session id (via Welcome).

    The Hello -> Welcome handshake happens on the transport thread; the
    server-assigned id is what every plan event must carry so the rows land
    under the right session. Falls back to the client-local id if the server
    never assigns one in time (still valid — the server uses it verbatim).
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        sid = client.session_id
        if sid:
            return sid
        time.sleep(0.05)
    return client.session_id


def main() -> None:
    server = os.environ.get("HARMONOGRAF_SERVER", "127.0.0.1:7531")
    # run_id ties the four events together as one goldfive "run". A fresh uuid
    # per process keeps reruns from colliding on the audit log.
    run_id = f"replan-run-{uuid.uuid4().hex[:12]}"

    client = Client(
        name="replan-scenario",
        framework="CUSTOM",
        server_addr=server,
        session_title="Replan scenario (4 revisions)",
    )
    try:
        # Emit one INVOCATION span first so the Hello lands and the server
        # assigns + persists a session row the plan revisions can FK to.
        from harmonograf_client import SpanKind, SpanStatus

        invocation = client.emit_span_start(
            kind=SpanKind.INVOCATION,
            name="replan_scenario",
            attributes={"scenario": "replan", "revisions": 4},
        )

        session_id = _wait_for_session(client)
        print(f"session_id={session_id}")
        print(f"run_id={run_id}")
        print(f"plan_id={PLAN_ID}")

        # Synthesize a distinct trigger event id per revision so the
        # intervention/trigger seams are unique (mirrors a real DriftDetected.id).
        trig_r1 = f"trigger-{uuid.uuid4().hex[:8]}"
        trig_r2 = f"trigger-{uuid.uuid4().hex[:8]}"
        trig_r3 = f"trigger-{uuid.uuid4().hex[:8]}"

        # --- rev0: PlanSubmitted -------------------------------------------
        e0 = _event(
            run_id=run_id,
            session_id=session_id,
            sequence=0,
            payload_field="plan_submitted",
            payload_msg=events_pb2.PlanSubmitted(plan=rev0_submitted(run_id)),
            emitted_offset_s=0.0,
        )
        client.emit_goldfive_event(e0)

        # --- rev1: add gather-sources --------------------------------------
        p1 = rev1_add_gather_sources(run_id, trig_r1)
        e1 = _event(
            run_id=run_id,
            session_id=session_id,
            sequence=1,
            payload_field="plan_revised",
            payload_msg=events_pb2.PlanRevised(
                plan=p1,
                drift_kind=p1.revision_kind,
                severity=p1.revision_severity,
                reason=p1.revision_reason,
                revision_index=1,
                trigger_event_id=trig_r1,
                diff=_diff(
                    added=["gather-sources"],
                    removed=[],
                    modified=["research"],
                    added_edges=[_edge("research", "gather-sources"), _edge("gather-sources", "draft")],
                    removed_edges=[_edge("research", "draft")],
                ),
                refine_input_summary="rev0 {research(running), draft, review}; reviewer wants citations",
                refine_output_summary="rev1 adds gather-sources before draft",
            ),
            emitted_offset_s=0.5,
        )
        client.emit_goldfive_event(e1)

        # --- rev2: supersede draft, drop review, add fact-check ------------
        p2 = rev2_supersede_and_drop(run_id, trig_r2)
        e2 = _event(
            run_id=run_id,
            session_id=session_id,
            sequence=2,
            payload_field="plan_revised",
            payload_msg=events_pb2.PlanRevised(
                plan=p2,
                drift_kind=p2.revision_kind,
                severity=p2.revision_severity,
                reason=p2.revision_reason,
                revision_index=2,
                trigger_event_id=trig_r2,
                diff=_diff(
                    added=["draft-v2", "fact-check"],
                    removed=["review"],
                    modified=["draft", "research", "gather-sources"],
                    added_edges=[_edge("gather-sources", "draft-v2"), _edge("draft-v2", "fact-check")],
                    removed_edges=[_edge("gather-sources", "draft"), _edge("draft", "review")],
                ),
                refine_input_summary="rev1 {…, draft, review}; deliverable became a sourced brief",
                refine_output_summary="rev2 supersedes draft->draft-v2, drops review, adds fact-check",
            ),
            emitted_offset_s=1.0,
        )
        client.emit_goldfive_event(e2)

        # --- rev3: terminal sweep ------------------------------------------
        p3 = rev3_mark_done(run_id, trig_r3)
        e3 = _event(
            run_id=run_id,
            session_id=session_id,
            sequence=3,
            payload_field="plan_revised",
            payload_msg=events_pb2.PlanRevised(
                plan=p3,
                drift_kind=p3.revision_kind,
                severity=p3.revision_severity,
                reason=p3.revision_reason,
                revision_index=3,
                trigger_event_id=trig_r3,
                diff=_diff(
                    added=[],
                    removed=[],
                    modified=["draft-v2", "fact-check"],
                ),
                refine_input_summary="rev2 work finishing; sweep to terminal",
                refine_output_summary="rev3 marks all surviving tasks completed",
            ),
            emitted_offset_s=1.5,
        )
        client.emit_goldfive_event(e3)

        # Close out the invocation span so the session reads as finished.
        client.emit_span_end(invocation, status=SpanStatus.COMPLETED)

        print("emitted revisions: 4 (rev0 submitted + rev1 + rev2 + rev3)")
        print("  rev0  initial plan install            {research, draft, review}")
        print("  rev1  new_work_discovered / info      + gather-sources")
        print("  rev2  plan_divergence / warning       draft->draft-v2, drop review, + fact-check")
        print("  rev3  (unspecified) / info            mark all completed")
    finally:
        # Flush generously — the four goldfive events plus the span pair must
        # drain before the transport shuts down.
        client.shutdown(flush_timeout=8.0)

    print(f"DONE. session_id={session_id}")


if __name__ == "__main__":
    main()
