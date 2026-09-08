import { useEffect, useRef, useState } from 'react';
import { useLanguage } from '@/contexts/LanguageContext';

// Faithful, live HTML/CSS reproduction of a real laintas-cli session.
//
// The visuals come straight from the runtime source, not from a screenshot:
//   - startup banner "Laintas CLI v1.25.1 · Lin7c" and the REPL status bar
//   - the real thinking spinner (symbols.py SPINNER_RELAY = "L· L› L» L›",
//     140ms per frame, brand green #3fb950) with the real label format
//     "Thinking… {s}s · {model} · {mode}" — agent_loop.py's _render()
//   - the real status symbols (→, ✓) and real tool names (fs.grep, fs.read)
//
// The user input is exactly the real case: "帮我分析一下laintas-cli".

const SPINNER_FRAMES = ['L·', 'L›', 'L»', 'L›'];
const SPINNER_MS = 140;
const CHARS_PER_TICK = 2;
const TYPE_MS = 14;

// Timeline of steps. Row semantics:
//   line    -> instant line (tone decides colour)
//   status  -> REPL status bar
//   blank   -> empty row
//   input   -> typed user input (green, with caret)
//   think   -> one row that holds a live spinner + clock, then resolves into
//              `resolved` (a tool line) or disappears
//   type    -> typed prose line
//   prompt  -> the `› ` prompt with a blinking caret
function buildSteps(lang) {
  const zh = lang === 'zh';
  return [
    { k: 'line', text: 'Laintas CLI v1.25.1 · Lin7c', tone: 'accent' },
    { k: 'status', text: '~/laintas_cli     L> 3 | primary | ACT | deepseek-v4-flash' },
    { k: 'blank' },
    { k: 'input', text: zh ? '帮我分析一下laintas-cli' : 'analyze laintas-cli' },
    { k: 'think', ms: 2600, label: 'Thinking', model: 'deepseek-v4-flash', mode: 'ACT',
      resolved: '✓ fs.grep  "laintas-cli"  README.md', resolvedTone: 'ok' },
    { k: 'think', ms: 2400, label: 'Thinking', model: 'deepseek-v4-flash', mode: 'ACT',
      resolved: '→ fs.read  README.md · 734 lines', resolvedTone: 'tool' },
    { k: 'think', ms: 2600, label: 'Thinking', model: 'deepseek-v4-flash', mode: 'ACT',
      resolved: '↳ highlights: agent runtime · PTY · policy', resolvedTone: 'plain' },
    { k: 'think', ms: 3200, label: 'Writing', model: 'deepseek-v4-flash', mode: 'ACT', resolved: null },
    { k: 'type', text: zh ? 'laintas-cli 是面向终端工作的自主 AI agent。普通 shell 命令保留在真实 PTY 中原生运行；自然语言任务进入可观察、可中断、可委派的 agent 循环——检查工作区、调用工具、拆分子任务，并保留每一步状态。它不是聊天窗口，而是离文件系统最近的那个 agent。'
                          : 'laintas-cli is an autonomous AI agent for terminal work. Commands run natively in a real PTY; natural-language tasks enter an observable, interruptible, delegating agent loop that inspects the workspace, calls tools, and keeps every step in state. It is the agent closest to your files.', tone: 'plain', hold: 800 },
    { k: 'prompt', text: '› ' },
  ];
}

