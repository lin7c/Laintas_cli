import { useCallback, useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import {
  ArrowDownToLine, ArrowRight, Check, CheckCircle2, ChevronRight, CircleDot,
  Code2, Copy, ExternalLink, GitBranch, Layers3, Monitor, Network, Package, Play,
  Radar, RotateCcw, ShieldCheck, TerminalSquare, Waypoints,
} from 'lucide-react';
import { useLanguage } from '@/contexts/LanguageContext';
import SiteFooter from './SiteFooter';

const RELEASE_FALLBACK = 'v1.24.3';
// Release files are served by GitHub Releases, the one place CI publishes to.
// The site's own /releases/ path was the channel until releasing moved into
// CI; nothing repopulates it now, so these links used to 404.
const RELEASE_BASE = 'https://github.com/lin7c/Laintas_cli/releases/latest/download';
const RELEASE_API = 'https://api.github.com/repos/lin7c/Laintas_cli/releases/latest';
const INSTALL_COMMANDS = {
  linux: 'curl -fsSL https://cli.laintas.com/install.sh | bash',
  windows: "irm https://cli.laintas.com/install.ps1 | iex",
};

const RUNTIME_SHOTS = [
  { id: 'commands', src: '/laintas-cli-runtime-commands.png?v=2' },
  { id: 'policy', src: '/laintas-cli-runtime-policy.png?v=2' },
  { id: 'agents', src: '/laintas-cli-runtime-agents.png?v=2' },
];

// Three cards, one per way in: the Linux installer picks the architecture
// itself, Windows is a single installer, and source is source. The other
// release artifacts (both tarballs, the .deb) stay on the release page —
// a card each would make the reader choose between things that are not
// really choices.
const DOWNLOADS = [
  { id: 'linux', names: { zh: 'Linux 版本', en: 'Linux' }, details: { zh: 'x86_64 / arm64 · 自动识别', en: 'x86_64 / arm64 · auto-detected' }, href: 'https://cli.laintas.com/install.sh', icon: Package },
  { id: 'windows', names: { zh: 'Windows 版本', en: 'Windows' }, details: { zh: 'x86_64 · 单文件安装器 · 独立 WSL2', en: 'x86_64 · single installer · private WSL 2' }, file: 'laintas-cli_windows_amd64_setup.exe', icon: Monitor },
  { id: 'source', names: { zh: '源码包', en: 'Source package' }, details: { zh: 'Python 3.10+ · 可审计', en: 'Python 3.10+ · auditable' }, file: 'laintas-cli_source.zip', icon: Code2 },
];

const COPY = {
  zh: {
    kicker: '跨平台终端 · AGENT 运行时 · 运维控制面', titleA: '把复杂工作，', titleB: '变成可运行的流程。',
    intro: 'Laintas CLI 让 Agent 与真实终端、文件系统和运维流程工作在同一个本地运行时中。拆分任务、约束权限、并行执行，并把每一步留在可观察的事件链里。',
    install: '安装 Laintas CLI', seeWorkflow: '查看运行流程', realEyebrow: 'LIVE RUNTIME · NATIVE CAPTURE', realTitle: '真实 Laintas CLI 运行序列',
    realNote: '三帧均来自同一个 v1.18.0 隔离会话的原生终端截图：Slash 菜单、策略选择与 Agents 控制面，没有重绘终端内容。', realTags: ['native terminal', 'policy: enforce', 'CLI v1.18.0'],
    runtimeShots: ['Slash 命令', '策略控制', 'Agents 视图'], openOriginal: '打开当前原图',
    flowKicker: '01 / REQUEST LIFECYCLE', flowTitle: '一条输入，八个真实运行阶段。', flowIntro: '这里展示的是 README 与运行时代码定义的请求生命周期，而不是抽象的营销流程。每个阶段都有对应的本地模块和状态边界。',
    flow: [
      ['分类输入', 'REPL 区分 Slash 指令、PATH 可执行命令与自然语言任务。'],
      ['本地路由', 'PATH 命令直接进入真实 PTY；Slash 指令由内置或扩展注册表解析。'],
      ['组装上下文', '合并模式、项目提示、规则、记忆、计划、角色、工作流阶段和终端状态。'],
      ['调用后端', '当前 Backend Profile 决定请求来源、认证边界、计费标签与模型。'],
      ['分发工具', '结构化调用进入统一注册表：Built-in、Skills、MCP、Extensions 与 Agents。'],
      ['逐层授权', 'Mode、Workflow、Role、Policy、Trust、Approval 与 Hooks 共同决定能否执行。'],
      ['执行与观察', '工具在本地运行，结构化结果被记录，并返回 Agent 进入下一轮判断。'],
      ['持久化与呈现', 'Events、History、Trace、Usage、Tasks、Plans、Memory 与 Workflow State 写入状态层。'],
    ],
    opsKicker: '02 / OPERATIONS', opsTitle: '为生产工作设计的 Agent 控制面。', opsIntro: '更接近运维系统，而不是聊天窗口：知道谁在运行、运行到哪一步、拥有什么权限，以及失败后如何继续。',
    cards: [
      ['终端原生', 'PATH 命令直接进入真实 PTY；交互程序、长任务和命名子终端保持熟悉的 shell 体验。'],
      ['流程编排', 'HWO 负责实时多 Agent 协作，HWG 把依赖编译为可恢复的持久工作图。'],
      ['策略边界', '模式、角色、阶段、全局策略、信任与审批逐层收窄工具权限。'],
      ['状态可恢复', '计划、事件、追踪、任务、记忆与流程状态跨会话保存，失败不会被伪装成完成。'],
      ['远程桥接', 'Helpwo 可把浏览器连接到本地运行时；共享存储与 PPOS 保持为显式可选集成。'],
      ['统一工具面', '内置工具、Skills、MCP 与 Extensions 进入同一注册表和授权管道。'],
    ],
    controlKicker: '03 / CONTROL PLANE', controlTitle: '并行，但不失控。',
    controlIntro: '用角色和阶段拆开责任；每次调用先经过授权，再执行、记录并回流。Prompt 负责意图，Runtime Policy 才是安全边界。',
    agents: ['Planner', 'Operator', 'Verifier'], policy: 'POLICY GATE', states: ['scope: project', 'mode: act', 'approval: enforce', 'trace: on'],
    priceKicker: 'MODEL & USAGE', priceTitle: '先把运行时装进终端，再按需要选择用量。', priceIntro: '下载与源码入口都在这里。模型服务、额度和账户方案请前往 Laintas 定价页查看。', pricing: '查看 Laintas 定价',
    downloadKicker: '04 / DOWNLOAD', downloadTitle: '现在，把它交给真实终端。', downloadIntro: '推荐一行命令安装。也可以按架构下载独立二进制，或使用源码包进行审计与二次开发。',
    quickInstall: '一行安装', linux: 'Linux', windows: 'Windows', copied: '已复制', copy: '复制', download: '下载', requirements: 'Linux 支持 x86_64 / arm64；Windows 支持 x86_64、Windows 10 2004+ / Windows 11，并需要启用 WSL2。', docs: '阅读文档', source: '查看源码', footer: 'Local runtime. Observable work. Controlled execution.',
  },
  en: {
    kicker: 'CROSS-PLATFORM TERMINAL · AGENT RUNTIME · OPS CONTROL PLANE', titleA: 'Turn complex work', titleB: 'into a runnable system.',
    intro: 'Laintas CLI puts agents, the real terminal, the filesystem, and operational workflows inside one local runtime. Decompose work, constrain access, execute in parallel, and keep every step observable.',
    install: 'Install Laintas CLI', seeWorkflow: 'See the workflow', realEyebrow: 'LIVE RUNTIME · NATIVE CAPTURE', realTitle: 'Real Laintas CLI runtime sequence',
    realNote: 'All three frames are native terminal captures from the same isolated v1.18.0 session: slash commands, policy selection, and the Agents control plane. No terminal content was redrawn.', realTags: ['native terminal', 'policy: enforce', 'CLI v1.18.0'],
    runtimeShots: ['Slash commands', 'Policy control', 'Agents view'], openOriginal: 'Open current image',
    flowKicker: '01 / REQUEST LIFECYCLE', flowTitle: 'One input. Eight real runtime stages.', flowIntro: 'This is the request lifecycle defined by the README and runtime code, not an abstract marketing funnel. Every stage maps to a local module and state boundary.',
    flow: [
      ['Classify input', 'The REPL distinguishes slash commands, PATH executables, and natural-language tasks.'],
      ['Route locally', 'PATH commands enter a real PTY; slash commands resolve through built-in or extension registries.'],
      ['Assemble context', 'Combine mode, project prompt, rules, memory, plan, role, workflow phase, and terminal state.'],
      ['Call backend', 'The active Backend Profile determines origin, credential boundary, billing label, and model.'],
      ['Dispatch tools', 'Structured calls enter one registry: Built-ins, Skills, MCP, Extensions, and Agents.'],
      ['Authorize action', 'Mode, Workflow, Role, Policy, Trust, Approval, and Hooks jointly decide execution.'],
      ['Execute & observe', 'The tool runs locally; its structured result is recorded and returned to the next agent iteration.'],
      ['Persist & render', 'Events, History, Trace, Usage, Tasks, Plans, Memory, and Workflow State feed the state layer.'],
    ],
    opsKicker: '02 / OPERATIONS', opsTitle: 'An agent control plane built for production work.', opsIntro: 'Closer to an operations system than a chat box: know what is running, where it is, what it may do, and how it can recover.',
    cards: [
      ['Terminal native', 'PATH commands run in a real PTY; interactive programs, long jobs, and named sub-terminals keep normal shell behavior.'],
      ['Workflow orchestration', 'HWO coordinates live multi-agent work; HWG compiles dependencies into durable, resumable graphs.'],
      ['Policy boundaries', 'Modes, roles, phases, global policy, trust, and approvals progressively narrow tool access.'],
      ['Recoverable state', 'Plans, events, traces, tasks, memory, and workflow state survive restarts without marking failed work complete.'],
      ['Remote bridge', 'Helpwo can connect a browser to the local runtime; shared storage and PPOS remain explicit optional integrations.'],
      ['Unified tool surface', 'Built-ins, Skills, MCP, and Extensions enter one registry and one authorization pipeline.'],
    ],
    controlKicker: '03 / CONTROL PLANE', controlTitle: 'Parallel, without losing control.',
    controlIntro: 'Separate responsibility with roles and phases. Every call is authorized, executed, recorded, and returned. Prompts shape intent; runtime policy defines the boundary.',
    agents: ['Planner', 'Operator', 'Verifier'], policy: 'POLICY GATE', states: ['scope: project', 'mode: act', 'approval: enforce', 'trace: on'],
    priceKicker: 'MODEL & USAGE', priceTitle: 'Install the runtime first. Choose usage as you need it.', priceIntro: 'Downloads and source are available here. Visit Laintas pricing for model service, allowance, and account options.', pricing: 'View Laintas pricing',
    downloadKicker: '04 / DOWNLOAD', downloadTitle: 'Now put it in a real terminal.', downloadIntro: 'Use the one-line installer, download a standalone build for your architecture, or audit and extend the source package.',
    quickInstall: 'One-line install', linux: 'Linux', windows: 'Windows', copied: 'Copied', copy: 'Copy', download: 'Download', requirements: 'Linux supports x86_64 / arm64. Windows supports x86_64 on Windows 10 2004+ or Windows 11 with WSL 2 enabled.', docs: 'Read the docs', source: 'View source', footer: 'Local runtime. Observable work. Controlled execution.',
  },
};

const CARD_ICONS = [TerminalSquare, Waypoints, ShieldCheck, RotateCcw, Radar, Layers3];

export default function DownloadSection() {
  const { lang } = useLanguage();
  const c = COPY[lang] || COPY.en;
  const [activeShot, setActiveShot] = useState(0);
  const [runtimePaused, setRuntimePaused] = useState(false);
  const [release, setRelease] = useState(RELEASE_FALLBACK);
  const [installPlatform, setInstallPlatform] = useState('linux');

  useEffect(() => {
    if (/Windows/i.test(window.navigator.userAgent)) setInstallPlatform('windows');
    // The GitHub API rather than the release's own manifest.json: asset
    // downloads redirect to a host that does not answer cross-origin, and the
    // tag is all this needs. A failure leaves the fallback in place, which is
    // the version this page was built for.
    fetch(RELEASE_API)
      .then((response) => response.ok ? response.json() : Promise.reject(new Error('release lookup failed')))
      .then((data) => { if (data.tag_name) setRelease(data.tag_name); })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (runtimePaused || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return undefined;
    const timer = window.setInterval(() => setActiveShot((current) => (current + 1) % RUNTIME_SHOTS.length), 4600);
    return () => window.clearInterval(timer);
  }, [runtimePaused]);

  const currentShot = RUNTIME_SHOTS[activeShot];
  return (
    <main className="product-page">
      <div className="ops-grid" aria-hidden="true" />
      <section className="hero-shell">
        <motion.div className="hero-copy" initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.55 }}>
          <p className="section-kicker"><CircleDot size={13} /> {c.kicker}</p>
          <h1><span>{c.titleA}</span><br />{c.titleB}</h1>
          <p className="hero-intro">{c.intro}</p>
          <div className="hero-actions">
            <a className="button button-primary" href="#download"><ArrowDownToLine size={17} />{c.install}</a>
            <a className="button button-ghost" href="#workflow">{c.seeWorkflow}<ArrowRight size={16} /></a>
          </div>
          <div className="hero-proof-line"><span><CheckCircle2 size={15} /> Linux / Windows</span><span><Network size={15} /> HWO / HWG</span><span><ShieldCheck size={15} /> Policy enforced</span></div>
        </motion.div>
        <motion.figure className="terminal-proof" initial={{ opacity: 0, scale: 0.98 }} animate={{ opacity: 1, scale: 1 }} transition={{ duration: 0.65, delay: 0.12 }}>
          <div className="proof-label"><span className="live-dot" />{c.realEyebrow}</div>
          <div className="runtime-carousel" onMouseEnter={() => setRuntimePaused(true)} onMouseLeave={() => setRuntimePaused(false)}>
            <a className="runtime-image-link" href={currentShot.src} target="_blank" rel="noreferrer" aria-label={`${c.realTitle} — ${c.openOriginal}`}>
              <motion.img key={currentShot.id} src={currentShot.src} alt={`${c.realTitle}：${c.runtimeShots[activeShot]}`} width="3040" height="1900" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.42 }} />
            </a>
            <div className="runtime-shot-nav" aria-label={lang === 'zh' ? '切换真实运行截图' : 'Select runtime capture'}>
              <span><b>0{activeShot + 1}</b> / 0{RUNTIME_SHOTS.length} · {c.runtimeShots[activeShot]}</span>
              <div>{RUNTIME_SHOTS.map((shot, index) => <button type="button" key={shot.id} className={index === activeShot ? 'active' : ''} onClick={() => setActiveShot(index)} aria-label={c.runtimeShots[index]} aria-current={index === activeShot ? 'true' : undefined} />)}</div>
            </div>
          </div>
          <figcaption><div><strong>{c.realTitle}</strong><span>{c.realNote}</span></div><div className="proof-tags">{c.realTags.map((tag) => <span key={tag}>{tag}</span>)}</div></figcaption>
        </motion.figure>
      </section>

      <SectionIntro id="workflow" kicker={c.flowKicker} title={c.flowTitle} intro={c.flowIntro} />
      <section className="workflow-rail page-shell" aria-label={c.flowTitle}>
        {c.flow.map(([title, detail], index) => <article className="workflow-step" key={title}><div className="step-index">0{index + 1}<span /></div><h3>{title}</h3><p>{detail}</p></article>)}
      </section>

      <SectionIntro id="operations" kicker={c.opsKicker} title={c.opsTitle} intro={c.opsIntro} />
      <section className="ops-bento page-shell">
        {c.cards.map(([title, detail], index) => { const Icon = CARD_ICONS[index]; return (
          <article className={`ops-card ops-card-${index + 1}`} key={title}><div className="ops-card-top"><Icon size={20} /><span>0{index + 1}</span></div><h3>{title}</h3><p>{detail}</p>
            {index === 1 && <MiniGraph />}{index === 2 && <div className="policy-list"><span>DENY</span><span>REVIEW</span><span>ALLOW</span></div>}
          </article> ); })}
      </section>

      <section id="security" className="control-section page-shell">
        <div className="control-copy"><p className="section-kicker">{c.controlKicker}</p><h2>{c.controlTitle}</h2><p>{c.controlIntro}</p><div className="state-list">{c.states.map((state) => <code key={state}>{state}</code>)}</div></div>
        <div className="control-diagram" aria-label="Agent authorization flow">
          <div className="agent-stack">{c.agents.map((agent, index) => <div key={agent}><span>0{index + 1}</span>{agent}<i /></div>)}</div><div className="flow-arrow"><ChevronRight /></div>
          <div className="policy-gate"><ShieldCheck /><span>{c.policy}</span><small>role · phase · trust · approval</small></div><div className="flow-arrow"><ChevronRight /></div>
          <div className="runtime-node"><Play /><span>LOCAL<br />RUNTIME</span><small>execute · observe · persist</small></div>
        </div>
      </section>

      <section className="pricing-band page-shell"><div><p className="section-kicker">{c.priceKicker}</p><h2>{c.priceTitle}</h2><p>{c.priceIntro}</p></div><a className="button button-light" href="https://laintas.com/pricing" target="_blank" rel="noreferrer">{c.pricing}<ExternalLink size={16} /></a></section>

      <section id="download" className="download-section page-shell">
        <div className="download-heading"><div><p className="section-kicker">{c.downloadKicker}</p><h2>{c.downloadTitle}</h2></div><p>{c.downloadIntro}</p></div>
        <div className="install-platforms" aria-label={lang === 'zh' ? '选择安装平台' : 'Select install platform'}>
          {['linux', 'windows'].map((platform) => <button type="button" key={platform} className={installPlatform === platform ? 'active' : ''} onClick={() => setInstallPlatform(platform)} aria-pressed={installPlatform === platform}>{c[platform]}</button>)}
        </div>
        <div className="install-block"><div><span>{c.quickInstall} · {c[installPlatform]}</span><code>{INSTALL_COMMANDS[installPlatform]}</code></div><CopyButton value={INSTALL_COMMANDS[installPlatform]} labels={c} /></div>
        <div className="download-grid">{DOWNLOADS.map(({ id, names, details, file, href, icon: Icon }) => <a className="download-card" href={href || `${RELEASE_BASE}/${file}`} key={id}><div><Icon size={20} /><span>{release}</span></div><h3>{names[lang] || names.en}</h3><p>{details[lang] || details.en}</p><strong>{c.download}<ArrowDownToLine size={16} /></strong></a>)}</div>
        <p className="requirements"><CheckCircle2 size={15} />{c.requirements}</p>
      </section>

      <SiteFooter />
    </main>
  );
}

function SectionIntro({ id, kicker, title, intro }) { return <section id={id} className="section-intro page-shell"><p className="section-kicker">{kicker}</p><div><h2>{title}</h2><p>{intro}</p></div></section>; }

function MiniGraph() { return <div className="mini-graph" aria-hidden="true"><span><GitBranch size={13} /> plan</span><i /><span><TerminalSquare size={13} /> execute</span><i /><span><CheckCircle2 size={13} /> verify</span></div>; }

function CopyButton({ value, labels }) {
  const [copied, setCopied] = useState(false);
  const copy = useCallback(async () => { await navigator.clipboard.writeText(value); setCopied(true); window.setTimeout(() => setCopied(false), 1600); }, [value]);
  return <button type="button" className="copy-button" onClick={copy} aria-label={labels.copy}>{copied ? <Check size={17} /> : <Copy size={17} />}{copied ? labels.copied : labels.copy}</button>;
}

