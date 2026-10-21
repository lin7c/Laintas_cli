import { useEffect, useRef, useState } from 'react';
import {
  ArrowDownToLine, ArrowRight, ArrowUpRight, Check, Code2, Copy, Layers3,
  Laptop, Monitor, Package, ShieldCheck, TerminalSquare, Waypoints,
} from 'lucide-react';
import { useLanguage } from '@/contexts/LanguageContext';
import SiteFooter from './SiteFooter';

const RELEASE_FALLBACK = 'laintas-cli-beta-v5';
// The site mirrors the verified assets from the latest GitHub Release.
const RELEASE_BASE = 'https://cli.laintas.com/releases/latest';
const RELEASE_API = 'https://api.github.com/repos/lin7c/Laintas_cli/releases/latest';
const RELEASE_DATE = '2026-10-21';
// laintas-cli-windows: the CLI in its own terminal window. It is published
// beside the Unix releases, apart from the Helpwo desktop channel in /windows/.
const WINDOWS_DOWNLOAD = 'https://cli.laintas.com/windows-cli/beta-v5/laintas-cli-windows_beta-v5_x64-setup.exe';
const WINDOWS_RELEASE = { version: 'beta v5', installer: WINDOWS_DOWNLOAD };
const INSTALL_COMMANDS = {
  linux: 'curl -fsSL https://cli.laintas.com/install.sh | bash',
  mac: 'curl -fsSL https://cli.laintas.com/install.sh | bash',
  windows: 'irm https://cli.laintas.com/install.ps1 | iex',
};

// Direct packages, for people who do not pipe scripts into their shell.
// Sizes and dates are those of the mirrored release assets.
const PACKAGES = [
  { id: 'linux-amd64', icon: Package, file: 'laintas-cli_linux_amd64.tar.gz', size: '56 MB', names: { zh: 'Linux · x86_64', en: 'Linux · x86_64' }, details: { zh: 'tar.gz · glibc 64 位', en: 'tar.gz · 64-bit glibc' } },
  { id: 'linux-arm64', icon: Package, file: 'laintas-cli_linux_arm64.tar.gz', size: '54 MB', names: { zh: 'Linux · arm64', en: 'Linux · arm64' }, details: { zh: 'tar.gz · glibc 64 位', en: 'tar.gz · 64-bit glibc' } },
  { id: 'mac-arm', icon: Laptop, file: 'laintas-cli_darwin_arm64.tar.gz', size: '25 MB', names: { zh: 'macOS · Apple Silicon', en: 'macOS · Apple Silicon' }, details: { zh: 'tar.gz', en: 'tar.gz' } },
  { id: 'mac-intel', icon: Laptop, file: 'laintas-cli_darwin_amd64.tar.gz', size: '24 MB', names: { zh: 'macOS · Intel', en: 'macOS · Intel' }, details: { zh: 'tar.gz', en: 'tar.gz' } },
  { id: 'windows', icon: Monitor, href: WINDOWS_DOWNLOAD, size: '55 MB', date: '2026-10-21', names: { zh: 'laintas-cli-windows', en: 'laintas-cli-windows' }, details: { zh: '安装程序 · 自带终端窗口，无需 WSL', en: 'Installer · its own terminal window, no WSL' } },
  { id: 'source', icon: Code2, file: 'laintas-cli_source.zip', size: '2.1 MB', names: { zh: '源码包', en: 'Source package' }, details: { zh: 'Python 3.10+ · 审计与二次开发', en: 'Python 3.10+ · audit and extend' } },
];

// Capability icons, in the order of COPY[lang].caps.
const CAP_ICONS = [TerminalSquare, Waypoints, ShieldCheck, Layers3];

