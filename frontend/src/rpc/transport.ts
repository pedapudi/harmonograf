// Connect-Web transport. We speak gRPC-Web so Connect-Web's createGrpcWebTransport
// is the right wire format for the Python grpcio server (fronted by Envoy or
// grpcwebproxy in dev — see frontend/README.md). The base URL is taken from
// VITE_HARMONOGRAF_API or defaults to the dev grpcwebproxy address.

import { createGrpcWebTransport } from '@connectrpc/connect-web';
import type { Transport } from '@connectrpc/connect';

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
