"""Plan state invariant validator.

This is a safety net that runs after each walker turn to catch
compounding state bugs that slip past the per-write guards in
``adk.py``. The individual guards (``_set_task_status``, the monotonic
status machine, the refine preservation logic) make illegal *single*
transitions structurally impossible; this module checks the *aggregate*
state of the plan is still coherent after the dust settles.

The public entry point is :func:`check_plan_state`, which returns a
list of :class:`InvariantViolation` describing any inconsistencies. The
validator is read-only: it never mutates ``state``, never touches the
client, and runs entirely in memory.

The module has no runtime dependency on ``adk.py`` — it talks to
``state`` and the plan via duck-typing so importing it doesn't pull in
ADK. A stateful :class:`InvariantChecker` is offered for callers that
want cross-turn history tracking (currently: the transition history
used by the monotonic-state check); the free function uses a private
module-level default checker so the spec signature
``check_plan_state(state, hsession_id)`` works out of the box.

Usage from the walker::

    violations = check_plan_state(state, hsession_id)
    for v in violations:
        log.log(v.log_level(), "invariant %s: %s", v.rule, v.detail)
    assert _not_in_tests() or not any(v.severity == "error" for v in violations)
"""

from __future__ import annotations

import dataclasses
import logging
import os
from typing import Any, Optional

log = logging.getLogger("harmonograf_client.invariants")


_VALID_STATUSES: frozenset[str] = frozenset(
    {"PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"}
)
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"COMPLETED", "FAILED", "CANCELLED"}
)
# Status A is allowed to transition to status B iff B is in
# ``_ALLOWED_TRANSITIONS[A]``. Terminal states have no outgoing edges.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING": frozenset({"PENDING", "RUNNING", "CANCELLED", "FAILED"}),
    "RUNNING": frozenset({"RUNNING", "COMPLETED", "FAILED", "CANCELLED"}),
    "COMPLETED": frozenset({"COMPLETED"}),
    "FAILED": frozenset({"FAILED"}),
    "CANCELLED": frozenset({"CANCELLED"}),
}


@dataclasses.dataclass
class InvariantViolation:
    """A single invariant check failure.

    :attr rule: short tag for the invariant that tripped (e.g.
        ``"monotonic_state"``). Stable across runs so telemetry can
        aggregate by rule.
    :attr severity: ``"warning"`` or ``"error"``. Errors fail CI /
        pytest assertions; warnings log loudly but do not stop the run.
    :attr detail: human-readable one-liner, safe to log.
    """

    rule: str
    severity: str
    detail: str

    def log_level(self) -> int:
        return logging.ERROR if self.severity == "error" else logging.WARNING


