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

export type SessionMetadataFilter = Record<string, string>;

// Parse generic exact metadata predicates from `#/sessions?metadata.<key>=<value>`.
// URLSearchParams performs component decoding; malformed or oversized routes
// fail closed to an empty filter.
export function sessionMetadataFilterFromHash(hash: string): SessionMetadataFilter {
  const query = /^#?\/sessions\?(.+)$/.exec(hash)?.[1];
  if (!query) return {};
  const result: SessionMetadataFilter = {};
  for (const [name, value] of new URLSearchParams(query)) {
    if (!name.startsWith('metadata.')) continue;
    const key = name.slice('metadata.'.length);
    if (!key || key in result || key.length > 128 || value.length > 512) return {};
    result[key] = value;
    if (Object.keys(result).length > 16) return {};
  }
  return result;
}

export function sessionsHash(filter: SessionMetadataFilter): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filter).sort(([a], [b]) => a.localeCompare(b))) {
    query.set(`metadata.${key}`, value);
  }
  const encoded = query.toString();
  return encoded ? `#/sessions?${encoded}` : '#/';
}
