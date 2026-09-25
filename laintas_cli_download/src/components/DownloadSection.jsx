import { useEffect, useRef, useState } from 'react';
import {
  ArrowDownToLine, ArrowRight, ArrowUpRight, Check, Code2, Copy, Layers3,
  Monitor, Package, ShieldCheck, TerminalSquare, Waypoints,
} from 'lucide-react';
import { useLanguage } from '@/contexts/LanguageContext';
import SiteFooter from './SiteFooter';
import TerminalReplay from './TerminalReplay';

const RELEASE_FALLBACK = 'v1.32.4';
// Release files are served by GitHub Releases, the one place CI publishes to.
const RELEASE_BASE = 'https://github.com/lin7c/Laintas_cli/releases/latest/download';
const RELEASE_API = 'https://api.github.com/repos/lin7c/Laintas_cli/releases/latest';
const INSTALL_COMMANDS = {
  linux: 'curl -fsSL https://cli.laintas.com/install.sh | bash',
  windows: "irm https://cli.laintas.com/install.ps1 | iex",
};

const DOWNLOADS = [
  { id: 'linux', names: { zh: 'Linux', en: 'Linux' }, details: { zh: 'x86_64 / arm64 · 安装脚本自动识别架构', en: 'x86_64 / arm64 · installer picks the architecture' }, href: 'https://cli.laintas.com/install.sh', icon: Package },
  { id: 'windows', names: { zh: 'Windows', en: 'Windows' }, details: { zh: 'x86_64 · 单文件安装器 · 自带独立 WSL 2', en: 'x86_64 · single installer · private WSL 2' }, file: 'laintas-cli_windows_amd64_setup.exe', icon: Monitor },
  { id: 'source', names: { zh: '源码包', en: 'Source package' }, details: { zh: 'Python 3.10+ · 审计与二次开发', en: 'Python 3.10+ · audit and extend' }, file: 'laintas-cli_source.zip', icon: Code2 },
];

// Capability icons, in the order of COPY[lang].caps.
const CAP_ICONS = [TerminalSquare, Waypoints, ShieldCheck, Layers3];

// Pricing shown on this page mirrors laintas.com/pricing (the authoritative
// source). Values are kept in sync with the main site's plan cards; the full
// tier table and allowance comparison live on the pricing page itself.
const PLANS = [
  { id: 'free', price: '$0', period: '/ mo', title: { zh: '按量使用', en: 'Pay as you go' }, desc: { zh: '无需订阅。用多少付多少，单价公开透明。', en: 'No subscription. Only pay for what you use.' }, features: { zh: ['新用户 100 次调用试用', '用多少付多少', '模型分档，单价透明'], en: ['100-call trial for new accounts', 'Only pay for usage', 'Transparent model tiers'] }, badge: null, highlight: false },
  { id: 'pro', price: '$19.9', period: '/ mo', title: { zh: 'Pro', en: 'Pro' }, desc: { zh: '每月 7,000 次调用，全产品共用一个额度池。', en: '7,000 calls a month, one pooled allowance.' }, features: { zh: ['7,000 次调用 / 月', '所有产品共用同一额度', '检索/嵌入/重排各计 1 次', '超出后可继续按量'], en: ['7,000 calls / month', 'One pool across every product', 'Retrieval/embed/rerank count as 1', 'Continue pay-as-you-go after'] }, badge: { zh: '最受欢迎', en: 'Most popular' }, highlight: true },
  { id: 'gen', price: '$49.9', period: '/ mo', title: { zh: 'Gen', en: 'Gen' }, desc: { zh: '每月 15,000 次调用，5 小时突发上限 1,500 次。', en: '15,000 calls a month, 1,500 in a 5-hour burst.' }, features: { zh: ['15,000 次调用 / 月', '突发速率提高', '全产品共用额度', '面向全职 agent 工作流'], en: ['15,000 calls / month', 'Higher burst rate', 'One pooled allowance', 'For full-time agent workflows'] }, badge: { zh: '全产品', en: 'All products' }, highlight: false },
];

