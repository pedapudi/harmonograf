---
name: hgraf-add-span-kind
description: Add a new SpanKind enum value — proto, converters, renderer color, status semantics, kind_string fallback, and tests.
---

# hgraf-add-span-kind

## When to use

A framework you want to support emits an activity category that doesn't map onto the existing nine kinds (`INVOCATION`, `LLM_CALL`, `TOOL_CALL`, `USER_MESSAGE`, `AGENT_MESSAGE`, `TRANSFER`, `WAIT_FOR_HUMAN`, `PLANNED`, `CUSTOM`). Prefer `SPAN_KIND_CUSTOM` with a `kind_string` label for framework-local labels — reach for a new enum value only when multiple frameworks emit the same semantic concept.

## Prerequisites

1. Read `proto/harmonograf/v1/types.proto:91-102` — the `SpanKind` enum and the comment establishing the `CUSTOM + kind_string` escape hatch.
2. Confirm the kind is reusable across frameworks. If it's one-off, don't add an enum value — use `SPAN_KIND_CUSTOM` with `kind_string = "framework_label"` instead.
3. Understand that the renderer resolves color via `frontend/src/gantt/colors.ts:24 KIND_TO_VAR`, pulling the actual hue from a CSS custom property set by `frontend/src/theme/themes.ts`.

## Step-by-step

### 1. Proto edit

`proto/harmonograf/v1/types.proto:91`:

```proto
enum SpanKind {
  SPAN_KIND_UNSPECIFIED = 0;
  // ...
  SPAN_KIND_CUSTOM = 9;
  SPAN_KIND_MEMORY_OP = 10;
}
```

### 2. Regen

```bash
make proto
```

### 3. Python ingest side

`server/harmonograf_server/convert.py` and `storage/base.py` carry a `SpanKind` Enum mirroring the proto. Grep for the string `SpanKind.TOOL_CALL` to find every Python enum definition and add `MEMORY_OP` everywhere it appears. Typical locations:

- `server/harmonograf_server/storage/base.py` — the Python enum used by the Store.
- `server/harmonograf_server/convert.py` — `_KIND_TO_STORAGE` / `_STORAGE_TO_KIND` bidirectional maps.
- `server/harmonograf_server/ingest.py` — the `_TASK_BINDING_SPAN_KINDS` frozenset (`ingest.py:56`) — decide if your new kind should bind to task state like LLM_CALL / TOOL_CALL do, or be observability-only.

On the client, `client/harmonograf_client/enums.py` holds the same Enum. Add the value there.

### 4. Frontend type + converter

`frontend/src/gantt/types.ts:4 SpanKind` is a TypeScript string-literal union; add `| 'MEMORY_OP'`.

`frontend/src/rpc/convert.ts:44 SPAN_KIND`:

```ts
const SPAN_KIND: Record<PbSpanKind, UiSpanKind> = {
  // ...existing entries...
  [PbSpanKind.MEMORY_OP]: 'MEMORY_OP',
};
```

### 5. Renderer color

`frontend/src/gantt/colors.ts:24 KIND_TO_VAR`:

```ts
const KIND_TO_VAR: Record<SpanKind, string> = {
  // ...existing...
  MEMORY_OP: '--hg-kind-memory-op',
};
```

Add the CSS variable in `frontend/src/theme/themes.ts` for every theme (light + dark). Grep for `--hg-kind-llm-call` as a template. If you forget, `cssVar` (`colors.ts:52`) returns `#888888` as the fallback and you'll see grey spans everywhere — which is a useful smoke test for missing theme entries.

### 6. Legend entry

`frontend/src/components/Gantt/GanttLegend.tsx` has a static array describing each kind for the UI legend. Add an entry.

### 7. `kind_string` fallback sanity check

If an agent emits the new kind but runs against an older frontend, the pb3 unknown enum rules mean the frontend sees `UNSPECIFIED` unless the TS stubs are regenerated. That's fine for internal deployments where you regen atomically. For external consumers, also emit a `kind_string` set to `"memory_op"` so downstream tooling has a human label. Document this in the proto comment.

### 8. Tests

- `server/tests/test_storage_extensive.py` — round-trip a span of the new kind through memory + sqlite stores.
- `server/tests/test_telemetry_ingest.py` — ingest pipeline accepts the new kind and stores it.
- `frontend/src/__tests__/rpc/convert.test.ts` — conversion maps `PbSpanKind.MEMORY_OP` to `'MEMORY_OP'`.
- `frontend/src/__tests__/gantt/renderer.test.ts` — renderer applies the right color variable.

### 9. Verification

```bash
make proto
uv run pytest client/tests server/tests -x -q -k span
cd frontend && pnpm test -- --run
cd frontend && pnpm typecheck
```

## Common pitfalls

- **Adding the enum without updating every mirror**: the Python enum in `storage/base.py`, the client enum in `enums.py`, the TS literal union in `gantt/types.ts`, and the converter in `rpc/convert.ts` are four separate sources of truth. Missing any one will compile-fail or silently mis-convert.
- **Forgetting the CSS variable**: grey spans in the UI = missing `--hg-kind-*`. The renderer won't warn.
- **Task-binding semantics**: `_TASK_BINDING_SPAN_KINDS` in `ingest.py:56` determines which kinds bind to `hgraf.task_id` attributes for task state derivation. Don't auto-bind a new kind without thinking through whether it's meaningful.
- **Overusing the enum**: the design explicitly supports `SPAN_KIND_CUSTOM` + `kind_string` for per-framework extension. If only one framework needs it, use CUSTOM.
