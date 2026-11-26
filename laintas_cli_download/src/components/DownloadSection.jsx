import { useEffect, useRef, useState } from 'react';
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import {
  ArrowDownToLine, ArrowRight, ArrowUpRight, Check, Code2, Copy,
  Laptop, Monitor, Package, Play, Pause, RotateCcw, BookOpen,
} from 'lucide-react';
import { useLanguage } from '@/contexts/LanguageContext';
import SiteFooter from './SiteFooter';
import { Tabs, TabsList, TabsTrigger, TabsContent } from './ui/tabs';
import { useClipboard } from '../hooks/useClipboard';
import { useRelease } from '../hooks/useRelease';
import { useMotionPreference } from '../hooks/useMotionPreference';

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

// Pricing shown on this page mirrors laintas.com/pricing (the authoritative
// source: laintas server/plans.js, served at laintas.com/api/plans). Values
// are kept in sync with the main site's plan cards; the full
// tier table and allowance comparison live on the pricing page itself.
const PLANS = [
  { id: 'free', price: '$0', period: '/ mo', title: { zh: '按量使用', en: 'Pay as you go' }, badge: null, highlight: false },
  { id: 'go', price: '$5', period: '/ mo', title: { zh: 'Go', en: 'Go' }, badge: null, highlight: false },
  { id: 'pro', price: '$19.9', period: '/ mo', title: { zh: 'Pro', en: 'Pro' }, badge: { zh: '最受欢迎', en: 'Most popular' }, highlight: true },
  { id: 'gen', price: '$49.9', period: '/ mo', title: { zh: 'Gen', en: 'Gen' }, badge: null, highlight: false },
];

