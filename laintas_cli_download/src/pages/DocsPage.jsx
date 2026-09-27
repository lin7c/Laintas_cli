import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { Check, Copy, Menu, X } from 'lucide-react';
import { useLanguage } from '../contexts/LanguageContext';
import SiteFooter from '../components/SiteFooter';
import { GROUPS } from '../docs/content';
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
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch { /* clipboard unavailable: the text is still selectable */ }
  }
  return (
    <div className="cd-code">
      <div className="cd-code-bar">
        <span>{pick(label, lang) || ''}</span>
        <button type="button" onClick={copy} aria-label={lang === 'zh' ? '复制代码' : 'Copy code'}>
          {copied ? <Check size={13} /> : <Copy size={13} />}
          <span>{copied ? (lang === 'zh' ? '已复制' : 'Copied') : (lang === 'zh' ? '复制' : 'Copy')}</span>
        </button>
      </div>
      <pre><code>{code}</code></pre>
    </div>
  );
}

function Block({ block, lang, sectionId }) {
  switch (block.t) {
    case 'p':
      return <p className="cd-p"><Inline text={pick(block, lang)} /></p>;
    case 'h':
      return <h3 id={`${sectionId}--${block.id || slug(pick(block, 'en'))}`} className="cd-h3">{pick(block, lang)}</h3>;
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
  const zh = lang === 'zh';
  const sections = useMemo(() => GROUPS.flatMap(g => g.sections), []);
  const [active, setActive] = useState(sections[0].id);
  const [drawer, setDrawer] = useState(false);

  useEffect(() => {
    const previous = document.title;
    document.title = zh ? 'Laintas CLI 文档' : 'Laintas CLI Documentation';
    return () => { document.title = previous; };
  }, [zh]);

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

  // A #hash on first load, and in-page #links inside the content.
  useEffect(() => {
    const id = decodeURIComponent(window.location.hash.slice(1));
    if (!id) return undefined;
    const t = setTimeout(() => document.getElementById(id)?.scrollIntoView({ block: 'start' }), 60);
    return () => clearTimeout(t);
  }, []);

  const go = useCallback((id) => {
    setDrawer(false);
    const el = document.getElementById(id);
    if (!el) return;
    window.history.replaceState(null, '', `#${id}`);
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, []);

  const onContentClick = (event) => {
    const anchor = event.target.closest('a[href^="#"]');
    if (!anchor) return;
    event.preventDefault();
    go(decodeURIComponent(anchor.getAttribute('href').slice(1)));
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
    <main className="product-page cd-page">
      <div className="cd-grid page-shell">
        <aside className="cd-sidebar">{nav}</aside>

        <article className="cd-article" onClick={onContentClick}>
          <header className="cd-hero">
            <p className="section-kicker">DOCUMENTATION</p>
            <h1>{zh ? 'Laintas CLI 文档' : 'Laintas CLI documentation'}</h1>
            <p>
              {zh
                ? '安装、日常使用、模式与安全策略、扩展方式和完整命令参考。本文档对应最新发布版本；你的版本以 /help 输出为准。'
                : 'Installing, everyday use, modes and the security policy, extending it, and a full command reference. Written for the latest release; /help shows what your installed version has.'}
            </p>
          </header>

          {GROUPS.map(group => group.sections.map(s => (
            <section key={s.id} id={s.id} className="cd-section" aria-labelledby={`${s.id}-title`}>
              <p className="cd-section-group">{pick(group.title, lang)}</p>
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
      {drawer && (
        <div className="cd-drawer-layer" role="dialog" aria-modal="true">
          <div className="cd-drawer-backdrop" onClick={() => setDrawer(false)} />
          <div className="cd-drawer">
            <button type="button" className="cd-drawer-close" onClick={() => setDrawer(false)} aria-label={zh ? '关闭' : 'Close'}>
              <X size={17} />
            </button>
            {nav}
          </div>
        </div>
      )}

      <SiteFooter />
    </main>
  );
}
