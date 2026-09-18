import { memo, useEffect, useMemo, useRef, useState } from 'react';
import { useLanguage } from '@/contexts/LanguageContext';

// Replay of a REAL laintas-cli session, not an illustration of one.
//
// A session was recorded off a PTY (every byte the terminal received, with
// timings), replayed through a VT emulator, and photographed frame by frame.
// What ships here is that photo roll: `public/demo/session-<lang>.json` holds
// the terminal size, the frame times, and for each frame only the screen rows
// that changed, as runs of [text, fg, bg, flags, cells].
//
// So the banner, the status slots, the ● tool rows, the shimmer spinner and
// the wrapped Markdown answer below are the CLI's own output at the version
// stated in `meta.version` — nothing here re-implements the renderer, and
// nothing can drift from it except by re-recording.
//
// Two rules keep the replay honest on a web page:
//   - cells, not characters. Every run is pinned to `cells × charWidth`, so a
//     CJK glyph occupies its two columns and long rows cannot drift out of
//     alignment no matter what the browser does with the font.
//   - the screen is laid out once at a fixed font size and then scaled to the
//     card with a CSS transform. Shrinking the font instead would hit the
//     browser's minimum font size on a phone — the glyphs stop shrinking, the
//     cells keep shrinking, and the columns collide. Scaling keeps the session
//     at its recorded width without ever re-wrapping it, which would make it a
//     different session from the one that was recorded.

// pyte reports palette entries by name and everything else as hex. These are
// the same semantic colors the CLI's own dark theme uses.
const NAMED = {
  black: '#0d1117', red: '#f85149', green: '#3fb950', brown: '#e3b341',
  blue: '#58a6ff', magenta: '#a78bfa', cyan: '#39c5cf', white: '#e6edf3',
  brightblack: '#6e7681', brightred: '#ff7b72', brightgreen: '#56d364',
  brightbrown: '#f2cc60', brightblue: '#79c0ff', brightmagenta: '#c9a2ff',
  brightcyan: '#56d4dd', brightwhite: '#f0f6fc',
};
// The screen is built at this size and scaled afterwards, so it never meets
// the browser's minimum font size.
const BASE_FONT = 13;
const PAD_X = 18;
// The size every session is recorded at (scripts/record_session.py); used to
// reserve the card's height before the recording has loaded.
const DEFAULT_COLS = 100;
const DEFAULT_ROWS = 34;
const PAD_Y = 14;
const TERMINAL_FONT = "'JetBrains Mono', Menlo, Consolas, 'Cascadia Mono', 'DejaVu Sans Mono', 'Noto Sans Mono', 'Noto Sans SC', monospace";

const FG_DEFAULT = '#c9d1d9';
const BG_DEFAULT = 'transparent';

function color(value, fallback) {
  if (!value || value === 'default') return fallback;
  return NAMED[value] || `#${value}`;
}

function Row({ runs, charWidth }) {
  return (
    <div className="tr-row">
      {runs.map((run, index) => {
        const [text, fg, bg, flags, cells] = run;
        const reverse = (flags & 2) !== 0;
        const foreground = color(fg, FG_DEFAULT);
        const background = color(bg, BG_DEFAULT);
        return (
          <span
            key={index}
            style={{
              width: `${cells * charWidth}px`,
              color: reverse ? (background === 'transparent' ? '#0b0f0c' : background) : foreground,
              background: reverse ? foreground : background,
              fontWeight: (flags & 1) !== 0 ? 600 : 400,
            }}
          >
            {text}
          </span>
        );
      })}
    </div>
  );
}

// Frames carry only the rows that changed, and a changed row keeps the same
// `runs` array identity across frames, so memoising here means a frame repaints
// the handful of rows it touched instead of the whole screen.
const MemoRow = memo(Row);

