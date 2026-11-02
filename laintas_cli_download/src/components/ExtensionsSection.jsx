import { useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Check, Copy, RefreshCw, Search, X } from 'lucide-react';
import { useLanguage } from '@/contexts/LanguageContext';
import { useClipboard } from '../hooks/useClipboard';
import './PluginMarket.css';

const COPY = {
  zh: {
    summaries: { 'ai-pow': '记录 Git 提交的工作过程与验证日志。', blindpick: '比较两个模型的任务结果，选择后应用。', canvas: '在终端查看和编辑 Excalidraw 白板。', issues: '管理 Issues 问答、个人任务与交付。', swebench: '生成 SWE-bench 预测结果。', whatsapp: '通过 WhatsApp 提交任务并接收执行结果。' },
    title: '插件市场', all: '全部', official: '官方', community: '社区',
    search: '搜索插件、作者或功能', refresh: '刷新', loading: '正在加载…',
    errorOfficial: '官方目录暂不可用。', errorCommunity: '社区目录暂不可用。',
    empty: '暂无插件。', noResults: '没有匹配的插件。', emptyCommunity: '暂无社区插件。',
    cached: '缓存', cachedTitle: '社区目录更新失败，显示缓存结果。',
    copy: '复制安装命令', copied: '已复制', copyFailed: '无法复制，请手动选择命令。',
    docs: '开发文档', installation: '安装说明',
    installNote: '在 laintas-cli 中执行安装命令。社区扩展安装前会进行 AI 辅助源码审查，并由你确认。',
  },
  en: {
    summaries: {},
    title: 'Plugin market', all: 'All', official: 'Official', community: 'Community',
    search: 'Search plugins, authors, or capabilities', refresh: 'Refresh', loading: 'Loading…',
    errorOfficial: 'Official directory unavailable.', errorCommunity: 'Community directory unavailable.',
    empty: 'No plugins available.', noResults: 'No matching plugins.', emptyCommunity: 'No community plugins yet.',
    cached: 'Cached', cachedTitle: 'The community directory could not refresh. Showing cached entries.',
    copy: 'Copy install command', copied: 'Copied', copyFailed: 'Copy unavailable. Select the command manually.',
    docs: 'Developer docs', installation: 'Installation',
    installNote: 'Run the install command in laintas-cli. Community extensions receive an AI-assisted source review and require your confirmation.',
  },
};

