import { useEffect, useState } from 'react';
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import {
  ArrowDownToLine, ArrowRight, ArrowUpRight, Check, Code2, Copy,
  Laptop, Monitor, Package,
} from 'lucide-react';
import { useLanguage } from '@/contexts/LanguageContext';
import SiteFooter from './SiteFooter';
import { Tabs, TabsList, TabsTrigger, TabsContent } from './ui/tabs';
import { useClipboard } from '../hooks/useClipboard';
import { useRelease } from '../hooks/useRelease';

// The site mirrors the verified assets from the latest GitHub Release.
const RELEASE_BASE = 'https://cli.laintas.com/releases/latest';
// laintas-cli-windows: the CLI in its own terminal window. It is published
// beside the Unix releases, apart from the Helpwow desktop channel in /windows/.
const WINDOWS_DOWNLOAD = 'https://cli.laintas.com/windows-cli/beta-v5/laintas-cli-windows_beta-v5_x64-setup.exe';
const WINDOWS_RELEASE = { version: 'beta v5', installer: WINDOWS_DOWNLOAD };
// The Windows channel publishes its own pointer file; read it live so this
// page can never point at a superseded installer again.
const WINDOWS_POINTER = 'https://cli.laintas.com/windows-cli/download.json';
const INSTALL_COMMANDS = {
  linux: 'curl -fsSL https://cli.laintas.com/install.sh | bash',
  mac: 'curl -fsSL https://cli.laintas.com/install.sh | bash',
  windows: 'irm https://cli.laintas.com/install.ps1 | iex',
};

// The three tabs are equal-width flex items, so the indicator position is
// pure arithmetic on the active index -- no measurement, no spring.
const PLATFORM_INDEX = { linux: 0, mac: 1, windows: 2 };

// Direct packages, for people who do not pipe scripts into their shell.
// The rolling release pointer owns version and asset metadata.
const PACKAGES = [
  { id: 'linux-amd64', icon: Package, file: 'laintas-cli_linux_amd64.tar.gz', names: { zh: 'Linux · x86_64', en: 'Linux · x86_64' } },
  { id: 'linux-arm64', icon: Package, file: 'laintas-cli_linux_arm64.tar.gz', names: { zh: 'Linux · arm64', en: 'Linux · arm64' } },
  { id: 'mac-arm', icon: Laptop, file: 'laintas-cli_darwin_arm64.tar.gz', names: { zh: 'macOS · Apple Silicon', en: 'macOS · Apple Silicon' } },
  { id: 'mac-intel', icon: Laptop, file: 'laintas-cli_darwin_amd64.tar.gz', names: { zh: 'macOS · Intel', en: 'macOS · Intel' } },
  { id: 'windows', icon: Monitor, href: WINDOWS_DOWNLOAD, names: { zh: 'Windows 桌面版', en: 'Windows desktop' }, tag: { zh: '无需 WSL', en: 'No WSL' } },
  { id: 'source', icon: Code2, file: 'laintas-cli_source.zip', names: { zh: '源码包', en: 'Source package' } },
];

const COPY = {
  zh: {
    downloadHeading: '下载',
    platformLabel: '选择安装平台', linux: 'Linux', mac: 'macOS', windows: 'Windows', copied: '已复制', copy: '复制',
    afterInstall: '装好后运行 laintas-cli，登录即可。',
    afterInstallWindows: '装好后从开始菜单打开 laintas-cli-windows，登录即可。',
    packages: '安装包', download: '下载', checksums: 'SHA256',
    requirementsLink: '系统要求',
  },
  en: {
    downloadHeading: 'Download',
    platformLabel: 'Select install platform', linux: 'Linux', mac: 'macOS', windows: 'Windows', copied: 'Copied', copy: 'Copy',
    afterInstall: 'Run laintas-cli once it is installed, and sign in.',
    afterInstallWindows: 'Then open laintas-cli-windows from Start and sign in.',
    packages: 'Packages', download: 'Download', checksums: 'SHA256',
    requirementsLink: 'System requirements',
  },
};

