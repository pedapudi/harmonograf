---
name: hgraf-run-demo
description: Launch the full harmonograf demo stack (server + Vite frontend + adk web presentation_agent), drive a prompt, tail logs, and capture a trace.
---

# hgraf-run-demo

## When to use

- You need a live end-to-end system to watch a scenario execute in the Gantt.
- Reproducing a drift banner, minimap, or context-window-overlay bug that only appears with real streaming telemetry.
- Demoing a new feature interactively, or capturing a trace (spans + task plan) into `data/` to replay later.
- You want to manually prompt the presentation_agent and see the resulting spans flow through ingest → bus → frontend.

Do **not** use this skill for unit-level work. Prefer `tests/e2e/test_scenarios.py` with `FakeLlm` (see `hgraf-write-e2e-scenario`) when you want hermetic, deterministic behavior — `make demo` talks to a real LLM backend and is sensitive to network + API keys.

## Prerequisites

1. The three install steps are done:
   ```bash
   make install   # runs server-install, client-install, frontend-install + git submodule update
   ```
   This hits `uv sync` under `server/` and `client/`, and `pnpm install --frozen-lockfile` under `frontend/`.

2. Proto stubs exist. Run once (and any time a `.proto` file changes):
   ```bash
   make proto     # proto-python + proto-ts
   ```
   See `Makefile` targets `proto-python` and `proto-ts`. `proto-ts` requires `frontend/buf.gen.yaml`; if missing it silently skips.

3. An OpenAI API key in the environment. The demo reads `OPENAI_API_KEY`; if unset, the Makefile falls back to the literal string `"dummy"` which will fail on the first model call. Export a real key:
   ```bash
   export OPENAI_API_KEY=sk-...
   ```

4. Ports free: **7531** (gRPC server), **5173** (Vite), **8080** (adk web). Override with `SERVER_PORT=`, `FRONTEND_PORT=`, `ADK_WEB_PORT=` (see `Makefile:demo`).

## Step-by-step

### 1. Launch the stack

```bash
cd /home/sunil/git/harmonograf
make demo
```

`Makefile:demo` is a single bash `-eu` script that:
1. Calls `.demo-agents-stage` to regenerate `.demo-agents/presentation_agent/` (a passthrough package — **not a symlink**, because ADK's `_resolve_agent_dir` rejects symlinks that escape the sandbox; see the comment block in `Makefile` above the `.demo-agents-stage` rule).
2. Starts `harmonograf-server` with `--store sqlite --data-dir data/`.
3. Starts `pnpm dev --port 5173 --strictPort`.
4. Starts `adk web --host 127.0.0.1 --port 8080 .demo-agents` with `HARMONOGRAF_SERVER=127.0.0.1:7531`.
5. Prints the three URLs and `wait`s. Ctrl-C triggers the trap, which kills all three children.

You should see:
```
==================================================================
  Harmonograf UI     : http://127.0.0.1:5173
  ADK web (agent UI) : http://127.0.0.1:8080
  harmonograf-server : 127.0.0.1:7531 (gRPC)
==================================================================
```

### 2. Drive a prompt

- Open the **ADK web UI** at `http://127.0.0.1:8080`. Select `presentation_agent`. Type your prompt in the chat panel.
- Open the **Harmonograf UI** at `http://127.0.0.1:5173` in a second tab. You should see a new session appear in the session list within ~1s of the first span.
- Click the session to open the Gantt. Spans stream in live.

The presentation agent itself is defined in `presentation_agent/agent.py`. It is instrumented through `HarmonografAgent` which auto-registers reporting tools into every `LlmAgent` in its sub-tree (`client/harmonograf_client/agent.py:335-355` — `_auto_register_reporting_tools`).

### 3. Tail logs

`make demo` runs all three processes in the same terminal, interleaving stdout. If you want the three streams separate, launch each target in its own terminal:
```bash
# terminal 1
make server-run
# terminal 2
make frontend-dev
# terminal 3
make demo-presentation
```

The server's log level can be bumped via `PYTHONLOGLEVEL=DEBUG` or `HARMONOGRAF_LOG_LEVEL=DEBUG` (see `server/harmonograf_server/logging_setup.py`). Client library log level uses Python's standard `logging` module under the `harmonograf_client` logger.

### 4. Capture a trace for offline replay

With `--store sqlite --data-dir data/`, every session is persisted to `data/harmonograf.sqlite` (schema in `server/harmonograf_server/storage/sqlite.py:45-170`). After driving the prompt:
1. Ctrl-C `make demo`.
2. Copy `data/harmonograf.sqlite` to a fixture location (e.g. `tests/fixtures/traces/<scenario>.sqlite`).
3. Replay by pointing a server at it:
   ```bash
   uv run python -m harmonograf_server --store sqlite --data-dir tests/fixtures/traces/
   ```

If you only need a specific session, use `make stats` to inspect session counts, or query the sqlite file directly:
```bash
sqlite3 data/harmonograf.sqlite "SELECT id, title, status, started_at FROM sessions ORDER BY started_at DESC LIMIT 5;"
```

## Verification

- `curl http://127.0.0.1:5173/` returns the Vite dev HTML.
- `curl -s http://127.0.0.1:8080/` returns the ADK web HTML.
- `make stats` prints a `GetStatsResponse` with non-zero sessions.
- In the Gantt, verify: (1) agent rows appear, (2) spans have coloured blocks, (3) the current-task strip at the top-left shows the active task after the first `report_task_started` reporting tool fires, (4) no `ERROR` logs in the server stdout.

## Common pitfalls

- **`OPENAI_API_KEY=dummy` is the default fallback.** The Makefile uses `$${OPENAI_API_KEY:-dummy}`. If your first call fails with `401 Unauthorized` you forgot to export a real key.
- **Port collisions.** `pnpm dev --strictPort` will refuse to start if 5173 is taken; the trap in `make demo` will then kill the server and adk web too because `bash -eu` exits. Either free the port or set `FRONTEND_PORT=5174`.
- **`.demo-agents/` is regenerated every run.** Do not edit files under `.demo-agents/` — your changes are wiped. Edit `presentation_agent/agent.py` instead; the passthrough loads it by absolute file path (see `Makefile:.demo-agents-stage`).
- **Symlinks under `.demo-agents/` will break ADK.** ADK's `_resolve_agent_dir` calls `Path.resolve()` and refuses paths that escape the agents root. The passthrough pattern in the Makefile is load-bearing — do not "simplify" it to a symlink.
- **Frontend cache.** If the UI shows stale state after a schema or renderer change, hard-reload (Ctrl-Shift-R). Vite HMR will **not** pick up changes to generated files under `frontend/src/pb/` — restart `pnpm dev`.
- **SQLite file retained across runs.** `data/harmonograf.sqlite` accumulates sessions forever until you delete it. Clear with `rm data/harmonograf.sqlite` before a clean-slate run.
- **Heartbeat timeout = 15s, stuck threshold = 3 beats.** See `server/harmonograf_server/ingest.py:62-65`. If you pause a debugger inside the agent process for >45s, the server will mark the agent STUCK and the UI will stripe its row. That is correct behavior — not a bug.
