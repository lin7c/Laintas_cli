import { useEffect, useMemo, useState } from 'react';
import { Check, Copy, PackageCheck, ShieldAlert, UserRound } from 'lucide-react';

export default function ExtensionsSection() {
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
    fetch('/api/extensions/community', { headers: { Accept: 'application/json' } })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((body) => {
        if (!active) return;
        setCommunity(Array.isArray(body.extensions) ? body.extensions : []);
        setCommunityState('ready');
      })
      .catch(() => active && setCommunityState('error'));
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

  return (
    <section id="extensions" className="extensions-section page-shell">
      <div className="extensions-heading">
        <div>
          <p className="section-kicker">04 / EXTENSIONS</p>
          <h2>Two sources.<br />No blurred trust.</h2>
        </div>
        <p>Official packages and user-published code remain visibly and technically separate. Community code is unreviewed and receives a fresh AI-assisted source review before every installation.</p>
      </div>

      <div className="extension-lane extension-lane-official">
        <div className="extension-lane-label">
          <PackageCheck size={18} />
          <div><strong>Official Extensions</strong><span>Maintained and shipped by Laintas</span></div>
        </div>
        <div className="extension-card-grid">
          {official.map((extension) => (
            <ExtensionCard key={extension.id} extension={extension} official />
          ))}
        </div>
        {officialState === 'loading' && <p className="extension-state">Loading official registry…</p>}
        {officialState === 'error' && <p className="extension-state extension-state-error">Official registry is temporarily unavailable.</p>}
        {officialState === 'ready' && official.length === 0 && <p className="extension-state">No official extensions are currently available.</p>}
      </div>

      <div className="extension-divider"><span>Independent publisher boundary</span></div>

      <div className="extension-lane extension-lane-community">
        <div className="extension-lane-label">
          <ShieldAlert size={18} />
          <div><strong>Community Extensions</strong><span>User-published · not reviewed by Laintas</span></div>
        </div>
        {communityState === 'loading' && <p className="extension-state">Loading community registry…</p>}
        {communityState === 'error' && <p className="extension-state extension-state-error">Community registry is temporarily unavailable.</p>}
        {communityState === 'ready' && authors.length === 0 && <p className="extension-state">No community extensions have been published yet.</p>}
        {authors.map(([author, extensions]) => (
          <div className="extension-author" key={author}>
            <div className="extension-author-name"><UserRound size={14} /><span>@{author}</span></div>
            <div className="extension-card-grid">
              {extensions.map((extension) => (
                <ExtensionCard key={`${extension.id}@${extension.version}`} extension={{
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

function ExtensionCard({ extension, official = false }) {
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
        <span>{official ? 'OFFICIAL' : 'UNREVIEWED'}</span>
        <code>v{extension.version}</code>
      </div>
      <h3>{extension.name}</h3>
      <p>{extension.summary || extension.description || ''}</p>
      <div className="extension-command">
        <code>{command}</code>
        <button type="button" onClick={copy} aria-label={`Copy install command for ${extension.name}`}>
          {copied ? <Check size={15} /> : <Copy size={15} />}
        </button>
      </div>
    </article>
  );
}