// Pricing shown on this page mirrors laintas.com/pricing (the authoritative
// source: laintas server/plans.js, served at laintas.com/api/plans). Values
// are kept in sync with the main site's plan cards; the full
// tier table and allowance comparison live on the pricing page itself.
const PLANS = [
  { id: 'free', price: '$0', period: '/ mo', title: { zh: '按量使用', en: 'Pay as you go' }, desc: { zh: '无需订阅。用多少付多少，单价公开透明。', en: 'No subscription. Only pay for what you use.' }, features: { zh: ['新用户 100 次调用试用', '用多少付多少', '模型分档，单价透明'], en: ['100-call trial for new accounts', 'Only pay for usage', 'Transparent model tiers'] }, badge: null, highlight: false },
  { id: 'go', price: '$5', period: '/ mo', title: { zh: 'Go', en: 'Go' }, desc: { zh: '以最低门槛用上会员价，适合轻度使用。', en: 'Member prices at the lowest entry point, for lighter use.' }, features: { zh: ['每月会员积分', '所有产品共用同一额度', '按会员价扣积分，比按量便宜', '超出后可继续按量'], en: ['Monthly member credits', 'One pool across every product', 'Drawn at member prices, below pay-as-you-go', 'Continue pay-as-you-go after'] }, badge: null, highlight: false },
  { id: 'pro', price: '$19.9', period: '/ mo', title: { zh: 'Pro', en: 'Pro' }, desc: { zh: '每月一份会员积分，全产品共用。', en: 'A monthly pool of member credits, shared across every product.' }, features: { zh: ['每月会员积分', '所有产品共用同一额度', '按会员价扣积分，比按量便宜', '超出后可继续按量'], en: ['Monthly member credits', 'One pool across every product', 'Drawn at member prices, below pay-as-you-go', 'Continue pay-as-you-go after'] }, badge: { zh: '最受欢迎', en: 'Most popular' }, highlight: true },
  { id: 'gen', price: '$49.9', period: '/ mo', title: { zh: 'Gen', en: 'Gen' }, desc: { zh: '每月更多会员积分，会员价更低。', en: 'A larger monthly pool of member credits, at a lower member price.' }, features: { zh: ['每月更多会员积分', '突发速率提高', '全产品共用额度', '面向全职 agent 工作流'], en: ['More monthly member credits', 'Higher burst rate', 'One pooled allowance', 'For full-time agent workflows'] }, badge: { zh: '全产品', en: 'All products' }, highlight: false },
];