// The presenter. One photograph per chapter, shot from one fixed camera, so
// scrolling reads as one person moving rather than a slideshow. Scenes 1–2 are
// a closer framing than 3–5; the 2→3 handoff is played as a dolly-out (see
// SCENE_MOTION) so the change of framing reads as the camera pulling back.
//   focus  transform-origin: roughly where his face is, so zooms stay on him.
//   enter  extra scale while fading in; exit: scale lost while fading out.
//   dip    how far the stage darkens at the middle of the fade into this
//          scene. Two poses cross-dissolved show a double exposure; a short
//          dip hides it, the way an editor would cut it.
const SCENES = [
  { src: 'scene-1', focus: '38% 22%', enter: 0, exit: 0, dip: 0 },
  { src: 'scene-2', focus: '34% 24%', enter: 0.04, exit: 0.12, dip: 0.3 },
  { src: 'scene-3', focus: '25% 18%', enter: 0.3, exit: 0, dip: 0.6 },
  { src: 'scene-4', focus: '24% 18%', enter: 0.03, exit: 0, dip: 0.3 },
  { src: 'scene-5', focus: '24% 18%', enter: 0.03, exit: 0, dip: 0.3 },
];

const clamp01 = (value) => Math.min(1, Math.max(0, value));

// The zoom runs over the whole handoff; the dissolve only over its middle.
const dissolve = (t) => { const x = clamp01((t - 0.35) / 0.3); return x * x * (3 - 2 * x); };

