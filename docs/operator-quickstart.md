# Operator Quickstart

A minimal walkthrough for getting a local Harmonograf instance running end-to-end: server, frontend, and one real agent emitting spans. Every command below is copy-pasteable from the repository root.

For deeper architectural context see [docs/design/03-server.md](design/03-server.md) and [docs/design/02-client-library.md](design/02-client-library.md).

## 1. Prerequisites

- Python 3.11+ with [`uv`](https://github.com/astral-sh/uv) on `PATH`
- Node 20+ with `pnpm`
- `git` (the presentation_agent demo pulls the ADK submodule)

## 2. Install

```bash
make install
```

This runs `uv sync` for `server/` and `client/`, `pnpm install --frozen-lockfile` for `frontend/`, and `git submodule update --init --recursive` to pull `third_party/adk-python`.

## 3. Run the server

```bash
make server-run
```

The `server-run` target invokes:

```
cd server && uv run python -m harmonograf_server --store sqlite --data-dir <repo>/data
```

Defaults you get from that invocation:

| Flag | Default | Notes |
|---|---|---|
| `--host` | `127.0.0.1` | Loopback only. Binding non-loopback emits a warning; v0 has no TLS. |
| `--port` | `7531` | Native gRPC (agent clients). |
| `--web-port` | `7532` | gRPC-Web via sonora + hypercorn (browser frontend). |
| `--store` | `sqlite` | Overridden by the Makefile target; use `--store memory` for ephemeral runs. |
| `--data-dir` | `~/.harmonograf/data` | Makefile target overrides this to `<repo>/data`. |
| `--log-level` | `INFO` | Also accepts `DEBUG`, `WARNING`, `ERROR`. |
| `--log-format` | `text` | Switch to `json` for log shippers. |

To run the server directly with different flags:

```bash
cd server && uv run python -m harmonograf_server \
    --store sqlite --data-dir ~/.harmonograf/data \
    --log-level DEBUG --log-format json
```

## 4. Run the frontend dev server

In a second terminal:

```bash
make frontend-dev
```

This runs `pnpm dev` under `frontend/` (Vite). The UI defaults to talking to the gRPC-Web listener at `http://127.0.0.1:7532`. To point it elsewhere, set `VITE_HARMONOGRAF_API` before starting Vite:

```bash
cd frontend && VITE_HARMONOGRAF_API=http://127.0.0.1:7532 pnpm dev
```

## 5. Run the presentation_agent demo

With the server running, drive one real ADK invocation into Harmonograf:

```bash
make demo-presentation
```

That expands to:

```
HARMONOGRAF_SERVER=127.0.0.1:7531 uv run --extra e2e python -m presentation_agent.run_harmonograf \
    --topic "Python programming" --server 127.0.0.1:7531
```

Override the topic or server address on the command line:

```bash
make demo-presentation TOPIC="Rust memory model" HARMONOGRAF_SERVER=127.0.0.1:7531
```

The demo prints `[harmonograf] session_id=<id>` as soon as the server assigns one — open the frontend and that session should materialize on the Gantt view while the coordinator → research → web_developer pipeline runs.

### Using a local OpenAI-compatible endpoint

`presentation_agent` defaults to `gemini-2.5-flash` (which routes through ADK's native Google models path and needs `GOOGLE_API_KEY` or ADC). To point it at a local OpenAI-compatible server (Ollama, vLLM, llama.cpp, LM Studio, anything that speaks `/v1/chat/completions`), set `USER_MODEL_NAME` to a LiteLLM provider-style identifier and export `OPENAI_API_BASE`:

```bash
export USER_MODEL_NAME="openai/qwen3.5:122b"
export OPENAI_API_BASE="http://kikuchi.lan:8080/v1"
# OPENAI_API_KEY is optional for local endpoints; the demo target defaults
# it to "dummy" if unset so LiteLLM stops complaining.
make demo
```

`presentation_agent/agent.py` detects provider-style strings (anything with a `/` before any `:`) and wraps them in `google.adk.models.lite_llm.LiteLlm`. Plain `gemini-*` names keep the native path and don't pull LiteLLM in. The `demo` / `demo-presentation` Makefile targets install LiteLLM via `uv run --extra demo …` — no extra steps required.

## 6. Health probes

Both endpoints live on the gRPC-Web port (`7532` by default) and are **always unauthenticated** so orchestrators can probe without credentials:

```bash
curl -sf http://127.0.0.1:7532/healthz   # -> "ok"
curl -sf http://127.0.0.1:7532/readyz    # -> "ready" (200) or "not ready" (503)
```

`/healthz` returns 200 as long as the process is serving. `/readyz` additionally calls `store.ping()` and returns 503 if the backing store is not reachable.

## 7. Bearer-token auth

Auth is off by default. To require a shared secret on every RPC (native gRPC *and* gRPC-Web), start the server with `--auth-token`:

```bash
cd server && uv run python -m harmonograf_server \
    --store sqlite --data-dir ~/.harmonograf/data \
    --auth-token "s3cret"
```

Clients then pass the same token:

```python
from harmonograf_client import Client

client = Client(
    name="my-agent",
    server_addr="127.0.0.1:7531",
    framework="ADK",
    token="s3cret",
)
```

For the presentation_agent demo, set the token on the command line (the sample currently does not thread a token through — use an unauthenticated dev server, or run the demo script directly and add `token=...` to the `Client(...)` call in `presentation_agent/run_harmonograf.py`).

`/healthz` and `/readyz` remain open regardless of `--auth-token`.

## 8. Logs, retention, and GC

Logs go to stderr. `--log-format text` (the default) is the human-readable formatter; `--log-format json` emits one JSON record per line with a stable `{ts, level, logger, msg}` shape plus any `extra={}` fields.

Retention is opt-in. By default, terminal (COMPLETED / ABORTED) sessions are kept forever. To sweep them:

```bash
cd server && uv run python -m harmonograf_server \
    --store sqlite --data-dir ~/.harmonograf/data \
    --retention-hours 24 \
    --retention-interval-seconds 300
```

- `--retention-hours 0` (default) disables the sweeper entirely.
- `--retention-hours N` deletes terminal sessions whose `ended_at` (or `created_at` if unset) is older than `N` hours.
- `--retention-interval-seconds` controls how often the sweeper wakes (default `300`). Live sessions are never touched.

Periodic metrics snapshots (sessions / spans / ingest rate / active streams) are emitted every `--metrics-interval-seconds` (default `30`); set to `0` to disable.

## 9. Known limitations

- **In-memory vs. sqlite.** `make server-run` uses `--store sqlite` against `<repo>/data`. `--store memory` is fine for tests but loses everything on restart.
- **sonora CORS shim.** The server monkey-patches `sonora.asgi` at startup to work around a bytes/str comparison bug in sonora's preflight path (see `server/harmonograf_server/_sonora_shim.py`, commit `5c00817`). An `INFO` line is logged on startup confirming the shim is active. If you upgrade sonora and the preflight path is fixed upstream, the shim can be removed.
- **No TLS.** The gRPC and gRPC-Web listeners are plaintext. Keep them on loopback; `--host` values other than `127.0.0.1` log a warning.
- **Frontend does not currently send a bearer token.** If you start the server with `--auth-token`, the UI will be rejected with 401 until frontend auth lands. Run the UI against an unauthenticated dev server for now.
- **Single-process, single-node.** There is no clustering, no replication, no horizontal scale. One server process owns the canonical timeline.
