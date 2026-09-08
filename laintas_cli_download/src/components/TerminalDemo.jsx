import { useEffect, useState } from 'react';
import { useLanguage } from '@/contexts/LanguageContext';

// A live terminal demo rendered in HTML/CSS — vector-crisp at any size, unlike
// a raster screenshot, and it can *show* the agent loop working: prompt, tool
// calls, and a final status. Every line is pure display copy — nothing here
// executes — and it stays honest to the runtime lifecycle described in
// README.md (classify → assemble context → dispatch tool → authorize →
// execute → observe → persist).

const SCRIPT_EN = [
  { text: '$ laintas-cli', tone: 'cmd', delay: 300 },
  { text: 'Laintas CLI v1.25.1 — agent runtime ready', tone: 'muted', delay: 420 },
  { text: '', tone: 'plain', delay: 220 },
  { text: '> fix the failing test in parser.py and run it', tone: 'input', caret: true, delay: 160 },
  { text: '', tone: 'plain', delay: 260 },
  { text: '◌ classify input → natural-language task → agent loop', tone: 'step', delay: 520 },
  { text: '✓ inspected workspace · parser.py · tests/test_parser.py', tone: 'ok', delay: 640 },
  { text: '→ tool  fs.read   parser.py', tone: 'tool', delay: 520 },
  { text: '  · read 412 lines', tone: 'plain', delay: 160 },
  { text: '→ tool  fs.edit   parser.py:96', tone: 'tool', delay: 520 },
  { text: '  · patched import — removed unused symbol', tone: 'plain', delay: 200 },
  { text: '→ run   pytest tests/test_parser.py', tone: 'tool', delay: 520 },
  { text: '  · 3 passed, 1 skipped', tone: 'ok', delay: 220 },
  { text: '', tone: 'plain', delay: 220 },
  { text: '● done — agent returned to prompt', tone: 'done', delay: 640 },
  { text: '$ █', tone: 'prompt', delay: 9999 },
];

const SCRIPT_ZH = [
  { text: '$ laintas-cli', tone: 'cmd', delay: 300 },
  { text: 'Laintas CLI v1.25.1 — agent 运行时已就绪', tone: 'muted', delay: 420 },
  { text: '', tone: 'plain', delay: 220 },
  { text: '> 修复 parser.py 里的失败测试并运行', tone: 'input', caret: true, delay: 160 },
  { text: '', tone: 'plain', delay: 260 },
  { text: '◌ 分类输入 → 自然语言任务 → agent 循环', tone: 'step', delay: 520 },
  { text: '✓ 检查工作区 · parser.py · tests/test_parser.py', tone: 'ok', delay: 640 },
  { text: '→ 工具  fs.read   parser.py', tone: 'tool', delay: 520 },
  { text: '  · 读取 412 行', tone: 'plain', delay: 160 },
  { text: '→ 工具  fs.edit   parser.py:96', tone: 'tool', delay: 520 },
  { text: '  · 已修复 import — 移除未使用的符号', tone: 'plain', delay: 200 },
  { text: '→ 运行  pytest tests/test_parser.py', tone: 'tool', delay: 520 },
  { text: '  · 3 通过, 1 跳过', tone: 'ok', delay: 220 },
  { text: '', tone: 'plain', delay: 220 },
  { text: '● 完成 — agent 已回到提示符', tone: 'done', delay: 640 },
  { text: '$ █', tone: 'prompt', delay: 9999 },
];

const CHARS_PER_TICK = 2;

export default function TerminalDemo() {
  const { lang } = useLanguage();
  const script = lang === 'zh' ? SCRIPT_ZH : SCRIPT_EN;
  const [lineIdx, setLineIdx] = useState(0);
  const [charIdx, setCharIdx] = useState(0);
  const [paused, setPaused] = useState(false);
  // Honour OS-level "reduce motion": skip the typing animation and render the
  // whole transcript at once. Evaluated once on mount (theme/language switches
  // do not change the media query meaningfully for this component).
  const reduceMotion = typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Advance one line at a time; type each line's characters progressively.
  useEffect(() => {
    if (paused || reduceMotion) return undefined;
    if (lineIdx >= script.length) return undefined;
    const line = script[lineIdx];
    const text = line.text || '';
    if (charIdx < text.length) {
      const timer = window.setTimeout(() => {
        setCharIdx((c) => Math.min(text.length, c + CHARS_PER_TICK));
      }, 14);
      return () => window.clearTimeout(timer);
    }
    // line finished -> pause its delay then move to next
    const timer = window.setTimeout(() => {
      setLineIdx((l) => l + 1);
      setCharIdx(0);
    }, line.delay ?? 120);
    return () => window.clearTimeout(timer);
  }, [lineIdx, charIdx, paused, script]);

  // Loop back to the start after the last line has played.
  useEffect(() => {
    if (lineIdx < script.length || paused) return undefined;
    const timer = window.setTimeout(() => {
      setLineIdx(0);
      setCharIdx(0);
    }, 3600);
    return () => window.clearTimeout(timer);
  }, [lineIdx, paused, script]);

  // Reduce-motion users see the full transcript instantly; otherwise reveal it
  // progressively as the typing animation advances.
  const visible = reduceMotion
    ? script.map((l) => ({ line: l, shown: l.text || '' }))
    : script.slice(0, lineIdx).map((l) => ({ line: l, shown: l.text }));
  if (!reduceMotion && lineIdx < script.length) {
    const cur = script[lineIdx];
    visible.push({ line: cur, shown: (cur.text || '').slice(0, charIdx) });
  }

  return (
    <div className="termdemo" onMouseEnter={() => setPaused(true)} onMouseLeave={() => setPaused(false)}>
      <div className="termdemo-bar">
        <span className="termdemo-dots"><i /><i /><i /></span>
        <span className="termdemo-title">laintas-cli — agent session</span>
        <span className="termdemo-rec"><b />LIVE</span>
      </div>
      <div className="termdemo-body" role="img" aria-label={lang === 'zh' ? 'Laintas CLI 实时终端会话演示' : 'Live Laintas CLI terminal session demo'}>
        {visible.map(({ line, shown }, index) => {
          const isCurrent = index === visible.length - 1 && lineIdx < script.length;
          return (
            <div key={index} className={`td-line td-${line.tone || 'plain'}`}>
              <span>{shown}</span>
              {isCurrent && <span className="td-caret" />}
            </div>
          );
        })}
      </div>
    </div>
  );
}
