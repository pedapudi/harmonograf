---
name: hgraf-add-capability
description: Add a new agent Capability — proto enum, client advertisement, server ingest, frontend gating in the control UI.
---

# hgraf-add-capability

## When to use

You need the frontend to know whether an agent supports a particular operation and grey out UI affordances for agents that don't. Capabilities are self-advertised on `Hello` and stored alongside the agent row. Every non-universal `ControlKind` should pair with a `Capability`.

## Prerequisites

1. Read `proto/harmonograf/v1/types.proto:57-65 Capability` and `types.proto:71-82 Agent.capabilities`.
2. Identify the `ControlKind` (or set of kinds) this capability gates. Common case: one capability per control kind; sometimes one capability gates multiple kinds (e.g. `CAPABILITY_PAUSE_RESUME` covers both `PAUSE` and `RESUME`).
3. Understand that capabilities flow **only client → server → frontend**. There is no negotiation — the client declares, the frontend obeys.

## Step-by-step

### 1. Proto edit

`proto/harmonograf/v1/types.proto:57`:

```proto
enum Capability {
  CAPABILITY_UNSPECIFIED = 0;
  // ...existing 1..6...
  CAPABILITY_RETRY_TOOL = 7;
}
```

Append at the end.

### 2. Regen

```bash
make proto
```

### 3. Client advertisement

The client declares capabilities in the `Hello` message. Search for where the existing list is built:

```bash
grep -rn "CAPABILITY_\|capabilities=" client/harmonograf_client/
```

Typical path: `client/harmonograf_client/identity.py` or the `HarmonografClient` constructor in `client/harmonograf_client/client.py` takes a `capabilities=[...]` arg and forwards it into `Hello.capabilities` via `client/harmonograf_client/transport.py :: _build_hello`.

For the ADK plugin, `client/harmonograf_client/adk.py :: make_adk_plugin` decides the default capabilities list. Add the new capability there only if every ADK-backed agent supports the matching control kind; otherwise leave it opt-in.

### 4. Server ingest → storage

`server/harmonograf_server/ingest.py :: handle_hello` reads `hello.capabilities` and writes them onto the `StreamContext` and through to `Store.upsert_agent`. The storage layer stores capabilities as a JSON list of enum names (see `server/harmonograf_server/storage/sqlite.py` `agents` table, `capabilities TEXT NOT NULL`).

The conversion from proto enum int → string name happens in `server/harmonograf_server/convert.py`. Grep for `Capability.Name` or the existing `_CAPABILITY_TO_STORAGE` map. Add an entry.

### 5. Frontend wire conversion

`frontend/src/rpc/convert.ts` converts `PbCapability` → UI `Capability`. Grep for `Capability.PAUSE_RESUME` in that file and add the new entry. The UI `Capability` type lives in `frontend/src/gantt/types.ts` or adjacent; add the string literal.

### 6. Frontend gating

Wherever the UI renders a control button (typically `frontend/src/components/shell/Drawer.tsx` `ControlTab` and `frontend/src/components/TransportBar/TransportBar.tsx`), grey out buttons whose matching capability is absent:

```tsx
const disabled = !agent.capabilities.includes('RETRY_TOOL');
<button disabled={disabled} ...>Retry</button>
```

If the button is shared across multiple agents in a multi-agent view, decide whether it should require all agents to support the capability (strict) or any agent (permissive). The existing pause/resume button is strict — see `TransportBar.tsx`.

### 7. Tests

- `client/tests/test_adk_adapter.py` — client Hello carries the new capability when configured.
- `server/tests/test_telemetry_ingest.py :: test_hello_capabilities_persisted` — capability survives the round-trip into storage.
- `server/tests/test_rpc_frontend.py` — `WatchSession` emits agents with the new capability string.
- `frontend/src/__tests__/components/TransportBar.test.tsx` or equivalent — button disabled when the agent lacks the capability.

### 8. Verification

```bash
make proto
uv run pytest client/tests server/tests -x -q -k capability
cd frontend && pnpm test -- --run
cd frontend && pnpm typecheck
```

## Common pitfalls

- **Capability advertised but control kind missing**: adding a capability without the corresponding `ControlKind` is harmless (the UI never wires it up) but confusing. Usually you want both.
- **Default-enabled on ADK**: flipping a capability on inside `make_adk_plugin` enables it for every ADK agent automatically — ensure every ADK agent really does handle the matching control kind.
- **Capabilities are not dynamic**: the `Hello` is the only opportunity to declare capabilities. A capability that becomes available mid-session requires a reconnect with a new `Hello`.
- **Frontend still shows button for unknown capability**: if `rpc/convert.ts` omits a case, the converter drops the enum value silently. Add a default branch that logs once in development.