// Titles are [plain, emphasised]: the second half is set in gold italic, the
// same way laintas.com sets its display lines.
const COPY = {
  zh: {
    chapters: ['开场', '为什么', '工作方式', '价格', '下载'],
    say: [
      '我每天都在终端里工作，所以我们把 agent 放进了终端。',
      '先说为什么：它不是又一个聊天窗口。',
      '一条指令进来，每一步都看得见、拦得住。',
      '运行时免费，模型用量按你的节奏来。',
      '就这些。一行命令，装进你的终端。',
    ],
    kicker: 'LAINTAS CLI · 终端里的 AI agent',
    title: ['命令照常敲，', '其余交给 agent。'],
    lead: 'Laintas CLI 是装进 shell 的自主 agent。ls、git、vim 这类命令照旧直接在真实 PTY 里运行，不经过模型；用一句话描述的任务，交给 agent 去读代码、跑命令、改文件 —— 每一步都摆在你眼前，关键操作等你点头。',
    install: '安装 Laintas CLI', seeHow: '看它怎么工作',
    facts: [['免费', '运行时免费使用，可下载源码审计'], ['Linux · Windows', 'x86_64 / arm64，Windows 10 2004+ / 11'], ['一个账号', '与 Helpwo、插件市场共用 Laintas 账号']],
    realEyebrow: '真实会话录像 · v1.29.4', realTitle: '一段真实会话，逐帧回放',
    realNote: 'agent 读项目、跑 pytest、找到失败用例的根因，把补丁摆出来等你批准，落盘后再跑一遍测试。只剪掉了等待模型的空白，画面一个字没改。',

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

    howKicker: '02 · 一条指令的旅程', howTitle: ['看一条指令', '怎么走完全程。'],
    howIntro: '以“修一下失败的测试”为例。下面是运行时真实经过的路径，每一步都对应本地模块和可追溯的状态。',
    steps: [
      ['分流', '先判断是命令、斜杠指令还是任务。pytest 这样的命令直接进 PTY，不花一次模型调用。', '$ pytest → PTY'],
      ['组装上下文', '模式、项目提示、规则、记忆、计划和当前终端状态拼成一次请求，发往你选定的后端。', 'mode: act · scope: project'],
      ['调用工具', '模型返回结构化调用；内置工具、Skills、MCP、扩展和子 agent 都从同一个注册表出发。', 'read · shell.exec · edit'],
      ['过闸', '每个动作先过策略闸：该放行的放行，该问你的停下来问，越界的直接拒绝。', 'allow · ask · deny'],
      ['执行并留痕', '工具在本地运行，结果回流给下一轮；事件、追踪和用量写进状态层，可回放、可恢复。', 'trace: on'],
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
    priceIntro: 'Laintas CLI 本身不收费。模型调用按量计费或订阅，Pro 与 Gen 的额度在所有 Laintas 产品间共用。',
    perMonth: '/ 月', pricing: '查看完整定价与模型分档',
    ecoTitle: '同一个账号，也通向这些',
    eco: [
      ['Helpwo', '网页端 AI 工作台。在 CLI 里 /connect，就能在浏览器里看到这台机器的终端并直接派任务。', 'https://helpwo.laintas.com/'],
      ['插件市场', '官方与社区扩展。/extensions install 一条命令装进 CLI，社区代码安装前会先过一遍审查。', '/plugins'],
      ['Laintas 账户', '余额、订阅、用量与订单，所有产品在同一处查看。', 'https://laintas.com/dashboard'],
    ],

    downloadKicker: '04 · 下载', downloadTitle: ['现在，', '交给你的终端。'],
    downloadIntro: '推荐一行命令安装；也可以直接下载安装包，或拿源码包审计与二次开发。',
    platformLabel: '选择安装平台', linux: 'Linux', windows: 'Windows', copied: '已复制', copy: '复制',
    startSteps: [['运行安装命令', '自动识别架构并完成安装'], ['启动并登录', '输入 laintas-cli，按提示登录 Laintas 账号'], ['说出第一个任务', '比如“看看这个项目怎么跑起来”']],
    packages: '安装包', download: '下载',
    requirements: 'Linux 需 64 位 glibc 系统（x86_64 / arm64）；Windows 需 x86_64、Windows 10 2004+ 或 Windows 11，并启用 WSL 2。',
  },
  en: {
    chapters: ['Intro', 'Why', 'How it works', 'Pricing', 'Download'],
    say: [
      'I live in the terminal. So that is where we put the agent.',
      'First, why: it is not another chat window.',
      'One request in. Every step visible, every action gated.',
      'The runtime is free. Model usage goes at your pace.',
      'That is it. One line, and it is in your terminal.',
    ],
    kicker: 'LAINTAS CLI · THE AI AGENT IN YOUR TERMINAL',
    title: ['Your shell, as usual.', 'Plus an agent.'],
    lead: 'Laintas CLI is an autonomous agent that lives in your shell. Commands like ls, git and vim still run straight in a real PTY, never through the model. Describe a task in a sentence and the agent reads code, runs commands and edits files — every step in front of you, every risky one waiting for your yes.',
    install: 'Install Laintas CLI', seeHow: 'See how it works',
    facts: [['Free', 'Free runtime, source you can download and audit'], ['Linux · Windows', 'x86_64 / arm64, Windows 10 2004+ / 11'], ['One account', 'Shared with Helpwo and the plugin market']],
    realEyebrow: 'RECORDED SESSION · v1.29.4', realTitle: 'A real session, replayed frame for frame',
    realNote: 'The agent reads the project, runs pytest, traces the failing case to its root cause, shows the patch and waits for approval, then re-runs the suite. Only the model pauses are cut; nothing on screen is rewritten.',

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

    howKicker: '02 · THE LIFE OF A REQUEST', howTitle: ['Follow one request', 'all the way through.'],
    howIntro: 'Take “fix the failing test”. This is the path the runtime actually walks; each step maps to a local module and traceable state.',
    steps: [
      ['Route', 'First: command, slash command, or task? A command like pytest goes straight to the PTY without spending a model call.', '$ pytest → PTY'],
      ['Assemble context', 'Mode, project prompt, rules, memory, plan and live terminal state become one request to the backend you chose.', 'mode: act · scope: project'],
      ['Call tools', 'The model answers with structured calls; built-ins, Skills, MCP, extensions and sub-agents all come from one registry.', 'read · shell.exec · edit'],
      ['Pass the gate', 'Every action meets the policy gate: allowed ones run, ask-first ones stop for you, out-of-bounds ones are refused.', 'allow · ask · deny'],
      ['Run and record', 'The tool runs locally and its result feeds the next turn; events, traces and usage land in state you can replay and resume.', 'trace: on'],
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
    priceIntro: 'Laintas CLI itself is free. Model calls are pay-as-you-go or subscription, and Pro and Gen allowances are shared across every Laintas product.',
    perMonth: '/ mo', pricing: 'Full pricing and model tiers',
    ecoTitle: 'The same account also opens',
    eco: [
      ['Helpwo', 'The web AI workspace. Run /connect in the CLI and see this machine’s terminal — and hand it tasks — from your browser.', 'https://helpwo.laintas.com/'],
      ['Plugin market', 'Official and community extensions, one /extensions install away. Community code is reviewed before it installs.', '/plugins'],
      ['Laintas account', 'Balance, subscription, usage and orders for every product, in one place.', 'https://laintas.com/dashboard'],
    ],

    downloadKicker: '04 · DOWNLOAD', downloadTitle: ['Now,', 'hand it to your terminal.'],
    downloadIntro: 'The one-line installer is recommended. You can also grab a package directly, or audit and extend the source.',
    platformLabel: 'Select install platform', linux: 'Linux', windows: 'Windows', copied: 'Copied', copy: 'Copy',
    startSteps: [['Run the installer', 'It detects your architecture and installs laintas-cli'], ['Start and sign in', 'Type laintas-cli and sign in to your Laintas account'], ['Give it a first task', 'Try “figure out how to run this project”']],
    packages: 'Packages', download: 'Download',
    requirements: 'Linux needs a 64-bit glibc system (x86_64 / arm64). Windows needs x86_64 on Windows 10 2004+ or Windows 11 with WSL 2 enabled.',
  },
};

// Scroll → scene. Each chapter after the first owns one handoff: while its top
// edge travels up through the middle band of the viewport, its scene fades in
// over the previous one. Styles are written straight to the layers from a rAF
// so scrolling never re-renders React; only the chapter index is state.
function useSceneScroll(chapterRefs, layerRefs, dipRef) {
  const [active, setActive] = useState(0);

  useEffect(() => {
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
    let frame = 0;

    const update = () => {
      frame = 0;
      const vh = window.innerHeight;
      // On narrow screens the stage is a strip over the top half, so the
      // handoff has to finish before a chapter slides under it.
      const narrow = window.innerWidth <= 960;
      const start = narrow ? 0.95 : 0.85;
      const span = narrow ? 0.4 : 0.45;
      // t[k]: how far scene k has faded in. Scene 0 is the base layer.
      const t = SCENES.map((_, k) => {
        if (k === 0) return 1;
        const node = chapterRefs.current[k];
        if (!node) return 0;
        const top = node.getBoundingClientRect().top;
        return clamp01((vh * start - top) / (vh * span));
      });
      const still = reduced.matches;
      let dip = 0;
      SCENES.forEach((scene, k) => {
        const layer = layerRefs.current[k];
        if (!layer) return;
        const incoming = t[k];
        const outgoing = k + 1 < SCENES.length ? t[k + 1] : 0;
        const scale = still ? 1 : (1 + scene.enter * (1 - incoming)) * (1 - scene.exit * outgoing);
        const fade = dissolve(incoming);
        layer.style.opacity = String(fade);
        layer.style.transform = `scale(${scale.toFixed(4)})`;
        dip = Math.max(dip, scene.dip * (1 - Math.abs(2 * fade - 1)));
      });
      if (dipRef.current) dipRef.current.style.opacity = dip.toFixed(3);
      let current = 0;
      t.forEach((value, k) => { if (value >= 0.5) current = k; });
      setActive((previous) => (previous === current ? previous : current));
    };

    const schedule = () => { if (!frame) frame = window.requestAnimationFrame(update); };
    update();
    window.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule);
    reduced.addEventListener?.('change', schedule);
    return () => {
      window.removeEventListener('scroll', schedule);
      window.removeEventListener('resize', schedule);
      reduced.removeEventListener?.('change', schedule);
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, [chapterRefs, layerRefs, dipRef]);

  return active;
}

export default function DownloadSection() {
  const { lang } = useLanguage();
  const c = COPY[lang] || COPY.en;
  const [release, setRelease] = useState(RELEASE_FALLBACK);
  const [installPlatform, setInstallPlatform] = useState('linux');
  const chapterRefs = useRef([]);
  const layerRefs = useRef([]);
  const dipRef = useRef(null);
  const active = useSceneScroll(chapterRefs, layerRefs, dipRef);

  useEffect(() => {
    if (/Windows/i.test(window.navigator.userAgent)) setInstallPlatform('windows');
    fetch(RELEASE_API)
      .then((response) => response.ok ? response.json() : Promise.reject(new Error('release lookup failed')))
      .then((data) => { if (data.tag_name) setRelease(data.tag_name); })
      .catch(() => {});
  }, []);

  const chapter = (index) => (node) => { chapterRefs.current[index] = node; };
  const goTo = (index) => chapterRefs.current[index]?.scrollIntoView({ behavior: 'smooth', block: 'start' });

  return (
    <main className="product-page story-page" lang={lang === 'zh' ? 'zh-CN' : 'en'}>
      <div className="story">
        {/* ── The presenter: one sticky stage, five stacked scenes ───── */}
        <div className="story-stage" aria-hidden="true">
          {SCENES.map((scene, index) => (
            <img
              key={scene.src}
              ref={(node) => { layerRefs.current[index] = node; }}
              className="story-scene"
              src={`/story/${scene.src}.webp`}
              srcSet={`/story/${scene.src}-960.webp 960w, /story/${scene.src}.webp 1672w`}
              sizes="100vw"
              alt=""
              style={{ transformOrigin: scene.focus, opacity: index === 0 ? 1 : 0 }}
              fetchPriority={index === 0 ? 'high' : 'auto'}
              decoding="async"
              draggable="false"
            />
          ))}
          <div className="story-dip" ref={dipRef} />
          <div className="story-shade" />
          <p className="story-say" key={`${lang}-${active}`}>{c.say[active]}</p>
        </div>

        <nav className="story-progress" aria-label={lang === 'zh' ? '章节' : 'Chapters'}>
          {c.chapters.map((label, index) => (
            <button type="button" key={label} className={index === active ? 'active' : ''} aria-current={index === active ? 'step' : undefined} onClick={() => goTo(index)}>
              <span>0{index + 1}</span><em>{label}</em>
            </button>
          ))}
        </nav>

        <div className="story-chapters">
          {/* ── Scene 1 · at the laptop: what it is ───────────────────────── */}
          <section ref={chapter(0)} id="top" className="story-chapter story-hero">
            <Kicker>{c.kicker}</Kicker>
            <h1 className="st-display"><Title parts={c.title} /></h1>
            <p className="st-lead">{c.lead}</p>
            <div className="st-actions">
              <a className="st-button st-button-primary" href="#download"><ArrowDownToLine size={16} />{c.install}</a>
              <a className="st-button" href="#operations">{c.seeHow}<ArrowRight size={15} /></a>
            </div>
            <dl className="st-facts">
              {c.facts.map(([term, detail]) => <div key={term}><dt>{term}</dt><dd>{detail}</dd></div>)}
            </dl>
            <figure className="hero-terminal st-replay">
              <div className="proof-label"><span className="live-dot" />{c.realEyebrow}</div>
              <TerminalReplay />
              <figcaption><strong>{c.realTitle}</strong><span>{c.realNote}</span></figcaption>
            </figure>
          </section>

          {/* ── Scene 2 · laptop closed, turns to us: why ─────────────────── */}
          <section ref={chapter(1)} id="operations" className="story-chapter">
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
          </section>

          {/* ── Scene 3 · stands and explains: how a request runs ─────────── */}
          <section ref={chapter(2)} id="workflow" className="story-chapter">
            <ChapterHead kicker={c.howKicker} title={c.howTitle} intro={c.howIntro} />
            <ol className="st-journey">
              {c.steps.map(([title, detail, tag], index) => (
                <li key={title}>
                  <span className="st-journey-dot">{index + 1}</span>
                  <div><h3>{title}<code>{tag}</code></h3><p>{detail}</p></div>
                </li>
              ))}
            </ol>
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

          {/* ── Scene 4 · faces us, open hands: pricing ───────────────────── */}
          <section ref={chapter(3)} id="pricing" className="story-chapter">
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

          {/* ── Scene 5 · points to the right: download ───────────────────── */}
          <section ref={chapter(4)} id="download" className="story-chapter">
            <ChapterHead kicker={c.downloadKicker} title={c.downloadTitle} intro={c.downloadIntro} />
            <div className="st-install">
              <div className="st-install-tabs" role="group" aria-label={c.platformLabel}>
                {['linux', 'windows'].map((platform) => (
                  <button type="button" key={platform} className={installPlatform === platform ? 'active' : ''} onClick={() => setInstallPlatform(platform)} aria-pressed={installPlatform === platform}>{c[platform]}</button>
                ))}
              </div>
              <div className="st-install-line">
                <code><span aria-hidden="true">{installPlatform === 'windows' ? 'PS>' : '$'}</span>{INSTALL_COMMANDS[installPlatform]}</code>
                <CopyButton value={INSTALL_COMMANDS[installPlatform]} labels={c} />
              </div>
            </div>
            <ol className="st-start">
              {c.startSteps.map(([title, detail], index) => (
                <li key={title}><span>0{index + 1}</span><strong>{title}</strong><p>{detail}</p></li>
              ))}
            </ol>
            <p className="st-eco-title">{c.packages}</p>
            <div className="st-packages">
              {DOWNLOADS.map(({ id, names, details, file, href, icon: Icon }) => (
                <a href={href || `${RELEASE_BASE}/${file}`} key={id}>
                  <Icon size={18} strokeWidth={1.6} />
                  <span><strong>{names[lang] || names.en}</strong><small>{details[lang] || details.en}</small></span>
                  <code>{release}</code>
                  <em>{c.download}<ArrowDownToLine size={15} /></em>
                </a>
              ))}
            </div>
            <p className="st-note">{c.requirements}</p>
          </section>
        </div>
      </div>

      <SiteFooter />
    </main>
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
