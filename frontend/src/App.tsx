import { useEffect, useState } from 'react';
import { Shell } from './components/shell/Shell';
import { StressPage } from './gantt/StressPage';

// Minimal hash router. The stress harness is dev-only and intentionally not
// linked anywhere user-facing. Visit /#/stress to open it.
function useHashRoute(): string {
  const [hash, setHash] = useState(() => window.location.hash || '#/');
  useEffect(() => {
    const onHash = () => setHash(window.location.hash || '#/');
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  return hash;
}

export default function App() {
  const hash = useHashRoute();
  if (hash.startsWith('#/stress')) return <StressPage />;
  return <Shell />;
}