export default function TerminalReplay({ hold = 12 }) {
  const { lang } = useLanguage();
  const [session, setSession] = useState(null);
  const [rows, setRows] = useState([]);
  const [metrics, setMetrics] = useState({ charWidth: BASE_FONT * 0.6, scale: 1 });
  const shellRef = useRef(null);
  // Pausing must not be a dependency of the playback effect. React would tear
  // the effect down and build it again on every hover, and the clock, the
  // frame cursor and the screen buffer live inside it — so pointing at the
  // terminal restarted the session instead of holding it still. The pointer
  // writes to a ref; the effect reads it and keeps running.
  const pausedRef = useRef(false);
  // Same treatment for "is it even on screen": scrolled out of view the
  // session holds where it is instead of running the clock (and the CPU) down
  // in a card nobody is looking at.
  const offscreenRef = useRef(false);

  const reduceMotion = typeof window !== 'undefined'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ── Load the recording for the current language ──────────────────────
  useEffect(() => {
    let cancelled = false;
    const url = `${import.meta.env.BASE_URL}demo/session-${lang === 'zh' ? 'zh' : 'en'}.json`;
    fetch(url)
      .then((response) => (response.ok ? response.json() : Promise.reject(response.status)))
      .then((data) => { if (!cancelled) { setSession(data); setRows([]); } })
      .catch(() => { if (!cancelled) setSession(null); });
    return () => { cancelled = true; };
  }, [lang]);

  // ── Measure the font, then scale the finished screen into the card ───
  useEffect(() => {
    if (!session || !shellRef.current) return undefined;
    const node = shellRef.current;

    const measure = () => {
      const probe = document.createElement('span');
      probe.style.cssText = `position:absolute;visibility:hidden;white-space:pre;font-size:${BASE_FONT}px;font-family:${TERMINAL_FONT}`;
      probe.textContent = '0'.repeat(50);
      node.appendChild(probe);
      const charWidth = probe.getBoundingClientRect().width / 50;
      probe.remove();
      const available = node.clientWidth - 2 * PAD_X;
      if (charWidth > 0 && available > 0) {
        setMetrics({ charWidth, scale: available / (charWidth * session.cols) });
      }
    };

    measure();
    // Web fonts land after first paint and change the cell width with them.
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(measure).catch(() => {});
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [session]);

  // ── Hold playback while the card is off screen ───────────────────────
  useEffect(() => {
    const node = shellRef.current;
    if (!node || typeof IntersectionObserver === 'undefined') return undefined;
    const observer = new IntersectionObserver(
      ([entry]) => { offscreenRef.current = !entry.isIntersecting; },
      { threshold: 0.05 });
    observer.observe(node);
    return () => observer.disconnect();
  }, [session]);

  // ── Drive the clock and apply frames ─────────────────────────────────
  const total = session ? session.duration + hold : 0;
  useEffect(() => {
    if (!session) return undefined;
    pausedRef.current = false;
    const blank = () => Array.from({ length: session.rows }, () => []);
    const apply = (buffer, delta) => {
      Object.entries(delta).forEach(([y, runs]) => { buffer[Number(y)] = runs; });
    };
    if (reduceMotion) {
      // No animation: show the finished session, which is the frame that
      // carries the answer.
      const buffer = blank();
      session.frames.forEach(([, delta]) => apply(buffer, delta));
      setRows(buffer);
      return undefined;
    }
    let raf;
    let last = performance.now();
    let clock = 0;
    let cursor = 0;
    let buffer = blank();
    setRows(buffer);
    const tick = (now) => {
      // A hidden tab stops rAF entirely. Without the clamp the first frame
      // back carries every second spent away and the session jumps forward
      // (or laps) the moment the tab is focused again.
      const dt = Math.min((now - last) / 1000, 0.25);
      last = now;
      if (!pausedRef.current && !offscreenRef.current) {
        clock += dt;
        if (clock >= total) {
          clock = 0;
          cursor = 0;
          buffer = blank();
          setRows(buffer);
        }
        let touched = false;
        while (cursor < session.frames.length && session.frames[cursor][0] <= clock) {
          buffer = buffer.slice();
          apply(buffer, session.frames[cursor][1]);
          cursor += 1;
          touched = true;
        }
        if (touched) setRows(buffer);
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [session, total, reduceMotion]);

  const label = lang === 'zh'
    ? `laintas-cli v${session?.meta?.version || ''} 真实会话录像`
    : `Recorded laintas-cli v${session?.meta?.version || ''} session`;

  const { charWidth, scale } = metrics;
  const lineHeight = Math.round(BASE_FONT * 1.45 * 100) / 100;
  const stageWidth = charWidth * (session?.cols || DEFAULT_COLS);
  const stageHeight = lineHeight * (session?.rows || DEFAULT_ROWS) + 2 * PAD_Y;

  const body = useMemo(() => rows.map((runs, index) => (
    // eslint-disable-next-line react/no-array-index-key
    <MemoRow key={index} runs={runs} charWidth={charWidth} />
  )), [rows, charWidth]);

  return (
    <div
      className="termreplay"
      ref={shellRef}
      onMouseEnter={() => { pausedRef.current = true; }}
      onMouseLeave={() => { pausedRef.current = false; }}
    >
      <div className="termreplay-bar">
        <span className="termreplay-dots"><i /><i /><i /></span>
        <span className="termreplay-title">
          {session ? `${session.meta?.shell || 'bash'} — laintas-cli` : 'laintas-cli'}
        </span>
        <span className="termreplay-rec"><b />REC</span>
      </div>
      <div
        className="termreplay-viewport"
        role="img"
        aria-label={label}
        style={{ height: `${stageHeight * scale}px` }}
      >
        <div
          className="termreplay-stage"
          style={{
            width: `${stageWidth}px`,
            padding: `${PAD_Y}px 0`,
            fontSize: `${BASE_FONT}px`,
            lineHeight: `${lineHeight}px`,
            transform: `scale(${scale})`,
          }}
        >
          {body}
        </div>
      </div>
    </div>
  );
}
