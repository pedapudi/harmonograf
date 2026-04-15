"""Tests for Client.submit_plan / submit_task_status_update.

Uses a stub Transport that captures notify() calls and exposes the
event ring buffer so we can assert the envelope shape and the
TelemetryUp serialisation without standing up a grpc server.
"""

from __future__ import annotations

from typing import Any

import pytest

from harmonograf_client import Client, Plan, Task, TaskEdge
from harmonograf_client.buffer import EnvelopeKind


class _StubTransport:
    def __init__(self, **kwargs: Any) -> None:
        self._events = kwargs["events"]
        self._payloads = kwargs["payloads"]
        self._agent_id = kwargs["agent_id"]
        self._session_id = kwargs["session_id"]
        self.notify_count = 0
        self.assigned_session_id = self._session_id
        self.assigned_stream_id = ""
        self.started = False

    def start(self) -> None:
        self.started = True

    def notify(self) -> None:
        self.notify_count += 1

    def enqueue_payload(self, digest: str, data: bytes, mime: str) -> bool:
        return True

    def register_control_handler(self, kind: str, cb: Any) -> None:
        pass

    def shutdown(self, timeout: float = 5.0) -> None:
        pass


@pytest.fixture
def client(tmp_path) -> Client:
    c = Client(
        name="planner-test",
        session_id="sess_1",
        agent_id="planner-test-agent",
        identity_root=str(tmp_path),
        _transport_factory=_StubTransport,
        autostart=False,
    )
    yield c


class TestSubmitPlan:
    def test_pushes_task_plan_envelope(self, client: Client):
        plan = Plan(
            tasks=[
                Task(id="t1", title="A", assignee_agent_id="research"),
                Task(id="t2", title="B", assignee_agent_id="writer"),
            ],
            edges=[TaskEdge(from_task_id="t1", to_task_id="t2")],
            summary="do things",
        )
        pid = client.submit_plan(plan, invocation_span_id="inv_span_1")
        assert pid
        # Drain the buffer and inspect.
        envs = list(client._events.drain())
        assert len(envs) == 1
        env = envs[0]
        assert env.kind is EnvelopeKind.TASK_PLAN
        tp = env.payload
        assert tp.id == pid
        assert tp.invocation_span_id == "inv_span_1"
        assert tp.session_id == "sess_1"
        assert tp.summary == "do things"
        assert [t.id for t in tp.tasks] == ["t1", "t2"]
        assert [t.title for t in tp.tasks] == ["A", "B"]
        assert [t.assignee_agent_id for t in tp.tasks] == ["research", "writer"]
        assert len(tp.edges) == 1
        assert tp.edges[0].from_task_id == "t1"
        assert tp.edges[0].to_task_id == "t2"
        # Notify was called.
        assert client._transport.notify_count >= 1

    def test_generates_plan_id_if_missing(self, client: Client):
        plan = Plan(tasks=[Task(id="t1", title="A")], edges=[])
        pid = client.submit_plan(plan)
        assert pid and isinstance(pid, str)
        envs = list(client._events.drain())
        assert envs[0].payload.id == pid

    def test_envelope_serialises_to_telemetry_up(self, client: Client):
        """The transport send loop must be able to wrap a TASK_PLAN
        envelope into a TelemetryUp(task_plan=...) message."""
        plan = Plan(tasks=[Task(id="t1", title="A")], edges=[])
        client.submit_plan(plan, plan_id="plan_abc")
        envs = list(client._events.drain())
        # Simulate what Transport._envelope_to_up does.
        from harmonograf_client.pb import telemetry_pb2
        from harmonograf_client.transport import Transport

        up = Transport._envelope_to_up(client._transport, envs[0], telemetry_pb2)  # type: ignore[arg-type]
        assert up is not None
        assert up.WhichOneof("msg") == "task_plan"
        assert up.task_plan.id == "plan_abc"


class TestSubmitTaskStatusUpdate:
    def test_pushes_status_update_envelope(self, client: Client):
        client.submit_task_status_update(
            "plan_1", "t1", "COMPLETED", bound_span_id="span_x"
        )
        envs = list(client._events.drain())
        assert len(envs) == 1
        env = envs[0]
        assert env.kind is EnvelopeKind.TASK_STATUS_UPDATE
        upd = env.payload
        assert upd.plan_id == "plan_1"
        assert upd.task_id == "t1"
        assert upd.bound_span_id == "span_x"
        # Verify serialisation
        from harmonograf_client.pb import telemetry_pb2
        from harmonograf_client.transport import Transport

        up = Transport._envelope_to_up(client._transport, env, telemetry_pb2)  # type: ignore[arg-type]
        assert up.WhichOneof("msg") == "task_status_update"
        assert up.task_status_update.plan_id == "plan_1"