class InvariantChecker:
    """Stateful validator that keeps a transition history across calls.

    The free :func:`check_plan_state` uses a module-level default
    instance; tests that want to exercise multiple independent runs
    should construct their own ``InvariantChecker``.
    """

    def __init__(self) -> None:
        # (hsession_id, task_id) -> last observed status. Used to detect
        # illegal edges that a caller might sneak in by mutating
        # ``task.status`` directly instead of going through
        # ``_set_task_status``.
        self._last_status: dict[tuple[str, str], str] = {}

    def check(
        self, state: Any, hsession_id: str
    ) -> list[InvariantViolation]:
        """Run every invariant check against the plan snapshot for
        ``hsession_id``. Returns the (possibly empty) list of violations
        in stable order so diffs are easy to read.
        """
        violations: list[InvariantViolation] = []
        plan_state = self._resolve_plan_state(state, hsession_id)
        if plan_state is None:
            return violations

        tasks = dict(getattr(plan_state, "tasks", {}) or {})
        edges = list(getattr(plan_state, "edges", []) or [])
        known_agents = list(getattr(plan_state, "available_agents", []) or [])

        self._check_monotonic(hsession_id, tasks, violations)
        self._check_dependency_consistency(tasks, edges, violations)
        self._check_assignee_validity(tasks, known_agents, violations)
        self._check_plan_id_uniqueness(state, hsession_id, violations)
        self._check_forced_task(state, hsession_id, tasks, violations)
        self._check_task_results_keys(state, tasks, violations)
        self._check_revision_history_monotone(plan_state, violations)
        self._check_span_bindings(state, tasks, violations)

        return violations

    # ------------------------------------------------------------------
    # individual invariants
    # ------------------------------------------------------------------

    def _check_monotonic(
        self,
        hsession_id: str,
        tasks: dict[str, Any],
        out: list[InvariantViolation],
    ) -> None:
        for tid, task in tasks.items():
            cur = (getattr(task, "status", "") or "PENDING")
            if cur not in _VALID_STATUSES:
                out.append(
                    InvariantViolation(
                        rule="monotonic_state",
                        severity="error",
                        detail=f"task {tid} has unknown status {cur!r}",
                    )
                )
                continue
            key = (hsession_id, tid)
            prev = self._last_status.get(key)
            if prev is not None and cur not in _ALLOWED_TRANSITIONS.get(
                prev, frozenset()
            ):
                out.append(
                    InvariantViolation(
                        rule="monotonic_state",
                        severity="error",
                        detail=(
                            f"task {tid} illegal transition "
                            f"{prev} → {cur}"
                        ),
                    )
                )
            self._last_status[key] = cur

    @staticmethod
    def _check_dependency_consistency(
        tasks: dict[str, Any],
        edges: list[Any],
        out: list[InvariantViolation],
    ) -> None:
        for e in edges:
            src = str(getattr(e, "from_task_id", "") or "")
            dst = str(getattr(e, "to_task_id", "") or "")
            if not src or not dst:
                continue
            src_task = tasks.get(src)
            dst_task = tasks.get(dst)
            if dst_task is None or src_task is None:
                continue
            dst_status = (getattr(dst_task, "status", "") or "PENDING")
            src_status = (getattr(src_task, "status", "") or "PENDING")
            if dst_status == "COMPLETED" and src_status == "PENDING":
                out.append(
                    InvariantViolation(
                        rule="dependency_consistency",
                        severity="warning",
                        detail=(
                            f"task {dst} is COMPLETED but dependency "
                            f"{src} is still PENDING"
                        ),
                    )
                )

    @staticmethod
    def _check_assignee_validity(
        tasks: dict[str, Any],
        known_agents: list[str],
        out: list[InvariantViolation],
    ) -> None:
        if not known_agents:
            return
        known = set(known_agents)
        for tid, task in tasks.items():
            assignee = str(getattr(task, "assignee_agent_id", "") or "")
            if assignee and assignee not in known:
                out.append(
                    InvariantViolation(
                        rule="assignee_validity",
                        severity="warning",
                        detail=(
                            f"task {tid} assignee {assignee!r} not in "
                            f"known_agents {sorted(known)!r}"
                        ),
                    )
                )

    @staticmethod
    def _check_plan_id_uniqueness(
        state: Any,
        hsession_id: str,
        out: list[InvariantViolation],
    ) -> None:
        by_session = getattr(state, "_active_plan_by_session", None)
        if not isinstance(by_session, dict):
            return
        seen_plan_ids: dict[str, str] = {}
        for sess, ps in by_session.items():
            pid = str(getattr(ps, "plan_id", "") or "")
            if not pid:
                continue
            prior = seen_plan_ids.get(pid)
            if prior is not None and prior != sess:
                out.append(
                    InvariantViolation(
                        rule="plan_id_uniqueness",
                        severity="error",
                        detail=(
                            f"plan_id {pid!r} appears on sessions "
                            f"{prior!r} and {sess!r}"
                        ),
                    )
                )
            else:
                seen_plan_ids[pid] = sess

    @staticmethod
    def _check_forced_task(
        state: Any,
        hsession_id: str,
        tasks: dict[str, Any],
        out: list[InvariantViolation],
    ) -> None:
        forced = str(getattr(state, "_forced_current_task_id", "") or "")
        if not forced:
            return
        tracked = tasks.get(forced)
        if tracked is None:
            out.append(
                InvariantViolation(
                    rule="forced_task_consistency",
                    severity="error",
                    detail=(
                        f"forced task {forced!r} not present in active plan "
                        f"for session {hsession_id!r}"
                    ),
                )
            )
            return
        status = (getattr(tracked, "status", "") or "PENDING")
        if status in _TERMINAL_STATUSES:
            out.append(
                InvariantViolation(
                    rule="forced_task_consistency",
                    severity="error",
                    detail=(
                        f"forced task {forced!r} is terminal ({status})"
                    ),
                )
            )

    @staticmethod
    def _check_task_results_keys(
        state: Any,
        tasks: dict[str, Any],
        out: list[InvariantViolation],
    ) -> None:
        results = getattr(state, "_task_results", None)
        if not isinstance(results, dict):
            return
        for tid in results:
            if tid not in tasks:
                out.append(
                    InvariantViolation(
                        rule="task_results_keys",
                        severity="warning",
                        detail=(
                            f"_task_results has entry {tid!r} with no "
                            f"matching task in the active plan"
                        ),
                    )
                )

    @staticmethod
    def _check_revision_history_monotone(
        plan_state: Any, out: list[InvariantViolation]
    ) -> None:
        revisions = list(getattr(plan_state, "revisions", []) or [])
        prev_ts: Optional[float] = None
        for idx, rev in enumerate(revisions):
            ts_raw = rev.get("revised_at") if isinstance(rev, dict) else None
            try:
                ts = float(ts_raw) if ts_raw is not None else None
            except (TypeError, ValueError):
                ts = None
            if ts is None:
                continue
            if prev_ts is not None and ts < prev_ts:
                out.append(
                    InvariantViolation(
                        rule="revision_history_monotone",
                        severity="warning",
                        detail=(
                            f"plan revision {idx} timestamp {ts} is "
                            f"earlier than previous {prev_ts}"
                        ),
                    )
                )
            prev_ts = ts

    @staticmethod
    def _check_span_bindings(
        state: Any,
        tasks: dict[str, Any],
        out: list[InvariantViolation],
    ) -> None:
        span_to_task = getattr(state, "_span_to_task", None)
        if not isinstance(span_to_task, dict):
            return
        for span_id, tid in span_to_task.items():
            if not tid:
                continue
            if tid not in tasks:
                out.append(
                    InvariantViolation(
                        rule="span_bindings",
                        severity="warning",
                        detail=(
                            f"span {span_id!r} bound to task {tid!r} "
                            f"which is not in the active plan"
                        ),
                    )
                )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_plan_state(state: Any, hsession_id: str) -> Any:
        if state is None or not hsession_id:
            return None
        by_session = getattr(state, "_active_plan_by_session", None)
        if not isinstance(by_session, dict):
            return None
        return by_session.get(hsession_id)


