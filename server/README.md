# harmonograf-server

gRPC fan-in for multi-agent telemetry and control, plus the self-served
console UI.

## Ports

- `--port` (default `7531`) — native gRPC, for agents/SDKs.
- `--web-port` (default `7532`) — gRPC-Web (for browsers) **and** the console
  SPA + health endpoints, all on one ASGI app.

## Self-served console UI

The web port serves the built console single-page app alongside gRPC-Web and
the `/healthz` / `/readyz` probes. One `harmonograf-server` process hands a
browser the whole console — no separate static host or frontend dev server.

- `GET /` and `GET /index.html` → the SPA's `index.html`.
- `GET /assets/*` and any other real file in the web root → served directly.
  Content-hashed `/assets/*` are sent `Cache-Control: immutable`; `index.html`
  is sent `no-cache` so a redeploy is picked up.
- Any other **extension-less** non-API path → falls back to `index.html`, so
  client-side hash routes such as `#/session/<id>` load when deep-linked. A
  missing *file-like* path (`*.js`, `*.svg`, …) returns `404` rather than HTML,
  to avoid handing the browser a MIME-mismatched `index.html`.
- gRPC-Web requests (`application/grpc-web*`), gRPC service-method paths
  (`/<pkg>.<Service>/<Method>`), and the health routes are never intercepted —
  they always reach the gRPC-Web / health app.

### Runtime endpoint config (one bundle, any host/port)

When serving `index.html`, the server injects a `window.__HARMONOGRAF_API__`
global with the gRPC-Web base URL the browser should use to reach this server.
The frontend's `transport.ts` reads the endpoint in this precedence:

1. `window.__HARMONOGRAF_API__` (runtime — injected by this server),
2. `VITE_HARMONOGRAF_API` (build-time env var, for `pnpm dev`),
3. the compiled-in default (`http://127.0.0.1:7532`).

So a single built bundle works behind any host/port with no rebuild. The
injected URL is derived per-request from the `Host` / `X-Forwarded-*` headers,
or set explicitly with `--public-base-url` when behind a reverse proxy.

### Locating the bundle

In precedence order:

1. `--web-root <dir>` (explicit override),
2. the packaged console shipped in the wheel
   (`harmonograf_server/_console/`, located via `importlib.resources`),
3. a dev fallback to `../frontend/dist` in a repo checkout.

If no bundle is found the server logs a warning and keeps serving gRPC-Web +
health (graceful degradation).

## Packaging: shipping the console in the wheel

`pip/uv install harmonograf-server` carries the console so consumers don't need
to build the frontend. The build flow:

1. `make console` builds the frontend (`pnpm build`) and stages the output
   into `server/harmonograf_server/_console/`.
2. `uv build` (hatchling) collects that directory into the wheel via the
   `tool.hatch.build.targets.wheel.artifacts` glob in `pyproject.toml`
   (`artifacts` overrides the repo `.gitignore`, which ignores build output).
3. At runtime `static_site.py` locates the packaged `_console/` directory via
   `importlib.resources`.

`make console` is only required when producing a distributable wheel —
`make server-run` in a repo checkout works without it (it falls back to
`../frontend/dist`). The `_console/` directory is build output and is
git-ignored.

**Release note:** a release/publish workflow must run `make console` before
`uv build` so the wheel includes the UI. CI currently runs tests only (no
wheel build); the server test suite is independent of a built bundle.
