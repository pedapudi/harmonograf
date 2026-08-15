// Hash-route parsing for the `#/session/<id>` deep link consumed by App.tsx.
// Kept in its own module so App.tsx only exports its component (react-refresh).

// Parse a `#/session/<id>` deep link, returning the (decoded) session id or
// null. Tolerates a trailing slash and an empty id. Accepts both
// `#/session/<id>` and a bare `/session/<id>` defensively.
export function sessionIdFromHash(hash: string): string | null {
  const m = /^#?\/session\/([^/]+)\/?$/.exec(hash);
  if (!m) return null;
  const raw = m[1];
  if (!raw) return null;
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

export interface MetadataPredicate {
  key: string;
  value: string;
}

// Parsed state of the `#/sessions?metadata.<key>=<value>` route. A
// discriminated union so "no filter requested" and "filter failed to parse"
// are distinct states: an untyped `{}` for both would make a malformed link
// fall through to an unfiltered ListSessions, exposing every session.
//   * absent  — the route carries no metadata predicates;
//   * valid   — every predicate is well-formed;
//   * invalid — a predicate was requested but is malformed; callers must
//     fail closed (render an empty/error state, issue no request).
// Predicates are an array, not an object map, so arbitrary external keys
// ("constructor", "__proto__", …) are plain data rather than property names.
export type SessionFilterRoute =
  | { kind: 'absent' }
  | { kind: 'valid'; predicates: readonly MetadataPredicate[] }
  | { kind: 'invalid'; reason: string };

// Bounds mirror the server's ListSessions validation; the browser check is
// early feedback, the server remains authoritative.
const MAX_PREDICATES = 16;
const MAX_KEY_LENGTH = 128;
const MAX_VALUE_LENGTH = 512;

// Parse generic exact metadata predicates from `#/sessions?metadata.<key>=<value>`.
// URLSearchParams performs component decoding.
export function sessionFilterRouteFromHash(hash: string): SessionFilterRoute {
  const query = /^#?\/sessions\?(.+)$/.exec(hash)?.[1];
  if (!query) return { kind: 'absent' };
  const invalid = (reason: string): SessionFilterRoute => ({ kind: 'invalid', reason });
  const predicates: MetadataPredicate[] = [];
  const seen = new Set<string>();
  for (const [name, value] of new URLSearchParams(query)) {
    if (!name.startsWith('metadata.')) continue;
    const key = name.slice('metadata.'.length);
    if (!key) return invalid('empty metadata key');
    if (seen.has(key)) return invalid(`duplicate metadata key "${key}"`);
    if (key.length > MAX_KEY_LENGTH) {
      return invalid(`metadata key over ${MAX_KEY_LENGTH} characters`);
    }
    if (value.length > MAX_VALUE_LENGTH) {
      return invalid(`metadata value over ${MAX_VALUE_LENGTH} characters`);
    }
    if (predicates.length === MAX_PREDICATES) {
      return invalid(`more than ${MAX_PREDICATES} metadata predicates`);
    }
    seen.add(key);
    predicates.push({ key, value });
  }
  if (predicates.length === 0) return { kind: 'absent' };
  return { kind: 'valid', predicates };
}

// Canonical encoder for filtered-picker links; the inverse of
// sessionFilterRouteFromHash for well-formed predicate lists.
export function sessionsHash(predicates: readonly MetadataPredicate[]): string {
  const query = new URLSearchParams();
  for (const { key, value } of [...predicates].sort((a, b) => a.key.localeCompare(b.key))) {
    query.set(`metadata.${key}`, value);
  }
  const encoded = query.toString();
  return encoded ? `#/sessions?${encoded}` : '#/';
}
