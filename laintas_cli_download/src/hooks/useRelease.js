import { useEffect, useState } from 'react';

const FALLBACK = 'v2.0.0';
let latest;

// Header and download packages share one request, including in StrictMode.
export function useRelease() {
  const [release, setRelease] = useState(FALLBACK);
  useEffect(() => {
    let active = true;
    latest ??= fetch('https://api.github.com/repos/lin7c/Laintas_cli/releases/latest', {
      signal: AbortSignal.timeout(8000),
    }).then(response => response.ok ? response.json() : Promise.reject())
      .then(data => typeof data.tag_name === 'string' ? data.tag_name : FALLBACK)
      .catch(() => FALLBACK);
    latest.then(value => { if (active) setRelease(value); });
    return () => { active = false; };
  }, []);
  return release;
}
