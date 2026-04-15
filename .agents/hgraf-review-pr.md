---
name: hgraf-review-pr
description: Harmonograf-specific PR review checklist — cross-layer coherence, state machine safety, test coverage, UI regression screens.
---

# hgraf-review-pr

## When to use

You've been asked to review a PR on the harmonograf repo, or you're self-reviewing before pushing. Generic PR-review advice applies, but harmonograf has cross-cutting concerns (proto → client → server → frontend) that are easy to miss if you only look at one layer.

## Prerequisites

1. Read `AGENTS.md` at the repo root — the project vision, the three-component architecture, the plan execution protocol (`session.state` + reporting tools + callback inspection).
2. Know which batch-1 / batch-2 skill applies to the area being changed. For example: a proto field change should be reviewed against `hgraf-add-proto-field.md`; a drift kind change against `hgraf-add-drift-kind.md`. This review skill is the meta layer — it asks whether the right sub-skill was followed.
3. Have the PR checked out locally. Don't review from the web UI alone — you'll miss generated-file drift and cross-layer incoherence.

## The review passes

Do these in order. Don't jump around.

### Pass 1 — Scope and intent

- What problem is this PR solving? If the description doesn't say, that's your first comment.
- Is the scope coherent? Harmonograf changes frequently span layers, but a single PR should still be one conceptual change. A "refactor + bugfix + new feature" PR is a rebase nightmare.
- Are the commits logical? (Not required for landing, but makes bisect usable later.)

### Pass 2 — Cross-layer coherence

Harmonograf is a shared-schema system. When the data model changes, the change must land in all layers atomically. For each of the following, check both sides are consistent:

| Change type | Layers that must move together |
|---|---|
| Add/rename proto field | `proto/` + Python stubs (client + server) + TS stubs (frontend) + converters on both sides + persistence |
| New SpanKind / ControlKind / Capability / AnnotationKind | Proto enum + client emission + server routing + frontend rendering (convert.ts + colors.ts / hooks.ts) |
| New drift kind | `DriftReason` enum + detector path + throttle table + refine prompt awareness + tests |
| SQLite schema | `SCHEMA` dict + PRAGMA-guarded ALTER + all INSERT/SELECT statements (positional!) |
| Reporting tool | `_TOOL_DEFINITIONS` + before_tool_callback dispatcher + state_protocol keys + docs/reporting-tools.md |

If any column in the matching row is missing, that's a cross-layer coherence bug. File a comment.

### Pass 3 — State machine safety

The client's `_AdkState` is monotonic. Check:

- Does any new code write `task.status` directly? If so, it must go through `_set_task_status` (which enforces `_ALLOWED_TRANSITIONS`). Direct assignment is a bug unless inside `_apply_refined_plan`'s terminal-preservation loop.
- Does any refine path bypass `_apply_refined_plan`? Refines must go through that method — it preserves terminal statuses, bumps `revision_index`, canonicalizes assignees.
- Does any code touch `revision_index` by hand? Never — always through `_apply_refined_plan`.
- Does any new drift path bypass the throttle at `adk.py:3283`? Critical-severity drifts legitimately bypass; anything else should not.

Cross-reference with `hgraf-interpret-invariant-violations.md` for the 8-rule taxonomy.

### Pass 4 — Thread and async safety

- Does the PR add any blocking work inside ADK callbacks (`before_model_callback`, `after_model_callback`, `before_tool_callback`, `on_event_callback`)? These run on the hot path — blocking operations break the agent's perceived latency. Move to background.
- Does any buffer or transport change take a lock and then await? Deadlock risk.
- Does any change to `_run_invariants` add O(n²) work on a hot path? Invariants run after every walker turn; be budget-aware.

Cross-reference `hgraf-profile-callback-perf.md`.

### Pass 5 — Test coverage

For each behavior change, look for:

- **Unit test** at the smallest unit (e.g., converter, buffer, invariant).
- **Integration test** across at least two layers (client + ADK runner with FakeLlm, or server + SQLite round-trip).
- **Regression test** if the PR is fixing a bug — a test that would have failed before the fix.

Red flags:

- A bugfix PR with no new test.
- A test that asserts on log output instead of state.
- A test that uses `time.sleep` rather than deterministic monotonic-time injection.
- A test that creates a new `InvariantChecker` silently relying on module-level state (see pitfall in `hgraf-interpret-invariant-violations.md`).

### Pass 6 — Frontend regressions

For UI changes:

