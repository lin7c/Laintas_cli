import { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import {
  ArrowDownToLine, ArrowRight, Check, CheckCircle2, ChevronRight, CircleDot,
  Code2, Copy, ExternalLink, GitBranch, Monitor, Network, Package, Play,
  Radar, RotateCcw, ShieldCheck, Layers3, TerminalSquare, Waypoints, Zap,
} from 'lucide-react';
import { useLanguage } from '@/contexts/LanguageContext';
import SiteFooter from './SiteFooter';
import TerminalDemo from './TerminalDemo';

const RELEASE_FALLBACK = 'v1.25.1';
// Release files are served by GitHub Releases, the one place CI publishes to.
const RELEASE_BASE = 'https://github.com/lin7c/Laintas_cli/releases/latest/download';
const RELEASE_API = 'https://api.github.com/repos/lin7c/Laintas_cli/releases/latest';
const INSTALL_COMMANDS = {
  linux: 'curl -fsSL https://cli.laintas.com/install.sh | bash',
  windows: "irm https://cli.laintas.com/install.ps1 | iex",
};

const DOWNLOADS = [
  { id: 'linux', names: { zh: 'Linux 版本', en: 'Linux' }, details: { zh: 'x86_64 / arm64 · 自动识别', en: 'x86_64 / arm64 · auto-detected' }, href: 'https://cli.laintas.com/install.sh', icon: Package },
  { id: 'windows', names: { zh: 'Windows 版本', en: 'Windows' }, details: { zh: 'x86_64 · 单文件安装器 · 独立 WSL2', en: 'x86_64 · single installer · private WSL 2' }, file: 'laintas-cli_windows_amd64_setup.exe', icon: Monitor },
  { id: 'source', names: { zh: '源码包', en: 'Source package' }, details: { zh: 'Python 3.10+ · 可审计', en: 'Python 3.10+ · auditable' }, file: 'laintas-cli_source.zip', icon: Code2 },
];

// Pricing shown on this page mirrors laintas.com/pricing (the authoritative
// source). Values are kept in sync with the main site's plan cards; the full
// tier table and allowance comparison live on the pricing page itself.
const PLANS = [
  { id: 'free', price: '$0', period: '/ mo', title: { zh: '按量使用', en: 'Pay as you go' }, desc: { zh: '无需订阅。用多少付多少，单价公开透明。', en: 'No subscription. Only pay for what you use.' }, features: { zh: ['多数产品可先试用', '用多少付多少', '模型分档，单价透明'], en: ['Try most products first', 'Only pay for usage', 'Transparent model tiers'] }, badge: null, highlight: false },
  { id: 'pro', price: '$19.9', period: '/ mo', title: { zh: 'Pro', en: 'Pro' }, desc: { zh: '每月 7,000 次调用，全产品共用一个额度池。', en: '7,000 calls a month, one pooled allowance.' }, features: { zh: ['7,000 次调用 / 月', '所有产品共用同一额度', '检索/嵌入/重排各计 1 次', '超出后可继续按量'], en: ['7,000 calls / month', 'One pool across every product', 'Retrieval/embed/rerank count as 1', 'Continue pay-as-you-go after'] }, badge: { zh: '最受欢迎', en: 'Most popular' }, highlight: true },
  { id: 'gen', price: '$49.9', period: '/ mo', title: { zh: 'Gen', en: 'Gen' }, desc: { zh: '每月 15,000 次调用，5 小时突发上限 1,500 次。', en: '15,000 calls a month, 1,500 in a 5-hour burst.' }, features: { zh: ['15,000 次调用 / 月', '突发速率提高', '全产品共用额度', '面向全职 agent 工作流'], en: ['15,000 calls / month', 'Higher burst rate', 'One pooled allowance', 'For full-time agent workflows'] }, badge: { zh: '全产品', en: 'All products' }, highlight: false },
];

const COPY = {
  zh: {
    kicker: '自主 AGENT · 真实终端 · 运维控制面',
    titleA: 'AI agent，', titleB: '住进你的真实终端。',
    intro: 'Laintas CLI 把自主 AI agent 直接放进你的 shell。普通命令仍在真实 PTY 中原生运行；自然语言任务进入可观察、可中断、可委派的 agent 循环 —— 检查工作区、调用工具、拆分任务、并行执行，并把每一步留在可追踪的状态里。',
    introHighlight: '不是聊天窗口。是离文件和终端最近的那个 agent。',
    install: '安装 Laintas CLI', seeWorkflow: '查看运行流程',
    proofLine: ['Linux / Windows', 'HWO / HWG 编排', '策略强制'],
    realEyebrow: 'LIVE SESSION · RENDERED', realTitle: '一段真实的 agent 会话',
    realNote: '用 HTML/CSS 实时渲染的终端会话：矢量清晰、可缩放，直接呈现 agent 如何读文件、改代码、跑测试并回到提示符。',
    flowKicker: '01 / 请求生命周期', flowTitle: '一条输入，八个真实运行阶段。', flowIntro: '这是 README 与运行时代码定义的请求生命周期，不是抽象营销漏斗。每个阶段都映射到本地模块与状态边界。',
    flow: [
      ['分类输入', 'REPL 区分 Slash 指令、PATH 可执行命令与自然语言任务。'],
      ['本地路由', 'PATH 命令进入真实 PTY；Slash 指令由内置或扩展注册表解析。'],
      ['组装上下文', '合并模式、项目提示、规则、记忆、计划、角色、工作流阶段和终端状态。'],
      ['调用后端', '当前 Backend Profile 决定来源、认证边界、计费标签与模型。'],
      ['分发工具', '结构化调用进入统一注册表：Built-in、Skills、MCP、Extensions 与 Agents。'],
      ['逐层授权', '模式、工作流、角色、策略、信任、审批与 Hooks 共同决定执行。'],
      ['执行与观察', '工具在本地运行，结构化结果被记录并回流到下一轮判断。'],
      ['持久化与呈现', '事件、历史、追踪、用量、任务、计划、记忆与状态写入状态层。'],
    ],
    opsKicker: '02 / 运维能力', opsTitle: '为生产工作设计的 agent 控制面。', opsIntro: '更接近运维系统，而不是聊天窗口：知道谁在运行、进行到哪一步、拥有什么权限，以及失败后如何继续。',
    cards: [
      { title: '终端原生', desc: 'PATH 命令直接进入真实 PTY；交互程序、长任务和命名子终端保持熟悉的 shell 体验。', icon: TerminalSquare, extra: 'terminal' },
      { title: '流程编排', desc: 'HWO 协调实时多 agent 协作；HWG 把依赖编译为可恢复的持久工作图。', icon: Waypoints, extra: 'graph' },
      { title: '策略边界', desc: '模式、角色、阶段、全局策略、信任与审批逐层收窄工具权限。', icon: ShieldCheck, extra: 'policy' },
      { title: '状态可恢复', desc: '计划、事件、追踪、任务、记忆与流程状态跨会话保存，失败不会被伪装成完成。', icon: RotateCcw, extra: null },
      { title: '可观测', desc: '事件链、资源浏览器、detail trace —— 每一步都留痕、可回放。', icon: Radar, extra: null },
      { title: '统一工具面', desc: '内置工具、Skills、MCP 与 Extensions 进入同一注册表和授权管道。', icon: Layers3, extra: null },
    ],
    controlKicker: '03 / 控制面', controlTitle: '并行，但不失控。', controlIntro: '用角色和阶段拆开责任；每次调用先经授权，再执行、记录并回流。Prompt 负责意图，Runtime Policy 才是安全边界。',
    agents: ['Planner', 'Operator', 'Verifier'], policy: 'POLICY GATE', states: ['scope: project', 'mode: act', 'approval: enforce', 'trace: on'],
    priceKicker: 'MODEL & USAGE', priceTitle: '先把运行时装进终端，再按需要选择用量。', priceIntro: 'Laintas CLI 本身免费开源。模型推理按量或订阅计费，Pro 与 Gen 覆盖全部产品、共用一个额度池。', pricing: '查看完整定价方案',
    downloadKicker: '04 / 下载', downloadTitle: '现在，把它交给真实终端。', downloadIntro: '推荐一行命令安装。也可以按架构下载独立二进制，或使用源码包进行审计与二次开发。',
    quickInstall: '一行安装', linux: 'Linux', windows: 'Windows', copied: '已复制', copy: '复制', download: '下载', requirements: 'Linux 支持 x86_64 / arm64；Windows 支持 x86_64、Windows 10 2004+ / Windows 11，并需要启用 WSL2。', docs: '阅读文档', source: '查看源码', footer: 'Local runtime. Observable work. Controlled execution.',
  },
  en: {
    kicker: 'AUTONOMOUS AGENT · REAL TERMINAL · OPS CONTROL PLANE',
    titleA: 'An AI agent', titleB: 'that lives in your terminal.',
    intro: 'Laintas CLI puts an autonomous AI agent directly into your shell. Commands still run natively in a real PTY; natural-language tasks enter an observable, interruptible, delegating agent loop — inspecting the workspace, calling tools, splitting work, and keeping every step in a recoverable state.',
    introHighlight: 'Not a chat window. The agent closest to your files and terminal.',
    install: 'Install Laintas CLI', seeWorkflow: 'See the workflow',
    proofLine: ['Linux / Windows', 'HWO / HWG', 'Policy enforced'],
    realEyebrow: 'LIVE SESSION · RENDERED', realTitle: 'A real agent session',
    realNote: 'Rendered live in HTML/CSS — vector-crisp and zoomable. Watch the agent read a file, edit code, run tests, and return to the prompt.',
    flowKicker: '01 / REQUEST LIFECYCLE', flowTitle: 'One input. Eight real runtime stages.', flowIntro: 'This is the request lifecycle defined by the README and runtime code, not an abstract funnel. Every stage maps to a local module and state boundary.',
    flow: [
      ['Classify input', 'The REPL distinguishes slash commands, PATH executables, and natural-language tasks.'],
      ['Route locally', 'PATH commands enter a real PTY; slash commands resolve through built-in or extension registries.'],
      ['Assemble context', 'Combine mode, project prompt, rules, memory, plan, role, workflow phase, and terminal state.'],
      ['Call backend', 'The active Backend Profile determines origin, credential boundary, billing label, and model.'],
      ['Dispatch tools', 'Structured calls enter one registry: Built-ins, Skills, MCP, Extensions, and Agents.'],
      ['Authorize action', 'Mode, Workflow, Role, Policy, Trust, Approval, and Hooks jointly decide execution.'],
      ['Execute & observe', 'The tool runs locally; its structured result is recorded and returned to the next iteration.'],
      ['Persist & render', 'Events, History, Trace, Usage, Tasks, Plans, Memory, and Workflow State feed the state layer.'],
    ],
    opsKicker: '02 / OPERATIONS', opsTitle: 'An agent control plane built for production work.', opsIntro: 'Closer to an operations system than a chat box: know what is running, where it is, what it may do, and how it recovers.',
    cards: [
      { title: 'Terminal native', desc: 'PATH commands run in a real PTY; interactive programs, long jobs, and named sub-terminals keep normal shell behavior.', icon: TerminalSquare, extra: 'terminal' },
      { title: 'Orchestration', desc: 'HWO coordinates live multi-agent work; HWG compiles dependencies into durable, resumable graphs.', icon: Waypoints, extra: 'graph' },
      { title: 'Policy boundaries', desc: 'Modes, roles, phases, global policy, trust, and approvals progressively narrow tool access.', icon: ShieldCheck, extra: 'policy' },
      { title: 'Recoverable state', desc: 'Plans, events, traces, tasks, memory, and workflow state survive restarts without marking failed work complete.', icon: RotateCcw, extra: null },
      { title: 'Observable', desc: 'Event chains, resource browsers, detail trace — every step is recorded and replayable.', icon: Radar, extra: null },
      { title: 'Unified tool surface', desc: 'Built-ins, Skills, MCP, and Extensions enter one registry and one authorization pipeline.', icon: Layers3, extra: null },
    ],
    controlKicker: '03 / CONTROL PLANE', controlTitle: 'Parallel, without losing control.', controlIntro: 'Separate responsibility with roles and phases. Every call is authorized, executed, recorded, and returned. Prompts shape intent; runtime policy defines the boundary.',
    agents: ['Planner', 'Operator', 'Verifier'], policy: 'POLICY GATE', states: ['scope: project', 'mode: act', 'approval: enforce', 'trace: on'],
    priceKicker: 'MODEL & USAGE', priceTitle: 'Install the runtime first. Choose usage as you need it.', priceIntro: 'Laintas CLI is free and open source. Model inference is metered or subscribed — Pro and Gen cover every product from one pooled allowance.', pricing: 'View full pricing',
    downloadKicker: '04 / DOWNLOAD', downloadTitle: 'Now put it in a real terminal.', downloadIntro: 'Use the one-line installer, download a standalone build for your architecture, or audit and extend the source package.',
    quickInstall: 'One-line install', linux: 'Linux', windows: 'Windows', copied: 'Copied', copy: 'Copy', download: 'Download', requirements: 'Linux supports x86_64 / arm64. Windows supports x86_64 on Windows 10 2004+ or Windows 11 with WSL 2 enabled.', docs: 'Read the docs', source: 'View source', footer: 'Local runtime. Observable work. Controlled execution.',
  },
};

export default function DownloadSection() {
  const { lang } = useLanguage();
  const c = COPY[lang] || COPY.en;
  const [release, setRelease] = useState(RELEASE_FALLBACK);
  const [installPlatform, setInstallPlatform] = useState('linux');

  useEffect(() => {
    if (/Windows/i.test(window.navigator.userAgent)) setInstallPlatform('windows');
    fetch(RELEASE_API)
      .then((response) => response.ok ? response.json() : Promise.reject(new Error('release lookup failed')))
      .then((data) => { if (data.tag_name) setRelease(data.tag_name); })
      .catch(() => {});
  }, []);

  return (
    <main className="product-page">
      <div className="ops-grid" aria-hidden="true" />
      <div className="hero-glow" aria-hidden="true" />

      {/* ── Hero: copy left, live terminal right ─────────────── */}
      <section className="hero-shell">
        <div className="hero-grid">
          <motion.div className="hero-copy" initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.55 }}>
            <p className="section-kicker"><CircleDot size={13} /> {c.kicker}</p>
            <h1><span>{c.titleA}</span><br />{c.titleB}</h1>
            <p className="hero-intro">{c.intro}</p>
            <p className="hero-highlight"><Zap size={15} />{c.introHighlight}</p>
            <div className="hero-actions">
              <a className="button button-primary" href="#download"><ArrowDownToLine size={17} />{c.install}</a>
              <a className="button button-ghost" href="#workflow">{c.seeWorkflow}<ArrowRight size={16} /></a>
            </div>
            <div className="hero-proof-line">
              {c.proofLine.map((label, i) => {
                const icons = [Monitor, Network, ShieldCheck];
                const Icon = icons[i];
                return <span key={label}><Icon size={15} />{label}</span>;
              })}
            </div>
          </motion.div>

          <motion.div className="hero-terminal" initial={{ opacity: 0, scale: 0.98 }} animate={{ opacity: 1, scale: 1 }} transition={{ duration: 0.65, delay: 0.12 }}>
            <div className="proof-label"><span className="live-dot" />{c.realEyebrow}</div>
            <TerminalDemo />
            <figcaption><div><strong>{c.realTitle}</strong><span>{c.realNote}</span></div></figcaption>
          </motion.div>
        </div>
      </section>

      {/* ── 01 / Request lifecycle ───────────────────────────── */}
      <SectionIntro id="workflow" kicker={c.flowKicker} title={c.flowTitle} intro={c.flowIntro} />
      <section className="workflow-rail page-shell" aria-label={c.flowTitle}>
        {c.flow.map(([title, detail], index) => (
          <article className="workflow-step" key={title}>
            <div className="step-index">0{index + 1}<span /></div>
            <h3>{title}</h3><p>{detail}</p>
          </article>
        ))}
      </section>

      {/* ── 02 / Operations ──────────────────────────────────── */}
      <SectionIntro id="operations" kicker={c.opsKicker} title={c.opsTitle} intro={c.opsIntro} />
      <section className="ops-bento page-shell">
        {c.cards.map(({ title, desc, icon: Icon, extra }, index) => (
          <article className={`ops-card ops-card-${index + 1}`} key={title}>
            <div className="ops-card-top"><Icon size={20} /><span>0{index + 1}</span></div>
            <h3>{title}</h3><p>{desc}</p>
            {extra === 'graph' && <MiniGraph />}
            {extra === 'policy' && <div className="policy-list"><span>DENY</span><span>REVIEW</span><span>ALLOW</span></div>}
            {extra === 'terminal' && <div className="terminal-chips"><span>$ shell</span><span>PTY</span><span>sub-term</span></div>}
          </article>
        ))}
      </section>

      {/* ── 03 / Control plane ───────────────────────────────── */}
      <section id="security" className="control-section page-shell">
        <div className="control-copy">
          <p className="section-kicker">{c.controlKicker}</p>
          <h2>{c.controlTitle}</h2>
          <p>{c.controlIntro}</p>
          <div className="state-list">{c.states.map((state) => <code key={state}>{state}</code>)}</div>
        </div>
        <div className="control-diagram" aria-label="Agent authorization flow">
          <div className="agent-stack">{c.agents.map((agent, index) => <div key={agent}><span>0{index + 1}</span>{agent}<i /></div>)}</div>
          <div className="flow-arrow"><ChevronRight /></div>
          <div className="policy-gate"><ShieldCheck /><span>{c.policy}</span><small>role · phase · trust · approval</small></div>
          <div className="flow-arrow"><ChevronRight /></div>
          <div className="runtime-node"><Play /><span>LOCAL<br />RUNTIME</span><small>execute · observe · persist</small></div>
        </div>
      </section>

      {/* ── MODEL & USAGE · Pricing ──────────────────────────── */}
      <section id="pricing" className="pricing-section page-shell">
        <div className="pricing-heading">
          <div>
            <p className="section-kicker">{c.priceKicker}</p>
            <h2>{c.priceTitle}</h2>
            <p>{c.priceIntro}</p>
          </div>
          <a className="button button-light" href="https://laintas.com/pricing" target="_blank" rel="noreferrer">{c.pricing}<ExternalLink size={16} /></a>
        </div>
        <div className="pricing-grid">
          {PLANS.map((plan) => {
            const title = plan.title[lang] || plan.title.en;
            const desc = plan.desc[lang] || plan.desc.en;
            const features = plan.features[lang] || plan.features.en;
            const badge = plan.badge ? (plan.badge[lang] || plan.badge.en) : null;
            return (
              <article className={`price-card${plan.highlight ? ' price-card-highlight' : ''}`} key={plan.id}>
                {badge && <span className="price-badge">{badge}</span>}
                <p className="price-name">{title}</p>
                <div className="price-value"><span className="price-amount">{plan.price}</span><span className="price-period">{plan.period}</span></div>
                <p className="price-desc">{desc}</p>
                <ul className="price-features">{features.map((f) => <li key={f}><Check size={13} />{f}</li>)}</ul>
                <a className="button price-cta" href={`https://laintas.com/pricing${plan.id === 'free' ? '#allowance-comparison' : ''}`} target="_blank" rel="noreferrer">{plan.highlight ? 'Subscribe' : lang === 'zh' ? '了解详情' : 'Learn more'}<ArrowRight size={15} /></a>
              </article>
            );
          })}
        </div>
      </section>

      {/* ── 04 / Download ────────────────────────────────────── */}
      <section id="download" className="download-section page-shell">
        <div className="download-heading">
          <div><p className="section-kicker">{c.downloadKicker}</p><h2>{c.downloadTitle}</h2></div>
          <p>{c.downloadIntro}</p>
        </div>
        <div className="install-platforms" aria-label={lang === 'zh' ? '选择安装平台' : 'Select install platform'}>
          {['linux', 'windows'].map((platform) => (
            <button type="button" key={platform} className={installPlatform === platform ? 'active' : ''} onClick={() => setInstallPlatform(platform)} aria-pressed={installPlatform === platform}>{c[platform]}</button>
          ))}
        </div>
        <div className="install-block">
          <div><span>{c.quickInstall} · {c[installPlatform]}</span><code>{INSTALL_COMMANDS[installPlatform]}</code></div>
          <CopyButton value={INSTALL_COMMANDS[installPlatform]} labels={c} />
        </div>
        <div className="download-grid">
          {DOWNLOADS.map(({ id, names, details, file, href, icon: Icon }) => (
            <a className="download-card" href={href || `${RELEASE_BASE}/${file}`} key={id}>
              <div><Icon size={20} /><span>{release}</span></div>
              <h3>{names[lang] || names.en}</h3>
              <p>{details[lang] || details.en}</p>
              <strong>{c.download}<ArrowDownToLine size={16} /></strong>
            </a>
          ))}
        </div>
        <p className="requirements"><CheckCircle2 size={15} />{c.requirements}</p>
      </section>

      <SiteFooter />
    </main>
  );
}

function SectionIntro({ id, kicker, title, intro }) {
  return <section id={id} className="section-intro page-shell"><p className="section-kicker">{kicker}</p><div><h2>{title}</h2><p>{intro}</p></div></section>;
}

function MiniGraph() {
  return <div className="mini-graph" aria-hidden="true"><span><GitBranch size={13} /> plan</span><i /><span><TerminalSquare size={13} /> execute</span><i /><span><CheckCircle2 size={13} /> verify</span></div>;
}

function CopyButton({ value, labels }) {
  const [copied, setCopied] = useState(false);
  async function copy() { await navigator.clipboard.writeText(value); setCopied(true); window.setTimeout(() => setCopied(false), 1600); }
  return <button type="button" className="copy-button" onClick={copy} aria-label={labels.copy}>{copied ? <Check size={17} /> : <Copy size={17} />}{copied ? labels.copied : labels.copy}</button>;
}
