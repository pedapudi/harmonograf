# ADR 0007 — SQLite as the v0 timeline store

## Status

Accepted.

## Context

The server owns the canonical timeline: every session, every agent, every
span, every payload ref, every annotation, every plan. For v0 we need to
pick a storage layer. The real question is not "which database is best"
but "what set of constraints does harmonograf operate under right now."

Constraints:

- **Single server process.** See ADR 0002. One canonical timeline, no
  clustering, no multi-writer story. A store that assumes a single writer
  is fine.
- **Local-loopback first.** v0 runs on the same machine as the agents or
  on a trusted LAN. Latency to the store is on the order of a disk seek,
  not a network round-trip. ADR 0020 (no auth in v0) reinforces this.
- **Session-scoped lifetimes.** The retention sweeper deletes terminal
  sessions after a configurable window; the absolute data volume is
  bounded by "how many concurrent sessions × how chatty an agent is."
  For typical demos: low tens of thousands of spans and a few hundred MB
  of payloads.
- **Operator does not want to run a database.** Installing Postgres is a
  non-trivial setup cost for someone who wants to watch an agent run for
  ten minutes. "Zero dependencies" is a real feature.
- **Tests need a real store.** Mocking the DB hides bugs; we want to run
  the full server in integration tests without spinning up Postgres in CI.

Options:

1. **In-memory only** — simplest, fastest, but dies on restart. We already
   ship this as `MemoryStore` for tests.
2. **SQLite** — embedded, zero-install, on-disk, transactional, handles
   tens of thousands of inserts per second on a single writer.
3. **Postgres** — the obvious "grown-up" choice. Durable, concurrent
   writers, rich query features, but requires a side-car process to run.
4. **A dedicated time-series store (Clickhouse, InfluxDB)** — optimized
   for the workload shape but adds the largest operational surface area.

## Decision

Ship **SQLite** as the default persistent store, with `MemoryStore` as the
in-memory alternative for tests. Both implement the same store interface
so the server has no compile-time knowledge of which backend it is using.

SQLite is chosen because it matches every v0 constraint:

- Zero operator install — `pip install harmonograf-server`, done.
- Single-writer fits the single-server-process architecture exactly.
- Transactional, so ingest fan-out and subscriber reads cannot observe
  a torn span (see commits around "sqlite stability" —
  `630e55c milestone B+C: graph view, liveness, popover, sqlite stability`).
- Handles the v0 workload comfortably; stress testing shows sustained
  ingest well above any real multi-agent run's rate.
- Tests run the real backend in temp files with no docker-compose.

The interface is behind an abstraction so a future Postgres backend is a
drop-in sibling, not a rewrite.

## Consequences

**Good.**
- Setup is nothing. The operator quickstart does not mention a database.
  This is a meaningful driver of adoption.
- Integration tests run the real store. Bugs found on SQLite are bugs
  found in the real server, not in a mock.
- Durability is sufficient for the use case: sessions survive server
  restarts, and a retention sweeper prunes terminal sessions on a timer.

**Bad.**
- **One writer.** SQLite's WAL handles multiple readers during a single
  writer fine, but two server processes pointing at the same file is
  asking for trouble. Any HA or horizontal scale story requires
  switching stores.
- **No rich querying.** Cross-session analytics ("how often does agent X
  hit `tool_error` on task Y?") is listed in the overview roadmap and
  would benefit from Postgres-class query features. On SQLite it is
  doable but more work.
- **File locking on network shares.** SQLite on NFS is famously unsafe.
  Operators deploying to a mounted network share would break in subtle
  ways. The operator quickstart warns local disk only.
- **Schema migrations are manual.** We have not adopted Alembic or a
  similar migration tool; schema changes between iterations require
  dropping the store. This is acceptable because the store holds
  ephemeral session data, not user work, but it is documented friction.

**When to switch.** The trigger is any of: (a) more than one server
process needs to write; (b) a cross-session analytics feature needs
indexed queries at sizes SQLite starts to struggle with; (c) auth v1
requires per-tenant isolation at the storage layer. Until one of those
holds, SQLite stays. The store interface is the insurance policy.
