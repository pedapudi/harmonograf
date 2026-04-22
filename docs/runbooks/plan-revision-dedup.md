# Intervention history: plan-revision dedup (harmonograf#99 / goldfive#199)

As of harmonograf#99 (pairs with goldfive#199), the intervention
aggregator merges plan-revision rows onto their originating annotation
or drift by **strict id only**. The pre-#99 time-window fallback is
gated behind the `ServerConfig.legacy_plan_attribution_window_ms` field
(CLI flag `--legacy-plan-attribution-window-ms`) on the server and the
`legacyPlanAttributionWindowMs` option on the frontend deriver, and is
disabled by default. See harmonograf#101 for the env-var → config/param
migration.

## What changed

**goldfive#199** stamps `trigger_event_id` on every `PlanRevised`
envelope:

- User-control refines (USER_STEER / USER_CANCEL): the source
  `annotation_id` from the originating ControlMessage.
- Autonomous drift refines (LOOPING_REASONING, CONFABULATION_RISK,
  PLAN_DIVERGENCE, TOOL_ERROR, GOAL_DRIFT, …): the `DriftDetected.id`
  (UUID4) of the producing drift.

**harmonograf#99** consumes that id as the strict dedup key:

- Tier 1 (always on): plan row's `trigger_event_id` must match an
  annotation id or drift id to merge.
- Tier 2 (opt-in): legacy time-window fallback. Off by default.

## Pre-fix data (pre-harmonograf#99 / pre-goldfive#199)

Plan-revision rows stored before this fix do not carry
`trigger_event_id`. The aggregator treats them as standalone cards —
**not** silently merged by a time-window heuristic. This is explicit:
the rescope's goal is to eliminate guesswork.

**Operator action: drop your dev database** after upgrading. Sessions
recorded before the upgrade won't render correctly in the Trajectory
view (duplicate STEER cards, orphan plan rows). Fresh sessions on the
upgraded server render correctly end-to-end.

```sh
rm -rf /tmp/demo-data          # or whatever --data-dir points at
```

## Legacy time-window fallback (opt-in)

If you need to keep pre-fix sessions showing up as they used to — for
example, a live investigation where you can't drop the DB yet — enable
the fallback via config / option. There is **no env var**; per
harmonograf#101 the surface is the normal config field + CLI flag.

### Server

CLI flag (operators):

```sh
harmonograf-server --legacy-plan-attribution-window-ms 900000   # 15 min
```

Programmatic (tests, stress harness, embeds):

```python
from harmonograf_server.config import ServerConfig
cfg = ServerConfig(legacy_plan_attribution_window_ms=900_000.0)
```

### Frontend

The aggregator (`deriveInterventions` / `deriveInterventionsFromStore`)
takes an optional `legacyPlanAttributionWindowMs` field. The React
components read it from the app's runtime config context — there is
no build-time `import.meta.env` lookup. A minimum-viable caller just
leaves it unset, which is equivalent to 0 / disabled:

```ts
deriveInterventions({
  annotations,
  drifts,
  plans,
  legacyPlanAttributionWindowMs: 900_000, // opt in
});
```

### WARNING on match

When the fallback fires, a WARNING is logged (server: `interventions`
logger; frontend: `console.warn`) so operators can see which rows
would not have merged under strict-id-only. Use that signal to track
down goldfive producers that aren't stamping `trigger_event_id`.

## Verifying end-to-end

After dropping the DB and starting fresh:

```sql
-- every non-initial plan row has a trigger_event_id
SELECT id, revision_index, trigger_event_id
  FROM task_plans
  WHERE revision_index > 0 AND trigger_event_id = '';
-- should return 0 rows

-- user-control plan revisions match the source annotation
SELECT a.id AS annotation_id, tp.trigger_event_id
  FROM annotations a
  JOIN task_plans tp ON tp.trigger_event_id = a.id
  WHERE a.kind = 'STEERING';
-- each row should show matching ids
```

UI check: the Trajectory view should show exactly one card per user
STEER, tagged `PLAN_REVISED:RN`. Autonomous drifts + their refine
collapse to one card too (previously they always split into two).
