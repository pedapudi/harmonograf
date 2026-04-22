"""Intervention history aggregation.

Builds the unified chronological view of a session's intervention events
— every point where the plan changed direction, whether driven by a
human operator, drift detection, or goldfive's own autonomous escalation.
See doc 01 §2 / types.proto ``Intervention`` for the data-model design
rationale and issue #69 for user-facing motivation.

The aggregator derives the list from primitives that already exist on
the wire, so no new persistence layer is introduced:

  * ``annotations`` table           — user STEER / HUMAN_RESPONSE rows.
  * Ingest pipeline drift ring      — ``drift_detected`` events, including
                                      the ``user_steer`` / ``user_cancel``
                                      drift kinds which flag user source.
  * ``task_plans.revision_kind``    — plan revisions that carry the drift
                                      kind or "cascade_cancel" / other
                                      goldfive-autonomous kinds.

The join happens in-memory on a per-call basis. Each source is projected
into a common ``(at, source, kind, body, author, outcome, severity,
revision_index, annotation_id, drift_kind)`` shape, then a second pass
attributes outcomes: a drift event immediately followed by a plan
revision becomes ``plan_revised:rN``; a drift event immediately followed
by cancelled tasks becomes ``cascade_cancel:N_tasks``. The attribution
window is deliberately small (configurable; default 5s) so unrelated
late events do not accidentally claim an outcome.

This module is deliberately tree-agnostic — it never inspects the plan's
task graph beyond counting cancelled tasks for cascade attribution, so
bespoke planner vocabularies (presentation agents, research flows, …)
render the same way without taxonomy hooks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from harmonograf_server.storage import (
    Annotation,
    AnnotationKind,
    Store,
    Task,
    TaskPlan,
    TaskStatus,
)


logger = logging.getLogger(__name__)


# Drift kinds that indicate the human operator initiated the change.
# Goldfive emits these when a user STEER / user CANCEL caused a plan
# revision — in that case the synthesized ``Intervention`` is tagged
# ``source="user"`` even though the immediate wire event is a drift.
_USER_DRIFT_KINDS: frozenset[str] = frozenset({"user_steer", "user_cancel"})

# Revision kinds that have no matching drift kind — goldfive minted the
# revision from its own escalation ladder. Treated as ``source="goldfive"``
# autonomous actions on the unified timeline.
_GOLDFIVE_REVISION_KINDS: frozenset[str] = frozenset(
    {"cascade_cancel", "refine_retry", "human_intervention_required"}
)

# How far forward we look to attribute an outcome to a drift. Five
# seconds is wide enough to cover realistic end-to-end latencies (drift
# detected → planner called → plan_revised emitted) but tight enough
# that a drift and a much later unrelated revision are not conflated.
_OUTCOME_WINDOW_S: float = 5.0

# Extended window applied only to user-control drifts (USER_STEER /
# USER_CANCEL). A user STEER flows through the planner's LLM which can
# take tens of seconds on a long prompt (issue #86 saw a 70s gap on a
# local Qwen3.5-35B), so the narrow 5s window used for autonomous
# drifts would strand the drift row and leak a second card. The
# extended window is still bounded so two separate user STEERs in a
# single session aren't claim-stolen by each other's plan revisions.
#
# harmonograf#95 bumped this from 300s to 900s after observing a
# 13m51s annotation→plan-revised gap on kikuchi/Qwen3.5-35B (user
# STEER at 5:24, plan-revised at 19:15). The strict-id dedup added
# via goldfive#196 is the primary fix — this wider window is a
# belt-and-suspenders fallback for pre-#196 producers and for edges
# where the id stamp fails to propagate.
_USER_OUTCOME_WINDOW_S: float = 900.0


def _outcome_window_for(drift_kind: str) -> float:
    """Per-kind window used by ``_project_plans`` and ``_attribute_outcomes``.

    User-control kinds get :data:`_USER_OUTCOME_WINDOW_S` — the planner's
    refine latency routinely exceeds the default 5s on larger models.
    Every other kind keeps the tight default because autonomous drift
    causes are fast-path heuristics: a looping-reasoning detection has
    no LLM round-trip to wait for.
    """
    return (
        _USER_OUTCOME_WINDOW_S
        if (drift_kind or "").lower() in _USER_DRIFT_KINDS
        else _OUTCOME_WINDOW_S
    )


# ---------------------------------------------------------------------------
# Intermediate record — the merge shape before outcome attribution.
# ---------------------------------------------------------------------------


@dataclass
class InterventionRecord:
    """In-memory projection used during aggregation.

    One per source row. Converted to ``types_pb2.Intervention`` at the
    RPC boundary.
    """

    at: float
    source: str
    kind: str
    body_or_reason: str = ""
    author: str = ""
    outcome: str = ""
    plan_revision_index: int = 0
    severity: str = ""
    annotation_id: str = ""
    drift_kind: str = ""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def list_interventions(
    session_id: str,
    *,
    store: Store,
    drifts_provider: Any,
) -> list[InterventionRecord]:
    """Return chronologically ordered interventions for ``session_id``.

    ``drifts_provider`` is any object with a ``drifts_for_session`` method
    (the IngestPipeline in prod; a stub in tests). Kept as a duck-type
    boundary so tests can drive the aggregator without spinning a full
    ingest stack.

    Dedup contract (harmonograf#75): when an annotation_id is present on
    a row, it becomes the canonical join key. Rows from other sources
    that share that annotation_id merge their outcome/severity onto the
    annotation row rather than producing their own card. The practical
    case is a single user STEER that currently surfaces as three
    records (annotation, USER_STEER drift, plan_revised:rN) — the
    deduper collapses them into one user-authored intervention card
    whose outcome reflects the plan revision.
    """

    annotations = await store.list_annotations(session_id=session_id)
    try:
        plans = await store.list_task_plans_for_session(session_id)
    except Exception as exc:  # noqa: BLE001 — degrade gracefully
        logger.debug("list_task_plans_for_session failed: %s", exc)
        plans = []
    drifts = []
    try:
        drifts = drifts_provider.drifts_for_session(session_id) or []
    except Exception as exc:  # noqa: BLE001 — degrade gracefully
        logger.debug("drifts_for_session failed: %s", exc)
        drifts = []

    records: list[InterventionRecord] = []
    records.extend(_project_annotations(annotations))
    records.extend(_project_drifts(drifts))
    records.extend(_project_plans(plans, drifts))

    records.sort(key=lambda r: r.at)
    _attribute_outcomes(records, plans)
    records = _merge_by_annotation_id(records)
    return records


# ---------------------------------------------------------------------------
# Per-source projection
# ---------------------------------------------------------------------------


def _project_annotations(annotations: Iterable[Annotation]) -> list[InterventionRecord]:
    """Map ``AnnotationKind`` rows to user-sourced intervention records.

    Only STEERING and HUMAN_RESPONSE are surfaced. COMMENT annotations are
    observational ("an operator read this and left a note") and not a
    plan-direction change, so the unified view omits them.
    """

    out: list[InterventionRecord] = []
    for ann in annotations:
        if ann.kind == AnnotationKind.STEERING:
            kind = "STEER"
        elif ann.kind == AnnotationKind.HUMAN_RESPONSE:
            kind = "HUMAN_RESPONSE"
        else:
            continue
        out.append(
            InterventionRecord(
                at=float(ann.created_at),
                source="user",
                kind=kind,
                body_or_reason=ann.body or "",
                author=ann.author or "user",
                annotation_id=ann.id,
            )
        )
    return out


def _project_drifts(drifts: Iterable[dict[str, Any]]) -> list[InterventionRecord]:
    """Map drift ring records to intervention records.

    Drift kinds that start with ``user_`` flag the row as ``source="user"``
    because goldfive emits those kinds when an operator intervention caused
    the plan revision; every other drift is ``source="drift"`` (model- or
    runtime-initiated).

    ``annotation_id`` (goldfive#176) is carried through when present so
    the downstream deduper can collapse the drift row into the source
    annotation row. Autonomous drifts (no backing ControlMessage) emit
    with an empty ``annotation_id`` and keep their own card.
    """

    out: list[InterventionRecord] = []
    for dr in drifts:
        drift_kind = (dr.get("kind") or "").lower()
        if not drift_kind:
            # DRIFT_KIND_UNSPECIFIED — ignore.
            continue
        is_user = drift_kind in _USER_DRIFT_KINDS
        source = "user" if is_user else "drift"
        # Normalize the kind label. User drifts get a compact English
        # label ("STEER" / "CANCEL") so trees render a uniform vocabulary
        # regardless of which upstream surface emitted the event.
        if drift_kind == "user_steer":
            kind_label = "STEER"
        elif drift_kind == "user_cancel":
            kind_label = "CANCEL"
        else:
            kind_label = drift_kind.upper()
        at = dr.get("recorded_at")
        if not isinstance(at, (int, float)):
            continue
        out.append(
            InterventionRecord(
                at=float(at),
                source=source,
                kind=kind_label,
                body_or_reason=dr.get("detail") or "",
                severity=dr.get("severity") or "",
                drift_kind=drift_kind,
                annotation_id=str(dr.get("annotation_id") or ""),
            )
        )
    return out


def _project_plans(
    plans: Iterable[TaskPlan], drifts: Iterable[dict[str, Any]]
) -> list[InterventionRecord]:
    """Map plan revisions that have no matching drift to intervention rows.

    Autonomous goldfive revisions (cascade_cancel, refine_retry, …) land
    here. Revisions whose ``revision_kind`` matches a drift that fired
    within the attribution window are skipped — the drift itself already
    represents the intervention and will own the ``plan_revised:rN``
    outcome attribution in the next pass.

    When the matching drift carries an ``annotation_id`` (goldfive#176),
    we propagate that id onto the plan record too so the final dedup
    pass (:func:`_merge_by_annotation_id`) can fold the plan outcome
    into the annotation row. Without this, a user STEER whose drift +
    plan revision land strictly within the 5s window gets its drift row
    suppressed here (good) but the plan row rolls through with no id —
    then the final merge can't fold it onto the annotation and the user
    sees a STEER annotation card plus a bonus STEER plan card.
    """

    out: list[InterventionRecord] = []
    drift_list = list(drifts)
    for plan in plans:
        rev_kind = (plan.revision_kind or "").lower()
        rev_index = int(plan.revision_index or 0)
        if not rev_kind or rev_index <= 0:
            # Initial plan submission — not an intervention.
            continue
        # If a drift with the same kind fired just before this plan
        # revision, let the drift row own the intervention and skip the
        # plan here. Otherwise this is an autonomous goldfive revision
        # (cascade_cancel, refine_retry) or a drift we never ingested,
        # and we record it directly.
        #
        # The window is kind-dependent (:func:`_outcome_window_for`):
        # user-control kinds allow a long refine latency (goldfive has
        # to run the planner LLM before emitting plan_revised); every
        # other kind keeps the tight 5s default. Issue #86 triggered a
        # 70s drift-to-plan gap on a local Qwen3.5-35B that the 5s
        # default strand-suppressed, leaving both drift and plan rows
        # as separate cards.
        window = _outcome_window_for(rev_kind)
        preceding_drift: dict[str, Any] | None = None
        for dr in drift_list:
            if (dr.get("kind") or "").lower() != rev_kind:
                continue
            ra = dr.get("recorded_at")
            if not isinstance(ra, (int, float)):
                continue
            delta = float(plan.created_at) - float(ra)
            if 0.0 <= delta <= window:
                preceding_drift = dr
                break
        if preceding_drift is not None:
            continue
        if rev_kind in _GOLDFIVE_REVISION_KINDS:
            source = "goldfive"
        elif rev_kind in _USER_DRIFT_KINDS:
            source = "user"
        else:
            # A drift kind that never made it through the ingest ring —
            # record it as drift-sourced so the timeline still shows it.
            source = "drift"
        kind_label = rev_kind.upper() if source != "user" else (
            "STEER" if rev_kind == "user_steer" else "CANCEL"
        )
        out.append(
            InterventionRecord(
                at=float(plan.created_at),
                source=source,
                kind=kind_label,
                body_or_reason=plan.revision_reason or "",
                severity=plan.revision_severity or "",
                plan_revision_index=rev_index,
                drift_kind=rev_kind if source != "goldfive" else "",
                outcome=f"plan_revised:r{rev_index}",
                # goldfive#196 / harmonograf#95: carry the source
                # annotation id stamped on the plan so the final
                # dedup pass can strict-join this row against the
                # source annotation — no more time-window fallback
                # for slow refines.
                annotation_id=str(
                    getattr(plan, "revision_annotation_id", "") or ""
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Outcome attribution
# ---------------------------------------------------------------------------


def _attribute_outcomes(
    records: list[InterventionRecord], plans: Iterable[TaskPlan]
) -> None:
    """Fill in ``outcome`` / ``plan_revision_index`` for drift + user rows.

    Mutates ``records`` in place. Runs after the sort so forward-looking
    attribution is O(n log n) — one binary-search sweep over ``records``
    plus an O(plans) scan per drift.

    Rules:
      • Drift/user row followed by a plan revision whose created_at is
        within ``_OUTCOME_WINDOW_S`` → ``plan_revised:rN``. The revision
        must share the row's drift_kind when available, otherwise the
        nearest revision wins.
      • Drift row with no matching revision but ≥1 CANCELLED task that
        transitioned to CANCELLED after the drift → ``cascade_cancel:N``.
      • Everything else → left as "recorded" (for drift/user) or the
        value the plan projector already set.
    """

    plan_list = sorted(plans, key=lambda p: p.created_at)

    for rec in records:
        if rec.source not in ("drift", "user"):
            # goldfive rows already carry their outcome.
            continue
        if rec.outcome:
            # Already attributed (e.g. from the plan projector path).
            continue

        revised = _find_matching_revision(rec, plan_list)
        if revised is not None:
            rec.outcome = f"plan_revised:r{revised.revision_index}"
            rec.plan_revision_index = int(revised.revision_index or 0)
            continue

        cascade = _count_cascade_cancels(rec, plan_list)
        if cascade > 0:
            rec.outcome = f"cascade_cancel:{cascade}_tasks"
            continue

        # No forward correlation — record the drift/user row as observed.
        rec.outcome = "recorded"


def _find_matching_revision(
    rec: InterventionRecord, plans: list[TaskPlan]
) -> Optional[TaskPlan]:
    # Use the extended window for user-control drifts (see
    # :func:`_outcome_window_for`) so a slow refine still attributes its
    # plan_revised outcome back to the drift row that caused it.
    window = _outcome_window_for(rec.drift_kind)
    best: Optional[TaskPlan] = None
    best_delta: float = window + 1.0
    for plan in plans:
        delta = float(plan.created_at) - rec.at
        if delta < 0 or delta > window:
            continue
        rev_kind = (plan.revision_kind or "").lower()
        # Require the revision to carry a revision_kind (i.e. it is
        # indeed a refine, not the initial plan submission).
        if not rev_kind or (plan.revision_index or 0) <= 0:
            continue
        # Prefer an exact kind match; otherwise fall back to the nearest
        # revision in the window.
        if rec.drift_kind and rev_kind == rec.drift_kind:
            if delta < best_delta:
                best_delta = delta
                best = plan
        elif best is None and not rec.drift_kind:
            best_delta = delta
            best = plan
    return best


def _merge_by_annotation_id(
    records: list[InterventionRecord],
) -> list[InterventionRecord]:
    """Collapse rows that share an ``annotation_id`` into a single card.

    Rule (harmonograf#75):

      * Rows with ``annotation_id`` populated (annotation rows, user-control
        drift rows tagged by goldfive#176) are grouped by that id.
      * Each group keeps exactly one row — the annotation row when present,
        otherwise the earliest surviving row in the group.
      * The kept row absorbs non-empty fields from the others — ``outcome``
        and ``plan_revision_index`` in particular, since the drift / plan
        path is usually the one that attributed a ``plan_revised:rN``.
        ``severity`` is also promoted so user-authored cards inherit the
        drift severity (WARNING for STEER, CRITICAL for CANCEL).

    Rows without ``annotation_id`` pass through untouched — autonomous
    drifts (LOOPING_REASONING, TOOL_ERROR, …) keep their own cards as
    the user directive requires.

    Additionally, when a group has an annotation row, any plan-sourced
    row whose ``at`` falls inside the attribution window after the
    annotation AND whose ``drift_kind`` matches the group's user-control
    kind gets folded in too. This catches the common case where the
    plan row made it through :func:`_project_plans` because the matching
    drift was suppressed but the plan row itself has no annotation_id
    to join on.

    As of goldfive#196 plans do carry ``revision_annotation_id`` on the
    wire, so most plan-sourced rows now group strictly by id in the
    primary pass and never touch the time-window fallback. The fallback
    survives for back-compat with pre-#196 data (older runs where the
    id wasn't stamped) and for any emit path that fails to propagate
    the id. See harmonograf#95 for the slow-refine case this fixes.
    """

    # Partition: rows keyed by annotation_id (the merge candidates), and
    # everything else that flows through unchanged.
    grouped: dict[str, list[InterventionRecord]] = {}
    passthrough: list[InterventionRecord] = []
    for rec in records:
        if rec.annotation_id:
            grouped.setdefault(rec.annotation_id, []).append(rec)
        else:
            passthrough.append(rec)

    if not grouped:
        return records

    merged: list[InterventionRecord] = list(passthrough)
    for ann_id, group in grouped.items():
        # Prefer the annotation row as the survivor; it carries the user's
        # text / author / wall-clock timestamp authoritatively.
        annotation_row: InterventionRecord | None = None
        others: list[InterventionRecord] = []
        for rec in group:
            # ``_project_annotations`` produces the only rows with
            # source="user" AND kind in {"STEER", "CANCEL", ...} AND
            # ``author`` set AND empty ``drift_kind``. That combination is
            # unique to annotation rows — drift rows tagged with the same
            # annotation_id carry ``drift_kind`` (e.g. "user_steer") and
            # have no author. Using drift_kind as the discriminator keeps
            # the logic robust to future annotation-source additions.
            if not rec.drift_kind and rec.source == "user":
                annotation_row = rec
            else:
                others.append(rec)
        if annotation_row is None:
            # No annotation row in the group (shouldn't happen in prod
            # — goldfive only stamps annotation_id on drifts minted from
            # a user annotation — but stay defensive). Keep the earliest
            # row and merge the rest onto it.
            group.sort(key=lambda r: r.at)
            annotation_row = group[0]
            others = group[1:]
        # Promote non-empty fields from ``others`` onto the survivor.
        # For ``outcome``: prefer a real attribution (``plan_revised:rN``,
        # ``cascade_cancel:N_tasks``) over the fallback ``recorded``. The
        # drift path is the one that actually knows which revision fired,
        # so if the annotation row had to fall back to ``recorded``
        # (e.g. because the user_steer drift was stranded outside the
        # default attribution window) we still get the right label on
        # the surviving card (issue #86).
        for other in others:
            if other.outcome and other.outcome != "recorded":
                if (
                    not annotation_row.outcome
                    or annotation_row.outcome == "recorded"
                ):
                    annotation_row.outcome = other.outcome
            elif other.outcome and not annotation_row.outcome:
                annotation_row.outcome = other.outcome
            if (
                other.plan_revision_index
                and not annotation_row.plan_revision_index
            ):
                annotation_row.plan_revision_index = other.plan_revision_index
            if other.severity and not annotation_row.severity:
                annotation_row.severity = other.severity
            if other.drift_kind and not annotation_row.drift_kind:
                annotation_row.drift_kind = other.drift_kind
        merged.append(annotation_row)

    # Also fold in plan-sourced rows whose drift_kind matches a merged
    # annotation row's promoted drift_kind and whose ``at`` lands inside
    # the attribution window — covers the case where _project_plans
    # emitted a row because its matching drift was deduped at the
    # suppress step, leaving no carrier for annotation_id. Uses the
    # kind-dependent window (:func:`_outcome_window_for`) so a slow
    # user-STEER refine can still fold — issue #86.
    def _find_merge_target(plan_row: InterventionRecord) -> InterventionRecord | None:
        window = _outcome_window_for(plan_row.drift_kind)
        for rec in merged:
            if not rec.annotation_id or not rec.drift_kind:
                continue
            if rec.drift_kind != plan_row.drift_kind:
                continue
            delta = plan_row.at - rec.at
            if 0.0 <= delta <= window:
                return rec
        return None

    survivors: list[InterventionRecord] = []
    for rec in merged:
        # Plan-sourced rows (no annotation_id, carries drift_kind + outcome,
        # user-control kind) are merge candidates.
        if (
            not rec.annotation_id
            and rec.drift_kind in _USER_DRIFT_KINDS
            and rec.outcome.startswith("plan_revised:")
        ):
            target = _find_merge_target(rec)
            if target is not None:
                # Prefer the plan's real outcome (``plan_revised:rN``)
                # over a fallback ``recorded`` that the annotation row
                # may have picked up during attribution when the drift
                # was stranded outside the default window (issue #86).
                if not target.outcome or target.outcome == "recorded":
                    target.outcome = rec.outcome
                if not target.plan_revision_index:
                    target.plan_revision_index = rec.plan_revision_index
                continue
        survivors.append(rec)

    survivors.sort(key=lambda r: r.at)
    return survivors


def _count_cascade_cancels(
    rec: InterventionRecord, plans: list[TaskPlan]
) -> int:
    """Count tasks that transitioned to CANCELLED in the drift's wake.

    We do not have per-task transition timestamps in the store — the
    closest proxy is the newest plan revision that landed after the drift
    and contains CANCELLED tasks not present as CANCELLED in the
    previous revision. Rather than reconstructing that diff, we fall back
    to a conservative heuristic: count CANCELLED tasks in the latest
    plan revision whose created_at sits after the drift. The frontend
    renders this as a rough indicator only ("cascade_cancel:N_tasks").
    """

    count = 0
    latest_after: Optional[TaskPlan] = None
    for plan in plans:
        if float(plan.created_at) <= rec.at:
            continue
        if latest_after is None or plan.created_at > latest_after.created_at:
            latest_after = plan
    if latest_after is None:
        return 0
    for task in latest_after.tasks:
        if task.status == TaskStatus.CANCELLED:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Proto conversion
# ---------------------------------------------------------------------------


def record_to_pb(rec: InterventionRecord, types_pb2_mod: Any) -> Any:
    """Translate an ``InterventionRecord`` to the generated proto message.

    ``types_pb2_mod`` is passed in rather than imported at module scope
    so the aggregator stays importable in contexts where the generated
    stubs are not on ``sys.path`` (e.g. unit tests that only exercise
    the pure aggregation logic).
    """

    pb = types_pb2_mod.Intervention(
        source=rec.source,
        kind=rec.kind,
        body_or_reason=rec.body_or_reason,
        author=rec.author,
        outcome=rec.outcome,
        plan_revision_index=int(rec.plan_revision_index),
        severity=rec.severity,
        annotation_id=rec.annotation_id,
        drift_kind=rec.drift_kind,
    )
    if rec.at:
        pb.at.seconds = int(rec.at)
        pb.at.nanos = int((rec.at - int(rec.at)) * 1e9)
    return pb
