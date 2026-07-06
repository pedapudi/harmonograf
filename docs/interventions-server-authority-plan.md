# Execution plan: make the server authoritative for interventions

**Status:** approved direction · **Decision (2026-07-05):** major data-semantics logic must not live in
the frontend. The intervention merge currently exists twice — `server/harmonograf_server/interventions.py`
(Python, unused in production, missing 4 source lanes) and `frontend/src/lib/interventions.ts` (TypeScript,
the production implementation). This plan makes the **Python side the single source of truth**, ports the
missing lanes to it, and deletes the TS merge core, keeping only presentation code in the frontend.

This document is written to be executed by an implementer with no prior context. Follow it in order.
Every phase ends with a verification gate; do not start the next phase until the gate passes.

---

## Global rules (read before starting)

1. **Never hand-edit generated code**: anything under `server/harmonograf_server/pb/`,
   `client/harmonograf_client/pb/`, or `frontend/src/pb/`. After editing any `.proto`, run `make proto`
   from the repo root and commit the regenerated stubs together with the `.proto` change.
2. **Run the phase's verification gate exactly as written.** If a gate fails, fix within the phase;
   do not proceed.
3. **If a file/line/symbol named here does not match what you find, STOP and re-locate it by searching
   for the quoted code.** Do not improvise a different design.
4. **Do not refactor beyond what a step asks.** No renames, no drive-by cleanups, no comment rewrites.
5. One commit per numbered phase minimum. Suggested messages are given per phase.
6. Test commands used throughout:
   - Server: `make server-test` (or `cd <root> && uv run --extra e2e --with pytest --with pytest-asyncio python -m pytest -q server/tests/`)
   - Frontend: `cd frontend && npx vitest run` and `cd frontend && npx tsc -b`
   - Full: `make test`

## Vocabulary

- **Intervention row**: one entry in the merged chronological history (sources: `user`, `drift`,
  `goldfive`, `cancel`, `refine`, `transition`; plus user-message rows under `user`).
- **The TS spec**: `frontend/src/lib/interventions.ts` (`deriveInterventions`, line ~298) and its test
  suite `frontend/src/__tests__/lib/interventions.test.ts`. During this migration the TS code is the
  **executable specification** — Python must reproduce its behavior. Do not "improve" semantics while
  porting; port bug-for-bug, then file follow-ups.
- **Sidecar events**: `RefineAttempted`, `RefineFailed`, `UserMessageReceived` — defined in
  `proto/harmonograf/v1/telemetry.proto`, arriving as their own `TelemetryUp` oneof variants (NOT inside
  `goldfive_event`).

---

## Phase 0 — Durable event log (prerequisite; fixes restart data loss)

**Problem being fixed:** sidecar events live only in in-memory rings on `IngestPipeline`
(`server/harmonograf_server/ingest.py`: `_refine_attempts_by_session`, `_refine_failures_by_session`,
`_user_messages_by_session`, declared near lines 250–263) and are lost on server restart. Drift events
are stored twice (ring `_drifts_by_session` + the persisted `goldfive_events` table). The interventions
aggregator cannot be authoritative while its inputs evaporate on restart.

### 0.1 Add a `sidecar_events` table + storage API

In `server/harmonograf_server/storage/base.py`:

```python
@dataclass
class SidecarEventRecord:
    session_id: str
    kind: str          # "refine_attempted" | "refine_failed" | "user_message"
    event_key: str     # unique dedup key, see 0.2
    recorded_at: float # server wall-clock at ingest
    payload_bytes: bytes  # serialized harmonograf.v1 telemetry proto matching `kind`
```

Add two abstract methods on `Store` (next to `append_goldfive_event` / `list_goldfive_events`,
around `base.py:530`):

```python
@abstractmethod
async def append_sidecar_event(self, record: SidecarEventRecord) -> None: ...
@abstractmethod
async def list_sidecar_events(
    self, session_id: str, *, kind: Optional[str] = None
) -> list[SidecarEventRecord]: ...
```

`append_sidecar_event` MUST be idempotent on `event_key` (second insert with the same key is a no-op).
`list_sidecar_events` returns rows ordered by `(recorded_at, rowid)` ascending.