export default function TerminalDemo() {
  const { lang } = useLanguage();
  const stepsRef = useRef(buildSteps(lang));
  const script = stepsRef.current;

  const [stepIdx, setStepIdx] = useState(0);
  const [charIdx, setCharIdx] = useState(0);
  const [frame, setFrame] = useState(0);
  const [paused, setPaused] = useState(false);
  const reduceMotion = typeof window !== 'undefined'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const step = script[Math.min(stepIdx, script.length - 1)];

  // Advance the character typing for `input`/`type` steps.
  useEffect(() => {
    if (paused || reduceMotion) return undefined;
    if (step.k !== 'input' && step.k !== 'type') return undefined;
    const text = step.text || '';
    if (charIdx >= text.length) {
      const t = window.setTimeout(() => {
        setStepIdx((i) => i + 1);
        setCharIdx(0);
      }, step.hold ?? 260);
      return () => window.clearTimeout(t);
    }
    const t = window.setTimeout(() => {
      setCharIdx((c) => Math.min(text.length, c + CHARS_PER_TICK));
    }, TYPE_MS);
    return () => window.clearTimeout(t);
  }, [step, charIdx, paused, reduceMotion]);

  // Live spinner: cycle frames while a `think` step is active.
  useEffect(() => {
    if (paused || reduceMotion || step.k !== 'think') return undefined;
    const t = window.setInterval(() => setFrame((f) => (f + 1) % SPINNER_FRAMES.length), SPINNER_MS);
    return () => window.clearInterval(t);
  }, [step, paused, reduceMotion]);

  // Advance out of a `think` step after its duration.
  useEffect(() => {
    if (paused || reduceMotion || step.k !== 'think') return undefined;
    const t = window.setTimeout(() => {
      setStepIdx((i) => i + 1);
      setCharIdx(0);
    }, step.ms ?? 2400);
    return () => window.clearTimeout(t);
  }, [step, paused, reduceMotion]);

  // Loop back to the start after the full transcript has played.
  useEffect(() => {
    if (stepIdx < script.length || paused || reduceMotion) return undefined;
    const t = window.setTimeout(() => { setStepIdx(0); setCharIdx(0); setFrame(0); }, 4200);
    return () => window.clearTimeout(t);
  }, [stepIdx, paused, reduceMotion, script]);

  // Build the visible rows.
  const rows = [];
  for (let i = 0; i < script.length; i += 1) {
    const s = script[i];
    if (i > stepIdx) break;
    if (i === stepIdx) {
      if (s.k === 'input') {
        rows.push({ key: i, cls: 'td-line td-input', html: s.text.slice(0, charIdx), caret: true });
      } else if (s.k === 'type') {
        rows.push({ key: i, cls: 'td-line td-plain', html: s.text.slice(0, charIdx), caret: charIdx < s.text.length });
      } else if (s.k === 'think') {
        rows.push({ key: i, cls: 'td-line td-spin', html: `${SPINNER_FRAMES[frame]} ${s.label}…  ${s.model} · ${s.mode}`, caret: false });
      } else if (s.k === 'prompt') {
        rows.push({ key: i, cls: 'td-line td-prompt', html: s.text, caret: true });
      } else if (s.k === 'blank') {
        rows.push({ key: i, cls: 'td-line td-blank', html: ' ', caret: false });
      } else if (s.k === 'status') {
        rows.push({ key: i, cls: 'td-status', html: s.text, caret: false });
      } else if (s.k === 'line') {
        rows.push({ key: i, cls: `td-line td-${s.tone || 'plain'}`, html: s.text, caret: false });
      }
      break;
    }
    // completed step
    if (s.k === 'input') {
      rows.push({ key: i, cls: 'td-line td-input', html: s.text, caret: false });
    } else if (s.k === 'type') {
      rows.push({ key: i, cls: 'td-line td-plain', html: s.text, caret: false });
    } else if (s.k === 'think') {
      if (s.resolved) rows.push({ key: i, cls: `td-line td-${s.resolvedTone || 'plain'}`, html: s.resolved, caret: false });
    } else if (s.k === 'prompt') {
      rows.push({ key: i, cls: 'td-line td-prompt', html: s.text, caret: false });
    } else if (s.k === 'blank') {
      rows.push({ key: i, cls: 'td-line td-blank', html: ' ', caret: false });
    } else if (s.k === 'status') {
      rows.push({ key: i, cls: 'td-status', html: s.text, caret: false });
    } else if (s.k === 'line') {
      rows.push({ key: i, cls: `td-line td-${s.tone || 'plain'}`, html: s.text, caret: false });
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
