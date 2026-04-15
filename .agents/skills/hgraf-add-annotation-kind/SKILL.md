---
name: hgraf-add-annotation-kind
description: Add a new AnnotationKind — proto, storage, ingress RPC, optional downstream delivery to agents, frontend authoring UI, tests.
---

# hgraf-add-annotation-kind

## When to use

You need a new kind of user-authored note attached to a span or agent-time point. Existing kinds (`types.proto:306-311`):

- `ANNOTATION_KIND_COMMENT` — UI-only, never delivered to the agent.
- `ANNOTATION_KIND_STEERING` — delivered to the agent as a `CONTROL_KIND_STEER` control event.
- `ANNOTATION_KIND_HUMAN_RESPONSE` — answers a `WAIT_FOR_HUMAN` span.

Add a new kind when the delivery semantics differ from all three above.

## Prerequisites

1. Read `proto/harmonograf/v1/types.proto:304-337` — `Annotation`, `AnnotationKind`, `AnnotationTarget`, `AgentTimePoint`.
2. Read `server/harmonograf_server/rpc/frontend.py` — the `PostAnnotation` RPC implementation; it's where STEERING fans out into `ControlRouter.deliver(...)`. Grep for `CONTROL_KIND_STEER` to find the existing pattern.
3. Decide delivery semantics: UI-only (like COMMENT), one-shot control event (like STEERING), or ack-bound response (like HUMAN_RESPONSE).

## Step-by-step

### 1. Proto edit

`proto/harmonograf/v1/types.proto:306`:

```proto
enum AnnotationKind {
  ANNOTATION_KIND_UNSPECIFIED = 0;
  ANNOTATION_KIND_COMMENT = 1;
  ANNOTATION_KIND_STEERING = 2;
  ANNOTATION_KIND_HUMAN_RESPONSE = 3;
  ANNOTATION_KIND_REDACT_PII = 4;
}
```

### 2. Regen

```bash
make proto
```

### 3. Python enum mirror

`server/harmonograf_server/storage/base.py` has a Python `AnnotationKind` Enum. Add the value. Then extend `_ANNOT_KIND_TO_STORAGE` / `_STORAGE_TO_ANNOT_KIND` maps in `server/harmonograf_server/convert.py`.

### 4. Storage

The `annotations` table in `sqlite.py:105` stores `kind TEXT NOT NULL`. Existing values are written as the enum **name without the prefix** (e.g., `"STEERING"`). No schema migration needed — TEXT columns accept any value. Just make sure `convert.py` writes the consistent short name.

### 5. Server-side dispatch in PostAnnotation

`server/harmonograf_server/rpc/frontend.py :: PostAnnotation` is the fan-out point. Current logic:

```python
if kind == ANNOTATION_KIND_STEERING:
    await control_router.deliver(... CONTROL_KIND_STEER ...)
elif kind == ANNOTATION_KIND_HUMAN_RESPONSE:
    # resolve the awaiting span
    ...
```

Add a branch for your new kind. If the semantics map to a control event, issue it through `control_router.deliver(...)` and set `annotation.delivered_at` when the ack arrives (see `types.proto:336`). If semantics are UI-only, just store and broadcast — no control event.

### 6. Watch delta

When the annotation is stored, the `PostAnnotation` handler publishes a delta onto `SessionBus` so all `WatchSession` subscribers see it. Grep `bus.publish` in `rpc/frontend.py` for the existing pattern. New kinds typically inherit this path for free.

### 7. Frontend

`frontend/src/rpc/convert.ts` maps `PbAnnotationKind` → UI string union. Extend it. The UI annotation type lives in `frontend/src/gantt/types.ts` or alongside.

`frontend/src/components/shell/Drawer.tsx :: AnnotationsTab` is the authoring surface. It currently exposes COMMENT / STEERING buttons via `useSendControl` + `usePostAnnotation`. Add a button and wire it to `usePostAnnotation({ kind: 'REDACT_PII', ... })`.

### 8. Tests

- `server/tests/test_rpc_frontend.py` — PostAnnotation persists the new kind and fires delivery if semantics require it.
- `server/tests/test_storage_extensive.py` — round-trip through memory + sqlite stores.
- `server/tests/test_control_e2e.py` — if your kind dispatches a control event, the ack path works.
- `frontend/src/__tests__/components/AnnotationsTab.test.tsx` — button exists, calls the hook with the right kind.

### 9. Verification

```bash
make proto
uv run pytest server/tests -x -q -k annotation
cd frontend && pnpm test -- --run Annotation
cd frontend && pnpm typecheck
```

## Common pitfalls

- **Silent UI-only**: forgetting the PostAnnotation dispatch branch leaves your kind as "store and broadcast only", which may be fine but is easy to miss if you *intended* delivery.
- **`delivered_at` never set**: if your kind has a control event, remember to update the `annotations.delivered_at` column when the ack arrives. Without it the UI won't show the delivery confirmation.
- **Enum name drift**: storage uses short names (`"COMMENT"`, not `"ANNOTATION_KIND_COMMENT"`). Maintain the shortening consistently in `convert.py`.
- **Bus delta not published**: if the new kind is added through a custom code path that bypasses `bus.publish`, `WatchSession` subscribers won't see it live — only on the next full `GetSession` refresh.
