"""Lightweight protocol counters for harmonograf's ADK callback path.

These exist so we can observe callback-driven protocol behavior in
production without paying any meaningful cost on the hot path. Every
recorded event is a single ``defaultdict[str, int]`` increment or a
plain ``+= 1`` on an int — no I/O, no allocations, no locks. Reads
(``format_protocol_metrics``) are diagnostic-only and may be slow.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class ProtocolMetrics:
    callbacks_fired: dict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    task_transitions: dict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    refine_fires: dict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    state_state_reads: int = 0
    state_state_writes: int = 0
    reporting_tools_invoked: dict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    invariant_violations: int = 0
    walker_iterations: int = 0


def format_protocol_metrics(metrics: ProtocolMetrics) -> str:
    lines: list[str] = ["protocol metrics:"]

    def _fmt_dict(name: str, d: dict[str, int]) -> None:
        if not d:
            lines.append(f"  {name}: (none)")
            return
        items = sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))
        rendered = ", ".join(f"{k}={v}" for k, v in items)
        lines.append(f"  {name}: {rendered}")

    _fmt_dict("callbacks_fired", metrics.callbacks_fired)
    _fmt_dict("task_transitions", metrics.task_transitions)
    _fmt_dict("refine_fires", metrics.refine_fires)
    _fmt_dict("reporting_tools_invoked", metrics.reporting_tools_invoked)
    lines.append(f"  state_state_reads: {metrics.state_state_reads}")
    lines.append(f"  state_state_writes: {metrics.state_state_writes}")
    lines.append(f"  walker_iterations: {metrics.walker_iterations}")
    lines.append(f"  invariant_violations: {metrics.invariant_violations}")
    return "\n".join(lines)
