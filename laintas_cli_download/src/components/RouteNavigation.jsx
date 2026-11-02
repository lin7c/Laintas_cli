import { useEffect, useLayoutEffect, useRef } from 'react';
import { useLocation, useNavigationType } from 'react-router-dom';
import { useLanguage } from '../contexts/LanguageContext';

export default function RouteNavigation() {
  const { pathname, hash, key } = useLocation();
  const navigationType = useNavigationType();
  const previous = useRef({ pathname, hash });
  const currentKey = useRef(key);
  const positions = useRef(new Map());
  const { lang } = useLanguage();
  useEffect(() => {
    const remember = () => positions.current.set(currentKey.current, window.scrollY);
    window.addEventListener('scroll', remember, { passive: true });
    const priorRestoration = history.scrollRestoration;
    history.scrollRestoration = 'manual';
    return () => { window.removeEventListener('scroll', remember); history.scrollRestoration = priorRestoration; };
  }, []);

  useLayoutEffect(() => {
    const changed = previous.current.pathname !== pathname;
    const changedHash = previous.current.hash !== hash;
    previous.current = { pathname, hash };
    currentKey.current = key;
    if (positions.current.size > 80) positions.current.delete(positions.current.keys().next().value);
    const saved = positions.current.get(key);
    if (!positions.current.has(key)) positions.current.set(key, changed ? 0 : window.scrollY);
    let id;
    try { id = hash ? decodeURIComponent(hash.slice(1)) : null; } catch { return; }
    if (!changed && !changedHash && !id && navigationType !== 'POP') return;
    const apply = () => {
      const main = document.querySelector('main');
      const target = id ? document.getElementById(id) : main;
      if (!target || !target.getClientRects().length) return false;
      if (navigationType === 'POP' && saved != null) window.scrollTo({ top: saved, behavior: 'instant' });
      else if (id) target.scrollIntoView({ block: 'start' });
      else if (changed) window.scrollTo({ top: 0, behavior: 'instant' });
      if (changed) main?.focus({ preventScroll: true });
      return true;
    };
    if (apply()) return;
    // A documentation chunk may arrive after the navigation effect.
    const observer = new MutationObserver(() => { if (apply()) observer.disconnect(); });
    observer.observe(document.getElementById('root'), { childList: true, subtree: true });
    const timeout = window.setTimeout(() => observer.disconnect(), 8000);
    return () => { observer.disconnect(); window.clearTimeout(timeout); };
  }, [pathname, hash, key, navigationType]);
  return <a className="skip-link" href="#main-content">{lang === 'zh' ? '跳到主要内容' : 'Skip to content'}</a>;
}
