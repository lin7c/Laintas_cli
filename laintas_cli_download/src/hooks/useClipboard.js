import { useCallback, useEffect, useRef, useState } from 'react';

export function useClipboard(value) {
  const [state, setState] = useState('idle');
  const generation = useRef(0);
  const timer = useRef();
  useEffect(() => {
    generation.current += 1;
    setState('idle');
    return () => { generation.current += 1; window.clearTimeout(timer.current); };
  }, [value]);

  const copy = useCallback(async () => {
    const current = generation.current;
    window.clearTimeout(timer.current);
    setState('pending');
    try {
      await navigator.clipboard.writeText(value);
      if (current !== generation.current) return;
      setState('copied');
      timer.current = window.setTimeout(() => setState('idle'), 2200);
    } catch {
      if (current === generation.current) setState('failed');
    }
  }, [value]);
  return { state, copy };
}
