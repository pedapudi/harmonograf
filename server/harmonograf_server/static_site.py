"""Serve the built console SPA on the web port, beside gRPC-Web + health.

A single ``harmonograf-server`` process should be able to hand a browser
the whole console without a separate static host or a frontend dev
server. This module wraps the gRPC-Web/health ASGI app with a thin static
layer that:

  * serves ``index.html`` at ``/`` (and as the SPA fallback),
  * serves hashed asset files under ``/assets/*`` and any other real file
    in the web root (favicon, etc.),
  * falls back to ``index.html`` for any *other* extension-less GET/HEAD
    path that is not a real file — so client-side hash routes like
    ``/#/session/<id>`` load the SPA when deep-linked, while a missing
    file-like path (``*.js``/``*.svg`` …) returns 404 rather than HTML, and
  * never touches gRPC-Web requests (content-type ``application/grpc-web*``)
    or the health routes — those flow straight through to ``inner``.

The served ``index.html`` is rewritten on the fly to inject the server's
own gRPC-Web base URL as ``window.__HARMONOGRAF_API__`` so one built
bundle works behind any host/port without a rebuild (see ``transport.ts``).

Locating the bundle (in precedence order):

  1. an explicit ``web_root`` (the ``--web-root`` CLI flag),
  2. the packaged console shipped inside the wheel
     (``harmonograf_server/_console``), located via ``importlib.resources``,
  3. a dev fallback to ``../../frontend/dist`` relative to this file
     (the repo checkout).

If none of those contains an ``index.html`` the layer logs a warning once
and forwards every request to ``inner`` — the server keeps serving
gRPC-Web + health, just without the bundled UI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from importlib import resources
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("harmonograf_server.static_site")


ASGIApp = Callable[..., Awaitable[None]]

# Minimal extension → content-type map. Covers everything Vite emits plus
# the static assets we ship; anything unknown falls back to
# application/octet-stream.
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".wasm": "application/wasm",
}

# Paths that must always reach the inner gRPC-Web/health app, never the
# static layer. gRPC-Web is additionally identified by content-type below.
_HEALTH_PATHS = frozenset({"/healthz", "/readyz"})

# gRPC service-method routes look like ``/<pkg>.<Service>/<Method>`` (e.g.
# ``/harmonograf.v1.Harmonograf/GetStats``). Real gRPC-Web traffic is POST
# with an ``application/grpc-web*`` content-type and is already excluded, but
# we additionally never hijack a gRPC-shaped path as an SPA route regardless
# of method — so the inner auth/gRPC-Web app always owns it (no SPA fallback
# masking a 401, no surprises for non-binary gRPC-Web). A leading segment
# containing a dot is the tell; the SPA never serves such paths.
_GRPC_PATH_RE = re.compile(r"^/[A-Za-z_][\w.]*\.[A-Za-z_]\w*/[A-Za-z_]\w*/?$")


def locate_web_root(web_root: Optional[str] = None) -> Optional[str]:
    """Return an absolute path to a directory containing ``index.html``,
    or ``None`` if no console bundle can be found.

    Precedence: explicit ``web_root`` → packaged ``_console`` → repo
    ``frontend/dist`` dev fallback.
    """

    candidates: list[str] = []
    if web_root:
        candidates.append(os.path.abspath(os.path.expanduser(web_root)))

    # Packaged console inside the installed wheel.
    try:
        pkg_console = resources.files("harmonograf_server") / "_console"
        # ``files()`` returns a Traversable; for a normal filesystem install
        # str() yields a usable path. Guard against zipped installs by
        # checking existence below.
        candidates.append(str(pkg_console))
    except (ModuleNotFoundError, AttributeError):  # pragma: no cover - defensive
        pass

    # Dev fallback: repo checkout's frontend/dist (../../frontend/dist).
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(
        os.path.abspath(os.path.join(here, "..", "..", "frontend", "dist"))
    )

    for cand in candidates:
        if cand and os.path.isfile(os.path.join(cand, "index.html")):
            return cand
    return None


def _content_type(path: str) -> str:
    _, ext = os.path.splitext(path)
    return _CONTENT_TYPES.get(ext.lower(), "application/octet-stream")


def _looks_like_file(path: str) -> bool:
    """True when the last path segment has a file extension (e.g. ``.js``,
    ``.svg``). Such a request is for a concrete asset, so when it isn't found
    we return 404 rather than the SPA fallback — handing back ``index.html``
    (text/html) for a missing ``.js``/``.css`` only yields confusing MIME
    errors in the browser. Navigation/hash routes have no extension and still
    fall through to the SPA."""

    return "." in os.path.basename(path)


def _is_grpc_web(scope: dict) -> bool:
    for k, v in scope.get("headers") or ():
        if k.lower() == b"content-type" and v.lower().startswith(b"application/grpc-web"):
            return True
    return False


def _safe_join(root: str, url_path: str) -> Optional[str]:
    """Resolve ``url_path`` against ``root``, refusing any traversal that
    escapes ``root``. Returns the absolute filesystem path or ``None``."""

    rel = url_path.lstrip("/")
    # Normalize and reject anything that climbs out of root.
    candidate = os.path.normpath(os.path.join(root, rel))
    root_abs = os.path.abspath(root)
    candidate_abs = os.path.abspath(candidate)
    if candidate_abs != root_abs and not candidate_abs.startswith(root_abs + os.sep):
        return None
    return candidate_abs


def _inject_runtime_config(html: str, api_base_url: str) -> str:
    """Insert ``window.__HARMONOGRAF_API__`` so the bundle learns its
    gRPC-Web endpoint at runtime. Injected just before the first module
    script so it runs before the app boots."""

    # Emit a valid JS string literal. ``json.dumps`` handles all string
    # escaping (quotes, backslashes, control chars, U+2028/U+2029); the URL is
    # largely operator-controlled but can be host/X-Forwarded-Host-derived, so
    # we additionally neutralise ``</`` (which JSON leaves intact) to keep a
    # crafted value from closing the <script> element early.
    literal = json.dumps(api_base_url).replace("</", "<\\/")
    snippet = f"<script>window.__HARMONOGRAF_API__ = {literal};</script>"
    needle = "<script type=\"module\""
    idx = html.find(needle)
    if idx != -1:
        return html[:idx] + snippet + "\n    " + html[idx:]
    # Fall back to injecting at end of <head>, then start of <body>.
    for anchor in ("</head>", "<body>"):
        idx = html.find(anchor)
        if idx != -1:
            if anchor == "</head>":
                return html[:idx] + snippet + "\n  " + html[idx:]
            return html[: idx + len(anchor)] + "\n    " + snippet + html[idx + len(anchor):]
    # Last resort: prepend.
    return snippet + html


def _resolve_api_base_url(scope: dict, configured_public_base: str, web_port: int) -> str:
    """Derive the gRPC-Web base URL the *browser* should use to reach this
    server. Prefers an operator-configured public base; otherwise derives
    scheme+host from the request (so it follows whatever host:port the user
    actually typed, including reverse proxies that set forwarded headers)."""

    if configured_public_base:
        return configured_public_base.rstrip("/")

    headers = {k.lower(): v for k, v in (scope.get("headers") or ())}

    # Honor common reverse-proxy forwarding headers when present.
    fwd_proto = headers.get(b"x-forwarded-proto")
    fwd_host = headers.get(b"x-forwarded-host")
    if fwd_host:
        scheme = (fwd_proto or b"http").split(b",")[0].strip().decode("latin-1")
        host = fwd_host.split(b",")[0].strip().decode("latin-1")
        return f"{scheme}://{host}"

    host = headers.get(b"host")
    if host:
        scheme = scope.get("scheme") or "http"
        return f"{scheme}://{host.decode('latin-1')}"

    # No Host header (rare): fall back to the bind port on loopback.
    return f"http://127.0.0.1:{web_port}"


def build_static_router(
    inner: ASGIApp,
    *,
    web_root: Optional[str],
    web_port: int,
    public_base_url: str = "",
) -> ASGIApp:
    """Wrap ``inner`` (gRPC-Web + health) with static SPA serving.

    ``web_root`` is an explicit override; when falsy (``None``/``""``) the
    bundle is auto-located. ``public_base_url`` (if non-empty) is injected
    verbatim as the SPA's gRPC-Web endpoint; otherwise it is derived
    per-request from the Host / forwarding headers.
    """

    root = locate_web_root(web_root)
    # Read index.html once at startup: it is an immutable build artifact, so
    # re-reading it per request would only add blocking disk I/O to the shared
    # event loop (which also serves gRPC-Web streaming). Only the per-request
    # endpoint injection varies, and that is done in memory below. A read
    # failure here degrades to "no bundle" (gRPC-Web + health only).
    index_template: Optional[str] = None
    if root is not None:
        index_path = os.path.join(root, "index.html")
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                index_template = f.read()
        except OSError:
            logger.exception(
                "failed to read index.html at %s; serving gRPC-Web + health only",
                index_path,
            )
            root = None

    if root is None:
        logger.warning(
            "console UI bundle not found (looked for index.html via "
            "--web-root, the packaged _console dir, and ../frontend/dist); "
            "serving gRPC-Web + health only. Build the frontend "
            "(cd frontend && pnpm build) or pass --web-root to enable the UI."
        )
    else:
        logger.info("serving console UI from %s", root)

    async def _send_bytes(
        send,
        status: int,
        body: bytes,
        content_type: str,
        *,
        extra_headers=None,
        head: bool = False,
    ) -> None:
        # Content-Length always reflects the GET body; a HEAD response carries
        # the headers but no body (RFC 9110 §9.3.2).
        headers = [
            (b"content-type", content_type.encode("latin-1")),
            (b"content-length", str(len(body)).encode("latin-1")),
        ]
        if extra_headers:
            headers.extend(extra_headers)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b"" if head else body})

    async def _serve_index(scope, send, *, status: int = 200, head: bool = False) -> None:
        api_base = _resolve_api_base_url(scope, public_base_url, web_port)
        html = _inject_runtime_config(index_template, api_base)
        await _send_bytes(
            send,
            status,
            html.encode("utf-8"),
            "text/html; charset=utf-8",
            extra_headers=[(b"cache-control", b"no-cache")],
            head=head,
        )

    async def app(scope, receive, send):
        # Non-HTTP (lifespan/websocket) and the "no bundle" case go straight
        # through to the inner app.
        if scope.get("type") != "http" or root is None:
            await inner(scope, receive, send)
            return

        path = scope.get("path", "") or "/"
        method = (scope.get("method") or "GET").upper()

        # Health + gRPC-Web (by content-type) + gRPC-shaped service paths
        # always belong to the inner app — never the SPA layer.
        if path in _HEALTH_PATHS or _is_grpc_web(scope) or _GRPC_PATH_RE.match(path):
            await inner(scope, receive, send)
            return

        # Anything that isn't a static-servable verb (POST gRPC-Web, etc.)
        # also belongs to the inner app — the SPA only needs GET/HEAD.
        if method not in ("GET", "HEAD"):
            await inner(scope, receive, send)
            return

        head = method == "HEAD"

        # Root + explicit index → serve the (config-injected) index.html.
        if path in ("/", "/index.html"):
            await _serve_index(scope, send, head=head)
            return

        # Try to serve a real file from the web root.
        fs_path = _safe_join(root, path)
        if fs_path and os.path.isfile(fs_path):
            try:
                # Off-load the (potentially large) read so a big bundle can't
                # block the event loop shared with gRPC-Web streaming.
                body = await asyncio.to_thread(_read_file, fs_path)
            except OSError:
                logger.exception("failed to read static file %s", fs_path)
                await _send_bytes(
                    send, 500, b"internal error\n", "text/plain; charset=utf-8", head=head
                )
                return
            # Vite content-hashes everything under /assets/, so it is safe to
            # cache immutably; other real files (favicon, etc.) stay uncached.
            extra = None
            if path.startswith("/assets/"):
                extra = [(b"cache-control", b"public, max-age=31536000, immutable")]
            await _send_bytes(
                send, 200, body, _content_type(fs_path), extra_headers=extra, head=head
            )
            return

        # A request for a concrete asset that doesn't exist → 404, not the SPA
        # fallback (returning index.html for a missing .js/.svg only produces
        # MIME errors). Extension-less paths are navigation/hash routes.
        if _looks_like_file(path):
            await _send_bytes(
                send, 404, b"not found\n", "text/plain; charset=utf-8", head=head
            )
            return

        # SPA fallback: any unknown non-API, non-file path serves index.html so
        # the client-side hash router can take over (e.g. /#/session/<id>).
        await _serve_index(scope, send, head=head)

    return app


def _read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()
