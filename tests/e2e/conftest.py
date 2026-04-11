"""Shared fixtures for the harmonograf end-to-end suite.

Boots a real Harmonograf server in-process using the composition root
at :class:`harmonograf_server.main.Harmonograf`. This is the same
bootstrap path that `python -m harmonograf_server` uses, just with an
InMemoryStore and ephemeral ports so the test suite is hermetic and
offline. Tests attach a real :class:`Client` to the server and drive an
ADK runner backed by a deterministic ``MockModel``, proving the whole
ingest → bus → control-router → storage loop end to end.

The fixture yields a dict with ``addr``, ``bus``, ``router``, ``ingest``,
``store``, and the underlying ``app`` so individual tests can poke
server-side state directly without going through an RPC.
"""

from __future__ import annotations

import socket

import pytest_asyncio

from harmonograf_server.config import ServerConfig
from harmonograf_server.main import Harmonograf


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture
async def harmonograf_server():
    cfg = ServerConfig(
        host="127.0.0.1",
        grpc_port=_free_port(),
        web_port=_free_port(),
        store_backend="memory",
        data_dir="",
        grace_seconds=0.5,
        log_level="WARNING",
    )
    app = await Harmonograf.from_config(cfg)
    await app.start()
    try:
        yield {
            "app": app,
            "port": cfg.grpc_port,
            "addr": f"127.0.0.1:{cfg.grpc_port}",
            "bus": app.bus,
            "ingest": app.ingest,
            "router": app.router,
            "store": app.store,
            "servicer": app.servicer,
        }
    finally:
        await app.stop()