SQLite (`storage/sqlite.py` — add to `SCHEMA` next to the `goldfive_events` DDL, ~line 297):

```sql
CREATE TABLE IF NOT EXISTS sidecar_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  event_key TEXT NOT NULL UNIQUE,
  recorded_at REAL NOT NULL,
  payload_bytes BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sidecar_session_time
  ON sidecar_events(session_id, recorded_at, id);
```

Insert with `INSERT INTO sidecar_events (...) VALUES (...) ON CONFLICT(event_key) DO NOTHING`.
Wrap in the same lock/commit pattern as `append_goldfive_event`.

Memory (`storage/memory.py`): a `dict[str, list[SidecarEventRecord]]` per session plus a
`set[str]` of seen `event_key`s; append skips seen keys; list filters by kind and returns in
insertion order. Also add stubs to `storage/postgres.py` raising `NotImplementedError` (and, while
there, add the missing `update_task_assignee` stub — the class currently cannot be instantiated
because that abstract method has no override).

### 0.2 Persist sidecar events at ingest; define `event_key`

In `server/harmonograf_server/ingest.py`, at the TOP of each of `_handle_refine_attempted`
(~line 1470), `_handle_refine_failed` (~line 1515), `_handle_user_message` (~line 1580) — before any
ring/publish logic — build and persist a `SidecarEventRecord`:

- `payload_bytes` = `msg.SerializeToString()` of the already-materialized proto
  (`telemetry_pb2.RefineAttempted` / `RefineFailed` / `UserMessageReceived`).
