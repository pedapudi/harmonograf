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
behind the ``legacy_plan_attribution_window_ms`` parameter so operators
with in-flight investigations can opt back in. Default: 0 (disabled).
The parameter is plumbed from ``ServerConfig.legacy_plan_attribution_window_ms``
/ CLI flag ``--legacy-plan-attribution-window-ms``. When enabled, a
merge via the fallback logs a WARNING so operators can diagnose
mis-attribution.

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
from dataclasses import dataclass, field
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
# by default. The old time-window path is preserved behind the
# ``legacy_plan_attribution_window_ms`` parameter on :func:`list_interventions`
# (plumbed from :class:`ServerConfig.legacy_plan_attribution_window_ms` /
# the ``--legacy-plan-attribution-window-ms`` CLI flag) so operators can
# opt back in during migration if they find the strict dedup too
# aggressive (e.g. live sessions in flight against a pre-#199 goldfive).
# Default: disabled (0.0).
# -----------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Intermediate record — the merge shape before outcome attribution.
# ---------------------------------------------------------------------------


@dataclass
class SeverityTransition:
    """One severity bump observed within a collapsed condition.

    Surfaced on :class:`InterventionRecord.severity_transitions` and
    translated to :class:`types_pb2.SeverityTransition` at the RPC
    boundary. ``frm`` (from) is the severity before the transition;
    ``to`` is the severity recorded on the observation that bumped.
    """

    frm: str
    to: str
    at: float


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
    # goldfive#318 / harmonograf I6: condition-collapse fields. The
    # aggregator's collapse pass (:func:`_collapse_by_condition_id`)
    # groups drift rows sharing a ``condition_id`` into one survivor and
    # populates the rest. Pre-#318 paths leave ``condition_id`` empty,
    # in which case the row passes through with ``count=1`` and the
    # frontend renders it as a single-emit row.
    condition_id: str = ""
    lifecycle: str = ""  # current lifecycle of the condition
    # ``prev_severity`` is set on per-emit drift rows before collapse so
    # the collapser can compute ``severity_transitions``. Cleared on the
    # surviving collapsed row (the bumps live in ``severity_transitions``).
    prev_severity: str = ""
    count: int = 1
    first_seen: float = 0.0
    last_seen: float = 0.0
    severity_transitions: list[SeverityTransition] = field(default_factory=list)
    # Internal: set of trigger_event_ids that the condition-collapse
    # absorbed from non-survivor observations. Used by
    # :func:`_merge_by_trigger_event_id` to fold plan rows that
    # strict-id-matched a non-survivor (typical: an early drift emit
    # triggered a refine; that drift's plan row's trigger_event_id is
    # the early drift's id, NOT the survivor's). Not surfaced on the
    # proto.
    absorbed_trigger_event_ids: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def list_interventions(
    session_id: str,
    *,
    store: Store,
    drifts_provider: Any,
    legacy_plan_attribution_window_ms: float = 0.0,
) -> list[InterventionRecord]:
    """Return chronologically ordered interventions for ``session_id``.

    ``drifts_provider`` is any object with a ``drifts_for_session`` method
    (the IngestPipeline in prod; a stub in tests). Kept as a duck-type
    boundary so tests can drive the aggregator without spinning a full
    ingest stack.

    ``legacy_plan_attribution_window_ms`` enables the opt-in Tier-2
    time-window fallback (see below). Default 0.0 disables. Plumbed
    from :class:`ServerConfig.legacy_plan_attribution_window_ms` /
    ``--legacy-plan-attribution-window-ms`` CLI flag.

    Dedup contract (harmonograf#99 rescope):

      * Tier 1 — always on. Plan-revision rows merge onto their source
        annotation / drift row when ``trigger_event_id`` matches the
        annotation id or drift id.
      * Tier 2 — opt-in via ``legacy_plan_attribution_window_ms``
        (default 0 / disabled). Time-window fallback: a plan row whose
        ``trigger_event_id`` matched nothing strictly merges onto a
        preceding user-control row of the same drift_kind within the
        configured window. Emits a WARNING log so operators can see
        what would not have merged under strict-id-only.

    Rows that don't match either tier surface as their own cards — never
    silently merged.
    """

    window_ms = max(0.0, float(legacy_plan_attribution_window_ms or 0.0))

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
    _attribute_outcomes(records, plans, legacy_window_ms=window_ms)
    # I6 (harmonograf-side fix for goldfive#318): collapse drift / user
    # rows that share a goldfive-minted ``condition_id`` into one
    # survivor before the trigger_event_id merge. This is the SERVER
    # mirror of the frontend's groupDriftConditions deriver — the gRPC
    # ``ListInterventions`` projection now carries already-collapsed
    # rows so historical fetches don't return one row per emit.
    # Backward-compat: rows with empty ``condition_id`` (pre-#318
    # events, annotation-only rows, plan rows) pass through untouched.
    records = _collapse_by_condition_id(records)
    records = _merge_by_trigger_event_id(records)
    # Tier 2 — opt-in legacy time-window merge for plan rows with NO
    # trigger_event_id (pre-#99 data or goldfive-bridge misconfigured).
    # Default off; WARNING logged on every match so operators notice.
    if window_ms > 0:
        records = _legacy_time_window_merge_orphan_plans(
            records, window_s=window_ms / 1000.0, window_ms=window_ms
        )
    return records