const COPY = {
  zh: {
    kicker: 'LAINTAS CLI · 终端里的 AI agent',
    title: ['命令照常敲，', '其余交给 agent。'],
    lead: 'Laintas CLI 是装进 shell 的自主 agent。ls、git、vim 这类命令照旧直接在真实 PTY 里运行，不经过模型；用一句话描述的任务，交给 agent 去读代码、跑命令、改文件 —— 每一步都摆在你眼前，关键操作等你点头。',
    install: '安装 Laintas CLI', seeHow: '看它怎么工作',
    facts: [['免费', '运行时免费使用，可下载源码审计'], ['Linux · macOS · Windows', 'x86_64 / arm64，Windows 10 2004+ / 11'], ['一个账号', '与 Helpwo、插件市场共用 Laintas 账号']],
    videoTitle: '一段真实会话',
    videoNote: '在真实机器上实跑录制：agent 读完整个项目、跑测试、自己验证产出，再动手写文件。没有脚本摆拍，等待模型的空白也没有剪掉。',

    whyKicker: '01 · 为什么是它', whyTitle: ['离你的文件', '最近的那个 agent。'],
    whyIntro: '聊天窗口里的 AI 只能给建议，复制、粘贴、执行、再把报错转述回去，这些活还是你在干。Laintas CLI 直接站在你的工作目录里。',
    compareHead: ['', '聊天窗口', 'Laintas CLI'],
    compare: [
      ['上下文', '你复制过去的几段代码', '整个工作目录、终端状态和项目记忆'],
      ['执行', '给你一段命令，你自己去跑', '自己跑，读完整输出，再决定下一步'],
      ['出错时', '你把报错转述回去', '它看到的就是你眼前那屏报错'],
      ['长任务', '关掉页面就断了', '计划、事件和会话都落盘，/resume 接着做'],
    ],
    caps: [
      ['真实终端', '直接命令绕过模型；交互程序、长任务和命名子终端都保持原生 shell 行为。'],
      ['并行分工', '子 agent 分头干活：HWO 协调实时协作，HWG 把依赖编译成可恢复的工作图。'],
      ['分层授权', '模式、角色、阶段、全局策略、信任与审批层层收窄权限，由运行时强制执行。'],
      ['一个工具面', '内置工具、Skills、MCP 服务器和插件市场里的扩展，走同一个注册表和授权管道。'],
    ],

    howKicker: '02 · 一条指令的旅程', howTitle: ['一条指令进来，', '每一步都看得见。'],
    howIntro: '以“修一下失败的测试”为例，运行时真实经过的路径是这样：',
    steps: [
      ['分流', '$ pytest → PTY'],
      ['组装上下文', 'mode: act · scope: project'],
      ['调用工具', 'read · shell.exec · edit'],
      ['过闸', 'allow · ask · deny'],
      ['执行并留痕', 'trace: on'],
    ],
    gateKicker: '安全边界', gateTitle: ['并行，', '但不失控。'],
    gateIntro: 'Prompt 只负责表达意图，边界在运行时。模型再会说，也绕不过一条已经拒绝的策略。',
    gates: [
      ['放行', 'allow', ['读取项目文件', '运行测试', 'git status / diff']],
      ['询问', 'ask', ['写入或删除文件', 'git push', '读取 ~/.ssh 等凭据']],
      ['拒绝', 'deny', ['把凭据发往外部', '策略禁用的工具', '越权的子 agent 调用']],
    ],
    gateNote: '具体落在哪一档，取决于当前模式、项目策略与组织策略。',

    priceKicker: '03 · 价格', priceTitle: ['先装上，', '再决定用多少。'],
    priceIntro: 'Laintas CLI 本身不收费。模型调用按量计费或订阅会员，会员额度在所有 Laintas 产品间共用。',
    perMonth: '/ 月', pricing: '查看完整定价与模型分档',
    ecoTitle: '同一个账号，也通向这些',
    eco: [
      ['Helpwo', '网页端 AI 工作台。在 CLI 里 /connect，就能在浏览器里看到这台机器的终端并直接派任务。', 'https://helpwo.laintas.com/'],
      ['插件市场', '官方与社区扩展。/extensions install 一条命令装进 CLI，社区代码安装前会先过一遍审查。', '/plugins'],
      ['Laintas 账户', '余额、订阅、用量与订单，所有产品在同一处查看。', 'https://laintas.com/dashboard'],
    ],

    downloadKicker: '04 · 下载', downloadTitle: ['现在，', '交给你的终端。'],
    downloadIntro: '推荐一行命令安装；也可以直接下载安装包，或拿源码包审计与二次开发。',
    platformLabel: '选择安装平台', linux: 'Linux', mac: 'macOS', windows: 'Windows', copied: '已复制', copy: '复制',
    windowsAlt: '或直接下载安装包',
    startSteps: [['运行安装命令', '自动识别架构并完成安装'], ['启动并登录', '输入 laintas-cli，按提示登录 Laintas 账号'], ['说出第一个任务', '比如“看看这个项目怎么跑起来”']],
    windowsSteps: [['安装', '在 PowerShell 执行指令，或直接下载安装包；无需管理员权限'], ['启动并登录', '从开始菜单打开 laintas-cli-windows，登录页会在默认浏览器里打开'], ['开始对话', '在默认工作区给 agent 一个任务']],
    packages: '安装包', download: '下载', checksums: '全部文件的 SHA256 校验和',
    requirements: 'Linux 需 64 位 glibc 系统；Mac 支持 Apple Silicon 和 Intel，暂不包含 Helpwo Kernel；laintas-cli-windows 需 x86_64、Windows 10/11 和 WebView2，安装器可联网安装 WebView2；无需 WSL。',
  },
  en: {
    kicker: 'LAINTAS CLI · THE AI AGENT IN YOUR TERMINAL',
    title: ['Your shell, as usual.', 'Plus an agent.'],
    lead: 'Laintas CLI is an autonomous agent that lives in your shell. Commands like ls, git and vim still run straight in a real PTY, never through the model. Describe a task in a sentence and the agent reads code, runs commands and edits files — every step in front of you, every risky one waiting for your yes.',
    install: 'Install Laintas CLI', seeHow: 'See how it works',
    facts: [['Free', 'Free runtime, source you can download and audit'], ['Linux · macOS · Windows', 'x86_64 / arm64, Windows 10 2004+ / 11'], ['One account', 'Shared with Helpwo and the plugin market']],
    videoTitle: 'A real session, recorded as it ran',
    videoNote: 'Recorded on a real machine: the agent reads the whole project, runs the tests, verifies the output itself, and only then writes a file. Nothing is staged, and the model pauses are not cut.',

    whyKicker: '01 · WHY IT EXISTS', whyTitle: ['The agent closest', 'to your files.'],
    whyIntro: 'An AI in a chat window can only advise. Copying, pasting, running, and relaying the error back is still your job. Laintas CLI stands in your working directory instead.',
    compareHead: ['', 'Chat window', 'Laintas CLI'],
    compare: [
      ['Context', 'The snippets you pasted in', 'The whole workspace, terminal state and project memory'],
      ['Execution', 'Hands you a command to run', 'Runs it, reads the full output, decides the next step'],
      ['On failure', 'You relay the error back', 'It sees the same screen of errors you do'],
      ['Long tasks', 'Close the tab and it is gone', 'Plans, events and sessions persist; /resume picks up'],
    ],
    caps: [
      ['Real terminal', 'Direct commands bypass the model; interactive programs, long jobs and named sub-terminals keep native shell behavior.'],
      ['Parallel work', 'Sub-agents split the job: HWO coordinates live collaboration, HWG compiles dependencies into resumable graphs.'],
      ['Layered authorization', 'Modes, roles, phases, global policy, trust and approvals narrow what a tool may do — enforced by the runtime.'],
      ['One tool surface', 'Built-in tools, Skills, MCP servers and plugin-market extensions share one registry and one authorization pipeline.'],
    ],

    howKicker: '02 · THE LIFE OF A REQUEST', howTitle: ['One request in.', 'Every step visible.'],
    howIntro: 'Take “fix the failing test”. The path the runtime actually walks:',
    steps: [
      ['Route', '$ pytest → PTY'],
      ['Assemble context', 'mode: act · scope: project'],
      ['Call tools', 'read · shell.exec · edit'],
      ['Pass the gate', 'allow · ask · deny'],
      ['Run and record', 'trace: on'],
    ],
    gateKicker: 'SECURITY BOUNDARY', gateTitle: ['Parallel,', 'never out of hand.'],
    gateIntro: 'Prompts express intent; the boundary lives in the runtime. No amount of model persuasion gets past a policy that already said no.',
    gates: [
      ['Allow', 'allow', ['Read project files', 'Run the tests', 'git status / diff']],
      ['Ask', 'ask', ['Write or delete files', 'git push', 'Read ~/.ssh and other credentials']],
      ['Deny', 'deny', ['Send credentials off the machine', 'Tools the policy disables', 'Sub-agent calls beyond their role']],
    ],
    gateNote: 'Which tier an action lands in depends on the active mode, project policy and organization policy.',

    priceKicker: '03 · PRICING', priceTitle: ['Install first.', 'Decide how much later.'],
    priceIntro: "Laintas CLI itself is free. Model calls are pay-as-you-go or by membership, and every plan's allowance is shared across every Laintas product.",
    perMonth: '/ mo', pricing: 'Full pricing and model tiers',
    ecoTitle: 'The same account also opens',
    eco: [
      ['Helpwo', 'The web AI workspace. Run /connect in the CLI and see this machine’s terminal — and hand it tasks — from your browser.', 'https://helpwo.laintas.com/'],
      ['Plugin market', 'Official and community extensions, one /extensions install away. Community code is reviewed before it installs.', '/plugins'],
      ['Laintas account', 'Balance, subscription, usage and orders for every product, in one place.', 'https://laintas.com/dashboard'],
    ],

    downloadKicker: '04 · DOWNLOAD', downloadTitle: ['Now,', 'hand it to your terminal.'],
    downloadIntro: 'The one-line installer is recommended. You can also grab a package directly, or audit and extend the source.',
    platformLabel: 'Select install platform', linux: 'Linux', mac: 'macOS', windows: 'Windows', copied: 'Copied', copy: 'Copy',
    windowsAlt: 'Or download the installer',
    startSteps: [['Run the installer', 'It detects your architecture and installs laintas-cli'], ['Start and sign in', 'Type laintas-cli and sign in to your Laintas account'], ['Give it a first task', 'Try “figure out how to run this project”']],
    windowsSteps: [['Install', 'Run the command in PowerShell, or download the installer; no administrator rights needed'], ['Launch and sign in', 'Open laintas-cli-windows from Start; the sign-in page opens in your default browser'], ['Start a conversation', 'Give the agent a task in the default workspace']],
    packages: 'Packages', download: 'Download', checksums: 'SHA256 checksums for every file',
    requirements: 'Linux needs a 64-bit glibc system. Mac supports Apple Silicon and Intel without Helpwo Kernel. laintas-cli-windows needs Windows 10/11 x86_64 and WebView2; the installer can download WebView2. WSL is not needed.',
  },
};

