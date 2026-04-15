// Connect-Web transport. We speak gRPC-Web so Connect-Web's createGrpcWebTransport
// is the right wire format for the Python harmonograf_server (sonora grpcASGI,
// served by hypercorn — no Envoy/grpcwebproxy needed). The base URL is taken
// from the Vite env var VITE_HARMONOGRAF_API, falling back to the server's
// default --web-port (7532) on loopback. To point at a non-default server,
// set the env var before running `pnpm dev`, e.g.:
//     VITE_HARMONOGRAF_API=http://127.0.0.1:17532 pnpm dev

import { createGrpcWebTransport } from '@connectrpc/connect-web';
import type { Transport } from '@connectrpc/connect';

// Must track server/harmonograf_server/cli.py --web-port default.
const DEFAULT_BASE_URL = 'http://127.0.0.1:7532';

export function apiBaseUrl(): string {
  // Vite injects env vars prefixed with VITE_.
  const fromEnv =
    typeof import.meta !== 'undefined' &&
    (import.meta as unknown as { env?: Record<string, string> }).env?.VITE_HARMONOGRAF_API;
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
