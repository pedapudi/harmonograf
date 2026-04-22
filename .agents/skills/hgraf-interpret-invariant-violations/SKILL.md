---
name: hgraf-interpret-invariant-violations
description: Retained for history. Plan-state invariants moved to goldfive. This skill redirects.
---

# hgraf-interpret-invariant-violations

## Historical scope

`client/harmonograf_client/invariants.py` was removed during the
goldfive migration (issue #2). Plan-state invariants (monotonic
task transitions, terminal-status preservation, assignee validity)
now live in goldfive's `DefaultSteerer` / task-state code.

Harmonograf no longer validates plan-state invariants on its own.
Whatever goldfive emits over `TelemetryUp.goldfive_event` is the
truth from harmonograf's perspective.

## If you see violation-shaped log lines

They're coming from goldfive, not harmonograf. The log namespace
starts with `goldfive.*`. Inspect goldfive's invariant code and
open a goldfive issue if a genuine bug.

Harmonograf-side validations that remain (unrelated to plan state):

- **Payload digest mismatch** — `ingest.py` rejects an upload whose
  recomputed sha256 doesn't match the declared digest. This is
  about integrity, not plan state.
- **Hello frame shape** — `ingest.py` rejects malformed Hello frames
  (empty `agent_id`, etc.).
- **Wire proto enum validity** — the generated protobuf stubs
  refuse unknown enum values on decode.

For any of those, look at the message in the server log and trace
to the relevant handler.

## Cross-links

- goldfive repo for plan-state invariants.
- `dev-guide/debugging.md` for harmonograf-side integrity checks.
