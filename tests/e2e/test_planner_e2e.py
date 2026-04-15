"""End-to-end planner pipeline: FakeLlmPlanner → Client.submit_plan → store.

Exercises the full planner path hermetically — no real LLM, no network
to a real model. We drive the ADK Runner with a scripted mock model so
the real harmonograf ADK plugin's ``before_run_callback`` fires
``_AdkState.maybe_run_planner`` → ``FakeLlmPlanner.generate`` → the
client's real ``submit_plan`` → the real gRPC ``StreamTelemetry``
ingress → the in-process server fixture's store. The plugin's own
``after_model_callback`` then triggers ``_AdkState._maybe_refine_plan``,
which calls ``FakeLlmPlanner.refine`` and upserts under the same
``plan_id``.

Assertions:
  (a) the terminal ``TaskPlan`` row lands in the store under the ADK
      session the adapter auto-created;
  (b) both ``generate`` and ``refine`` fired (steering-moment hook);
  (c) the upsert preserves ``plan_id`` and reflects the refine-mutated
      4-task set (regression guard for Client.submit_plan upsert);
  (d) at least one emitted span carries ``hgraf.task_id``, proving the
      adapter stamps planned spans with their task binding;
  (e) a live ``WatchSession`` stream opened via the real gRPC servicer
      delivers a ``task_plan`` ``SessionUpdate`` for the session.

The only stubs are (1) the planner itself — ``FakeLlmPlanner`` returns a
deterministic plan and a deterministic refined plan — and (2) the ADK
model, which is a minimal scripted ``BaseLlm`` that yields a single
``"done"`` text part. Everything else — Client, transport, server,
ingest pipeline, store, and WatchSession RPC — is real.
"""

from __future__ import annotations

import asyncio
import importlib.util
from typing import Any, Optional

import pytest

from harmonograf_client import Client, attach_adk
from harmonograf_client.planner import Plan, PlannerHelper, Task, TaskEdge


_ADK_AVAILABLE = importlib.util.find_spec("google.adk") is not None

pytestmark = pytest.mark.skipif(
    not _ADK_AVAILABLE,
    reason="google.adk is not installed — run `make install` to pick up the submodule",
)


# ---------------------------------------------------------------------------
# FakeLlmPlanner — deterministic, side-effect free
# ---------------------------------------------------------------------------


class FakeLlmPlanner(PlannerHelper):
    """Deterministic planner that returns a fixed 3-task plan on
    :meth:`generate` and a deterministically-mutated 4-task plan on
    :meth:`refine`. No LLM, no network.

    ``refine_calls`` and ``generate_calls`` expose call counts so tests
    can assert the hooks actually fired.
    """

    def __init__(self) -> None:
        self.generate_calls = 0
        self.refine_calls = 0

    def generate(
        self,
        *,
        request: str,
        available_agents: list[str],
        context: Optional[Any] = None,
    ) -> Optional[Plan]:
        self.generate_calls += 1
        return Plan(
            summary="research → draft → review",
            tasks=[
                Task(id="t1", title="Research", assignee_agent_id="worker_agent"),
                Task(id="t2", title="Draft", assignee_agent_id="worker_agent"),
                Task(id="t3", title="Review", assignee_agent_id="worker_agent"),
            ],
            edges=[
                TaskEdge(from_task_id="t1", to_task_id="t2"),
                TaskEdge(from_task_id="t2", to_task_id="t3"),
            ],
        )

    def refine(self, plan: Plan, event: Any) -> Optional[Plan]:
        self.refine_calls += 1
        return Plan(
            summary="refined after tool_end",
            tasks=[
                Task(id="t1", title="Research", assignee_agent_id="worker_agent"),
                Task(id="t2", title="Draft (revised)", assignee_agent_id="worker_agent"),
                Task(id="t3", title="Review", assignee_agent_id="worker_agent"),
                Task(id="t4", title="Follow-up", assignee_agent_id="worker_agent"),
            ],
            edges=[
                TaskEdge(from_task_id="t1", to_task_id="t2"),
                TaskEdge(from_task_id="t2", to_task_id="t3"),
                TaskEdge(from_task_id="t3", to_task_id="t4"),
            ],
        )


