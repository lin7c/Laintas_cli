import { useLayoutEffect } from 'react';
import { useLocation } from 'react-router-dom';

const pages = {
  '/': ['Laintas CLI — Agent Runtime for Terminal Operations', 'An autonomous coding agent in your terminal. Run commands, edit files, and manage agent workflows with observable controls.'],
  '/docs': ['Laintas CLI Documentation', 'Install Laintas CLI and learn terminal commands, agent workflows, permissions, plugins, and local runtime integration.'],
  '/plugins': ['Laintas CLI Plugins', 'Explore extensions and integrations for the Laintas CLI autonomous terminal agent.'],
};

function setMeta(selector, attrs) {
  let element = document.head.querySelector(selector);
  if (!element) {
    element = document.createElement('meta');
    document.head.appendChild(element);
  }
  for (const [name, value] of Object.entries(attrs)) element.setAttribute(name, value);
}

export default function RouteMetadata() {
  const { pathname } = useLocation();
  useLayoutEffect(() => {
    const page = pages[pathname];
    if (!page) return;
    const [title, description] = page;
    const url = `https://cli.laintas.com${pathname}`;
    document.title = title;
    setMeta('meta[name="description"]', { name: 'description', content: description });
    setMeta('meta[property="og:title"]', { property: 'og:title', content: title });
    setMeta('meta[property="og:description"]', { property: 'og:description', content: description });
    setMeta('meta[property="og:url"]', { property: 'og:url', content: url });
    document.head.querySelector('link[rel="canonical"]')?.setAttribute('href', url);
  }, [pathname]);
  return null;
}
