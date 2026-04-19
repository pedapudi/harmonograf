"""Goldfive orchestration + harmonograf observability — for comparison.

The *other* path. If you want plans, tasks, and drift detection in the
harmonograf UI (Tasks panel populated, drift banners, plan-scoped
telemetry), this is the pattern: use goldfive.wrap() to produce a
Runner, then attach harmonograf_client.observe() to ship its events to
the harmonograf server.

This example is the counterpoint to `spans_only.py` and
`adk_telemetry.py` — it is only useful if you have installed the
optional orchestration extra::

    uv sync --extra orchestration

Run:
    export OPENAI_API_KEY=...
    export HARMONOGRAF_SERVER=127.0.0.1:7531
    uv run --extra orchestration python examples/standalone_observability/with_orchestration.py

Contrast with `spans_only.py`, which does NOT import goldfive and which
only populates the Gantt (Tasks panel stays empty).
"""

from __future__ import annotations

import asyncio
import os

import goldfive
from goldfive import (
    CallableAdapter,
    InvocationResult,
    PassthroughGoalDeriver,
    Plan,
    ReportingToolSpec,
    Runner,
    SequentialExecutor,
    Session,
    StaticPlanner,
    Task,
    TaskEdge,
)

import harmonograf_client


def build_plan() -> Plan:
    return Plan(
        id="with-orchestration",
        run_id="",
        goal_ids=["g1"],
        tasks=[
            Task(id="research", title="Gather notes", assignee_agent_id="worker"),
            Task(id="draft", title="Draft summary", assignee_agent_id="worker"),
            Task(id="review", title="Review draft", assignee_agent_id="worker"),
        ],
        edges=[
            TaskEdge(from_task_id="research", to_task_id="draft"),
            TaskEdge(from_task_id="draft", to_task_id="review"),
        ],
        summary="Research, draft, review.",
    )


async def worker(
    task: Task,
    session: Session,
    tools: list[ReportingToolSpec],
) -> InvocationResult:
    _ = session, tools
    replies = {
        "research": "Collected three bullet points.",
        "draft": "Drafted a two-paragraph summary.",
        "review": "Reviewed — looks good.",
    }
    return InvocationResult(task_id=task.id, text=replies.get(task.id, "(noop)"))


async def main() -> None:
    server = os.environ.get("HARMONOGRAF_SERVER", "127.0.0.1:7531")
    plan = build_plan()
    runner = Runner(
        agent=CallableAdapter(worker, name="worker"),
        planner=StaticPlanner(plan=plan),
        executor=SequentialExecutor(),
        goal_deriver=PassthroughGoalDeriver(),
        sinks=[],
    )
    # Attach a HarmonografSink via the observe() convenience helper. This
    # is a pure observability hook — it does not touch planning, steering,
    # or execution. See docs/goldfive-integration.md.
    runner = harmonograf_client.observe(
        runner,
        name="goldfive-orchestrated-demo",
        server_addr=server,
    )
    outcome = await runner.run("Research, draft, and review a short summary.")
    print(f"done: {outcome.final_task_id}")


if __name__ == "__main__":
    asyncio.run(main())