export default function DownloadSection() {
  const { lang } = useLanguage();
  const c = COPY[lang] || COPY.en;
  const [release, setRelease] = useState(RELEASE_FALLBACK);
  const [windowsRelease, setWindowsRelease] = useState(WINDOWS_RELEASE);
  const [installPlatform, setInstallPlatform] = useState('linux');

  useEffect(() => {
    if (/Windows/i.test(window.navigator.userAgent)) setInstallPlatform('windows');
    else if (/Macintosh|Mac OS X/i.test(window.navigator.userAgent)) setInstallPlatform('mac');
    fetch(RELEASE_API)
      .then((response) => response.ok ? response.json() : Promise.reject(new Error('release lookup failed')))
      .then((data) => { if (data.tag_name) setRelease(data.tag_name); })
      .catch(() => {});
  }, []);

  return (
    <main className="product-page" lang={lang === 'zh' ? 'zh-CN' : 'en'}>
      <div className="page-shell">
        {/* ── Hero: one line, one command, one real session ─────────────── */}
        <section id="top" className="hp-hero">
          <Kicker>{c.kicker}</Kicker>
          <h1 className="st-display hp-display"><Title parts={c.title} /></h1>
          <p className="st-lead">{c.lead}</p>

          <InstallBar platform={installPlatform} setPlatform={setInstallPlatform} c={c} windowsRelease={windowsRelease} />

          <DemoVideo lang={lang} c={c} />

          <dl className="st-facts">
            {c.facts.map(([term, detail]) => <div key={term}><dt>{term}</dt><dd>{detail}</dd></div>)}
          </dl>
        </section>

        {/* ── Why, and the boundary that keeps it safe ──────────────────── */}
        <section id="operations" className="hp-section">
          <ChapterHead kicker={c.whyKicker} title={c.whyTitle} intro={c.whyIntro} />
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
          <div className="st-caps">
            {c.caps.map(([title, desc], index) => {
              const Icon = CAP_ICONS[index];
              return (
                <article key={title}>
                  <Icon size={18} strokeWidth={1.6} />
                  <h3>{title}</h3>
                  <p>{desc}</p>
                </article>
              );
            })}
          </div>

          <div id="workflow" className="hp-flow-block">
            <ChapterHead kicker={c.howKicker} title={c.howTitle} intro={c.howIntro} small />
            <ol className="hp-flow">
              {c.steps.map(([title, tag], index) => (
                <li key={title}>
                  <span className="hp-flow-num">0{index + 1}</span>
                  <span className="hp-flow-title">{title}</span>
                  <code>{tag}</code>
                  {index < c.steps.length - 1 && <ArrowRight className="hp-flow-arrow" size={13} />}
                </li>
              ))}
            </ol>
          </div>

          <div id="security" className="st-gate-block">
            <ChapterHead kicker={c.gateKicker} title={c.gateTitle} intro={c.gateIntro} small />
            <div className="st-gates">
              {c.gates.map(([label, tone, items]) => (
                <div className={`st-gate st-gate-${tone}`} key={tone}>
                  <p><i />{label}</p>
                  <ul>{items.map((item) => <li key={item}>{item}</li>)}</ul>
                </div>
              ))}
            </div>
            <p className="st-note">{c.gateNote}</p>
          </div>
        </section>

        {/* ── Pricing ───────────────────────────────────────────────────── */}
        <section id="pricing" className="hp-section">
          <ChapterHead kicker={c.priceKicker} title={c.priceTitle} intro={c.priceIntro} />
          <div className="st-plans">
            {PLANS.map((plan) => {
              const badge = plan.badge ? (plan.badge[lang] || plan.badge.en) : null;
              return (
                <a className={`st-plan${plan.highlight ? ' st-plan-highlight' : ''}`} key={plan.id} href={`https://laintas.com/pricing${plan.id === 'free' ? '#allowance-comparison' : ''}`} target="_blank" rel="noreferrer">
                  <div className="st-plan-price">
                    <p>{plan.title[lang] || plan.title.en}{badge && <em>{badge}</em>}</p>
                    <strong>{plan.price}<small>{c.perMonth}</small></strong>
                  </div>
                  <div className="st-plan-body">
                    <p>{plan.desc[lang] || plan.desc.en}</p>
                    <ul>{(plan.features[lang] || plan.features.en).map((f) => <li key={f}>{f}</li>)}</ul>
                  </div>
                  <ArrowUpRight className="st-plan-arrow" size={18} />
                </a>
              );
            })}
          </div>
          <a className="st-link" href="https://laintas.com/pricing" target="_blank" rel="noreferrer">{c.pricing}<ArrowUpRight size={14} /></a>
          <div className="st-eco">
            <p className="st-eco-title">{c.ecoTitle}</p>
            {c.eco.map(([name, desc, href]) => (
              <a key={name} href={href} {...(href.startsWith('http') ? { target: '_blank', rel: 'noreferrer' } : {})}>
                <strong>{name}</strong><span>{desc}</span><ArrowUpRight size={16} />
              </a>
            ))}
          </div>
        </section>

        {/* ── Download ──────────────────────────────────────────────────── */}
        <section id="download" className="hp-section">
          <ChapterHead kicker={c.downloadKicker} title={c.downloadTitle} intro={c.downloadIntro} />
          <InstallBar platform={installPlatform} setPlatform={setInstallPlatform} c={c} windowsRelease={windowsRelease} />
          <ol className="st-start">
            {(installPlatform === 'windows' ? c.windowsSteps : c.startSteps).map(([title, detail], index) => (
              <li key={title}><span>0{index + 1}</span><strong>{title}</strong><p>{detail}</p></li>
            ))}
          </ol>
          <p className="st-eco-title">{c.packages}</p>
          <div className="st-packages">
            {PACKAGES.map(({ id, icon: Icon, file, href, size, date, names, details }) => (
              <a href={href || `${RELEASE_BASE}/${file}`} key={id}>
                <Icon size={18} strokeWidth={1.6} />
                <span><strong>{names[lang] || names.en}</strong><small>{details[lang] || details.en}</small></span>
                <code>{id === 'windows' ? windowsRelease.version : release} · {size} · {date || RELEASE_DATE}</code>
                <em>{c.download}<ArrowDownToLine size={15} /></em>
              </a>
            ))}
          </div>
          <a className="st-link" href={`${RELEASE_BASE}/SHA256SUMS.txt`}>{c.checksums}<ArrowUpRight size={14} /></a>
          <p className="st-note">{c.requirements}</p>
        </section>
      </div>

      <SiteFooter />
    </main>
  );
}

