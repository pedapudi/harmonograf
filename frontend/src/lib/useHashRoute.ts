import { useEffect, useState } from 'react';

// Subscribe to window.location.hash, normalized so an empty hash reads as
// '#/'. Shared by App's router and SessionsSyncer's metadata-filter parsing
// so both derive from the same value.
export function useHashRoute(): string {
  const [hash, setHash] = useState(() => window.location.hash || '#/');
  useEffect(() => {
    const onHash = () => setHash(window.location.hash || '#/');
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  return hash;
}
