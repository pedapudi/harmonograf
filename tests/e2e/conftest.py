"""Shared fixtures for the harmonograf end-to-end suite.

The E2E tests boot a real gRPC server in-process with an InMemoryStore,
attach a real :class:`Client` to it, and drive an ADK runner backed by
``MockModel`` so the test is deterministic and offline. This proves the
full round trip server-dev, client-dev, and frontend-dev are building
against — minus the frontend, which is exercised by a fake
``WatchSession`` consumer within the test process.

The server bootstrap uses the same pattern as
``server/tests/test_telemetry_ingest.py::running_server``. Once task #4
(CLI/bootstrap) lands, the ``harmonograf_server`` module will expose a
single entry point (tentatively ``harmonograf_server.app.make_server``)
that wraps storage + bus + ingest + servicer into one call; the fixture
here should be updated to use that entry point instead of wiring the
components by hand.

See docs/design/03 for the server architecture and docs/design/01 for
the wire contract.
"""

from __future__ import annotations

import grpc
import pytest_asyncio

from harmonograf_server.bus import SessionBus
from harmonograf_server.control_router import ControlRouter
from harmonograf_server.ingest import IngestPipeline
from harmonograf_server.pb import service_pb2_grpc
from harmonograf_server.rpc.telemetry import TelemetryServicer
from harmonograf_server.storage import make_store


@pytest_asyncio.fixture
async def harmonograf_server():
    """Start a gRPC harmonograf server with an InMemoryStore on a random
    loopback port. Yields a dict with ``port``, ``bus``, ``ingest``,
    ``router``, and ``store`` so tests can both connect a real Client
    and inspect server-side state directly.

    TODO(#4): when the server bootstrap entry point lands, replace the
    manual wiring below with ``harmonograf_server.app.make_server()``.
    """
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
            "port": port,
            "addr": f"127.0.0.1:{port}",
            "bus": bus,
            "ingest": ingest,
            "router": router,
            "store": store,
        }
    finally:
        try:
            await server.stop(grace=0.5)
        finally:
            await store.close()
