// Thin wrapper around the generated Harmonograf service. We keep a single
// client per transport so every hook shares the same underlying fetch state.
// Reconnect is handled per-call in the hooks that open server-streaming RPCs
// (WatchSession); the unary calls rely on caller-level retry.

import { createClient, type Client } from '@connectrpc/connect';
import { Harmonograf } from '../pb/harmonograf/v1/service_pb.js';
import { getTransport } from './transport';

let cached: Client<typeof Harmonograf> | null = null;

export function getHarmonografClient(): Client<typeof Harmonograf> {
  if (cached) return cached;
  cached = createClient(Harmonograf, getTransport());
  return cached;
}

export function resetHarmonografClient(): void {
  cached = null;
}