/* The one primary action on the page: platform tabs + the install command. */
function InstallBar({ platform, setPlatform, c, windowsRelease }) {
  return (
    <div className="hp-install">
      <div className="st-install">
        <div className="st-install-tabs" role="group" aria-label={c.platformLabel}>
          {['linux', 'mac', 'windows'].map((p) => (
            <button type="button" key={p} className={platform === p ? 'active' : ''} onClick={() => setPlatform(p)} aria-pressed={platform === p}>{c[p]}</button>
          ))}
        </div>
        <div className="st-install-line">
          <code><span aria-hidden="true">{platform === 'windows' ? 'PS>' : '$'}</span>{INSTALL_COMMANDS[platform]}</code>
          <CopyButton key={platform} value={INSTALL_COMMANDS[platform]} labels={c} />
        </div>
      </div>
      {platform === 'windows' && <a className="st-link" href={windowsRelease.installer}><ArrowDownToLine size={16} />{c.windowsAlt} · {windowsRelease.version}</a>}
    </div>
  );
}

/* A real laintas-cli session, recorded on a real machine (the daily series
   footage, terminal layer only — no narration, no subtitles). Plays only
   while on screen; under prefers-reduced-motion it stays on its poster. */
function DemoVideo({ lang, c }) {
  const ref = useRef(null);
  const variant = lang === 'zh' ? 'zh' : 'en';

  useEffect(() => {
    const video = ref.current;
    if (!video) return;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      video.pause();
      video.removeAttribute('autoplay');
      return;
    }
    const io = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) video.play().catch(() => {});
      else video.pause();
    }, { threshold: 0.2 });
    io.observe(video);
    return () => io.disconnect();
  }, [variant]);

  return (
    <figure className="hp-video">
      <div className="hp-video-frame">
        <video ref={ref} key={variant} muted loop playsInline autoPlay preload="metadata" controls
          poster={`/media/laintas-cli-demo-${variant}.jpg`}
          aria-label={c.videoTitle}>
          <source src={`/media/laintas-cli-demo-${variant}.webm`} type="video/webm" />
          <source src={`/media/laintas-cli-demo-${variant}.mp4`} type="video/mp4" />
        </video>
      </div>
      <figcaption><strong>{c.videoTitle}</strong><span>{c.videoNote}</span></figcaption>
    </figure>
  );
}

function Kicker({ children }) {
  return <p className="st-kicker"><i aria-hidden="true" />{children}</p>;
}

function Title({ parts: [plain, emphasised] }) {
  return <>{plain}<br /><em>{emphasised}</em></>;
}

function ChapterHead({ kicker, title, intro, small = false }) {
  return (
    <header className={`st-head${small ? ' st-head-small' : ''}`}>
      <Kicker>{kicker}</Kicker>
      <h2 className="st-display"><Title parts={title} /></h2>
      <p>{intro}</p>
    </header>
  );
}

function CopyButton({ value, labels }) {
  const [copied, setCopied] = useState(false);
  async function copy() { await navigator.clipboard.writeText(value); setCopied(true); window.setTimeout(() => setCopied(false), 1600); }
  return <button type="button" className="st-copy" onClick={copy} aria-label={labels.copy}>{copied ? <Check size={15} /> : <Copy size={15} />}{copied ? labels.copied : labels.copy}</button>;
}
