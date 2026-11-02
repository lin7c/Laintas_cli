import { useSyncExternalStore } from 'react';

let query;
const preference = () => query ??= window.matchMedia('(prefers-reduced-motion: reduce)');
const subscribe = callback => {
  const media = preference();
  media.addEventListener('change', callback);
  return () => media.removeEventListener('change', callback);
};
const snapshot = () => preference().matches;

export function useMotionPreference() {
  return useSyncExternalStore(subscribe, snapshot, () => true);
}
