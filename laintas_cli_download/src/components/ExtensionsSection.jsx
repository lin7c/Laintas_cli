import { useEffect, useMemo, useState } from 'react';
import { Check, Copy, PackageCheck, ShieldAlert, UserRound } from 'lucide-react';
import { useLanguage } from '@/contexts/LanguageContext';

const COPY = {
  zh: {
    kicker: '扩展',
    title: ['两个来源。', '信任不糊弄。'],
    intro: '官方包与用户发布的代码，在页面上和技术上都明确分开。社区代码未经 Laintas 审核，每次安装前都会过一遍 AI 辅助的源码审查。',
    official: '官方扩展', officialSub: '由 Laintas 维护与发布',
    community: '社区扩展', communitySub: '用户发布 · 未经 Laintas 审核',
    boundary: '独立发布者边界',
    officialTag: '官方', communityTag: '未审核',
    loadingOfficial: '正在加载官方注册表…', loadingCommunity: '正在加载社区注册表…',
    errorOfficial: '官方注册表暂时不可用。', errorCommunity: '社区注册表暂时不可用。',
    emptyOfficial: '目前没有可用的官方扩展。', emptyCommunity: '还没有用户发布社区扩展。',
    copyLabel: '复制安装命令', copied: '已复制',
  },
  en: {
    kicker: 'EXTENSIONS',
    title: ['Two sources.', 'No blurred trust.'],
    intro: 'Official packages and user-published code remain visibly and technically separate. Community code is unreviewed and receives a fresh AI-assisted source review before every installation.',
    official: 'Official Extensions', officialSub: 'Maintained and shipped by Laintas',
    community: 'Community Extensions', communitySub: 'User-published · not reviewed by Laintas',
    boundary: 'Independent publisher boundary',
    officialTag: 'OFFICIAL', communityTag: 'UNREVIEWED',
    loadingOfficial: 'Loading official registry…', loadingCommunity: 'Loading community registry…',
    errorOfficial: 'Official registry is temporarily unavailable.', errorCommunity: 'Community registry is temporarily unavailable.',
    emptyOfficial: 'No official extensions are currently available.', emptyCommunity: 'No community extensions have been published yet.',
    copyLabel: 'Copy install command', copied: 'Copied',
  },
};

// `standalone` is set when this is the whole page rather than a section of
// another page: the page needs the header's height cleared, and the heading
// is the page's h1.
export default function ExtensionsSection({ standalone = false }) {
  const { lang } = useLanguage();
  const c = COPY[lang] || COPY.en;
  const [official, setOfficial] = useState([]);
  const [community, setCommunity] = useState([]);
  const [officialState, setOfficialState] = useState('loading');
  const [communityState, setCommunityState] = useState('loading');

  useEffect(() => {
    let active = true;
    fetch('/extensions/official-registry.json', { headers: { Accept: 'application/json' } })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((body) => {
        if (!active) return;
        setOfficial(Array.isArray(body.extensions) ? body.extensions : []);
        setOfficialState('ready');
      })
      .catch(() => active && setOfficialState('error'));

    // Community data comes from the live API. A snapshot of that API is
    // baked into the build (public/extensions/community-snapshot.json) so
    // the prerendered page — and any first paint — shows real content
    // instead of an error; the live fetch then refreshes it in place.
    let communityReady = false;
    const applyCommunity = (body) => {
      if (!active) return;
      communityReady = true;
      setCommunity(Array.isArray(body.extensions) ? body.extensions : []);
      setCommunityState('ready');
    };
    fetch('/extensions/community-snapshot.json', { headers: { Accept: 'application/json' } })
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`))))
      .then(applyCommunity)
      .catch(() => {})
      .then(() => fetch('/api/extensions/community', { headers: { Accept: 'application/json' } }))
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`))))
      .then(applyCommunity)
      .catch(() => { if (active && !communityReady) setCommunityState('error'); });
    return () => { active = false; };
  }, []);

  const authors = useMemo(() => {
    const grouped = new Map();
    community.forEach((extension) => {
      const author = extension.author || 'unknown';
      grouped.set(author, [...(grouped.get(author) || []), extension]);
    });
    return [...grouped.entries()].sort(([left], [right]) => left.localeCompare(right));
  }, [community]);

  const Heading = standalone ? 'h1' : 'h2';

  return (
    <section id="extensions" className={`extensions-section page-shell${standalone ? ' extensions-standalone' : ''}`} lang={lang === 'zh' ? 'zh-CN' : 'en'}>
      <div className="extensions-heading">
        <div>
          <p className="st-kicker"><i aria-hidden="true" />{c.kicker}</p>
          <Heading className="st-display ext-display">{c.title[0]}<br /><em>{c.title[1]}</em></Heading>
        </div>
        <p>{c.intro}</p>
      </div>

      <div className="extension-lane extension-lane-official">
        <div className="extension-lane-label">
          <PackageCheck size={18} />
          <div><strong>{c.official}</strong><span>{c.officialSub}</span></div>
        </div>
        <div className="extension-card-grid">
          {official.map((extension) => (
            <ExtensionCard key={extension.id} extension={extension} official c={c} />
          ))}
        </div>
        {officialState === 'loading' && <p className="extension-state">{c.loadingOfficial}</p>}
        {officialState === 'error' && <p className="extension-state extension-state-error">{c.errorOfficial}</p>}
        {officialState === 'ready' && official.length === 0 && <p className="extension-state">{c.emptyOfficial}</p>}
      </div>

      <div className="extension-divider"><span>{c.boundary}</span></div>

      <div className="extension-lane extension-lane-community">
        <div className="extension-lane-label">
          <ShieldAlert size={18} />
          <div><strong>{c.community}</strong><span>{c.communitySub}</span></div>
        </div>
        {communityState === 'loading' && <p className="extension-state">{c.loadingCommunity}</p>}
        {communityState === 'error' && <p className="extension-state extension-state-error">{c.errorCommunity}</p>}
        {communityState === 'ready' && authors.length === 0 && <p className="extension-state">{c.emptyCommunity}</p>}
        {authors.map(([author, extensions]) => (
          <div className="extension-author" key={author}>
            <div className="extension-author-name"><UserRound size={14} /><span>@{author}</span></div>
            <div className="extension-card-grid">
              {extensions.map((extension) => (
                <ExtensionCard key={`${extension.id}@${extension.version}`} c={c} extension={{
                  ...extension,
                  name: extension.manifest?.displayName || extension.slug,
                  summary: extension.manifest?.summary || extension.manifest?.description || '',
                }} />
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function ExtensionCard({ extension, official = false, c }) {
  const command = `/extensions install ${extension.id}`;
  const [copied, setCopied] = useState(false);
  async function copy() {
    await navigator.clipboard.writeText(command);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1400);
  }
  return (
    <article className={`extension-card ${official ? 'official' : 'community'}`}>
      <div className="extension-card-meta">
        <span><i className="ext-dot" aria-hidden="true" />{official ? c.officialTag : c.communityTag}</span>
        <code>v{extension.version}</code>
      </div>
      <h3>{extension.name}</h3>
      <p>{extension.summary || extension.description || ''}</p>
      <div className="extension-command">
        <code>{command}</code>
        <button type="button" onClick={copy} aria-label={`${c.copyLabel}: ${extension.name}`}>
          {copied ? <Check size={15} /> : <Copy size={15} />}
        </button>
      </div>
    </article>
  );
}