# ---------------------------------------------------------------------------
# Module-level convenience API
# ---------------------------------------------------------------------------


_DEFAULT_CHECKER = InvariantChecker()


def check_plan_state(
    state: Any, hsession_id: str
) -> list[InvariantViolation]:
    """Run all invariant checks against the active plan for
    ``hsession_id``. Returns an empty list if everything is consistent.

    Uses a process-wide default :class:`InvariantChecker` so the
    monotonic-state check can observe transitions across turns of the
    same run. Tests that need independent history should construct
    their own checker.
    """
    return _DEFAULT_CHECKER.check(state, hsession_id)


def reset_default_checker() -> None:
    """Drop the default checker's transition history. Primarily useful
    in tests that want a clean slate between scenarios.
    """
    _DEFAULT_CHECKER._last_status.clear()


# ---------------------------------------------------------------------------
# Runtime integration
# ---------------------------------------------------------------------------


def in_test_mode() -> bool:
    """True when we're running under pytest, so the walker can raise
    on error-level violations instead of just logging them.
    """
    return "PYTEST_CURRENT_TEST" in os.environ


def enforce(
    violations: list[InvariantViolation], *, context: str = ""
) -> None:
    """Log every violation at its declared level. If any violation is
    an error AND we're running under pytest, raise ``AssertionError``
    so the test fails loudly instead of limping on with bad state.
    """
    if not violations:
        return
    for v in violations:
        log.log(
            v.log_level(),
            "invariant %s %s%s: %s",
            v.rule,
            f"({context}) " if context else "",
            v.severity,
            v.detail,
        )
    if in_test_mode():
        errors = [v for v in violations if v.severity == "error"]
        if errors:
            summary = "; ".join(f"{v.rule}: {v.detail}" for v in errors)
            raise AssertionError(
                f"plan state invariant violation{f' ({context})' if context else ''}: {summary}"
            )