const COPY = {
  zh: {
    title: ['命令照常敲，', '其余交给 agent。'],
    lead: '在你原来的终端里读代码、改文件、跑测试，全程看得见，权限你说了算。',
    install: '安装 Laintas CLI', seeHow: '看它怎么工作',

    whyTitle: ['离你的文件', '最近的那个 agent。'],
    compareHead: ['', '聊天窗口', 'Laintas CLI'],
    compare: [
      ['上下文', '你粘贴的代码', '工作区、终端、记忆'],
      ['执行', '给你命令，自己跑', '自己跑完，定下一步'],
      ['出错时', '你转述报错', '同屏看到报错'],
      ['长任务', '关页就断', '落盘，/resume 续'],
    ],

    howTitle: ['一条指令进来，', '每一步都看得见。'],
    steps: ['分流', '组装上下文', '调用工具', '过闸', '执行并留痕'],
    gateTitle: ['并行，', '但不失控。'],
    gates: [
      ['放行', 'allow', ['读取项目文件', '运行测试', 'git status / diff']],
      ['询问', 'ask', ['写入或删除文件', 'git push', '读取 ~/.ssh 等凭据']],
      ['拒绝', 'deny', ['把凭据发往外部', '策略禁用的工具', '越权的子 agent 调用']],
    ],
    priceTitle: ['先装上，', '再决定用多少。'],
    priceIntro: '额度全产品共用，按会员价扣，超了转按量。',
    perMonth: '/ 月', pricing: '查看完整定价与模型分档',
    ecoTitle: '同一个账号，也通向这些',
    eco: [
      ['Helpwow', 'https://helpwow.laintas.com/'],
      ['插件市场', '/plugins'],
      ['Laintas 账户', 'https://laintas.com/dashboard'],
    ],

    downloadHeading: '下载',
    platformLabel: '选择安装平台', linux: 'Linux', mac: 'macOS', windows: 'Windows', copied: '已复制', copy: '复制',
    windowsAlt: '或直接下载安装包',
    afterInstall: '装好后运行 laintas-cli，登录即可。',
    afterInstallWindows: '装好后从开始菜单打开 laintas-cli-windows，登录即可。',
    packages: '安装包', download: '下载', checksums: 'SHA256',
    requirementsLink: '系统要求',
  },
  en: {
    title: ['Your shell, as usual.', 'Plus an agent.'],
    lead: 'Read code, edit files and run tests right in your own terminal — every step visible, permissions in your control.',
    install: 'Install Laintas CLI', seeHow: 'See how it works',

    whyTitle: ['The agent closest', 'to your files.'],
    compareHead: ['', 'Chat window', 'Laintas CLI'],
    compare: [
      ['Context', 'Code you paste in', 'Workspace, terminal, memory'],
      ['Execution', 'Hands you a command to run', 'Runs it, then decides'],
      ['On failure', 'You relay the error', 'Sees the same error screen'],
      ['Long tasks', 'Dies with the tab', 'Persisted; /resume picks up'],
    ],

    howTitle: ['One request in.', 'Every step visible.'],
    steps: ['Route', 'Assemble context', 'Call tools', 'Pass the gate', 'Run and record'],
    gateTitle: ['Parallel,', 'never out of hand.'],
    gates: [
      ['Allow', 'allow', ['Read project files', 'Run the tests', 'git status / diff']],
      ['Ask', 'ask', ['Write or delete files', 'git push', 'Read ~/.ssh and other credentials']],
      ['Deny', 'deny', ['Send credentials off the machine', 'Tools the policy disables', 'Sub-agent calls beyond their role']],
    ],
    priceTitle: ['Install first.', 'Decide how much later.'],
    priceIntro: 'One allowance shared across every product, drawn at member prices; anything beyond it is pay-as-you-go.',
    perMonth: '/ mo', pricing: 'Full pricing and model tiers',
    ecoTitle: 'The same account also opens',
    eco: [
      ['Helpwow', 'https://helpwow.laintas.com/'],
      ['Plugin market', '/plugins'],
      ['Laintas account', 'https://laintas.com/dashboard'],
    ],

    downloadHeading: 'Download',
    platformLabel: 'Select install platform', linux: 'Linux', mac: 'macOS', windows: 'Windows', copied: 'Copied', copy: 'Copy',
    windowsAlt: 'Or download the installer',
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
        {/* ── Hero: one line, one command, one real session ─────────────── */}
        <section id="top" className="hp-hero">
          <div className="hp-intro-grid">
            <div className="hp-intro">
              <h1 className="st-display hp-display"><Title parts={c.title} /></h1>
              <p className="st-lead">{c.lead}</p>
              <div className="hp-hero-links"><a href="#demo"><Play size={15} aria-hidden="true" />{c.seeHow}</a><Link to="/docs#install"><BookOpen size={15} aria-hidden="true" />{lang === 'zh' ? '安装指南' : 'Installation guide'}</Link></div>
            </div>
            <div className="hp-quickstart">
              <InstallBar platform={installPlatform} setPlatform={setInstallPlatform} c={c} windowsRelease={windowsRelease} lang={lang} />
              <Link className="hp-download-link" to={`/?platform=${installPlatform}#download`}>{lang === 'zh' ? '查看安装步骤与全部安装包' : 'Install steps & all packages'}<ArrowRight size={15} aria-hidden="true" /></Link>
            </div>
          </div>

          <DemoVideo lang={lang} c={c} />
        </section>

        {/* ── Why, and the boundary that keeps it safe ──────────────────── */}
        <section id="operations" className="hp-section">
          <ChapterHead title={c.whyTitle} />
          <div className="st-compare" role="table">
            <div className="st-compare-row st-compare-head" role="row">
              {c.compareHead.map((label, index) => <span role="columnheader" key={index}>{label}</span>)}
            </div>
            {c.compare.map(([label, chat, cli]) => (
              <div className="st-compare-row" role="row" key={label}>
                <span role="rowheader">{label}</span>
                <span role="cell" data-label={c.compareHead[1]}>{chat}</span>
                <span role="cell" data-label={c.compareHead[2]}><Check size={14} />{cli}</span>
              </div>
            ))}
          </div>
          <div id="workflow" className="hp-flow-block">
            <ChapterHead title={c.howTitle} small />
            <ol className="hp-flow">
              {c.steps.map((title, index) => (
                <li key={title}>
                  <span className="hp-flow-num">0{index + 1}</span>
                  <span className="hp-flow-title">{title}</span>
                  {index < c.steps.length - 1 && <ArrowRight className="hp-flow-arrow" size={13} />}
                </li>
              ))}
            </ol>
          </div>

          <div id="security" className="st-gate-block">
            <ChapterHead title={c.gateTitle} small />
            <div className="st-gates">
              {c.gates.map(([label, tone, items]) => (
                <div className={`st-gate st-gate-${tone}`} key={tone}>
                  <p><i />{label}</p>
                  <ul>{items.map((item) => <li key={item}>{item}</li>)}</ul>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* ── Pricing ───────────────────────────────────────────────────── */}
        <section id="pricing" className="hp-section">
          <ChapterHead title={c.priceTitle} />
          <p className="st-note">{c.priceIntro}</p>
          <div className="st-plans">
            {PLANS.map((plan) => {
              const badge = plan.badge ? (plan.badge[lang] || plan.badge.en) : null;
              return (
                <a className={`st-plan${plan.highlight ? ' st-plan-highlight' : ''}`} key={plan.id} href={`https://laintas.com/pricing${plan.id === 'free' ? '#allowance-comparison' : ''}`} target="_blank" rel="noreferrer">
                  <div className="st-plan-price">
                    <p>{plan.title[lang] || plan.title.en}{badge && <em>{badge}</em>}</p>
                    <strong>{plan.price}<small>{c.perMonth}</small></strong>
                  </div>
                  <ArrowUpRight className="st-plan-arrow" size={18} />
                </a>
              );
            })}
          </div>
          <a className="st-link" href="https://laintas.com/pricing" target="_blank" rel="noreferrer">{c.pricing}<ArrowUpRight size={14} /></a>
          <div className="st-eco">
            <p className="st-eco-title">{c.ecoTitle}</p>
            {c.eco.map(([name, href]) => (
              <a key={name} href={href} {...(href.startsWith('http') ? { target: '_blank', rel: 'noreferrer' } : {})}>
                <strong>{name}</strong><ArrowUpRight size={16} />
              </a>
            ))}
          </div>
        </section>

        {/* ── Download ──────────────────────────────────────────────────── */}
        <section id="download" className="hp-section">
          <h2 className="st-display st-download-title">{c.downloadHeading}</h2>
          <InstallBar minimal platform={installPlatform} setPlatform={setInstallPlatform} c={c} windowsRelease={windowsRelease} lang={lang} />
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

/* The two install entry points share platform state through the URL. */
function InstallBar({ platform, setPlatform, c, windowsRelease, lang, minimal = false }) {
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
      {minimal ? (state === 'failed' && <p className="install-feedback" role="status" data-state={state}>
        {zh ? '无法自动复制，请选中上方命令后手动复制。' : 'Copy unavailable. Select the command above and copy it manually.'}
      </p>) : <p className="install-feedback" role="status" data-state={state}>
        {state === 'failed' ? (zh ? '无法自动复制，请选中上方命令后手动复制。' : 'Copy unavailable. Select the command above and copy it manually.')
          : state === 'copied' ? (zh ? '已复制。粘贴到终端运行，然后按下方步骤启动。' : 'Copied. Paste into your terminal, then follow the launch steps below.')
          : platform === 'windows' ? (zh ? '在 PowerShell 中运行 · 无需 WSL' : 'Run in PowerShell · No WSL needed')
          : (zh ? '在终端中运行 · 自动识别 CPU 架构' : 'Run in your terminal · Detects your CPU architecture')}
      </p>}
    </Tabs>
    {!minimal && <a className={`st-link install-alt-link${platform === 'windows' ? '' : ' is-off'}`} href={windowsRelease.installer} aria-hidden={platform !== 'windows'} tabIndex={platform === 'windows' ? undefined : -1}><ArrowDownToLine size={16} aria-hidden="true" />{c.windowsAlt} · {windowsRelease.version}</a>}
  </div>;
}

/* Visibility may pause playback; only the visitor can undo a manual pause. */
function DemoVideo({ lang, c }) {
  const ref = useRef(null);
  const visible = useRef(false);
  const wantsPlayback = useRef(true);
  const automaticPause = useRef(false);
  const [playing, setPlaying] = useState(false);
  const [failed, setFailed] = useState(false);
  const reducedMotion = useMotionPreference();
  const variant = lang === 'zh' ? 'zh' : 'en';
  const zh = lang === 'zh';

  useEffect(() => {
    const video = ref.current;
    if (!video) return;
    wantsPlayback.current = !reducedMotion;
    automaticPause.current = false;
    setFailed(false);
    setPlaying(false);
    const pause = () => {
      if (!video.paused) { automaticPause.current = true; video.pause(); }
    };
    const update = () => {
      if (visible.current && !document.hidden && wantsPlayback.current) video.play().catch(() => {});
      else pause();
    };
    const observer = new IntersectionObserver(([entry]) => {
      visible.current = entry.isIntersecting;
      update();
    }, { threshold: 0.2 });
    observer.observe(video);
    document.addEventListener('visibilitychange', update);
    return () => { observer.disconnect(); document.removeEventListener('visibilitychange', update); pause(); };
  }, [variant, reducedMotion]);

  function toggle() {
    const video = ref.current;
    if (!video) return;
    if (video.paused) {
      wantsPlayback.current = true;
      video.play().catch(() => setFailed(true));
    } else {
      wantsPlayback.current = false;
      video.pause();
    }
  }
  function restart() {
    const video = ref.current;
    if (!video) return;
    if (failed) video.load();
    setFailed(false);
    video.currentTime = 0;
    wantsPlayback.current = true;
    video.play().catch(() => setFailed(true));
  }
  return <figure id="demo" className="hp-video">
    <div className="hp-video-toolbar">
      <div><button type="button" onClick={toggle} aria-label={zh ? (playing ? '暂停演示' : '播放演示') : (playing ? 'Pause demo' : 'Play demo')} aria-pressed={playing}>
        {playing ? <Pause size={15} aria-hidden="true" /> : <Play size={15} aria-hidden="true" />}<span>{zh ? (playing ? '暂停' : '播放') : (playing ? 'Pause' : 'Play')}</span>
      </button><button type="button" onClick={restart} aria-label={zh ? '重新播放' : 'Restart demo'}><RotateCcw size={15} aria-hidden="true" /><span>{zh ? '重播' : 'Restart'}</span></button></div>
    </div>
    <div className="hp-video-frame">
      <video ref={ref} key={variant} muted loop playsInline preload="metadata" controls
        onPlay={() => { wantsPlayback.current = true; setPlaying(true); }}
        onPause={() => {
          setPlaying(false);
          if (automaticPause.current) automaticPause.current = false;
          else wantsPlayback.current = false;
        }}
        onError={() => setFailed(true)}
        poster={`/media/laintas-cli-demo-${variant}.jpg`} aria-label={zh ? 'Laintas CLI 演示' : 'Laintas CLI demo'}>
        <source src={`/media/laintas-cli-demo-${variant}.webm`} type="video/webm" />
        <source src={`/media/laintas-cli-demo-${variant}.mp4`} type="video/mp4" />
      </video>
    </div>
    {failed && <p className="demo-error" role="status">{zh ? '演示暂时无法播放。' : 'The demo could not play. '}
      <a href={`/media/laintas-cli-demo-${variant}.mp4`}>{zh ? '打开视频文件' : 'Open the video file'}</a>
      <button type="button" onClick={restart}>{zh ? '重新加载' : 'Try again'}</button>
    </p>}
  </figure>;
}

function Title({ parts: [plain, emphasised] }) {
  return <>{plain}<br /><em>{emphasised}</em></>;
}

function ChapterHead({ title, intro, small = false }) {
  return <header className={`st-head${small ? ' st-head-small' : ''}`}>
    <h2 className="st-display"><Title parts={title} /></h2>
    {intro && <p>{intro}</p>}
  </header>;
}