- `event_key`:
  - refine_attempted: `f"ra:{attempt_id}"` (attempt_id is a goldfive-minted UUID4, always set)
  - refine_failed: `f"rf:{attempt_id}"`
  - user_message: `f"um:{run_id}:{sequence}:{emitted_at_us}"` where `emitted_at_us` is the integer
    microsecond value of `emitted_at` (0 if unset). Rationale: `sequence` alone resets per turn
    (harmonograf#61), so it is not unique; the emit timestamp disambiguates.
- Wrap the `await store.append_sidecar_event(...)` in the same log-and-continue `try/except` pattern
  used by `append_goldfive_event` in `_handle_goldfive_event` (~line 887).

Persist BEFORE publishing to the bus so replay-after-restart can never lag the live stream.

### 0.3 Replace ring-based replay with storage reads

In `server/harmonograf_server/rpc/frontend.py`, inside `WatchSession`:

1. **Refine attempts/failures** (sections labeled `4b.3`, ~lines 429–486): replace
   `self._ingest.refine_attempts_for_session(...)` / `refine_failures_for_session(...)` with
   `await self._store.list_sidecar_events(session_id, kind="refine_attempted")` (resp. `"refine_failed"`),
   then for each record `ParseFromString(rec.payload_bytes)` into the matching `telemetry_pb2` message and
   yield it exactly as today. Delete the field-by-field dict→proto reconstruction (it is no longer needed —
   the stored bytes are already the final proto). Preserve arrival order (the list method's ordering).
2. **User messages** (section `4b.4`, ~lines 495–519): same pattern, `kind="user_message"`.
3. **Drifts** (section `4b.1`, ~lines 352–387): replace the ring read + hand-reconstruction with the
   pattern already used for `invocation_cancelled` at ~lines 397–418: 
   `await self._store.list_goldfive_events(session_id, kind="drift_detected")`, parse each
   `rec.payload_bytes` into `goldfive_events_pb2.Event`, `continue` on parse failure with a debug log,
   yield verbatim. Do NOT stamp/overwrite `emitted_at` — the persisted envelope already carries it.

### 0.4 Delete the rings

In `ingest.py`, delete: `_drifts_by_session`, `_drift_ring_max`, `_refine_attempts_by_session`,
`_refine_failures_by_session`, `_refine_ring_max`, `_user_messages_by_session`,
`_user_message_ring_max`, the ring-append blocks in the four handlers, and the public accessors
`drifts_for_session`, `refine_attempts_for_session`, `refine_failures_for_session`,
`user_messages_for_session`. Grep the whole `server/` tree for each accessor name first; the only
callers should be `rpc/frontend.py` (updated in 0.3) and `interventions.py` (updated in Phase 1 —
for THIS phase, change `interventions.py`'s `list_interventions` to read
`await store.list_goldfive_events(session_id, kind="drift_detected")` and project drift fields from
the parsed proto instead of ring dicts; field mapping is 1:1 — `kind`/`severity` enums must be
lowercased with the existing helpers in `convert.py`, and use `Event.emitted_at` seconds as the
timestamp, falling back to the record's `recorded_at`). Update `server/tests/` that monkeypatch or
seed rings (`test_refine_events_ingest.py`, `test_user_messages_ingest.py`,
`test_refine_events_rpc.py`, `test_user_messages_rpc.py`, `test_interventions.py`) to seed storage
instead.

### 0.5 Make `event_id` the real dedup key for `goldfive_events` (fixes silent event drop)

Current bug: `sqlite.py` ~1626 uses `INSERT OR IGNORE` with `PRIMARY KEY (session_id, run_id, sequence)`;
two DISTINCT events can collide on that composite (per-turn sequence resets) and the second is silently
dropped before the `UNIQUE(event_id)` index matters. Fix by rebuilding the table (SQLite cannot alter a PK):

In `_ensure_schema` / the migration block (~line 354), add a one-time migration guarded by inspecting the
existing schema (`SELECT sql FROM sqlite_master WHERE name='goldfive_events'`; run the migration only if
the DDL still contains `PRIMARY KEY (session_id, run_id, sequence)`):

```sql
CREATE TABLE goldfive_events_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  kind TEXT NOT NULL,
  recorded_at REAL NOT NULL,
  payload_bytes BLOB NOT NULL,
  event_id TEXT NOT NULL DEFAULT ''
);
INSERT INTO goldfive_events_new
  (session_id, run_id, sequence, kind, recorded_at, payload_bytes, event_id)
  SELECT session_id, run_id, sequence, kind, recorded_at, payload_bytes, event_id
  FROM goldfive_events;
DROP TABLE goldfive_events;
ALTER TABLE goldfive_events_new RENAME TO goldfive_events;
CREATE UNIQUE INDEX idx_goldfive_events_event_id
  ON goldfive_events(event_id) WHERE event_id != '';
CREATE INDEX idx_goldfive_events_session_kind ON goldfive_events(session_id, kind, recorded_at, sequence);
CREATE INDEX idx_goldfive_events_session_time ON goldfive_events(session_id, recorded_at);
```

Change `append_goldfive_event` insert logic to: if `event_id` is non-empty →
`INSERT ... ON CONFLICT(event_id) WHERE event_id != '' DO NOTHING` is not valid SQLite syntax for
partial-index conflict targets on older versions, so use the explicit form: `SELECT 1 FROM
goldfive_events WHERE event_id = ?` → return if found → plain `INSERT`. If `event_id` is empty
(legacy producers) → keep the old composite check (`SELECT 1 ... WHERE session_id=? AND run_id=? AND
sequence=?`) → return if found → `INSERT`. Mirror the same two-tier check in `memory.py`
(`memory.py` ~547–568 currently checks the composite FIRST unconditionally — swap the order: event_id
check first when non-empty).

### Phase 0 verification gate

1. `make server-test` green.
2. Add + pass a restart-survival test (new file `server/tests/test_sidecar_durability.py`): using a
   `SqliteStore` on a tmp-path file, ingest one refine_attempted + one refine_failed + one user_message +
   one drift_detected; close the store; open a NEW `SqliteStore` on the same file; assert
   `list_sidecar_events` returns all three sidecar records and `list_goldfive_events(kind="drift_detected")`
   returns the drift; then drive `WatchSession` (see `test_refine_events_rpc.py` for the harness pattern)
   and assert the burst contains the refine/user-message/drift frames.
3. Add + pass a dedup test: append the same sidecar record twice → one row; append two DIFFERENT
   goldfive events sharing `(session_id, run_id, sequence)` but distinct non-empty `event_id`s → BOTH rows
   present (this asserts the 0.5 fix).

Commit: `feat(server): durable sidecar_events + drift replay from storage; event_id-first goldfive dedup`

---

## Phase 1 — Port the missing lanes into `interventions.py` (TS is the spec)

All inputs are now durable. Extend `list_interventions` (`server/harmonograf_server/interventions.py:186`)
to produce the four lanes the TS deriver has and the Python side lacks. For each lane, open the quoted TS
range and reproduce its filtering/labeling/merging EXACTLY. Extend `InterventionRecord` with the new
fields listed in Phase 2 (plain dataclass fields defaulting to `""`).

New inputs to load alongside annotations/plans/drifts:

```python
cancels     = await store.list_goldfive_events(session_id, kind="invocation_cancelled")
transitions = await store.list_goldfive_events(session_id, kind="task_transitioned")
attempts    = await store.list_sidecar_events(session_id, kind="refine_attempted")
failures    = await store.list_sidecar_events(session_id, kind="refine_failed")
user_msgs   = await store.list_sidecar_events(session_id, kind="user_message")
```

(parse `payload_bytes` as in Phase 0; skip-and-log on parse failure).

Lane specs (TS reference ranges in `frontend/src/lib/interventions.ts`):

1. **cancel** (TS ~391–434): `source="cancel"`, `kind="CANCELLED"`, deliberately EMPTY
   `trigger_event_id` (cancels never merge onto other rows), carry `drift_id` backlink,
   `target_agent_id` from the event's agent field, body from `reason`/description, severity passthrough.
2. **refine** (TS ~436–564): one row per `RefineAttempted`, `source="refine"`, correlate failures by
   `attempt_id` and successful revisions by matching a plan whose `trigger_event_id == drift_id`;
   outcome = `plan_revised:rN` on success, `REFINE_FAILED:<failure_kind>` shape per the TS code on
   failure, pending otherwise. Copy the exact strings from TS.
3. **user message** (TS ~596–615): `source="user"`, `kind="USER_MESSAGE"` or
   `"USER_MESSAGE_INTERJECTION"` when `mid_turn`, body = content, author passthrough.
4. **transition** (TS ~617–657): filter to terminal `to_status` values × the "meaningful source" set —
   copy the TS filter ladder verbatim; `source="transition"`, `kind=to_status` upper-case; carry
   `transition_to_status` / `transition_source` / `transition_task_id`.

Cross-cutting changes in the same phase:

- **Clock rule:** every row's `at` uses the event's goldfive-emitted time (`emitted_at`) when present,
  falling back to server `recorded_at`. Plans already use `plan.created_at` (goldfive clock);
  annotations necessarily keep server clock. This removes the drift-vs-plan cross-clock misordering.
- **Stable row key:** populate a `key` string on every record, wire-stable (NOT frontend-local seq):
  `ann:{annotation_id}` · `drift:{drift_id}` (post-collapse survivor keeps its own id) ·
  `plan:{plan_id}:r{N}` · `cancel:{event_id or invocation_id}` · `refine:{attempt_id}` ·
  `usermsg:{event_key}` · `transition:{event_id or f"{task_id}:{to_status}:{emitted_at_us}"}`.
- Ensure the existing pipeline steps (`_attribute_outcomes`, `_collapse_by_condition_id`,
  `_merge_by_trigger_event_id`) run over the union, mirroring the TS ordering
  (`attributeOutcomes` → `mergeByTriggerEventId` → optional legacy window → `groupDriftConditions`;
  note the TS collapse runs LAST while Python currently collapses before merging — match the TS order
  and confirm against the TS tests you port).
- Populate per-observation sub-rows on collapsed drift rows (mirror TS `observations`).

**Test strategy (this is the flaw-gate):** port the TS test scenarios in
`frontend/src/__tests__/lib/interventions.test.ts` (~1005 lines) into
`server/tests/test_interventions.py`, one Python test per TS `it(...)` case that exercises merge
semantics, asserting the same row count / source / kind / outcome / key fields. Name each Python test
after the TS case so reviewers can diff coverage. Keep the existing Python tests passing (where a
Python test contradicts a TS test, the TS behavior wins — update the Python test and note it in the
commit message).

### Phase 1 verification gate

`make server-test` green, including the ported suite. Spot-check: run the e2e control test
(`server/tests/test_control_e2e.py`) unchanged.

Commit: `feat(server): interventions parity — cancel/refine/transition/user-message lanes, emitted_at clock, stable row keys`

---

## Phase 2 — Proto: extend `Intervention`, regenerate

In `proto/harmonograf/v1/types.proto`, `message Intervention` (last existing field:
`severity_transitions = 16`). Append EXACTLY:

```proto
  // Wire-stable row identity; frontend uses it as the React list key and
  // for upsert-on-refetch. See interventions.py for the per-source scheme.
  string key = 17;
  string target_agent_id = 18;   // cancel rows: agent whose invocation was cancelled
  string drift_id = 19;          // cancel rows: triggering drift backlink
  string attempt_id = 20;        // refine rows: RefineAttempted correlation id
  string failure_kind = 21;      // refine rows with failed terminal
  string transition_to_status = 22;
  string transition_source = 23;
  string transition_task_id = 24;
  string target_plan_id = 25;    // plan-scoping for multi-plan sessions
  repeated DriftObservation observations = 26;  // collapsed-condition sub-rows
```

Add next to `SeverityTransition`:

```proto
message DriftObservation {
  google.protobuf.Timestamp at = 1;
  string severity = 2;
  string lifecycle = 3;
  string detail = 4;
  string drift_id = 5;
}
```

Update the `source` doc comment (line ~284) to list `user | drift | goldfive | cancel | refine | transition`.
Extend `record_to_pb` (`interventions.py` ~905) to emit every new field. Run `make proto`; commit
regenerated stubs under all three `pb/` trees together with the `.proto` edit.

Gate: `make server-test` + `cd frontend && npx tsc -b` both green (frontend not yet consuming the fields).

Commit: `feat(proto): Intervention row key + cancel/refine/transition fields + observations`

---

## Phase 3 — Frontend: consume the RPC, live via debounced refetch

### 3.1 New store + hook

Create `frontend/src/state/interventionsStore.ts`:

- Module-level `Map<string, InterventionRow[]>` + `Set<() => void>` listeners + `subscribe`/`emit`
  (copy the pattern from `state/annotationStore.ts`).
- `async refresh(sessionId)`: call `getHarmonografClient().listInterventions({ sessionId })`
  (client wrapper: `frontend/src/rpc/client.ts`), convert each proto `Intervention` → `InterventionRow`,
  replace the session's array wholesale, emit. Concurrent-call guard: keep a per-session in-flight
  flag; if a refresh is requested while one is running, run exactly one more after it completes.
- Converter (put it in `lib/interventions.ts`): map proto fields 1:1 onto `InterventionRow`;
  `atMs` = absolute ms of `at` minus the session start ms — reuse the exact session-start source that
  `rpc/goldfiveEvent.ts` uses for its `emittedAtMs` conversions (search for `tsToMsAbs` usage there and
  mirror it; do not invent a new epoch).
- `useInterventions(sessionId)`: `useSyncExternalStore` over the store.

### 3.2 Refetch triggers

In the hook (or a small `InterventionsSyncer` mounted once per console): on session open call
`refresh(sessionId)`; subscribe to the `SessionStore` registries that carry intervention-relevant
deltas — `drifts`, `tasks` (plan revisions), `refineAttempts`, `refineFailures`, `transitions`,
`userMessages`, `invocationCancels` (see the registry class list at the top of `gantt/index.ts`) —
plus `annotationStore`; debounce 300 ms; call `refresh`. Also refresh once when the watch reports
`initialBurstComplete`. Copy the subscription-wiring pattern from `GanttView.tsx` ~lines 80–102.

### 3.3 Swap the four consumers

Replace every production call to `deriveInterventionsFromStore` with the store/hook:

- `frontend/src/components/shell/views/GanttView.tsx:165`
- `frontend/src/components/shell/views/TrajectoryView.tsx:450`
- `frontend/src/components/zicato/adapter.ts:833` and `:854` — IMPORTANT: fetch once and pass the same
  array into both `buildTicks` and `buildLadder` (this also removes today's duplicated derivation).
  `adapter.ts` is not a React component at those call sites; read the store synchronously
  (`interventionsStore.get(sessionId)`) inside the memo and add the store to the adapter's existing
  subscription/tick wiring so a refresh triggers a rebuild.

Gate: `npx tsc -b` green; `npx vitest run` — expect failures ONLY in tests that exercise the deriver
directly (handled in Phase 4); `make demo` smoke: drive the orchestrated reference agent, confirm
trajectory markers and ladder rows appear within ~1 s of a STEER, and survive a browser refresh AND a
server restart mid-session.

Commit: `feat(frontend): interventions from ListInterventions RPC (debounced refetch), drop live client merge`

---

## Phase 4 — Delete the TS merge core

In `frontend/src/lib/interventions.ts` KEEP: `InterventionSource`, `InterventionRow`,
`DriftObservation` types, the proto→row converter (from 3.1), `SEVERITY_WEIGHT`, `markerRadiusFor`,
`SOURCE_COLOR`, `SOURCE_GLYPH`. DELETE: `deriveInterventions`, `deriveInterventionsFromStore`,
`DeriveInput`, and every merge helper (`attributeOutcomes`, `mergeByTriggerEventId`,
`legacyTimeWindowMerge`, `groupDriftConditions`, `latestPlanAfter`, the constant sets that only they
used). Update the header comment: the server (`interventions.py` + `ListInterventions`) is the single
source of truth; this file is presentation + wire conversion only.

Tests: delete the merge-semantics cases from `__tests__/lib/interventions.test.ts` (they were ported
to Python in Phase 1 — verify each deleted case has a named Python twin before deleting); keep/add
converter tests (proto→row field mapping, atMs rebase). The suites under `__tests__/rpc/` that assert
end-to-end "delta in → intervention row out" via the deriver (`refineEvents.test.ts`,
`goldfiveEventLanes.test.ts`, invocation-cancel and user-message tests) must be reworked to assert
**registry contents** (what the stream wrote into `SessionStore`) instead of derived rows — the
derived-row behavior is now covered server-side. This is the fiddliest step of the plan; budget time
for it and do not weaken assertions silently.

Gate: `cd frontend && npx tsc -b && npx vitest run` fully green; `grep -rn "deriveInterventions" frontend/src`
returns nothing outside comments.

Commit: `refactor(frontend): delete client-side intervention merge; server is authoritative`

---

## Phase 5 — Final verification & acceptance

1. `make test` fully green (server + client + frontend).
2. `make demo` end-to-end: STEER from the UI → intervention row appears in zicato ladder/ticks and the
   Trajectory list within ~1 s; drift markers show condition collapse (count badge) as before.
3. Restart drill: mid-session, kill and restart the server, reload the browser → the full intervention
   history (including refine attempts and user messages) is present.
4. `grep` checks: no references to the deleted ring accessors; no production `deriveInterventionsFromStore`.
5. Docs: update `proto/harmonograf/v1/frontend.proto` ListInterventions comment (it currently says the
   frontend "recomputes live updates from WatchSession deltas" — now false) and the header comment in
   `lib/interventions.ts`.

## Footguns — do NOT

- Do not keep the rings "as a cache" — delete them; two read paths is how this drifted originally.
- Do not parse `sidecar_events.payload_bytes` as `goldfive.v1.Event` — they are harmonograf
  `telemetry_pb2` messages; the two tables intentionally hold different proto types.
- Do not use frontend-local registry `seq` values in row keys — keys must be derivable server-side.
- Do not reuse or renumber proto fields; only append (17+) as specified.
- Do not change `_delta_to_session_update` or the bus in this plan — live *event* streaming is untouched;
  only the merged-history computation moves.
- Do not delete `GetSessionPlanHistory` or touch MD3 files — separate workstreams.

## Explicit non-goals / follow-ups (tracked separately, not in this plan)

Frontend stream reconnect; `get_spans` open-span window fix; `convert.py` truthiness fix; ingest
dispatch idempotency + `_task_index` lifecycle; `ListSessions` N+1; MD3 retirement; deleting the other
dead RPCs (`GetSpanTree`, `GetStats`, `DeleteSession`).
