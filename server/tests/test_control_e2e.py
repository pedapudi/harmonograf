"""End-to-end: control events delivered via SubscribeControl and acked
back on the StreamTelemetry upstream, exactly as doc 01 §4.1 specifies."""

from __future__ import annotations

import asyncio

import grpc
import pytest
import pytest_asyncio

from harmonograf_server.bus import SessionBus
from harmonograf_server.control_router import ControlRouter, DeliveryResult
from harmonograf_server.ingest import IngestPipeline
from harmonograf_server.pb import (
    control_pb2,
    service_pb2_grpc,
    telemetry_pb2,
    types_pb2,
)
from harmonograf_server.rpc.telemetry import TelemetryServicer
from harmonograf_server.storage import make_store


@pytest_asyncio.fixture
async def running_server():
    store = make_store("memory")
    await store.start()
    bus = SessionBus()
    router = ControlRouter()
    ingest = IngestPipeline(store, bus, control_sink=router)
    servicer = TelemetryServicer(ingest, router=router)

    server = grpc.aio.server()
    service_pb2_grpc.add_HarmonografServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield {
            "server": server,
            "port": port,
            "router": router,
            "ingest": ingest,
            "bus": bus,
            "store": store,
        }
    finally:
        await server.stop(grace=0.5)
        await store.close()


@pytest_asyncio.fixture
async def channel(running_server):
    ch = grpc.aio.insecure_channel(f"127.0.0.1:{running_server['port']}")
    try:
        yield ch
    finally:
        await ch.close()


def _make_hello(agent_id: str, session_id: str = "") -> telemetry_pb2.TelemetryUp:
    return telemetry_pb2.TelemetryUp(
        hello=telemetry_pb2.Hello(
            agent_id=agent_id,
            session_id=session_id,
            name=agent_id,
            framework=types_pb2.FRAMEWORK_ADK,
        )
    )


async def test_control_roundtrip_via_telemetry_ack(running_server, channel):
    stub = service_pb2_grpc.HarmonografStub(channel)
    router = running_server["router"]

    # 1. Open telemetry stream; receive Welcome.
    telemetry_q: asyncio.Queue = asyncio.Queue()
    await telemetry_q.put(_make_hello("agent-ctrl", session_id="sess_ctrl"))

    async def telemetry_requests():
        while True:
            item = await telemetry_q.get()
            if item is None:
                return
            yield item

    telemetry_call = stub.StreamTelemetry(telemetry_requests())
    down = await telemetry_call.read()
    assert down.WhichOneof("msg") == "welcome"
    stream_id = down.welcome.assigned_stream_id

    # 2. Open SubscribeControl with that stream_id.
    control_call = stub.SubscribeControl(
        control_pb2.SubscribeControlRequest(
            session_id="sess_ctrl", agent_id="agent-ctrl", stream_id=stream_id
        )
    )

    # Give the server a moment to register the subscription.
    for _ in range(10):
        if router.live_stream_ids("agent-ctrl"):
            break
        await asyncio.sleep(0.02)
    assert router.live_stream_ids("agent-ctrl") == [stream_id]

    # 3. Kick off deliver; fetch the event; ack it back via telemetry.
    deliver_task = asyncio.create_task(
        router.deliver(
            session_id="sess_ctrl",
            agent_id="agent-ctrl",
            kind=types_pb2.CONTROL_KIND_PAUSE,
            timeout_s=2.0,
        )
    )

    event = await asyncio.wait_for(control_call.read(), timeout=1)
    assert event.kind == types_pb2.CONTROL_KIND_PAUSE
    assert event.target.agent_id == "agent-ctrl"

    # Ack rides TELEMETRY upstream, not the control stream.
    await telemetry_q.put(
        telemetry_pb2.TelemetryUp(
            control_ack=types_pb2.ControlAck(
                control_id=event.id,
                result=types_pb2.CONTROL_ACK_RESULT_SUCCESS,
                detail="paused",
            )
        )
    )

    outcome = await deliver_task
    assert outcome.result == DeliveryResult.SUCCESS
    assert outcome.acks[0].stream_id == stream_id
    assert outcome.acks[0].detail == "paused"

    # Cleanup
    control_call.cancel()
    await telemetry_q.put(None)
    telemetry_call.cancel()
