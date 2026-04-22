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
into a common ``InterventionRecord`` shape and merged by
``trigger_event_id`` — a strict goldfive-minted identifier introduced by
goldfive#199 / harmonograf#99 that every ``PlanRevised`` envelope
carries:

  * User-control refines → ``trigger_event_id`` == source ``annotation.id``.
  * Autonomous drift refines → ``trigger_event_id`` == ``DriftDetected.id``.

Dedup is strict-id-only by default — plan-revision rows only merge onto
their originating annotation / drift when the id matches. Rows that do
not match surface as their own cards (not silently dropped).

A legacy time-window fallback (the pre-#99 behaviour) is preserved
behind the ``HARMONOGRAF_LEGACY_PLAN_ATTRIBUTION_WINDOW_MS`` env var so
operators with in-flight investigations can opt back in. Default: 0
(disabled). When enabled, a merge via the fallback logs a WARNING so
operators can diagnose mis-attribution.

Pre-fix (pre-harmonograf#99) data that lacks ``trigger_event_id`` is
explicitly unsupported — operators should drop their dev databases
after upgrading. The fallback path exists for live-migration scenarios
only.

This module is deliberately tree-agnostic — it never inspects the plan's
task graph beyond counting cancelled tasks for cascade attribution, so
bespoke planner vocabularies (presentation agents, research flows, …)
render the same way without taxonomy hooks.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from harmonograf_server.storage import (
    Annotation,
    AnnotationKind,
    Store,
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

# -----------------------------------------------------------------------------
# Legacy time-window fallback (harmonograf#99 rescope).
#
# Pre-#99, the aggregator merged plan-revision rows onto their
# originating drift/annotation using a time window. That was brittle:
# a slow refine (kikuchi/Qwen ~14min) would strand the plan row outside
# the window and leak a duplicate card.
#
# Rescope: goldfive#199 stamps ``trigger_event_id`` on EVERY refine
# (user-control + autonomous), and harmonograf merges by strict id only
# by default. The old time-window path is preserved behind an env var
# so operators can opt back in during migration if they find the strict
# dedup too aggressive (e.g. live sessions in flight against a pre-#199
# goldfive). Default: disabled.
# -----------------------------------------------------------------------------


def _read_legacy_window_ms() -> float:
    """Read the legacy time-window from the env var (0 / disabled by default).

    Env var: ``HARMONOGRAF_LEGACY_PLAN_ATTRIBUTION_WINDOW_MS``.
    Returns 0.0 when unset, invalid, or set to 0.
    """
    raw = os.environ.get("HARMONOGRAF_LEGACY_PLAN_ATTRIBUTION_WINDOW_MS", "")
    if not raw:
        return 0.0
    try:
        val = float(raw)
    except ValueError:
        logger.warning(
            "HARMONOGRAF_LEGACY_PLAN_ATTRIBUTION_WINDOW_MS is not a number: %r; "
            "disabling fallback",
            raw,
        )
        return 0.0
    if val <= 0:
        return 0.0
    return val


# Cached on module load so tests that mutate the env before import see
# a live value; tests that mutate at runtime should call the getter
# directly rather than reading the cached constant.
_LEGACY_TIME_WINDOW_PLAN_ATTRIBUTION_MS: float = _read_legacy_window_ms()


def _legacy_window_ms() -> float:
    """Return the active legacy window in ms (re-read each call).

    Tests and operators toggling the env var at runtime are reflected
    without requiring a reimport. Returns 0.0 when disabled.
    """
    return _read_legacy_window_ms()


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
    # ``annotation_id`` is the source annotation's id on user-authored
    # rows (STEER / HUMAN_RESPONSE). Also populated on drift rows minted
    # from a user ControlMessage for the user-side strict merge.
    annotation_id: str = ""
    drift_kind: str = ""
    # harmonograf#99 / goldfive#199: opaque id of the event that
    # triggered a plan revision (or that the row _is_, for drift/annotation
    # rows). Dedup key used by :func:`_merge_by_trigger_event_id`:
    #   * For an annotation row, equals the annotation's own id.
    #   * For a drift row, equals the goldfive drift.id (UUID4). Also
    #     equals the annotation_id when the drift was minted from a
    #     user ControlMessage — user-control plan revisions match on
    #     that value; autonomous plan revisions match on the drift.id.
    #   * For a plan-revision row, equals the ``PlanRevised.trigger_event_id``
    #     goldfive stamped on the wire.
    trigger_event_id: str = ""


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

    Dedup contract (harmonograf#99 rescope):

      * Tier 1 — always on. Plan-revision rows merge onto their source
        annotation / drift row when ``trigger_event_id`` matches the
        annotation id or drift id.
      * Tier 2 — opt-in via ``HARMONOGRAF_LEGACY_PLAN_ATTRIBUTION_WINDOW_MS``
        env var (default 0 / disabled). Time-window fallback: a plan
        row whose ``trigger_event_id`` matched nothing strictly merges
        onto a preceding user-control row of the same drift_kind within
        the configured window. Emits a WARNING log so operators can see
        what would not have merged under strict-id-only.

    Rows that don't match either tier surface as their own cards — never
    silently merged.
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
    records.extend(_project_plans(plans))

    records.sort(key=lambda r: r.at)
    _attribute_outcomes(records, plans)
    records = _merge_by_trigger_event_id(records)
    # Tier 2 — opt-in legacy time-window merge for plan rows with NO
    # trigger_event_id (pre-#99 data or goldfive-bridge misconfigured).
    # Default off; WARNING logged on every match so operators notice.
    window_ms = _legacy_window_ms()
    if window_ms > 0:
        records = _legacy_time_window_merge_orphan_plans(
            records, window_s=window_ms / 1000.0, window_ms=window_ms
        )
    return records


def _legacy_time_window_merge_orphan_plans(
    records: list[InterventionRecord], *, window_s: float, window_ms: float
) -> list[InterventionRecord]:
    """Opt-in legacy merge for plan rows without a ``trigger_event_id``.

    Fires only when ``HARMONOGRAF_LEGACY_PLAN_ATTRIBUTION_WINDOW_MS`` is
    set to a positive value. Walks the sorted record list; a plan row
    (no trigger_event_id, drift_kind set, outcome=plan_revised:*) folds
    onto the most recent preceding user / drift row within ``window_s``
    whose drift_kind matches. WARNING logged on every match.

    Strict-id rows are never touched — they're already merged and their
    trigger_event_id provides the authoritative join.
    """
    survivors: list[InterventionRecord] = []
    for rec in records:
        is_orphan_plan = (
            not rec.trigger_event_id
            and rec.drift_kind
            and rec.outcome.startswith("plan_revised:")
        )
        if not is_orphan_plan:
            survivors.append(rec)
            continue
        # Look back for a merge target: most recent user/drift row with
        # same drift_kind inside the window.
        target: InterventionRecord | None = None
        for prior in reversed(survivors):
            if prior.source not in ("user", "drift"):
                continue
            if prior.drift_kind != rec.drift_kind:
                continue
            delta = rec.at - prior.at
            if 0.0 <= delta <= window_s:
                target = prior
                break
            if delta > window_s:
                break  # past the window; search no further
        if target is not None:
            logger.warning(
                "interventions: legacy time-window fallback merged "
                "plan_revision_index=%d (drift_kind=%s, no trigger_event_id) "
                "onto %s row. HARMONOGRAF_LEGACY_PLAN_ATTRIBUTION_WINDOW_MS=%.0f. "
                "Investigate why strict-id did not match (pre-#99 data? "
                "goldfive < #199?).",
                rec.plan_revision_index,
                rec.drift_kind,
                target.source,
                window_ms,
            )
            if not target.outcome or target.outcome == "recorded":
                target.outcome = rec.outcome
            if not target.plan_revision_index:
                target.plan_revision_index = rec.plan_revision_index
            if not target.severity and rec.severity:
                target.severity = rec.severity
            continue
        survivors.append(rec)
    return survivors


# ---------------------------------------------------------------------------
# Per-source projection
# ---------------------------------------------------------------------------


def _project_annotations(annotations: Iterable[Annotation]) -> list[InterventionRecord]:
    """Map ``AnnotationKind`` rows to user-sourced intervention records.

    Only STEERING and HUMAN_RESPONSE are surfaced. COMMENT annotations are
    observational ("an operator read this and left a note") and not a
    plan-direction change, so the unified view omits them.

    The annotation row's ``trigger_event_id`` equals the annotation's
    own id — that's the identifier goldfive stamps on a user-control
    ``PlanRevised.trigger_event_id`` when the revision was driven by
    this annotation.
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
                # harmonograf#99: the annotation's id IS the strict join
                # key for a downstream PlanRevised refine.
                trigger_event_id=ann.id,
            )
        )
    return out


def _project_drifts(drifts: Iterable[dict[str, Any]]) -> list[InterventionRecord]:
    """Map drift ring records to intervention records.

    Drift kinds that start with ``user_`` flag the row as ``source="user"``
    because goldfive emits those kinds when an operator intervention caused
    the plan revision; every other drift is ``source="drift"`` (model- or
    runtime-initiated).

    ``trigger_event_id`` (harmonograf#99): for user-control drifts we
    use the source annotation_id (so the downstream merge folds drift +
    plan onto the annotation row). For autonomous drifts we use the
    goldfive-minted drift id — a subsequent PlanRevised whose
    ``trigger_event_id`` equals this value merges onto the drift row.
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
        ann_id = str(dr.get("annotation_id") or "")
        drift_id = str(dr.get("id") or "")
        # For user-control drifts prefer annotation_id as the
        # trigger_event_id so we merge onto the annotation row; for
        # autonomous drifts use the drift.id so a subsequent refine's
        # PlanRevised can strict-match back to this drift row.
        trig = ann_id if is_user and ann_id else drift_id
        out.append(
            InterventionRecord(
                at=float(at),
                source=source,
                kind=kind_label,
                body_or_reason=dr.get("detail") or "",
                severity=dr.get("severity") or "",
                drift_kind=drift_kind,
                annotation_id=ann_id,
                trigger_event_id=trig,
            )
        )
    return out


def _project_plans(plans: Iterable[TaskPlan]) -> list[InterventionRecord]:
    """Project every plan revision (index > 0) to an InterventionRecord.

    Unlike the pre-#99 projector, this one does **not** try to suppress
    the plan row when a matching drift exists — the merge is strict-id
    (:func:`_merge_by_trigger_event_id`) and doesn't need a heuristic
    pre-filter. Plan rows that successfully merge disappear during merge;
    orphans (no matching annotation / drift) surface as their own cards.

    ``trigger_event_id`` comes off the persisted plan row (stamped by
    ingest.py::_on_plan_revised from ``PlanRevised.trigger_event_id``).
    """

    out: list[InterventionRecord] = []
    for plan in plans:
        rev_kind = (plan.revision_kind or "").lower()
        rev_index = int(plan.revision_index or 0)
        if not rev_kind or rev_index <= 0:
            # Initial plan submission — not an intervention.
            continue
        if rev_kind in _GOLDFIVE_REVISION_KINDS:
            source = "goldfive"
        elif rev_kind in _USER_DRIFT_KINDS:
            source = "user"
        else:
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
                trigger_event_id=plan.trigger_event_id or "",
            )
        )
    return out


# ---------------------------------------------------------------------------
# Outcome attribution
# ---------------------------------------------------------------------------


def _attribute_outcomes(
    records: list[InterventionRecord], plans: Iterable[TaskPlan]
) -> None:
    """Fill in ``outcome`` for drift and user rows.

    Runs before the trigger_event_id merge so merged-away plan rows can
    still donate their outcome to the survivor. Unlike the pre-#99
    attributor, the primary pairing is strict-id:

      * A drift/user row with a non-empty trigger_event_id is paired
        with the plan row whose trigger_event_id matches — that pairing
        yields ``plan_revised:rN``.
      * Only when strict-id yields nothing does the legacy time-window
        get consulted (and only when enabled via env var).
      * Rows with no strict pairing fall through to the cascade-cancel
        heuristic for user/drift rows, or are left as ``recorded``.
    """

    plan_list = sorted(plans, key=lambda p: p.created_at)
    # Build a quick lookup of revision plans by trigger_event_id.
    plan_by_trigger: dict[str, TaskPlan] = {}
    for plan in plan_list:
        if int(plan.revision_index or 0) <= 0:
            continue
        if not plan.trigger_event_id:
            continue
        plan_by_trigger.setdefault(plan.trigger_event_id, plan)

    window_ms = _legacy_window_ms()
    window_s = window_ms / 1000.0 if window_ms > 0 else 0.0

    for rec in records:
        if rec.source not in ("drift", "user"):
            continue
        if rec.outcome:
            continue

        # Tier 1 — strict-id.
        matched = (
            plan_by_trigger.get(rec.trigger_event_id) if rec.trigger_event_id else None
        )
        if matched is not None:
            rec.outcome = f"plan_revised:r{matched.revision_index}"
            rec.plan_revision_index = int(matched.revision_index or 0)
            continue

        # Tier 2 — legacy time-window (opt-in).
        if window_s > 0:
            fallback = _legacy_find_matching_revision(
                rec, plan_list, window_s=window_s
            )
            if fallback is not None:
                logger.warning(
                    "interventions: legacy time-window fallback matched "
                    "drift_kind=%s trigger_event_id=%r -> plan rev=%d "
                    "(trigger_event_id=%r). HARMONOGRAF_LEGACY_PLAN_"
                    "ATTRIBUTION_WINDOW_MS=%.0f. Investigate why strict-id "
                    "did not match (pre-#99 data? goldfive < #199?).",
                    rec.drift_kind,
                    rec.trigger_event_id,
                    fallback.revision_index,
                    fallback.trigger_event_id,
                    window_ms,
                )
                rec.outcome = f"plan_revised:r{fallback.revision_index}"
                rec.plan_revision_index = int(fallback.revision_index or 0)
                continue

        cascade = _count_cascade_cancels(rec, plan_list)
        if cascade > 0:
            rec.outcome = f"cascade_cancel:{cascade}_tasks"
            continue

        rec.outcome = "recorded"


def _legacy_find_matching_revision(
    rec: InterventionRecord, plans: list[TaskPlan], *, window_s: float
) -> Optional[TaskPlan]:
    """Pre-#99 time-window matcher. Opt-in only."""
    best: Optional[TaskPlan] = None
    best_delta: float = window_s + 1.0
    for plan in plans:
        delta = float(plan.created_at) - rec.at
        if delta < 0 or delta > window_s:
            continue
        rev_kind = (plan.revision_kind or "").lower()
        if not rev_kind or (plan.revision_index or 0) <= 0:
            continue
        if rec.drift_kind and rev_kind == rec.drift_kind:
            if delta < best_delta:
                best_delta = delta
                best = plan
        elif best is None and not rec.drift_kind:
            best_delta = delta
            best = plan
    return best


def _merge_by_trigger_event_id(
    records: list[InterventionRecord],
) -> list[InterventionRecord]:
    """Collapse rows that share a ``trigger_event_id`` into a single card.

    Strict-id merge (harmonograf#99 rescope):

      * Rows with a non-empty ``trigger_event_id`` group on that id.
      * Each group keeps one survivor: the annotation row if present,
        otherwise the drift row, otherwise the earliest row.
      * Survivor absorbs ``outcome`` / ``plan_revision_index`` /
        ``severity`` / ``drift_kind`` from the others.

    Rows with empty ``trigger_event_id`` pass through unchanged. That
    preserves:

      * Legitimate standalone cards (e.g. a drift with no downstream
        refine).
      * Pre-#99 plan rows (unsupported, documented as such) that would
        have merged via time-window on the old code path. They now
        surface as their own cards — the rescope's explicit semantic.

    The legacy time-window fallback lives in :func:`_attribute_outcomes`
    (outcome attribution) rather than here: the merge stays strict
    regardless of env flag, so mis-attributed cards via the legacy
    window never collapse silently.
    """

    grouped: dict[str, list[InterventionRecord]] = {}
    passthrough: list[InterventionRecord] = []
    for rec in records:
        if rec.trigger_event_id:
            grouped.setdefault(rec.trigger_event_id, []).append(rec)
        else:
            passthrough.append(rec)

    if not grouped:
        return records

    merged: list[InterventionRecord] = list(passthrough)
    for _trig, group in grouped.items():
        group.sort(key=lambda r: r.at)
        # Prefer the annotation row as the survivor (annotation rows have
        # source="user" AND no drift_kind).
        survivor: InterventionRecord | None = None
        for rec in group:
            if rec.source == "user" and not rec.drift_kind:
                survivor = rec
                break
        if survivor is None:
            # No annotation row — prefer the drift row (has drift_kind set
            # but no plan_revision_index), then fall back to earliest.
            for rec in group:
                if rec.drift_kind and not rec.plan_revision_index:
                    survivor = rec
                    break
        if survivor is None:
            survivor = group[0]
        others = [r for r in group if r is not survivor]
        for other in others:
            if other.outcome and other.outcome != "recorded":
                if not survivor.outcome or survivor.outcome == "recorded":
                    survivor.outcome = other.outcome
            elif other.outcome and not survivor.outcome:
                survivor.outcome = other.outcome
            if other.plan_revision_index and not survivor.plan_revision_index:
                survivor.plan_revision_index = other.plan_revision_index
            if other.severity and not survivor.severity:
                survivor.severity = other.severity
            if other.drift_kind and not survivor.drift_kind:
                survivor.drift_kind = other.drift_kind
            if other.annotation_id and not survivor.annotation_id:
                survivor.annotation_id = other.annotation_id
        merged.append(survivor)

    merged.sort(key=lambda r: r.at)
    return merged


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