function normalizeEntries(entries, source) {
  const latest = new Map();
  for (const entry of entries) {
    if (!entry || typeof entry.id !== 'string' || typeof entry.version !== 'string'
      || !(source === 'official' ? /^laintas\/[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/ : /^@[a-z0-9][a-z0-9-]{1,38}[a-z0-9]\/[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$/).test(entry.id)) continue;
    const name = source === 'official' ? entry.name || entry.id.split('/')[1] : entry.manifest?.displayName || entry.slug || entry.id.split('/')[1];
    const description = source === 'official' ? entry.description || '' : entry.manifest?.summary || entry.manifest?.description || '';
    if (typeof name !== 'string' || !name.trim() || typeof description !== 'string') continue;
    const previous = latest.get(entry.id);
    // The CLI resolves community identities by publication time, not lexical version order.
    if (previous && Number(previous.publishedAt || 0) >= Number(entry.publishedAt || 0)) continue;
    latest.set(entry.id, {
      ...entry, source,
      name, description,
      publisher: source === 'official' ? 'Laintas' : entry.id.slice(1).split('/')[0],
    });
  }
  return [...latest.values()].sort((left, right) => left.id.localeCompare(right.id));
}

async function registry(url, signal) {
  const response = await fetch(url, { headers: { Accept: 'application/json' }, signal });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const body = await response.json();
  if (!body || !Array.isArray(body.extensions)) throw new Error('Invalid extension directory.');
  return body.extensions;
}

export default function ExtensionsSection({ standalone = false }) {
  const { lang } = useLanguage();
  const c = COPY[lang] || COPY.en;
  const [official, setOfficial] = useState([]);
  const [community, setCommunity] = useState([]);
  const [officialState, setOfficialState] = useState('loading');
  const [communityState, setCommunityState] = useState('loading');
  const [params, setParams] = useSearchParams();
  const sourceParam = ['official', 'community'].includes(params.get('source')) ? params.get('source') : 'all';
  const queryParam = params.get('q') || '';
  const [source, setSource] = useState(sourceParam);
  const [query, setQuery] = useState(queryParam);
  useEffect(() => setSource(sourceParam), [sourceParam]);
  useEffect(() => setQuery(queryParam), [queryParam]);
  const setFilter = (key, value) => {
    if (key === 'source') setSource(value); else setQuery(value);
    setParams(previous => {
      if (value && value !== 'all') previous.set(key, value); else previous.delete(key);
      return previous;
    }, { replace: key === 'q', preventScrollReset: true });
  };
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const controller = new AbortController(); let active = true;
    setOfficialState('loading'); setCommunityState('loading');
    const timeout = window.setTimeout(() => controller.abort(), 30_000);
    const officialRequest = registry('/extensions/official-registry.json', controller.signal)
      .then(items => { if (active) { setOfficial(normalizeEntries(items, 'official')); setOfficialState('ready'); } })
      .catch(() => { if (active) { setOfficial([]); setOfficialState('error'); } });
    const communityRequest = (async () => {
      let snapshot = null;
      try {
        snapshot = normalizeEntries(await registry('/extensions/community-snapshot.json', controller.signal), 'community');
        if (active) setCommunity(snapshot);
      } catch { /* The live registry can work without a snapshot. */ }
      try {
        const items = []; let offset = 0;
        // Fetch every page so search and publisher counts describe the whole catalog.
        while (true) {
          const page = await registry(`/api/extensions/community?limit=200&offset=${offset}`, controller.signal);
          items.push(...page);
          if (page.length < 200) break;
          offset += page.length;
        }
        if (active) { setCommunity(normalizeEntries(items, 'community')); setCommunityState('ready'); }
      } catch {
        if (active) { setCommunity(snapshot || []); setCommunityState(snapshot ? 'cached' : 'error'); }
      }
    })();
    void Promise.allSettled([officialRequest, communityRequest]).finally(() => window.clearTimeout(timeout));
    return () => { active = false; controller.abort(); window.clearTimeout(timeout); };
  }, [revision]);

  const visible = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return [...official, ...community].filter(entry => (source === 'all' || source === entry.source)
      && `${entry.name} ${entry.id} ${entry.publisher} ${entry.description} ${entry.source === 'official' ? c.summaries[entry.id.split('/')[1]] || '' : ''}`.toLocaleLowerCase().includes(needle));
  }, [official, community, source, query, c]);
  const loading = officialState === 'loading' || communityState === 'loading';
  const Heading = standalone ? 'h1' : 'h2';

  return <section id="extensions" className={`cli-plugin-market plugin-market${standalone ? ' is-standalone' : ''}`} lang={lang === 'zh' ? 'zh-CN' : 'en'}>
    <div className="plugin-market-heading"><Heading>{c.title}</Heading><Link className="plugin-market-docs" to="/docs#extensions">{c.docs}</Link></div>
    <p className="plugin-market-intro">{lang === 'zh' ? '找到需要的能力，复制命令，在 CLI 中安装。' : 'Find a capability. Copy the command. Install it in your CLI.'}</p>
    <div className="plugin-market-search"><Search size={18} aria-hidden="true" /><input type="search" name="plugins-search" autoComplete="off" maxLength={200} aria-label={c.search} placeholder={`${c.search}…`} value={query} onChange={event => setFilter('q', event.target.value)} /><button className={`plugin-market-refresh${loading ? ' is-loading' : ''}`} disabled={loading} onClick={() => setRevision(value => value + 1)} aria-label={c.refresh} title={c.refresh}><RefreshCw size={16} aria-hidden="true" /></button></div>
    <fieldset className="plugin-market-filters"><legend className="sr-only">{lang === 'zh' ? '插件来源' : 'Plugin source'}</legend>
      {['all', 'official', 'community'].map(value => <label key={value} className={source === value ? 'is-selected' : ''}><input type="radio" name="plugin-source" value={value} checked={source === value} onChange={() => setFilter('source', value)} />{c[value]}</label>)}
      {loading && <span className="plugin-market-loading" role="status">{c.loading}</span>}
      {communityState === 'cached' && source !== 'official' && <span className="plugin-market-cached" title={c.cachedTitle}>{c.cached}</span>}
    </fieldset>
    {communityState === 'cached' && source !== 'official' && <p className="plugin-market-cache-note" role="status">{c.cachedTitle} <button type="button" onClick={() => setRevision(value => value + 1)}>{lang === 'zh' ? '重试更新' : 'Retry update'}</button></p>}
    {((officialState === 'error' && source !== 'community') || (communityState === 'error' && source !== 'official')) && <p className="plugin-market-error" role="alert">{officialState === 'error' && source !== 'community' ? c.errorOfficial : ''} {communityState === 'error' && source !== 'official' ? c.errorCommunity : ''} <button type="button" disabled={loading} onClick={() => setRevision(value => value + 1)}>{lang === 'zh' ? '重新加载' : 'Try again'}</button></p>}
    {query.trim() && <div className="plugin-market-result-count"><span role="status">{lang === 'zh' ? `找到 ${visible.length} 个插件` : `${visible.length} matching plugins`}</span><button type="button" onClick={() => setFilter('q', '')}><X size={14} aria-hidden="true" />{lang === 'zh' ? '清除搜索' : 'Clear search'}</button></div>}
    {['official', 'community'].map(group => {
      const entries = visible.filter(entry => entry.source === group);
      if (!entries.length) return null;
      return <section className="plugin-market-source" key={group} aria-label={c[group]}><h2 className="plugin-market-section">{c[group]}<span>{entries.length}</span></h2><div className="plugin-market-grid">
        {entries.map(extension => <ExtensionCard key={`${extension.id}@${extension.version}`} extension={extension} c={c} />)}
      </div></section>;
    })}
    {!loading && visible.length === 0 && officialState !== 'error' && communityState !== 'error' && <p className="plugin-market-empty">{query.trim() ? c.noResults : c.empty}</p>}
    {!loading && !query.trim() && community.length === 0 && communityState === 'ready' && source !== 'official' && official.length > 0 && <section className="plugin-market-source"><h2 className="plugin-market-section">{c.community}<span>0</span></h2><p className="plugin-market-empty">{c.emptyCommunity}</p></section>}
    <details className="plugin-market-help"><summary>{c.installation}</summary><p>{c.installNote}</p></details>
  </section>;
}

function ExtensionCard({ extension, c }) {
  const command = `/extensions install ${extension.id}`;
  const { state, copy } = useClipboard(command);
  return <article className={`plugin-market-card is-${extension.source}`}>
    <div className="plugin-market-card-top"><span className="plugin-market-glyph" aria-hidden="true">{extension.name.slice(0, 2).toUpperCase()}</span><div className="plugin-market-card-identity"><h3>{extension.name}</h3><small>{extension.source === 'official' ? extension.publisher : `@${extension.publisher}`}</small></div>{extension.source === 'official' && <span className="plugin-market-badge">{c.official}</span>}</div>
    <p>{extension.source === 'official' ? c.summaries[extension.id.split('/')[1]] || extension.description : extension.description}</p>
    <div className="plugin-market-card-footer"><code>v{extension.version}</code><button type="button" disabled={state === 'pending'} onClick={copy} title={command} aria-label={`${c.copy}: ${extension.name}`}><span aria-live="polite">{state === 'copied' ? c.copied : c.copy}</span>{state === 'copied' ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}</button></div>
    {state === 'failed' && <label className="plugin-market-copy-fallback"><span role="status">{c.copyFailed}</span><input readOnly value={command} onFocus={event => event.target.select()} /></label>}
  </article>;
}
