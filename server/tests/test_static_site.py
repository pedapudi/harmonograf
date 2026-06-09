"""Tests for serving the built console SPA on the web port.

Covers both the pure ASGI wrapper (``build_static_router``) with a temporary
web root and an end-to-end run through ``Harmonograf`` confirming:

  * ``GET /`` serves index.html with the injected ``window.__HARMONOGRAF_API__``,
  * a real static asset under ``/assets/`` is served with the right type,
  * an unknown non-API path falls back to index.html (SPA hash routing),
  * ``/healthz`` + ``/readyz`` still work, and
  * gRPC-shaped service paths are never hijacked by the SPA layer.
"""

from __future__ import annotations

import asyncio
import os
import socket

import pytest

from harmonograf_server.config import ServerConfig
from harmonograf_server.main import Harmonograf
from harmonograf_server.static_site import (
    _inject_runtime_config,
    _safe_join,
    build_static_router,
    locate_web_root,
)


INDEX_HTML = (
    "<!doctype html>\n<html><head><title>Harmonograf</title>\n"
    '<script type="module" crossorigin src="/assets/index.js"></script>\n'
    "</head><body><div id=\"root\"></div></body></html>\n"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_web_root(tmp_path) -> str:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (root / "assets" / "index.js").write_text("console.log('hi');\n", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return str(root)


# ---- pure ASGI wrapper ---------------------------------------------------


async def _collect(app, scope):
    """Drive an ASGI app once and return (status, headers, body)."""
    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    headers = {k.lower(): v for k, v in start["headers"]}
    return start["status"], headers, body


def _http_scope(path: str, *, method: str = "GET", host: str = "console.test:9000", headers=None):
    hdrs = [(b"host", host.encode())]
    if headers:
        hdrs.extend(headers)
    return {"type": "http", "method": method, "path": path, "headers": hdrs, "scheme": "http"}


@pytest.mark.asyncio
async def test_serves_index_with_injected_runtime_config(tmp_path):
    root = _make_web_root(tmp_path)

    async def inner(scope, receive, send):  # pragma: no cover - must not be hit
        raise AssertionError("inner should not be called for GET /")

    app = build_static_router(inner, web_root=root, web_port=7532)
    status, headers, body = await _collect(app, _http_scope("/"))
    assert status == 200
    assert headers[b"content-type"].startswith(b"text/html")
    text = body.decode()
    # Runtime endpoint global injected, derived from the request Host.
    assert 'window.__HARMONOGRAF_API__ = "http://console.test:9000"' in text
    # The original module script is still present.
    assert "/assets/index.js" in text


@pytest.mark.asyncio
async def test_public_base_url_overrides_host_derivation(tmp_path):
    root = _make_web_root(tmp_path)

    async def inner(scope, receive, send):  # pragma: no cover
        raise AssertionError("inner should not be called")

    app = build_static_router(
        inner, web_root=root, web_port=7532, public_base_url="https://obs.example/"
    )
    _, _, body = await _collect(app, _http_scope("/"))
    assert 'window.__HARMONOGRAF_API__ = "https://obs.example"' in body.decode()


@pytest.mark.asyncio
async def test_serves_static_asset(tmp_path):
    root = _make_web_root(tmp_path)

    async def inner(scope, receive, send):  # pragma: no cover
        raise AssertionError("inner should not be called")

    app = build_static_router(inner, web_root=root, web_port=7532)
    status, headers, body = await _collect(app, _http_scope("/assets/index.js"))
    assert status == 200
    assert headers[b"content-type"].startswith(b"text/javascript")
    assert b"console.log" in body


@pytest.mark.asyncio
async def test_spa_fallback_for_unknown_path(tmp_path):
    root = _make_web_root(tmp_path)

    async def inner(scope, receive, send):  # pragma: no cover
        raise AssertionError("inner should not be called for SPA fallback")

    app = build_static_router(inner, web_root=root, web_port=7532)
    status, headers, body = await _collect(app, _http_scope("/some/deep/route"))
    assert status == 200
    assert headers[b"content-type"].startswith(b"text/html")
    assert "window.__HARMONOGRAF_API__" in body.decode()


@pytest.mark.asyncio
async def test_health_and_grpc_paths_pass_through(tmp_path):
    root = _make_web_root(tmp_path)
    forwarded: list[str] = []

    async def inner(scope, receive, send):
        forwarded.append(scope["path"])

    app = build_static_router(inner, web_root=root, web_port=7532)
    # Health endpoints belong to the inner app.
    await _collect_passthrough(app, _http_scope("/healthz"))
    await _collect_passthrough(app, _http_scope("/readyz"))
    # gRPC-shaped service path is never hijacked, even on GET.
    await _collect_passthrough(app, _http_scope("/harmonograf.v1.Harmonograf/GetStats"))
    # A gRPC-Web POST (by content-type) also passes through.
    await _collect_passthrough(
        app,
        _http_scope(
            "/harmonograf.v1.Harmonograf/GetStats",
            method="POST",
            headers=[(b"content-type", b"application/grpc-web+proto")],
        ),
    )

    # Re-run against a forwarding inner to assert paths actually reached it.
    app2 = build_static_router(inner, web_root=root, web_port=7532)

    async def noop_send(_msg):
        pass

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    for scope in (
        _http_scope("/healthz"),
        _http_scope("/harmonograf.v1.Harmonograf/GetStats"),
    ):
        await app2(scope, receive, noop_send)
    assert "/healthz" in forwarded
    assert forwarded.count("/harmonograf.v1.Harmonograf/GetStats") >= 1


async def _collect_passthrough(app, scope):
    """Assert the request reaches inner (which here records nothing/sends nothing)."""
    sent: list[dict] = []

    async def send(msg):  # pragma: no cover - inner may not send
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await app(scope, receive, send)


@pytest.mark.asyncio
async def test_no_bundle_forwards_everything(monkeypatch):
    """With no locatable bundle, every request flows to inner unchanged.

    We force ``locate_web_root`` to ``None`` because a repo checkout normally
    has a built ``frontend/dist`` the locator would fall back to.
    """
    import harmonograf_server.static_site as ss

    monkeypatch.setattr(ss, "locate_web_root", lambda *_a, **_k: None)

    forwarded: list[str] = []

    async def inner(scope, receive, send):
        forwarded.append(scope["path"])

    app = build_static_router(inner, web_root="", web_port=7532)

    async def send(_msg):  # pragma: no cover
        pass

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await app(_http_scope("/"), receive, send)
    await app(_http_scope("/anything"), receive, send)
    assert forwarded == ["/", "/anything"]


def test_locate_web_root_prefers_explicit(tmp_path):
    root = _make_web_root(tmp_path)
    assert locate_web_root(root) == os.path.abspath(root)
    # A bogus explicit root falls through (here to the repo dev fallback or
    # None); the function must not return the bogus path.
    assert locate_web_root(str(tmp_path / "nope")) != str(tmp_path / "nope")


@pytest.mark.asyncio
async def test_missing_file_like_path_returns_404(tmp_path):
    """A request for a concrete asset that doesn't exist must 404, not serve
    the SPA fallback (returning index.html for a missing .js/.svg only yields
    MIME errors in the browser)."""
    root = _make_web_root(tmp_path)

    async def inner(scope, receive, send):  # pragma: no cover
        raise AssertionError("inner should not be called")

    app = build_static_router(inner, web_root=root, web_port=7532)
    status, headers, body = await _collect(app, _http_scope("/does-not-exist.js"))
    assert status == 404
    assert headers[b"content-type"].startswith(b"text/plain")
    assert b"window.__HARMONOGRAF_API__" not in body


@pytest.mark.asyncio
async def test_assets_get_immutable_cache_header(tmp_path):
    """Vite content-hashes /assets/*, so they are served cache-immutable."""
    root = _make_web_root(tmp_path)

    async def inner(scope, receive, send):  # pragma: no cover
        raise AssertionError("inner should not be called")

    app = build_static_router(inner, web_root=root, web_port=7532)
    _, headers, _ = await _collect(app, _http_scope("/assets/index.js"))
    assert b"immutable" in headers.get(b"cache-control", b"")


@pytest.mark.asyncio
async def test_head_request_sends_no_body_but_keeps_content_length(tmp_path):
    """HEAD carries the headers (incl. the GET Content-Length) but no body."""
    root = _make_web_root(tmp_path)

    async def inner(scope, receive, send):  # pragma: no cover
        raise AssertionError("inner should not be called")

    app = build_static_router(inner, web_root=root, web_port=7532)
    status, headers, body = await _collect(app, _http_scope("/", method="HEAD"))
    assert status == 200
    assert body == b""
    # Content-Length reflects what a GET would have returned (non-zero).
    assert int(headers[b"content-length"]) > 0


def test_safe_join_rejects_traversal(tmp_path):
    """Path traversal must not escape the web root."""
    root = _make_web_root(tmp_path)
    # A benign nested path resolves inside root.
    inside = _safe_join(root, "/assets/index.js")
    assert inside is not None and inside.startswith(os.path.abspath(root))
    # Climbing out of root is refused.
    assert _safe_join(root, "/../../etc/passwd") is None
    assert _safe_join(root, "/../" + os.path.basename(root) + "_sibling/x") is None


def test_inject_runtime_config_escapes_hostile_url():
    """A crafted endpoint can't break out of the JS string or the <script>."""
    html = '<html><head><script type="module" src="/a.js"></script></head></html>'
    out = _inject_runtime_config(html, '"></script><script>alert(1)//')
    # No raw closing tag survives to terminate our injected <script> early.
    assert "</script><script>alert(1)" not in out
    # The original module script is preserved exactly once.
    assert out.count('<script type="module"') == 1


# ---- end-to-end through Harmonograf -------------------------------------


async def _wait_for_port(port: int, timeout: float = 3.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    last_err: Exception | None = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return
        except OSError as e:
            last_err = e
            await asyncio.sleep(0.05)
    raise TimeoutError(f"port {port} not ready after {timeout}s: {last_err}")


async def _http_get(port: int, path: str) -> tuple[int, bytes, bytes]:
    await _wait_for_port(port)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    req = (
        f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    )
    writer.write(req.encode())
    await writer.drain()
    resp = b""
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            break
        resp += chunk
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    head, _, body = resp.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    return status, head, body


@pytest.mark.asyncio
async def test_end_to_end_serves_spa_and_keeps_health(tmp_path):
    root = _make_web_root(tmp_path)
    cfg = ServerConfig(
        host="127.0.0.1",
        grpc_port=_free_port(),
        web_port=_free_port(),
        store_backend="memory",
        data_dir="",
        grace_seconds=0.5,
        metrics_interval_seconds=0.0,
        web_root=root,
    )
    app = await Harmonograf.from_config(cfg)
    await app.start()
    try:
        # GET / → SPA with injected config.
        status, head, body = await _http_get(cfg.web_port, "/")
        assert status == 200
        assert b"text/html" in head.lower()
        text = body.decode()
        assert "window.__HARMONOGRAF_API__" in text
        assert f"127.0.0.1:{cfg.web_port}" in text

        # Static asset.
        status, head, body = await _http_get(cfg.web_port, "/assets/index.js")
        assert status == 200
        assert b"javascript" in head.lower()

        # SPA fallback for an unknown deep path.
        status, _, body = await _http_get(cfg.web_port, "/deeplink/here")
        assert status == 200
        assert b"window.__HARMONOGRAF_API__" in body

        # Health still works alongside the SPA.
        status, _, body = await _http_get(cfg.web_port, "/healthz")
        assert status == 200
        assert b"ok" in body
        status, _, body = await _http_get(cfg.web_port, "/readyz")
        assert status == 200
        assert b"ready" in body
    finally:
        await app.stop()
