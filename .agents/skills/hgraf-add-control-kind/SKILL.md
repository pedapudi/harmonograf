---
name: hgraf-add-control-kind
description: Add a new ControlKind — capability advertisement, client handler, server routing, ack semantics, frontend button, and tests — all the way through.
---

# hgraf-add-control-kind

## When to use

You are adding a new user-triggered action that flows frontend → server → agent (e.g. `RETRY_LAST_TOOL`, `RESTART_AGENT`). This crosses every layer: proto, capabilities, routing, dispatch, UI, ack path.

For pure observability (no agent response required), prefer an annotation kind instead — see `hgraf-add-annotation-kind.md`.

## Prerequisites

1. Read `proto/harmonograf/v1/types.proto:345-403` — the `ControlKind` enum, `ControlEvent`, `ControlAck`, `ControlAckResult`.
2. Read `proto/harmonograf/v1/control.proto` — there is no control envelope there beyond `SubscribeControlRequest`; control payload itself lives in `types.proto`.
3. Understand happens-before: acks ride upstream on `StreamTelemetry` (folded into `TelemetryUp.control_ack`), not on `SubscribeControl`. See [`docs/protocol/control-stream.md`](../../../docs/protocol/control-stream.md).
4. Decide whether the new kind requires a new `Capability` (it probably does — advertise new verbs so the frontend greys out agents that don't support them).

## Step-by-step

### 1. Add the enum value

Edit `proto/harmonograf/v1/types.proto:345`:

```proto
enum ControlKind {
  // ...existing 0..10...
  // payload: json-encoded retry policy
  CONTROL_KIND_RETRY_LAST_TOOL = 11;
}
```

Document the payload format inline (utf-8 text, JSON, span id, etc.) — this is the only source of truth agent authors will read.

### 2. Optionally add a Capability

Same file, around `types.proto:57-65`:

```proto
enum Capability {
  // ...existing...
  CAPABILITY_RETRY_TOOL = 7;
}
```

Then extend the `CAPABILITY_TO_CONTROLS` / `CONTROL_TO_CAPABILITY` mapping if one exists — grep `grep -rn "CAPABILITY_RETRY\|capability_for" server/ client/` to find the lookup table. If none exists yet, create one in `server/harmonograf_server/rpc/frontend.py` next to the existing ControlKind handling.

### 3. Regenerate stubs

```bash
make proto
```

Verify `git status` shows updated `*_pb2.py`, `*_pb2.pyi`, and `frontend/src/pb/harmonograf/v1/types_pb.ts`.

### 4. Client-side handler registration

Control dispatch in the client lives in `client/harmonograf_client/transport.py:649 _dispatch_control`. It resolves the enum name via `_control_kind_name` (`transport.py:700`) which strips the `CONTROL_KIND_` prefix — so your new kind dispatches under the key `"RETRY_LAST_TOOL"`.

Handlers are registered via `Client.on_control(kind, cb)` — see
`client/harmonograf_client/client.py :: on_control`. The callback
receives a `ControlEvent` and returns an optional
`ControlAckSpec(result="success"|"failure"|"unsupported", detail="...")`.

For STEER / CANCEL / PAUSE-like kinds, goldfive's control bridge
(`client/harmonograf_client/_control_bridge.py`) takes over via
`Client.set_control_forward(...)` and handles dispatch through to
goldfive's steerer. When adding a new user-control kind, make sure:

- The server's `PostAnnotation` fan-out (rpc/frontend.py) propagates
  the annotation's `author` and `id` onto the control event — STEER
  does this via `event.steer.author` / `event.steer.annotation_id`
  (goldfive#171).
- Goldfive emits a matching drift kind so the intervention
  aggregator surfaces it on the timeline. See `hgraf-add-drift-kind`.

Example callback body (for non-STEER/CANCEL kinds handled directly):

```python
def _handle_retry_last_tool(event):
    try:
        # ...actual retry logic...
        return ControlAckSpec(result="success", detail="retried tool foo")
    except Exception as exc:
        return ControlAckSpec(result="failure", detail=str(exc))

client.on_control("RETRY_LAST_TOOL", _handle_retry_last_tool)
```

### 5. Server-side frontend → agent routing

The `SendControl` RPC in `server/harmonograf_server/rpc/frontend.py` receives a `ControlEvent` from the frontend and hands it to `ControlRouter.deliver` (`server/harmonograf_server/control_router.py`). The router is kind-agnostic: no change required there unless your kind has special delivery semantics.

If the new kind must be denied for agents lacking the matching Capability, add a check in `rpc/frontend.py :: SendControl` before calling `deliver` (grep for `CAPABILITY_PAUSE_RESUME` or similar as a reference point).

If the new kind needs special ack handling (e.g. STATUS_QUERY is broadcast back to all `WatchSession` subscribers — see `control_router.py:304`), add a matching branch in `ControlRouter.record_ack`.

### 6. Frontend button and kind mapping

Extend `frontend/src/rpc/hooks.ts:530 CONTROL_KIND`:

```ts
const CONTROL_KIND: Record<string, ControlKind> = {
  // ...existing entries...
  RETRY_LAST_TOOL: ControlKind.RETRY_LAST_TOOL,
};
```

And `SendControlArgs.kind` widens automatically because it's `keyof typeof CONTROL_KIND`.

Add the button in `frontend/src/components/shell/Drawer.tsx` under the `ControlTab` — grep for `useSendControl` and follow the existing PAUSE/RESUME precedent. Grey out the button when `agent.capabilities` doesn't include the matching capability; you read capabilities off the `Agent` in the session store.

### 7. Tests

- `client/tests/test_protocol_callbacks.py` or `test_adk_adapter.py` — handler fires and builds the expected ack.
- `server/tests/test_control_e2e.py` — end-to-end SendControl → ControlRouter → telemetry ack round trip.
- `server/tests/test_control_router.py` — the new kind routes correctly and honors capability gating.
- `frontend/src/__tests__/components/Drawer*.test.tsx` — button rendered, greyed out when capability missing, calls `useSendControl` with the right kind string.

### 8. Verification

```bash
make proto
uv run pytest client/tests server/tests -x -q -k "control or capability"
cd frontend && pnpm test -- --run Drawer
cd frontend && pnpm typecheck
```

## Common pitfalls

- **Unsupported fallthrough**: if you forget to register a handler, `_dispatch_control` (`transport.py:653`) emits an `UNSUPPORTED` ack — which looks like a working UI but silently no-ops. Add a unit test that exercises the handler, not just the button.
- **Capability/kind drift**: adding the `ControlKind` but forgetting the `Capability` means every agent will accept the control by default. Decide up front whether this is OK.
- **Wire enum ordering**: `_control_kind_name` uses the generated `ControlKind.Name(int)` lookup. If you reuse a number (which proto3 forbids anyway) or renumber, every serialized ack breaks. Always append at the end.
- **Forgetting payload docs**: the `ControlKind.payload` bytes field is opaque; the only contract is the comment in `types.proto`. If you skip the comment, other agent authors will guess wrong.
- **Broadcast on ack**: only `STATUS_QUERY` currently re-broadcasts its ack detail to `WatchSession` subscribers (`control_router.py:304`). If you want similar behavior for your new kind, mimic that branch explicitly.