export default function DownloadSection() {
  const { lang } = useLanguage();
  const c = COPY[lang] || COPY.en;
  const release = useRelease();
  const [windowsRelease, setWindowsRelease] = useState(WINDOWS_RELEASE);
  const [params] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const [detectedPlatform] = useState(() => /Windows/i.test(navigator.userAgent) ? 'windows' : /Macintosh|Mac OS X/i.test(navigator.userAgent) ? 'mac' : 'linux');
  const platformParam = ['linux', 'mac', 'windows'].includes(params.get('platform')) ? params.get('platform') : detectedPlatform;
  const [installPlatform, selectPlatform] = useState(platformParam);
  useEffect(() => selectPlatform(platformParam), [platformParam]);
  const setInstallPlatform = platform => {
    selectPlatform(platform);
    const next = new URLSearchParams(params);
    next.set('platform', platform);
    navigate({ search: `?${next}`, hash: location.hash }, { preventScrollReset: true });
  };

  useEffect(() => {
    const controller = new AbortController();
    fetch(WINDOWS_POINTER, { signal: controller.signal })
      .then((response) => response.ok ? response.json() : Promise.reject(new Error('windows pointer lookup failed')))
      .then((data) => {
        if (!controller.signal.aborted && typeof data.label === 'string' && typeof data.installer === 'string'
          && data.installer.startsWith('https://cli.laintas.com/windows-cli/')) setWindowsRelease({ version: data.label, installer: data.installer });
      })
      .catch(() => {});
    return () => controller.abort();
  }, []);

  return (
    <main id="main-content" tabIndex={-1} className="product-page" lang={lang === 'zh' ? 'zh-CN' : 'en'}>
      <div className="page-shell">
        <section id="download" className="hp-section">
          <h1 className="st-display st-download-title">{c.downloadHeading}</h1>
          <p className="st-download-sub" translate="no">Laintas CLI · {release}</p>
          <InstallBar platform={installPlatform} setPlatform={setInstallPlatform} c={c} lang={lang} />
          <p className="st-note st-after-install">{installPlatform === 'windows' ? c.afterInstallWindows : c.afterInstall}</p>
          <p className="st-eco-title">{c.packages}</p>
          <div className="st-packages">
            {PACKAGES.map(({ id, icon: Icon, file, href, names, tag }) => (
              <a href={id === 'windows' ? windowsRelease.installer : href || `${RELEASE_BASE}/${file}`} key={id}>
                <Icon size={18} strokeWidth={1.6} />
                <span><strong>{names[lang] || names.en}{tag && <i className="st-pkg-tag">{tag[lang] || tag.en}</i>}</strong></span>
                <code>{id === 'windows' ? windowsRelease.version : release}</code>
                <em>{c.download}<ArrowDownToLine size={15} /></em>
              </a>
            ))}
          </div>
          <div className="st-download-links">
            <a className="st-link" href={`${RELEASE_BASE}/SHA256SUMS.txt`}>{c.checksums}<ArrowUpRight size={14} /></a>
            <Link className="st-link" to="/docs#install--requirements">{c.requirementsLink}<ArrowRight size={14} /></Link>
          </div>
        </section>
      </div>

      <SiteFooter />
    </main>
  );
}

function InstallBar({ platform, setPlatform, c, lang }) {
  const { state, copy } = useClipboard(INSTALL_COMMANDS[platform]);
  const zh = lang === 'zh';
  return <div className="hp-install">
    <Tabs className="st-install" value={platform} onValueChange={setPlatform}>
      <TabsList variant="install" className="st-install-tabs" aria-label={c.platformLabel} style={{ '--tab-index': PLATFORM_INDEX[platform] ?? 0 }}>
        <span className="install-tab-indicator" aria-hidden="true" />
        {['linux', 'mac', 'windows'].map(p => <TabsTrigger variant="install" value={p} key={p}>
          <span>{c[p]}</span>
        </TabsTrigger>)}
      </TabsList>
      {['linux', 'mac', 'windows'].map(p => <TabsContent variant="install" value={p} key={p}>
        <div className="st-install-line">
          <code translate="no"><span aria-hidden="true">{p === 'windows' ? 'PS>' : '$'}</span>{INSTALL_COMMANDS[p]}</code>
          <button type="button" className="st-copy" onClick={copy} disabled={state === 'pending'} data-state={state} aria-label={zh ? '复制安装命令' : 'Copy install command'}>
            {state === 'copied' ? <Check size={15} aria-hidden="true" /> : <Copy size={15} aria-hidden="true" />}
            {state === 'copied' ? c.copied : state === 'pending' ? (zh ? '复制中…' : 'Copying…') : c.copy}
          </button>
        </div>
      </TabsContent>)}
      {state === 'failed' && <p className="install-feedback" role="status" data-state={state}>
        {zh ? '无法自动复制，请选中上方命令后手动复制。' : 'Copy unavailable. Select the command above and copy it manually.'}
      </p>}
    </Tabs>
  </div>;
}
