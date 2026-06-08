// Coverage for transport.apiBaseUrl()'s resolution precedence:
//   1. window.__HARMONOGRAF_API__ (runtime, injected by the serving server)
//   2. VITE_HARMONOGRAF_API (build-time env var)
//   3. the compiled-in default.
//
// The runtime global is the new path that lets a single built bundle work
// behind any host/port without a rebuild (see static_site.py).

import { afterEach, describe, expect, it } from 'vitest';
import { apiBaseUrl } from '../../rpc/transport';

const DEFAULT_BASE_URL = 'http://127.0.0.1:7532';

afterEach(() => {
  delete window.__HARMONOGRAF_API__;
});

describe('apiBaseUrl', () => {
  it('prefers the runtime window.__HARMONOGRAF_API__ global', () => {
    window.__HARMONOGRAF_API__ = 'http://example.test:9000';
    expect(apiBaseUrl()).toBe('http://example.test:9000');
  });

  it('ignores an empty runtime global and falls through', () => {
    window.__HARMONOGRAF_API__ = '';
    // No VITE env set in the test runner, so this lands on the default.
    expect(apiBaseUrl()).toBe(DEFAULT_BASE_URL);
  });

  it('falls back to the compiled-in default when nothing is configured', () => {
    expect(apiBaseUrl()).toBe(DEFAULT_BASE_URL);
  });
});
