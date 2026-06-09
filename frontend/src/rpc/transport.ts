// Connect-Web transport. We speak gRPC-Web so Connect-Web's createGrpcWebTransport
// is the right wire format for the Python harmonograf_server (sonora grpcASGI,
// served by hypercorn — no Envoy/grpcwebproxy needed). The base URL is resolved
// in three steps, most-specific first:
//
//   1. window.__HARMONOGRAF_API__ — a runtime global the server injects into
//      the index.html it serves itself (see server/harmonograf_server/
//      static_site.py). This lets a single built bundle work behind any
//      host/port without a rebuild, because the server fills in the URL the
//      browser actually used to reach it.
//   2. VITE_HARMONOGRAF_API — a build-time env var, useful for `pnpm dev`
//      against a non-default server:
//          VITE_HARMONOGRAF_API=http://127.0.0.1:17532 pnpm dev
//   3. the compiled-in default --web-port (7532) on loopback.

import { createGrpcWebTransport } from '@connectrpc/connect-web';
import type { Transport } from '@connectrpc/connect';

// Must track server/harmonograf_server/cli.py --web-port default.
const DEFAULT_BASE_URL = 'http://127.0.0.1:7532';

// The runtime global the server injects when it serves the bundle itself.
declare global {
  interface Window {
    __HARMONOGRAF_API__?: string;
  }
}

export function apiBaseUrl(): string {
  // 1. Runtime global injected by the serving harmonograf_server.
  if (typeof window !== 'undefined') {
    const fromRuntime = window.__HARMONOGRAF_API__;
    if (typeof fromRuntime === 'string' && fromRuntime.length > 0) {
      return fromRuntime;
    }
  }
  // 2. Build-time env var (Vite injects env vars prefixed with VITE_).
  const fromEnv =
    typeof import.meta !== 'undefined' &&
    (import.meta as unknown as { env?: Record<string, string> }).env?.VITE_HARMONOGRAF_API;
  // 3. Compiled-in default.
  return fromEnv || DEFAULT_BASE_URL;
}

let cached: Transport | null = null;

export function getTransport(): Transport {
  if (cached) return cached;
  cached = createGrpcWebTransport({
    baseUrl: apiBaseUrl(),
    // Streaming RPCs (WatchSession, SubscribeControl, StreamTelemetry) need
    // the fetch API to be kept open; Connect-Web handles this internally.
    useBinaryFormat: true,
  });
  return cached;
}

// Test hook: allow injecting a stub transport (used in unit tests until a real
// server is available).
export function setTransport(t: Transport | null): void {
  cached = t;
}
