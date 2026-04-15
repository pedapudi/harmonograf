from __future__ import annotations

from typing import Any, Literal

from harmonograf_server.storage.base import Store
from harmonograf_server.storage.memory import InMemoryStore
from harmonograf_server.storage.sqlite import SqliteStore


def make_store(kind: Literal["memory", "sqlite", "postgres"], **opts: Any) -> Store:
    if kind == "memory":
        return InMemoryStore()
    if kind == "sqlite":
        db_path = opts.get("db_path")
        if db_path is None:
            raise ValueError("sqlite store requires db_path")
        return SqliteStore(db_path=db_path, payload_dir=opts.get("payload_dir"))
    if kind == "postgres":
        raise NotImplementedError(
            "postgres store is a stub. See docs/dev-guide/storage-backends.md "
            "and the hgraf-add-storage-backend skill for how to add a real backend."
        )
    raise ValueError(f"unknown store kind: {kind}")