def _legacy_time_window_merge_orphan_plans(
    records: list[InterventionRecord], *, window_s: float, window_ms: float
) -> list[InterventionRecord]:
    """Opt-in legacy merge for plan rows without a ``trigger_event_id``.

    Fires only when the caller passed a positive
    ``legacy_plan_attribution_window_ms`` (via ServerConfig / CLI flag /
    direct kwarg). Walks the sorted record list; a plan row (no
    trigger_event_id, drift_kind set, outcome=plan_revised:*) folds onto
    the most recent preceding user / drift row within ``window_s`` whose
    drift_kind matches. WARNING logged on every match.

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
                "onto %s row. legacy_plan_attribution_window_ms=%.0f "
                "(ServerConfig field / --legacy-plan-attribution-window-ms). "
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
        # goldfive#318 / harmonograf I6: pull condition_id / lifecycle /
        # prev_severity off the drift ring record so the collapse pass
        # can group multiple emits of the same condition_id onto one
        # row. ``count``/``first_seen``/``last_seen`` start as the
        # per-observation values; the collapser overwrites them on the
        # survivor when group size > 1.
        condition_id = str(dr.get("condition_id") or "")
        lifecycle = str(dr.get("lifecycle") or "")
        prev_severity = str(dr.get("prev_severity") or "")
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
                condition_id=condition_id,
                lifecycle=lifecycle,
                prev_severity=prev_severity,
                count=1,
                first_seen=float(at),
                last_seen=float(at),
            )
        )
    return out


def _project_plans(
    plans: Iterable[TaskPlan],
) -> list[InterventionRecord]:
    """Project every plan revision (index > 0) to an InterventionRecord.

    Unlike the pre-#99 projector, this one does **not** try to suppress
    the plan row when a matching drift exists — the merge is strict-id
    (:func:`_merge_by_trigger_event_id`) and doesn't need a heuristic
    pre-filter. Plan rows that successfully merge disappear during merge;
    orphans (no matching annotation / drift) surface as their own cards.

    ``trigger_event_id`` comes off the persisted plan row (stamped by
    ingest.py::_on_plan_revised from ``PlanRevised.trigger_event_id``).

    Note: pre-Option-A this function also filtered plan rows whose
    ``trigger_event_id`` matched a known "synthetic" drift fabricated by
    ``Runner._install_revision``. After goldfive#271 Option A, no
    synthetic drift exists — turn-1 installs emit ``PlanRevised`` only
    (no ``DriftDetected``), so any orphan first-revision row simply
    surfaces with no upstream intervention to merge onto.
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
    records: list[InterventionRecord],
    plans: Iterable[TaskPlan],
    *,
    legacy_window_ms: float = 0.0,
) -> None:
    """Fill in ``outcome`` for drift and user rows.

    Runs before the trigger_event_id merge so merged-away plan rows can
    still donate their outcome to the survivor. Unlike the pre-#99
    attributor, the primary pairing is strict-id:

      * A drift/user row with a non-empty trigger_event_id is paired
        with the plan row whose trigger_event_id matches — that pairing
        yields ``plan_revised:rN``.
      * Only when strict-id yields nothing does the legacy time-window
        get consulted (and only when ``legacy_window_ms > 0``).
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

    window_ms = max(0.0, float(legacy_window_ms or 0.0))
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
                    "(trigger_event_id=%r). legacy_plan_attribution_"
                    "window_ms=%.0f (ServerConfig field / "
                    "--legacy-plan-attribution-window-ms). Investigate "
                    "why strict-id did not match (pre-#99 data? "
                    "goldfive < #199?).",
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


def _collapse_by_condition_id(
    records: list[InterventionRecord],
) -> list[InterventionRecord]:
    """Collapse drift rows sharing a ``condition_id`` into one survivor.

    Mirror of the frontend's ``groupDriftConditions`` deriver in
    ``frontend/src/lib/interventions.ts``. Server-side collapse fires on
    the historical fetch (``ListInterventions``) so a session with N
    observations of the same logical drift returns ONE row instead of N
    — fixing the iter_1 escalation report's I6 finding (13 drift events,
    9 condition_ids → server returned 18 rows instead of 9).

    Contract:

      * Only rows with a non-empty ``condition_id`` participate. Pre-#318
        events / annotation-only rows / plan rows have empty
        ``condition_id`` and pass through untouched.
      * Rows are grouped by ``condition_id`` regardless of source. In
        practice the source is always ``"drift"`` or ``"user"`` (only
        :func:`_project_drifts` populates ``condition_id`` today), but
        the predicate is condition-id-presence so a future projector
        that stamps a condition can join the same group.
      * Survivor = the latest observation in the group (by ``at``), so
        the row's ``severity``/``lifecycle``/``trigger_event_id``/
        ``outcome`` reflect the CURRENT state of the condition. This
        matches the frontend deriver's choice and the operator
        intuition: a drift's "row" should show what the condition is
        right now, not what it was when first observed.
      * ``count`` = group size. ``first_seen`` = earliest ``at``;
        ``last_seen`` = latest ``at``. ``at`` itself is updated to
        ``last_seen`` (already true since survivor is latest).
      * ``severity_transitions`` collects every observation in the
        group whose ``prev_severity != severity`` (and both non-empty).
        The list is wall-clock-ordered so consumers can render
        "WARNING → CRITICAL @ t" markers along the row's timeline.
      * ``outcome`` / ``plan_revision_index`` are donated from any
        non-survivor observation that already attributed an outcome
        (typical: an early emit triggered a refine; later emits are
        plain ``recorded``). This preserves the strict-id outcome
        attribution done in :func:`_attribute_outcomes` even though we
        keep the latest emit as the row's identity.

    The merge stays single-pass — no quadratic scan — by indexing
    groups in a dict keyed on ``condition_id`` and iterating the input
    once. Order is preserved for passthrough rows; collapsed rows take
    their position from the survivor's ``at``.
    """

    passthrough: list[InterventionRecord] = []
    groups: dict[str, list[InterventionRecord]] = {}
    for rec in records:
        if not rec.condition_id:
            passthrough.append(rec)
            continue
        groups.setdefault(rec.condition_id, []).append(rec)

    if not groups:
        return records

    survivors: list[InterventionRecord] = list(passthrough)
    for _cid, group in groups.items():
        if len(group) == 1:
            # Single-observation condition still gets count/first_seen/
            # last_seen populated (already are from _project_drifts) so
            # the proto layer can emit the metadata uniformly. Pass it
            # through.
            survivors.append(group[0])
            continue

        group.sort(key=lambda r: r.at)
        survivor = group[-1]
        first_at = group[0].at
        last_at = group[-1].at

        # Compute severity transitions from EVERY observation in the
        # group (not just the survivor). prev_severity is stamped per
        # emit by goldfive#318 so a transition lives on the observation
        # that bumped — we walk the sorted group and emit one entry per
        # observed bump.
        transitions: list[SeverityTransition] = []
        for obs in group:
            if obs.prev_severity and obs.severity and obs.prev_severity != obs.severity:
                transitions.append(
                    SeverityTransition(
                        frm=obs.prev_severity,
                        to=obs.severity,
                        at=obs.at,
                    )
                )

        # Donate outcome / plan_revision_index from any non-survivor
        # that already attributed (typical: an early emit triggered a
        # refine; the survivor — latest emit — is plain "recorded").
        # Survivor wins ties so the latest condition state is stable.
        if not survivor.outcome or survivor.outcome == "recorded":
            for obs in group:
                if obs is survivor:
                    continue
                if obs.outcome and obs.outcome != "recorded":
                    survivor.outcome = obs.outcome
                    if obs.plan_revision_index and not survivor.plan_revision_index:
                        survivor.plan_revision_index = obs.plan_revision_index
                    break

        survivor.count = len(group)
        survivor.first_seen = first_at
        survivor.last_seen = last_at
        # Clear prev_severity on the collapsed row — the bumps now live
        # in severity_transitions; leaving prev_severity set on a multi-
        # observation row would imply "the row itself is a transition"
        # which is misleading for a collapsed condition view.
        survivor.prev_severity = ""
        survivor.severity_transitions = transitions
        # Absorb every non-survivor's trigger_event_id so the strict-id
        # merge that runs next can fold any plan row that
        # strict-id-matched an earlier emit. Without this, a refine
        # triggered by emit #1 would surface as a phantom standalone
        # plan card next to the collapsed row (the plan's
        # trigger_event_id matches d1, but the survivor's is d2).
        for obs in group:
            if obs is survivor:
                continue
            if obs.trigger_event_id:
                survivor.absorbed_trigger_event_ids.add(obs.trigger_event_id)
        survivors.append(survivor)

    survivors.sort(key=lambda r: r.at)
    return survivors


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

    # Build an alias map: any trigger_event_id absorbed by a collapsed
    # condition (see :func:`_collapse_by_condition_id`) routes to the
    # survivor's own trigger_event_id. This lets a plan row that
    # strict-id-matched an early (now-discarded) emit fold onto the
    # collapsed survivor instead of surfacing as a phantom card.
    alias: dict[str, str] = {}
    for rec in records:
        if rec.absorbed_trigger_event_ids and rec.trigger_event_id:
            for ate in rec.absorbed_trigger_event_ids:
                # Multiple survivors mapping the same alias is a logic
                # bug (two conditions don't share an emit's id). First
                # writer wins; rare collisions surface as the original
                # row passing through.
                alias.setdefault(ate, rec.trigger_event_id)

    grouped: dict[str, list[InterventionRecord]] = {}
    passthrough: list[InterventionRecord] = []
    for rec in records:
        key = alias.get(rec.trigger_event_id, rec.trigger_event_id)
        if key:
            grouped.setdefault(key, []).append(rec)
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
            # No annotation row — prefer a condition-collapse drift row
            # (count > 1 OR condition_id set). After
            # :func:`_collapse_by_condition_id` runs, this is the row
            # carrying the per-observation breakdown that the proto
            # surfaces as ``count`` / ``severity_transitions`` /
            # ``first_seen`` / ``last_seen``. It would still match the
            # next preference branch (drift_kind set), but that branch
            # rejects rows whose plan_revision_index was already
            # populated by the collapse-time outcome donation. So we
            # promote condition-bearing drift rows ahead of that gate.
            for rec in group:
                if rec.drift_kind and (rec.count > 1 or rec.condition_id):
                    survivor = rec
                    break
        if survivor is None:
            # No condition-bearing drift — prefer the drift row (has
            # drift_kind set but no plan_revision_index), then fall back
            # to earliest.
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


def _ts_set(ts_msg: Any, value: float) -> None:
    """Helper: stamp a ``google.protobuf.Timestamp`` from a float.

    Skips when ``value`` is falsy (defaults to the zero timestamp), so
    unset fields stay unset rather than encoding 1970-01-01.
    """
    if not value:
        return
    ts_msg.seconds = int(value)
    ts_msg.nanos = int((value - int(value)) * 1e9)


def record_to_pb(rec: InterventionRecord, types_pb2_mod: Any) -> Any:
    """Translate an ``InterventionRecord`` to the generated proto message.

    ``types_pb2_mod`` is passed in rather than imported at module scope
    so the aggregator stays importable in contexts where the generated
    stubs are not on ``sys.path`` (e.g. unit tests that only exercise
    the pure aggregation logic).

    The condition-collapse fields (goldfive#318 / harmonograf I6) are
    additive: clients ignoring them see the legacy projection
    unchanged, while opt-in clients can prefer ``count`` /
    ``severity_transitions`` over the WatchSession streaming path's
    per-emit events.
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
        condition_id=rec.condition_id,
        # ``count`` defaults to 1 even on non-collapsed rows so consumers
        # can read it unconditionally (the per-emit projection from
        # _project_drifts already initializes count=1).
        count=int(rec.count or 1),
        lifecycle=rec.lifecycle,
    )
    _ts_set(pb.at, rec.at)
    _ts_set(pb.first_seen, rec.first_seen or rec.at)
    _ts_set(pb.last_seen, rec.last_seen or rec.at)
    for trans in rec.severity_transitions:
        st = pb.severity_transitions.add()
        # ``from`` is a Python keyword so the proto field is set via
        # the generated message's attribute name (``getattr`` to keep
        # the generator's mangling tolerant — generated code uses
        # ``setattr(msg, 'from', value)`` because the descriptor still
        # exposes the proto field name).
        setattr(st, "from", trans.frm)
        st.to = trans.to
        _ts_set(st.at, trans.at)
    return pb