# ---------------------------------------------------------------------------
# Minimal scripted ADK runner
# ---------------------------------------------------------------------------


def _build_scripted_runner() -> Any:
    import contextlib
    from typing import AsyncGenerator

    from google.adk.agents.llm_agent import LlmAgent
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import InMemoryRunner
    from google.genai import types as genai_types

    def _text(text: str) -> LlmResponse:
        return LlmResponse(
            content=genai_types.Content(
                role="model", parts=[genai_types.Part(text=text)]
            )
        )

    class _ScriptedModel(BaseLlm):  # type: ignore[misc]
        model: str = "scripted-planner"
        cursor: int = -1

        @classmethod
        def supported_models(cls) -> list[str]:
            return ["scripted-planner"]

        async def generate_content_async(
            self, llm_request, stream: bool = False
        ) -> "AsyncGenerator[LlmResponse, None]":
            self.cursor += 1
            yield _text("done")

        @contextlib.asynccontextmanager
        async def connect(self, llm_request):
            yield None

    worker_agent = LlmAgent(
        name="worker_agent",
        model=_ScriptedModel(),
        instruction="Say 'done'.",
        description="Worker.",
        tools=[],
    )
    return InMemoryRunner(agent=worker_agent, app_name="planner_e2e")


async def _drive_invocation(
    runner: Any, user_text: str, *, observer_hook: Any = None
) -> None:
    from google.genai import types as genai_types

    session = await runner.session_service.create_session(
        app_name=runner.app_name, user_id="e2e_user"
    )
    event_count = 0
    async for _event in runner.run_async(
        user_id="e2e_user",
        session_id=session.id,
        new_message=genai_types.Content(
            role="user", parts=[genai_types.Part(text=user_text)]
        ),
    ):
        event_count += 1
        # Fire the observer hook once mid-invocation so drift-driven
        # refine runs while the PlanState is still live in
        # ``_active_plan_by_session`` (it's cleared at invocation end).
        if observer_hook is not None and event_count == 1:
            observer_hook()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _wait_for(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def _wait_for_async(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def _resolve_adk_session_id(store: Any) -> str:
    sessions = await store.list_sessions()
    for s in sessions:
        if s.id.startswith("adk_"):
            return s.id
    return ""


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPlannerEndToEnd:
    async def test_fake_planner_full_pipeline(
        self, harmonograf_server, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HARMONOGRAF_HOME", str(tmp_path))

        planner = FakeLlmPlanner()

        client = Client(
            name="planner_e2e",
            server_addr=harmonograf_server["addr"],
            framework="ADK",
            capabilities=["HUMAN_IN_LOOP", "STEERING"],
        )

        runner = _build_scripted_runner()
        handle = attach_adk(runner, client, planner=planner, refine_on_events=True)
        try:
            from harmonograf_client.adk import DriftReason

            state = handle.plugin._hg_state  # type: ignore[attr-defined]

            def _observer_hook() -> None:
                # The observer in HarmonografAgent calls
                # refine_plan_on_drift after detecting drift. This test
                # drives the raw worker_agent (no HarmonografAgent
                # wrapper), so we simulate the observer by firing
                # refine mid-invocation while PlanState is still live.
                hsession_ids = list(state._active_plan_by_session.keys())
                if not hsession_ids:
                    return
                state.refine_plan_on_drift(
                    hsession_ids[0],
                    DriftReason(
                        kind="tool_call_wrong_agent",
                        detail="e2e driven refine",
                    ),
                    current_task=None,
                )

            await _drive_invocation(
                runner, "Do the thing", observer_hook=_observer_hook
            )

            assert await _wait_for(
                lambda: client.session_id != "" and client._transport.connected,
                timeout=5.0,
            ), "transport never connected"

            store = harmonograf_server["store"]

            # (a) + (c): a TaskPlan row lands in the store, and the real
            # ADK ``model_end`` steering moment triggers
            # ``FakeLlmPlanner.refine`` which upserts the same plan_id
            # with the 4-task mutated set. We assert on the terminal
            # state: one plan, 4 tasks, with the refine-only "t4" task.
            async def _have_refined_plan() -> bool:
                sid = await _resolve_adk_session_id(store)
                if not sid:
                    return False
                plans = await store.list_task_plans_for_session(sid)
                return any(
                    len(p.tasks) == 4 and any(t.id == "t4" for t in p.tasks)
                    for p in plans
                )

            assert await _wait_for_async(_have_refined_plan, timeout=10.0), (
                f"refined FakeLlmPlanner plan never reached the store "
                f"(generate_calls={planner.generate_calls}, "
                f"refine_calls={planner.refine_calls})"
            )

            assert planner.generate_calls >= 1, (
                "FakeLlmPlanner.generate was never invoked"
            )
            assert planner.refine_calls >= 1, (
                "FakeLlmPlanner.refine was never invoked — the model_end "
                "steering hook did not fire"
            )

            session_id = await _resolve_adk_session_id(store)
            plans = await store.list_task_plans_for_session(session_id)
            assert len(plans) == 1, (
                f"expected upsert (same plan_id), got {len(plans)} distinct plans: "
                f"{[p.id for p in plans]}"
            )
            plan_row = plans[0]
            plan_id = plan_row.id
            assert [t.id for t in plan_row.tasks] == ["t1", "t2", "t3", "t4"]
            assert plan_row.tasks[1].title == "Draft (revised)"
            assert plan_row.summary == "refined after tool_end"

            # (d) at least one emitted span carries ``hgraf.task_id``,
            # proving the adapter stamps planned spans on their way out.
            # (Task→span binding in the server is best-effort because
            # the refine upsert can land between span_start and the
            # server's bind step — we check the stamp directly.)
            spans = await store.get_spans(session_id)
            stamped = [
                s
                for s in spans
                if (getattr(s, "attributes", None) or {}).get("hgraf.task_id")
            ]
            assert stamped, (
                "no span in the session carries hgraf.task_id — "
                "adapter did not stamp planned spans"
            )

            # (b) open a WatchSession stream against the real server via
            # gRPC, replay it, and assert a task_plan SessionUpdate for
            # our plan_id appears in the initial burst. This exercises
            # the real frontend RPC path end-to-end.
            import grpc
            from harmonograf_server.pb.harmonograf.v1 import (
                frontend_pb2,
                service_pb2_grpc,
            )

            channel = grpc.aio.insecure_channel(harmonograf_server["addr"])
            try:
                stub = service_pb2_grpc.HarmonografStub(channel)
                call = stub.WatchSession(
                    frontend_pb2.WatchSessionRequest(session_id=session_id)
                )
                saw_plan = False
                saw_plan_id = ""
                # The initial burst is bounded; give it 5s to flush.
                try:
                    async def _consume() -> None:
                        nonlocal saw_plan, saw_plan_id
                        async for upd in call:
                            which = upd.WhichOneof("kind")
                            if which == "task_plan":
                                saw_plan = True
                                saw_plan_id = upd.task_plan.id
                            if which == "burst_complete":
                                return

                    await asyncio.wait_for(_consume(), timeout=5.0)
                except asyncio.TimeoutError:
                    call.cancel()
                assert saw_plan, (
                    "WatchSession initial burst did not include a task_plan"
                )
                assert saw_plan_id == plan_id, (
                    f"task_plan SessionUpdate plan_id mismatch: "
                    f"saw {saw_plan_id!r}, expected {plan_id!r}"
                )
            finally:
                await channel.close()
        finally:
            handle.detach()
            client.shutdown(flush_timeout=5.0)
