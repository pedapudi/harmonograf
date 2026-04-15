# Harmonograf quickstart

This is the step-by-step walk-through: clone the repo, install all three
components, point an ADK agent at a local LLM, and watch one real rollout
populate the harmonograf timeline in real time. Copy-paste each block in order;
it should take under ten minutes on a warm machine.

If you just want a list of server flags and ops knobs, jump to
[operator-quickstart.md](operator-quickstart.md) instead. If you want the
motivation and design context, read [overview.md](overview.md) first.

---

## Prerequisites

You need the following on your `PATH`:

| Tool | Minimum version | Notes |
|---|---|---|
| Python | 3.11 | Required by the server and client libraries. |
| [`uv`](https://github.com/astral-sh/uv) | recent | The server and client live in `uv`-managed projects. `pip` is not a supported substitute. |
| Node | 20 | Required by the Vite frontend. |
| `pnpm` | recent | The frontend uses `pnpm` with a frozen lockfile. |
| `git` | any | The ADK third-party submodule is pulled by `make install`. |

Plus one of:

- A reachable **OpenAI-compatible endpoint** (Ollama, vLLM, llama.cpp, LM Studio,
  any OpenAI-compatible proxy). The demo in this guide assumes one.
- Or a `GOOGLE_API_KEY` for the default `gemini-2.5-flash` routing, if you would
  rather use Google's hosted models.

---

## Step 1 — Clone

```bash
git clone https://github.com/<your-org>/harmonograf.git
cd harmonograf
```

From here on, every command assumes you are at the repository root.

## Step 2 — Install

```bash
make install
```

This does four things:

1. `uv sync` under `server/` — installs the server's Python deps.
2. `uv sync` under `client/` — installs the client library's Python deps.
3. `pnpm install --frozen-lockfile` under `frontend/` — installs frontend deps.
4. `git submodule update --init --recursive` — pulls `third_party/adk-python`,
   which the end-to-end test and some of the ADK integration tests need.

Expected wall-clock time: 30–90 seconds on a warm machine. If `uv` has never
hydrated its cache it can take longer on the first run.

If you edit any `.proto` file under `proto/harmonograf/v1/` you will also need:

```bash
make proto
```

On a fresh clone the pre-generated stubs under `server/harmonograf_server/pb/`,
`client/harmonograf_client/pb/`, and `frontend/src/pb/` already match the proto
sources, so `make proto` is not strictly required on first install. Run it
whenever you change a proto file.

## Step 3 — Point at an LLM

The demo drives a five-agent `presentation_agent` rollout: coordinator → research
→ web_developer → reviewer → (debugger when needed). You have two choices for
where the LLM calls actually land.

### Option A — Local OpenAI-compatible endpoint (recommended)

`presentation_agent` detects provider-style model strings (anything with a `/`
before any `:`) and wraps them in LiteLLM, which routes through any OpenAI-compatible
`/v1/chat/completions` endpoint. To wire that up, export:

```bash
export OPENAI_API_BASE=http://localhost:8080/v1
export OPENAI_API_KEY=dummy
export USER_MODEL_NAME=openai/qwen3.5:122b
```

- `OPENAI_API_BASE` — base URL of your endpoint. The example points at
  `localhost:8080`; replace with wherever your local LLM is serving.
- `OPENAI_API_KEY` — most local endpoints ignore this, but LiteLLM complains if
  it is unset. `dummy` is fine. The `make demo` target defaults it to `dummy`
  for you if unset.
- `USER_MODEL_NAME` — a LiteLLM provider-style identifier. `openai/qwen3.5:122b`
  works for any OpenAI-compatible endpoint; substitute the model your endpoint
  actually serves.

### Option B — Google hosted (Gemini)

If you skip the three env vars above, `presentation_agent` defaults to
`gemini-2.5-flash` through ADK's native Google models path. That path needs
`GOOGLE_API_KEY` (or Application Default Credentials) to be configured. This
route does not pull LiteLLM into the runtime.

## Step 4 — Boot the demo stack

```bash
make demo
```

`make demo` starts three processes in one foreground shell under a shared
trap-on-exit, so Ctrl-C takes all three down together:

| Process | URL | What it is |
|---|---|---|
| `harmonograf-server` | `127.0.0.1:7531` (gRPC) / `127.0.0.1:7532` (gRPC-Web) | The canonical timeline. Writes to `./data/` via SQLite. |
| Vite frontend | `http://127.0.0.1:5173` | The harmonograf UI. Talks gRPC-Web to `:7532`. |
| `adk web` | `http://127.0.0.1:8080` | ADK's own UI, hosting a staged copy of `presentation_agent` under `.demo-agents/`. |

Once all three are up `make demo` prints a summary block with the three URLs and
waits. The harmonograf frontend is the one you care about; the ADK tab is where
you actually drive a rollout.

The ports are overridable:

```bash
make demo SERVER_PORT=17531 FRONTEND_PORT=15173 ADK_WEB_PORT=18080
```

## Step 5 — Drive a rollout

1. Open the **ADK tab** at `http://127.0.0.1:8080`. You should see ADK's chat UI
   with `presentation_agent` available.
2. In the ADK chat, type a prompt like:

   ```
   Build a slide deck about the Python programming language with
   five slides, including an example snippet.
   ```

3. Switch to the **harmonograf tab** at `http://127.0.0.1:5173`. The session
   picker should auto-select the newest live session. You should see:
   - A row per agent (`coordinator_agent`, `research_agent`,
     `web_developer_agent`, `reviewer_agent`, and `debugger_agent` if the
     reviewer surfaces issues).
   - Blocks on the timeline filling in as each task moves through started →
     running → completed.
   - A live-tail cursor following the head of the run.
   - A plan-diff banner if the coordinator replans mid-run (e.g. because the
     reviewer flagged an issue and the debugger was added).

4. Click any block to open the **inspector drawer** and see the tool call
   arguments, return value, and any payloads.

5. When you are done, Ctrl-C the `make demo` shell. All three processes exit
   together.

---

## Expected output

A healthy first run looks like this on stdout:

```text
[demo] starting harmonograf-server on :7531 ...
[demo] starting frontend Vite dev server on :5173 ...
[demo] starting adk web presentation_agent on :8080 ...

==================================================================
  Harmonograf UI     : http://127.0.0.1:5173
  ADK web (agent UI) : http://127.0.0.1:8080
  harmonograf-server : 127.0.0.1:7531 (gRPC)
==================================================================
  Press Ctrl-C to stop all three.
```

In the harmonograf frontend, a successful rollout looks like:

- 4–5 agent rows populated within a few seconds of sending the ADK prompt.
- Task blocks that transition colour as they complete (harmonograf uses a
  monotonic state machine — blocks never go backwards).
- A small liveness indicator on each row that pulses while the agent is
  actively emitting.
- A completed session badge at the top of the timeline when the coordinator
  calls `report_task_completed` on the root task.

---

## Troubleshooting

**The ADK tab loads but `presentation_agent` is not listed.**
`.demo-agents/` is regenerated fresh on every `make demo` invocation. If it is
missing, something interrupted the `.demo-agents-stage` step. Run `make demo`
again; on a clean second invocation the stage step should succeed. If it keeps
failing, look for a stale `.demo-agents/` directory and remove it manually, then
re-run.

**`make demo` exits immediately with "address already in use".**
One of `:7531`, `:7532`, `:5173`, or `:8080` is already bound. Either stop the
other process or override the port:

```bash
make demo SERVER_PORT=17531 FRONTEND_PORT=15173 ADK_WEB_PORT=18080
```

**The harmonograf frontend loads but shows "Server unreachable — showing demo
sessions".** The Vite dev server is up but cannot reach the gRPC-Web listener.
Check that `harmonograf-server` is actually running (look for `[demo] starting
harmonograf-server on :7531 ...` followed by no error in the `make demo`
output), and verify `VITE_HARMONOGRAF_API` — if set, it overrides the default
`http://127.0.0.1:7532`. Unset it, or set it to match the server's `--web-port`.

**ADK errors with `LiteLLM` complaining about `OPENAI_API_KEY`.**
Export `OPENAI_API_KEY=dummy` before `make demo`. Most local endpoints ignore
the value but LiteLLM still requires the env var to be present. The `make demo`
target defaults it to `dummy` when unset; if you are running the underlying
commands by hand, set it yourself.

**The agent calls reporting tools but nothing shows up in harmonograf.**
Verify the `HARMONOGRAF_SERVER` env var matches the server's gRPC port (`7531`
by default). `make demo` sets this for you; if you are running the agent
outside the Makefile target, set it explicitly:

```bash
HARMONOGRAF_SERVER=127.0.0.1:7531 uv run --extra demo adk web .demo-agents
```

**Tasks appear in harmonograf but never transition past `STARTED`.**
The agent is emitting spans but not calling `report_task_completed`. This is
usually a model issue — smaller or poorly-prompted models sometimes describe
their work in prose instead of calling the reporting tools. Harmonograf has a
belt-and-suspenders path (`after_model_callback` scans for structured
signals), but the canonical contract is the reporting tools. Either switch to
a stronger model or tighten the sub-agent instruction to require the tools.
See [reporting-tools.md](reporting-tools.md) for the full protocol.

**SQLite errors on startup.**
`make demo` points the server at `<repo>/data/` via `--data-dir`. If that
directory is on a filesystem that doesn't support SQLite WAL mode (some network
mounts), switch to in-memory for quick experiments:

```bash
cd server && uv run python -m harmonograf_server --store memory
```

In-memory loses everything on restart but is fine for one-off testing.

**`make install` fails pulling the ADK submodule.**
Your `git` probably can't reach GitHub. The submodule is optional for running
the demo itself — only the end-to-end test under `tests/e2e/` needs it. You can
skip the submodule and proceed with the rest of `make install` by running the
individual install targets:

```bash
make server-install client-install frontend-install
```

---

## What to read next

- [overview.md](overview.md) — motivation, design principles, what ships, what
  doesn't, roadmap.
- [operator-quickstart.md](operator-quickstart.md) — every server flag, health
  probes, retention, bearer-token auth.
- [reporting-tools.md](reporting-tools.md) — the canonical agent ↔ harmonograf
  protocol.
- [docs/user-guide/](user-guide/) — *(task #6, in progress)* the UI walk-through
  with screenshots and keyboard shortcuts.
- [docs/dev-guide/](dev-guide/) — *(task #7, in progress)* building from source,
  adding a storage backend, writing tests, contribution workflow.
- [docs/protocol/](protocol/) — *(task #8, in progress)* proto reference,
  session-state schema, drift taxonomy, plan-diff semantics.
