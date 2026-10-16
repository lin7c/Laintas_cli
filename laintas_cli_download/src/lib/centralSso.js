import { checkCentralSession } from '../../../../laintas/src/lib/centralSso.js';

export async function enableCentralSso() {
  if (window.__LAINTAS_STATIC_RENDER__) return;
  try {
    const response = await fetch('/api/auth/get-session', {
      credentials: 'include', cache: 'no-store', signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) return;
    const session = await response.json();
    if (!session?.user) await checkCentralSession();
  } catch { /* A failed session check must not trigger a login redirect. */ }
}
