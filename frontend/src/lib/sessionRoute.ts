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