- Has the author actually run the dev server and clicked the feature? (The global `AGENTS.md` mandates this.) Ask explicitly — "did you verify in a browser?"
- Does the PR touch any component that owns shared state (`uiStore`, `SessionStore`, `TaskRegistry`)? If yes, every consumer needs a sanity check.
- Did the PR change a CSS variable in `index.css`? Grep for `var(--<name>)` across the frontend — if the variable is referenced from a component not touched by the PR, that component's visual state is affected.
- Does a snapshot test exist for the affected component? If the snapshot changed, does the diff match the intent?

### Pass 7 — Generated file drift

- `*_pb2.py`, `*_pb2.pyi`, `*_pb.ts` — generated files. If the proto changed, all of these should have been regenerated in the same PR. If only one language regenerated, comment.
- `pnpm-lock.yaml`, `uv.lock` — lockfiles. Changes are fine; unexpected changes (e.g., a PR that shouldn't touch deps but does) warrant a comment.
- `frontend/dist/` — should never be committed. If it is, reject.

### Pass 8 — Docs and memory

- If the PR adds a new external concept (a drift kind, a control kind, a reporting tool), should `docs/` gain an entry? `docs/reporting-tools.md` is the canonical reference for those.
- Does `AGENTS.md` at the root need an update to reflect the change? (Rare — only for architecture-level edits.)
- Batch-1 and batch-2 skills in `.agents/` — is there a skill that now needs updating because the PR changed its prerequisites (e.g., moved a file, renamed a symbol)?

## Specific harmonograf gotchas

### The 2-second refine throttle

`adk.py:378 _DRIFT_REFINE_THROTTLE_SECONDS = 2.0`. A new test that fires drifts in quick succession will see only the first one trigger a refine. New tests need deterministic monotonic-time injection or they flake.

### Terminal status preservation

`adk.py:3494-3520` preserves `RUNNING/COMPLETED/FAILED/CANCELLED` across refines. A change that "simplifies" this loop is a Gantt history regression — reject unless the author proves the preservation is being done elsewhere.

### Invariant ordering

`InvariantChecker.check` returns violations in stable order (`invariants.py:93-118`). Tests that filter `violations[0]` rely on that order. A refactor that reorders the check list will break those tests silently (they'll filter the wrong violation).

### Positional INSERT drift on schema additions

`hgraf-migrate-sqlite-schema.md` pitfall. If a PR adds a column and all INSERTs in the codebase use positional placeholders, the new column must be the LAST one, or every INSERT must update. Grep for `INSERT INTO <table>` across the server code.

### Control ack happens-before

`types.proto:387-390` — control acks must ride upstream on `StreamTelemetry`. A PR that routes control responses through a separate channel violates the contract; the server won't correlate them with the originating request.

### Span-telemetry separation

Spans are telemetry only; they do not drive task state. A PR that inspects span fields to infer task status is reintroducing the pre-refactor architecture. Reject.

## Communicating review feedback

- **Blocker** — "this is wrong and must change before merge". Use sparingly. State the rule that's broken.
- **Question** — "why this approach?". Use when the diff is surprising but might be right. The author's answer teaches you the constraint.
- **Nit** — "optional style". Prefix with `nit:`. Don't block on these.
- **Praise** — if something is notably well-done, say so. Review is a social contract; silent approval demoralizes.

One principle: the PR description is the first thing you read, the code is the second. If the description says "fixes X" but the code does Y, ask before reviewing the code — you might be reviewing with the wrong model in your head.

## Verification

```bash
# Run whichever tests overlap with the diff
git diff --stat main | awk '{print $1}' | grep -E '\.(py|ts)' | head
uv run pytest client/tests -x -q
uv run pytest server/tests -x -q
cd frontend && pnpm test && pnpm typecheck && pnpm lint
```

## Common pitfalls in reviews

- **Reviewing only the +/- lines**: cross-layer coherence lives in files the PR doesn't touch. Always grep for callers of changed symbols.
- **Trusting the CI green light**: CI runs unit tests, not the whole integration surface. A green CI doesn't mean "works end-to-end with real LLM".
- **Nitpicking style over substance**: leave the formatting comments to the linter. Focus on correctness and design.
- **Approving without running the PR**: for UI or end-to-end changes, pull the branch and run it. A working screenshot beats a convincing argument.
- **Assuming the tests exist because they passed**: check the actual test count delta. A PR with no new tests for behavior changes is under-tested regardless of CI.
- **Skipping the memory check**: harmonograf's `.agents/` skills and the project memory at `~/.claude/projects/.../memory/` may carry invariants that aren't in the PR diff. If the change contradicts a saved rule, ask the author about it.
