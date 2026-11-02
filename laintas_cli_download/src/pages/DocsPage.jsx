import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Check, Copy, Menu, Search, X, ArrowRight } from 'lucide-react';
import { useLanguage } from '../contexts/LanguageContext';
import SiteFooter from '../components/SiteFooter';
import { GROUPS } from '../docs/content';
import { useClipboard } from '../hooks/useClipboard';
import './docs.css';

// cli.laintas.com/docs. Content lives in ../docs/content.js as data — every
// string a { zh, en } pair with three inline marks (`code`, **bold**,
// [text](href)) — so this file only lays it out. Every section is #<id> and
// every subheading #<id>--<slug>, so any heading can be linked to.

const pick = (value, lang) => (value && typeof value === 'object' && !Array.isArray(value)
  ? (value[lang] ?? value.en)
  : value);

const slug = (text) => String(text).toLowerCase()
  .replace(/[`*[\]()]/g, '')
  .replace(/[^a-z0-9]+/g, '-')
  .replace(/^-+|-+$/g, '');

const INLINE = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)\s]+\))/g;

function Inline({ text }) {
  if (typeof text !== 'string') return text ?? null;
  return text.split(INLINE).map((part, i) => {
    if (!part) return null;
    if (part.length > 1 && part.startsWith('`') && part.endsWith('`')) return <code key={i}>{part.slice(1, -1)}</code>;
    if (part.length > 3 && part.startsWith('**') && part.endsWith('**')) return <strong key={i}>{part.slice(2, -2)}</strong>;
    const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(part);
    if (link) {
      const [, label, href] = link;
      if (href.startsWith('#')) return <a key={i} href={href}>{label}</a>;
      if (href.startsWith('/') && !href.startsWith('//') && !href.startsWith('/#')) return <Link key={i} to={href}>{label}</Link>;
      const external = /^https?:/.test(href);
      return <a key={i} href={href} {...(external ? { target: '_blank', rel: 'noreferrer' } : {})}>{label}</a>;
    }
    return part;
  });
}

function CodeBlock({ label, code, lang }) {
  const { state, copy } = useClipboard(code);
  return (
    <div className="cd-code">
      <div className="cd-code-bar">
        <span>{pick(label, lang) || ''}</span>
        <button type="button" onClick={copy} disabled={state === 'pending'} aria-label={lang === 'zh' ? '复制代码' : 'Copy code'}>
          {state === 'copied' ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
          <span aria-live="polite">{state === 'copied' ? (lang === 'zh' ? '已复制' : 'Copied') : (lang === 'zh' ? '复制' : 'Copy')}</span>
        </button>
      </div>
      <pre><code>{code}</code></pre>
      {state === 'failed' && <p className="cd-copy-error" role="status">{lang === 'zh' ? '无法自动复制，请选中代码后手动复制。' : 'Copy unavailable. Select the code and copy it manually.'}</p>}
    </div>
  );
}

function Block({ block, lang, sectionId }) {
  switch (block.t) {
    case 'p':
      return <p className="cd-p"><Inline text={pick(block, lang)} /></p>;
    case 'h':
      return <h3 tabIndex={-1} id={`${sectionId}--${block.id || slug(pick(block, 'en'))}`} className="cd-h3">{pick(block, lang)}</h3>;
    case 'list':
      return <ul className="cd-list">{pick(block, lang).map((item, i) => <li key={i}><Inline text={item} /></li>)}</ul>;
    case 'steps':
      return (
        <ol className="cd-steps">
          {pick(block, lang).map((item, i) => (
            <li key={i}><span className="cd-step-n">{String(i + 1).padStart(2, '0')}</span><span><Inline text={item} /></span></li>
          ))}
        </ol>
      );
    case 'code':
      return <CodeBlock label={block.label} code={block.code} lang={lang} />;
    case 'table': {
      const head = pick(block.head, lang);
      const rows = pick(block.rows, lang);
      return (
        <div className="cd-table-wrap">
          <table className="cd-table">
            <thead><tr>{head.map((h, i) => <th key={i}>{h}</th>)}</tr></thead>
            <tbody>
              {rows.map((row, ri) => (
                <tr key={ri}>{row.map((cell, ci) => <td key={ci}><Inline text={cell} /></td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }
    case 'note':
    case 'warn':
      return (
        <div className={`cd-callout cd-callout-${block.t}`}>
          {block.title && <p className="cd-callout-title">{pick(block.title, lang)}</p>}
          <p className="cd-callout-body"><Inline text={pick(block, lang)} /></p>
        </div>
      );
    default:
      return null;
  }
}

export default function DocsPage() {
  const { lang } = useLanguage();
  const navigate = useNavigate();
  const zh = lang === 'zh';
  const sections = useMemo(() => GROUPS.flatMap(g => g.sections), []);
  const [active, setActive] = useState(sections[0].id);
  const [drawer, setDrawer] = useState(false);
  const [query, setQuery] = useState('');
  const dialogRef = useRef(null);
  const searchRef = useRef(null);
  const results = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return [];
    const text = value => {
      if (value == null) return '';
      if (Array.isArray(value)) return value.map(text).join(' ');
      if (typeof value === 'object') {
        if ('zh' in value || 'en' in value) return text(pick(value, lang));
        return Object.values(value).map(text).join(' ');
      }
      return String(value);
    };
    return sections.map(section => ({ section, title: pick(section.title, lang), content: text(section) }))
      .filter(result => result.content.toLocaleLowerCase().includes(needle))
      .sort((a, b) => Number(b.title.toLocaleLowerCase().includes(needle)) - Number(a.title.toLocaleLowerCase().includes(needle)));
  }, [query, sections, lang]);

  useEffect(() => {
    if (!drawer) return;
    const dialog = dialogRef.current;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialog?.showModal();
    return () => { dialog?.close(); document.body.style.overflow = overflow; };
  }, [drawer]);

  // Scroll-spy from positions: the active section is the last one whose top
  // has passed below the fixed header.
  useEffect(() => {
    let frame = 0;
    const update = () => {
      frame = 0;
      let current = sections[0].id;
      for (const s of sections) {
        const el = document.getElementById(s.id);
        if (el && el.getBoundingClientRect().top <= 130) current = s.id;
      }
      if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) {
        current = sections[sections.length - 1].id;
      }
      setActive(current);
    };
    const onScroll = () => { if (!frame) frame = requestAnimationFrame(update); };
    update();
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll);
    return () => {
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('resize', onScroll);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [sections]);

  const go = useCallback((id) => {
    setDrawer(false);
    const el = document.getElementById(id);
    if (!el) return;
    el.focus({ preventScroll: true });
    const hash = `#${encodeURIComponent(id)}`;
    if (window.location.hash === hash) el.scrollIntoView({ block: 'start' });
    else navigate({ hash }, { preventScrollReset: true });
  }, [navigate]);

  const onContentClick = (event) => {
    if (event.defaultPrevented) return;
    const anchor = event.target.closest('a[href^="#"]');
    if (!anchor) return;
    event.preventDefault();
    try { go(decodeURIComponent(anchor.getAttribute('href').slice(1))); } catch { /* Ignore malformed fragment URLs. */ }
  };

  const activeSection = sections.find(s => s.id === active);
  const outline = (activeSection?.blocks || []).filter(b => b.t === 'h');

  const nav = (
    <nav className="cd-nav" aria-label={zh ? '文档目录' : 'Documentation contents'}>
      {GROUPS.map(group => (
        <div key={group.title.en} className="cd-nav-group">
          <p className="cd-nav-title">{pick(group.title, lang)}</p>
          {group.sections.map(s => (
            <a key={s.id} href={`#${s.id}`} onClick={(e) => { e.preventDefault(); go(s.id); }}
              className={active === s.id ? 'is-active' : undefined}
              aria-current={active === s.id ? 'location' : undefined}>
              {pick(s.title, lang)}
            </a>
          ))}
        </div>
      ))}
    </nav>
  );

  return (
    <main id="main-content" tabIndex={-1} className="product-page cd-page">
      <div className="cd-grid page-shell">
        <aside className="cd-sidebar">{nav}</aside>

        <article className="cd-article" onClick={onContentClick}>
          <header className="cd-hero">
            <h1>{zh ? 'Laintas CLI 文档' : 'Laintas CLI documentation'}</h1>
            <p>
              {zh
                ? '安装、日常使用、模式与安全策略、扩展方式和完整命令参考。本文档对应最新发布版本；你的版本以 /help 输出为准。'
                : 'Installing, everyday use, modes and the security policy, extending it, and a full command reference. Written for the latest release; /help shows what your installed version has.'}
            </p>
          </header>

          <div className="cd-search-area">
            <label className="cd-search"><Search size={18} aria-hidden="true" /><span className="sr-only">{zh ? '搜索文档' : 'Search documentation'}</span>
              <input ref={searchRef} name="docs-search" type="search" autoComplete="off" placeholder={zh ? '搜索安装、命令、权限…' : 'Search installation, commands, permissions…'} value={query} onChange={event => setQuery(event.target.value)} onKeyDown={event => { if (event.key === 'Escape') { setQuery(''); event.stopPropagation(); } }} aria-controls={query.trim() ? 'docs-search-results' : undefined} />
            </label>
            {query.trim() && <div id="docs-search-results" className="cd-search-results">
              <p role="status">{results.length ? (zh ? `找到 ${results.length} 个相关章节` : `${results.length} matching sections`) : (zh ? '没有找到相关内容，试试更短的关键词。' : 'No matches. Try a shorter search term.')}</p>
              {results.map(({ section, title }) => <a key={section.id} href={`#${section.id}`} onClick={event => { event.preventDefault(); setQuery(''); go(section.id); }}>{title}<ArrowRight size={15} aria-hidden="true" /></a>)}
              <button type="button" onClick={() => { setQuery(''); searchRef.current?.focus(); }}>{zh ? '清除搜索' : 'Clear search'}</button>
            </div>}
          </div>

          {GROUPS.map(group => group.sections.map(s => (
            <section key={s.id} id={s.id} tabIndex={-1} className="cd-section" aria-labelledby={`${s.id}-title`}>
              <h2 id={`${s.id}-title`}><a className="cd-anchor" href={`#${s.id}`}>{pick(s.title, lang)}</a></h2>
              {s.lead && <p className="cd-lead"><Inline text={pick(s.lead, lang)} /></p>}
              {s.blocks.map((b, i) => <Block key={i} block={b} lang={lang} sectionId={s.id} />)}
            </section>
          )))}
        </article>

        <aside className="cd-outline" aria-label={zh ? '本节内容' : 'In this section'}>
          {outline.length > 0 && <>
            <p className="cd-nav-title">{zh ? '本节内容' : 'In this section'}</p>
            {outline.map(h => {
              const id = `${activeSection.id}--${h.id || slug(pick(h, 'en'))}`;
              return <a key={id} href={`#${id}`} onClick={(e) => { e.preventDefault(); go(id); }}>{pick(h, lang)}</a>;
            })}
          </>}
        </aside>
      </div>

      <button type="button" className="cd-drawer-button" onClick={() => setDrawer(true)}
        aria-label={zh ? '打开文档目录' : 'Open contents'}>
        <Menu size={15} /><span>{pick(activeSection?.title, lang)}</span>
      </button>
      <dialog ref={dialogRef} className="cd-drawer-layer" aria-labelledby="contents-title" onClose={() => setDrawer(false)} onClick={event => { if (event.target === event.currentTarget) setDrawer(false); }}>
          <div className="cd-drawer">
            <h2 id="contents-title">{zh ? '文档目录' : 'Contents'}</h2>
            <button type="button" className="cd-drawer-close" onClick={() => setDrawer(false)} aria-label={zh ? '关闭' : 'Close'}>
              <X size={17} />
            </button>
            {nav}
          </div>
      </dialog>

      <SiteFooter />
    </main>
  );
}
