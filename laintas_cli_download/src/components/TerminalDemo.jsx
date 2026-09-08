import { useEffect, useMemo, useRef, useState } from 'react';
import { useLanguage } from '@/contexts/LanguageContext';

// Faithful, live HTML/CSS reproduction of a real laintas-cli session.
//
// Visuals come from the runtime source, not a screenshot:
//   - startup banner "Laintas CLI v1.25.1 · Lin7c" and the REPL status bar
//   - the real thinking spinner (symbols.py SPINNER_RELAY = "L· L› L» L›",
//     140ms/frame, green #3fb950) with label format "Thinking… · model · mode"
//   - real symbols → ✓ ↳ and real tool names (fs.grep, fs.read)
// The user input is exactly the real case: "帮我分析一下laintas-cli".
//
// DRIVER: a single monotonic clock position `pos` walks a precomputed timeline
// (cumulative per-step durations). Every render derives the visible rows from
// `pos` alone — there are NO interlocking step/char/frame effects to race.
//   line/status/blank : shown whole, advance after `delay`
//   input/type        : characters revealed as `pos` crosses the typing window
//   think             : spinner frames cycle for `ms`, then its `resolved`
//                       tool line is rendered once the step is complete
//   prompt            : end state (blinking caret); when `pos` passes `total`
//                       the clock wraps and the demo loops from the top

const SPINNER_FRAMES = ['L·', 'L›', 'L»', 'L›'];
const SPINNER_MS = 140;
const CHARS_PER_TICK = 2;
const TYPE_MS = 14;

function buildSteps(lang) {
  const zh = lang === 'zh';
  return [
    { k: 'line', text: 'Laintas CLI v1.25.1 · Lin7c', tone: 'accent', delay: 340 },
    { k: 'status', text: '~/laintas_cli     L> 3 | primary | ACT | deepseek-v4-flash', delay: 320 },
    { k: 'blank', delay: 200 },
    { k: 'input', text: zh ? '帮我分析一下laintas-cli' : 'analyze laintas-cli', hold: 340 },
    { k: 'think', ms: 2600, label: 'Thinking', model: 'deepseek-v4-flash', mode: 'ACT',
      resolved: '✓ fs.grep  "laintas-cli"  README.md', resolvedTone: 'ok' },
    { k: 'think', ms: 2400, label: 'Thinking', model: 'deepseek-v4-flash', mode: 'ACT',
      resolved: '→ fs.read  README.md · 734 lines', resolvedTone: 'tool' },
    { k: 'think', ms: 2600, label: 'Thinking', model: 'deepseek-v4-flash', mode: 'ACT',
      resolved: '↳ highlights: agent runtime · PTY · policy', resolvedTone: 'plain' },
    { k: 'think', ms: 3200, label: 'Writing', model: 'deepseek-v4-flash', mode: 'ACT', resolved: null },
    { k: 'type', text: zh ? 'laintas-cli 是面向终端工作的自主 AI agent。普通 shell 命令保留在真实 PTY 中原生运行；自然语言任务进入可观察、可中断、可委派的 agent 循环——检查工作区、调用工具、拆分子任务，并保留每一步状态。它不是聊天窗口，而是离文件系统最近的那个 agent。'
                          : 'laintas-cli is an autonomous AI agent for terminal work. Commands run natively in a real PTY; natural-language tasks enter an observable, interruptible, delegating agent loop that inspects the workspace, calls tools, and keeps every step in state. It is the agent closest to your files.', tone: 'plain', hold: 900 },
    { k: 'prompt', text: '› ', hold: 3800 },
  ];
}

// Per-step duration in ms.
function stepDur(s) {
  if (s.k === 'line' || s.k === 'status' || s.k === 'blank') return s.delay ?? 240;
  if (s.k === 'input' || s.k === 'type') {
    const typing = Math.ceil((s.text || '').length / CHARS_PER_TICK) * TYPE_MS;
    return typing + (s.hold ?? 300);
  }
  if (s.k === 'think') return s.ms ?? 2500;
  if (s.k === 'prompt') return s.hold ?? 3500;
  return 300;
}

