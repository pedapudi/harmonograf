"""Round-trip coverage for ``convert.py`` plan / task converters.

The converters between ``goldfive.v1.Plan`` (proto) and harmonograf's
storage ``TaskPlan`` dataclass historically dropped fields on the
trip in either direction. The fields involved in this regression:

* ``Plan.run_id`` — the run that produced this plan
* ``Task.supersedes`` — explicit supersession link (goldfive#237)
* ``Task.supersedes_kind`` — REPLACE / CORRECT (goldfive#251)

Pre-fix, ``goldfive_pb_plan_to_storage`` ignored ``pb.run_id``, the
storage schema had no column for it, and ``storage_plan_to_goldfive_pb``
wrote an empty ``run_id`` back. Same shape for the supersedes fields
on ``Task``. The frontend's chain-collapse renderer (#183) requires
``supersedes`` to reconstruct revision chains; null after round-trip
broke the rendering.

These tests pin both directions of the converter PLUS the storage
write/read path, since the converter and the persistence layer must
agree.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from goldfive.pb.goldfive.v1 import types_pb2 as gt
from goldfive.types import SupersessionKind

from harmonograf_server.convert import (
    goldfive_pb_plan_to_storage,
    plan_from_snapshot_json,
    plan_to_snapshot_json,
    storage_plan_to_goldfive_pb,
)
from harmonograf_server.storage import make_store


# ---- pure converter (no storage) ----------------------------------------


def _build_plan_pb(
    *,
    plan_id: str = "p1",
    run_id: str = "run-abc",
    tasks: list[tuple[str, str, int]] | None = None,
) -> gt.Plan:
    """Construct a ``goldfive.v1.Plan`` with the supersedes-bearing fields set.

    ``tasks`` is a list of ``(task_id, supersedes, supersedes_kind_int)``.
    """

    plan = gt.Plan()
    plan.id = plan_id
    plan.run_id = run_id
    plan.summary = "round-trip test"
    for tid, sup, sup_kind in tasks or []:
        t = plan.tasks.add()
        t.id = tid
        t.title = f"task {tid}"
        t.status = gt.TASK_STATUS_PENDING
        if sup:
            t.supersedes = sup
        if sup_kind:
            t.supersedes_kind = sup_kind
    return plan


def test_pb_to_storage_preserves_run_id_and_supersedes():
    pb = _build_plan_pb(
        plan_id="p1",
        run_id="run-abc",
        tasks=[
            ("t_new", "t_old", gt.SUPERSESSION_KIND_CORRECT),
            ("t_other", "", gt.SUPERSESSION_KIND_UNSPECIFIED),
        ],
    )
    stored = goldfive_pb_plan_to_storage(
        pb,
        session_id="sess-1",
        created_at=1.0,
        invocation_span_id="span-1",
        planner_agent_id="agent-1",
    )
    assert stored.run_id == "run-abc"

    by_id = {t.id: t for t in stored.tasks}
    assert by_id["t_new"].supersedes == "t_old"
    assert by_id["t_new"].supersedes_kind == SupersessionKind.CORRECT
    # Tasks without a supersedes link land with empty defaults.
    assert by_id["t_other"].supersedes == ""
    assert by_id["t_other"].supersedes_kind == SupersessionKind.UNSPECIFIED


def test_storage_to_pb_preserves_run_id_and_supersedes():
    """Inverse: storage → pb writes the new fields back to the proto."""

    pb = _build_plan_pb(
        plan_id="p1",
        run_id="run-abc",
        tasks=[
            ("t_new", "t_old", gt.SUPERSESSION_KIND_REPLACE),
            ("t_keep", "", 0),
        ],
    )
    stored = goldfive_pb_plan_to_storage(
        pb, session_id="sess-1", created_at=1.0
    )
    pb_out = storage_plan_to_goldfive_pb(stored)

    assert pb_out.run_id == "run-abc"
    by_id = {t.id: t for t in pb_out.tasks}
    assert by_id["t_new"].supersedes == "t_old"
    assert by_id["t_new"].supersedes_kind == gt.SUPERSESSION_KIND_REPLACE
    # Empty defaults stay empty — the converter must NOT accidentally
    # null something real or stamp UNSPECIFIED with stale state.
    assert by_id["t_keep"].supersedes == ""
    assert by_id["t_keep"].supersedes_kind == gt.SUPERSESSION_KIND_UNSPECIFIED


def test_round_trip_idempotent_via_snapshot_json():
    """The wire form used by ``task_plan_revisions`` is bit-stable across the round-trip."""

    pb = _build_plan_pb(
        plan_id="p1",
        run_id="run-xyz",
        tasks=[
            ("a", "", 0),
            ("b", "a", gt.SUPERSESSION_KIND_REPLACE),
            ("c", "b", gt.SUPERSESSION_KIND_CORRECT),
        ],
    )
    stored = goldfive_pb_plan_to_storage(
        pb, session_id="sess-1", created_at=1.0
    )

    # Snapshot path goes through both directions of the converter
    # (storage_plan_to_goldfive_pb on write, goldfive_pb_plan_to_storage
    # on read). Two trips must reproduce the original storage state.
    snapshot = plan_to_snapshot_json(stored)
    rebuilt = plan_from_snapshot_json(snapshot)

    assert rebuilt.run_id == stored.run_id == "run-xyz"
    by_id = {t.id: t for t in rebuilt.tasks}
    assert by_id["b"].supersedes == "a"
    assert by_id["b"].supersedes_kind == SupersessionKind.REPLACE
    assert by_id["c"].supersedes == "b"
    assert by_id["c"].supersedes_kind == SupersessionKind.CORRECT
    assert by_id["a"].supersedes == ""
    assert by_id["a"].supersedes_kind == SupersessionKind.UNSPECIFIED


def test_legacy_plan_without_supersedes_round_trips_with_empty_defaults():
    """Plans without supersedes / run_id keep their empty defaults intact."""

    pb = _build_plan_pb(
        plan_id="p-legacy",
        run_id="",  # legacy: no run_id on the wire
        tasks=[("only", "", 0)],
    )
    stored = goldfive_pb_plan_to_storage(
        pb, session_id="sess-1", created_at=1.0
    )
    pb_out = storage_plan_to_goldfive_pb(stored)
    assert stored.run_id == ""
    assert pb_out.run_id == ""
    assert pb_out.tasks[0].supersedes == ""
    assert pb_out.tasks[0].supersedes_kind == gt.SUPERSESSION_KIND_UNSPECIFIED


# ---- storage layer round-trip (sqlite + memory) -------------------------


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def store(request, tmp_path: Path):
    if request.param == "memory":
        s = make_store("memory")
    else:
        db_path = tmp_path / "test.db"
        s = make_store("sqlite", db_path=str(db_path))
    await s.start()
    try:
        yield s
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_storage_round_trip_preserves_run_id_and_supersedes(store):
    """Plan written → read round-trip preserves all three fields.

    Exercises both the in-memory store (which stores the dataclass
    verbatim) and the sqlite store (which goes through the schema +
    migration path the brief specified).
    """

    pb = _build_plan_pb(
        plan_id="p-store",
        run_id="run-store-1",
        tasks=[
            ("t1", "", 0),
            ("t2", "t1", gt.SUPERSESSION_KIND_REPLACE),
            ("t3", "t2", gt.SUPERSESSION_KIND_CORRECT),
        ],
    )
    stored = goldfive_pb_plan_to_storage(
        pb,
        session_id="sess-store",
        created_at=42.0,
        invocation_span_id="span-1",
        planner_agent_id="agent-1",
    )
    written = await store.put_task_plan(stored)
    assert written.run_id == "run-store-1"

    fetched = await store.get_task_plan("p-store")
    assert fetched is not None
    assert fetched.run_id == "run-store-1"

    by_id = {t.id: t for t in fetched.tasks}
    assert by_id["t1"].supersedes == ""
    assert by_id["t1"].supersedes_kind == SupersessionKind.UNSPECIFIED
    assert by_id["t2"].supersedes == "t1"
    assert by_id["t2"].supersedes_kind == SupersessionKind.REPLACE
    assert by_id["t3"].supersedes == "t2"
    assert by_id["t3"].supersedes_kind == SupersessionKind.CORRECT


@pytest.mark.asyncio
async def test_storage_round_trip_legacy_plan(store):
    """Legacy plan (no supersedes, empty run_id) round-trips with empty defaults."""

    pb = _build_plan_pb(
        plan_id="p-legacy",
        run_id="",
        tasks=[("only", "", 0)],
    )
    stored = goldfive_pb_plan_to_storage(
        pb, session_id="sess-store", created_at=1.0
    )
    await store.put_task_plan(stored)
    fetched = await store.get_task_plan("p-legacy")
    assert fetched is not None
    assert fetched.run_id == ""
    assert fetched.tasks[0].supersedes == ""
    assert fetched.tasks[0].supersedes_kind == SupersessionKind.UNSPECIFIED


# ---- migration: pre-existing DB without the new columns ----------------


@pytest.mark.asyncio
async def test_sqlite_migration_adds_columns_no_data_loss(tmp_path: Path):
    """Existing DB with old schema gains the new columns cleanly.

    Simulates an older DB created before this fix: ``task_plans`` has
    no ``run_id`` column; ``tasks`` has no ``supersedes`` /
    ``supersedes_kind`` columns. The store's ``start()`` should ALTER
    TABLE to add them without dropping the legacy row.
    """

    import sqlite3

    db_path = tmp_path / "legacy.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE task_plans (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            invocation_span_id TEXT,
            planner_agent_id TEXT,
            created_at REAL NOT NULL,
            summary TEXT,
            edges TEXT,
            revision_reason TEXT NOT NULL DEFAULT '',
            revision_kind TEXT NOT NULL DEFAULT '',
            revision_severity TEXT NOT NULL DEFAULT '',
            revision_index INTEGER NOT NULL DEFAULT 0,
            trigger_event_id TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO task_plans (
            id, session_id, invocation_span_id, planner_agent_id,
            created_at, summary, edges
        ) VALUES (
            'plan_legacy', 'sess_legacy', 'inv', 'planner', 1.0,
            'legacy', '[]'
        );
        CREATE TABLE tasks (
            plan_id TEXT NOT NULL,
            id TEXT NOT NULL,
            title TEXT,
            description TEXT,
            assignee_agent_id TEXT,
            status TEXT NOT NULL,
            predicted_start_ms INTEGER DEFAULT 0,
            predicted_duration_ms INTEGER DEFAULT 0,
            bound_span_id TEXT,
            cancel_reason TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (plan_id, id)
        );
        INSERT INTO tasks (
            plan_id, id, title, status
        ) VALUES (
            'plan_legacy', 't_legacy', 'old task', 'PENDING'
        );
        """
    )
    con.commit()
    con.close()

    store = make_store("sqlite", db_path=str(db_path))
    await store.start()
    try:
        # Legacy row survives the migration with empty defaults.
        got = await store.get_task_plan("plan_legacy")
        assert got is not None
        assert got.run_id == ""
        assert len(got.tasks) == 1
        assert got.tasks[0].id == "t_legacy"
        assert got.tasks[0].supersedes == ""
        assert got.tasks[0].supersedes_kind == SupersessionKind.UNSPECIFIED

        # Fresh writes can use the new columns.
        pb = _build_plan_pb(
            plan_id="plan_new",
            run_id="run-fresh",
            tasks=[("t_new", "t_legacy", gt.SUPERSESSION_KIND_REPLACE)],
        )
        stored = goldfive_pb_plan_to_storage(
            pb, session_id="sess_legacy", created_at=2.0
        )
        await store.put_task_plan(stored)
        rebuilt = await store.get_task_plan("plan_new")
        assert rebuilt is not None
        assert rebuilt.run_id == "run-fresh"
        assert rebuilt.tasks[0].supersedes == "t_legacy"
        assert rebuilt.tasks[0].supersedes_kind == SupersessionKind.REPLACE
    finally:
        await store.close()