// Rendered form once a step is fully complete (never the running state).
function completedStep(s) {
  if (s.k === 'input') return { html: s.text, cls: 'td-line td-input', caret: false };
  if (s.k === 'type') return { html: s.text, cls: 'td-line td-plain', caret: false };
  if (s.k === 'think') return s.resolved ? { html: s.resolved, cls: `td-line td-${s.resolvedTone || 'plain'}`, caret: false } : null;
  if (s.k === 'prompt') return { html: s.text, cls: 'td-line td-prompt', caret: false };
  if (s.k === 'blank') return { html: ' ', cls: 'td-line td-blank', caret: false };
  if (s.k === 'status') return { html: s.text, cls: 'td-status', caret: false };
  return { html: s.text, cls: `td-line td-${s.tone || 'plain'}`, caret: false };
}

export default function TerminalDemo() {
  const { lang } = useLanguage();
  const script = useMemo(() => buildSteps(lang), [lang]);

  // Cumulative time offsets of each step; total = full cycle length.
  const { cum, total } = useMemo(() => {
    const c = [0];
    for (let i = 0; i < script.length; i += 1) c.push(c[i] + stepDur(script[i]));
    return { cum: c, total: c[c.length - 1] };
  }, [script]);

  const [pos, setPos] = useState(0);
  const [paused, setPaused] = useState(false);
  const lastRef = useRef(0);
  const reduceMotion = typeof window !== 'undefined'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const animating = !paused && !reduceMotion;

  // Reset the clock when the language (and therefore the timeline) changes.
  useEffect(() => { setPos(0); }, [lang]);

  // Single clock driver: advance `pos` by real elapsed ms, wrapping at `total`.
  useEffect(() => {
    if (!animating) return undefined;
    let raf;
    let last = performance.now();
    const tick = (now) => {
      const dt = now - last;
      last = now;
      setPos((p) => (p + dt) % total);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [animating, total]);

  // Find which step `pos` is inside.
  let stepIdx = 0;
  for (let i = 0; i < script.length; i += 1) {
    if (pos >= cum[i + 1]) stepIdx = i + 1;
    else break;
  }
  if (stepIdx >= script.length) stepIdx = script.length - 1;
  const local = pos - cum[stepIdx];
  const step = script[stepIdx];

  // Build the rows to show right now.
  const rows = [];
  // Reduce motion / paused shows the full completed transcript.
  if (!animating) {
    for (let i = 0; i < script.length; i += 1) {
      const done = completedStep(script[i]);
      if (done) rows.push({ key: i, ...done });
    }
  } else {
    // Steps fully behind the cursor render completed.
    for (let i = 0; i < stepIdx; i += 1) {
      const done = completedStep(script[i]);
      if (done) rows.push({ key: i, ...done });
    }
    // The current step renders its running state.
    if (step.k === 'input' || step.k === 'type') {
      const shown = (step.text || '').slice(0, CHARS_PER_TICK * Math.floor(local / TYPE_MS));
      rows.push({
        key: stepIdx,
        html: shown,
        cls: step.k === 'input' ? 'td-line td-input' : 'td-line td-plain',
        caret: true,
      });
    } else if (step.k === 'think') {
      const fr = Math.floor(local / SPINNER_MS) % SPINNER_FRAMES.length;
      rows.push({
        key: stepIdx,
        html: `${SPINNER_FRAMES[fr]} ${step.label}…  ${step.model} · ${step.mode}`,
        cls: 'td-line td-spin',
        caret: false,
      });
    } else if (step.k === 'prompt') {
      rows.push({ key: stepIdx, html: step.text, cls: 'td-line td-prompt', caret: true });
    } else if (step.k === 'blank') {
      rows.push({ key: stepIdx, html: ' ', cls: 'td-line td-blank', caret: false });
    } else if (step.k === 'status') {
      rows.push({ key: stepIdx, html: step.text, cls: 'td-status', caret: false });
    } else {
      rows.push({ key: stepIdx, html: step.text, cls: `td-line td-${step.tone || 'plain'}`, caret: false });
    }
  }

  return (
    <div className="termdemo" onMouseEnter={() => setPaused(true)} onMouseLeave={() => setPaused(false)}>
      <div className="termdemo-bar">
        <span className="termdemo-dots"><i /><i /><i /></span>
        <span className="termdemo-title">laintas-cli — agent session</span>
        <span className="termdemo-rec"><b />LIVE</span>
      </div>
      <div className="termdemo-body" role="img" aria-label={lang === 'zh' ? 'Laintas CLI 实时终端会话演示' : 'Live Laintas CLI terminal session demo'}>
        {rows.map((r) => (
          <div key={r.key} className={r.cls}>
            <span>{r.html}</span>
            {r.caret && <span className="td-caret" />}
          </div>
        ))}
      </div>
    </div>
  );
}
