import { useState, useEffect } from 'react';
import { motion } from 'framer-motion';
import { useLanguage } from '../contexts/LanguageContext';
import { useTheme } from '../contexts/ThemeContext';

const OS_OPTIONS = [
  {
    id: 'windows',
    label: 'Windows',
    ext: 'exe',
    icon: WindowsIcon,
    arch: 'x64 / ARM64',
  },
  {
    id: 'linux',
    label: 'Linux',
    ext: 'deb',
    icon: LinuxIcon,
    arch: 'amd64 / arm64',
  },
];

export default function DownloadPage() {
  const { t } = useLanguage();
  const { theme } = useTheme();
  const isDark = theme === 'dark';

  const [selectedOS, setSelectedOS] = useState(() => {
    try { return localStorage.getItem('laintas-os') || detectOS(); }
    catch { return detectOS(); }
  });

  useEffect(() => {
    try { localStorage.setItem('laintas-os', selectedOS); } catch {}
  }, [selectedOS]);

  const current = OS_OPTIONS.find(o => o.id === selectedOS) || OS_OPTIONS[0];

  return (
    <div className="relative min-h-screen">
      <Background isDark={isDark} />

      <main className="relative z-10 pt-40 pb-20 px-4">
        <div className="max-w-[680px] mx-auto">
          {/* ─── Hero ─── */}
          <HeroSection t={t} isDark={isDark} />

          {/* ─── Platform Tabs ─── */}
          <PlatformTabs
            options={OS_OPTIONS}
            selected={selectedOS}
            onSelect={setSelectedOS}
          />

          {/* ─── Main Download Card ─── */}
          <DownloadCard current={current} t={t} isDark={isDark} />

          {/* ─── Web app links ─── */}
          <WebAppLinks t={t} isDark={isDark} />

          {/* ─── Script install ─── */}
          <ScriptInstall t={t} isDark={isDark} current={current} />

          {/* ─── Divider ─── */}
          <div className="my-16 flex items-center gap-4" aria-hidden>
            <div className="flex-1 h-px" style={{ background: 'var(--border-subtle)' }} />
            <span className="font-mono text-[0.65rem] tracking-widest uppercase" style={{ color: 'var(--text-muted)' }}>
              {t.hero.webApp}
            </span>
            <div className="flex-1 h-px" style={{ background: 'var(--border-subtle)' }} />
          </div>

          {/* ─── Web app link ─── */}
          <div className="text-center">
            <a
              href="https://helpwo.laintas.com"
              className="inline-flex items-center gap-2 px-6 py-3 rounded-xl text-sm font-medium transition-all duration-200"
              style={{
                background: 'var(--bg-hover)',
                color: 'var(--text-secondary)',
                border: '1px solid var(--border-subtle)',
              }}
              onMouseEnter={e => {
                e.currentTarget.style.background = 'var(--accent-dim)';
                e.currentTarget.style.color = 'var(--accent-soft)';
                e.currentTarget.style.borderColor = 'var(--border-accent)';
              }}
              onMouseLeave={e => {
                e.currentTarget.style.background = 'var(--bg-hover)';
                e.currentTarget.style.color = 'var(--text-secondary)';
                e.currentTarget.style.borderColor = 'var(--border-subtle)';
              }}
            >
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <path d="M7 1v8M3 5l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                <path d="M2 10v1.5a.5.5 0 00.5.5h9a.5.5 0 00.5-.5V10" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
              </svg>
              {t.hero.webApp}
            </a>
          </div>
        </div>
      </main>

      {/* ─── Footer ─── */}
      <Footer t={t} />
    </div>
  );
}

/* ─── Hero ─── */
function HeroSection({ t, isDark }) {
  return (
    <motion.div
      className="text-center mb-14"
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.6, ease: [0.25, 0.46, 0.45, 0.94] }}
    >
      {/* App icon */}
      <motion.div
        className="mx-auto mb-8 w-16 h-16 rounded-2xl flex items-center justify-center"
        style={{
          background: `linear-gradient(135deg, ${isDark ? 'rgba(201,149,62,0.2)' : 'rgba(201,149,62,0.15)'}, ${isDark ? 'rgba(201,149,62,0.06)' : 'rgba(201,149,62,0.04)'})`,
          border: '1px solid var(--border-accent)',
          boxShadow: isDark
            ? '0 0 32px rgba(201,149,62,0.15), 0 0 64px rgba(201,149,62,0.06)'
            : '0 0 32px rgba(201,149,62,0.1), 0 0 64px rgba(201,149,62,0.03)',
        }}
        initial={{ scale: 0.9, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        transition={{ duration: 0.5, delay: 0.1, ease: [0.25, 0.46, 0.45, 0.94] }}
      >
        <TerminalIcon />
      </motion.div>

      {/* Title */}
      <h1 className="font-display text-4xl md:text-5xl font-bold italic tracking-tight mb-4"
        style={{ color: 'var(--text-primary)' }}>
        {t.hero.title}
      </h1>

      {/* Subtitle */}
      <p className="text-base md:text-lg"
        style={{ color: 'var(--text-tertiary)', fontFamily: 'var(--font-sans)' }}>
        {t.hero.subtitle}
      </p>
    </motion.div>
  );
}

/* ─── Platform Tabs ─── */
function PlatformTabs({ options, selected, onSelect }) {
  return (
    <motion.div
      className="flex justify-center gap-1.5 mb-8"
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay: 0.2 }}
    >
      <div
        className="inline-flex rounded-xl p-1"
        style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-subtle)' }}
      >
        {options.map((os) => {
          const isActive = os.id === selected;
          return (
            <button
              key={os.id}
              onClick={() => onSelect(os.id)}
              className="relative flex items-center gap-2 px-5 py-2.5 rounded-lg text-sm font-medium transition-colors duration-200"
              style={{
                fontFamily: 'var(--font-sans)',
                color: isActive ? 'var(--text-primary)' : 'var(--text-tertiary)',
                background: isActive ? 'var(--bg-hover)' : 'transparent',
              }}
            >
              <os.icon />
              <span className="hidden sm:inline">{os.label}</span>
              {isActive && (
                <motion.div
                  layoutId="platform-tab"
                  className="absolute inset-0 rounded-lg"
                  style={{ border: '1px solid var(--border-accent)' }}
                  transition={{ type: 'spring', stiffness: 380, damping: 28 }}
                />
              )}
            </button>
          );
        })}
      </div>
    </motion.div>
  );
}

/* ─── Main Download Card ─── */
function DownloadCard({ current, t, isDark }) {
  return (
    <motion.div
      className="rounded-2xl overflow-hidden mb-8"
      style={{
        background: 'var(--card-bg)',
        border: '1px solid var(--card-border)',
        backdropFilter: 'blur(24px)',
      }}
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay: 0.25 }}
    >
      <div className="p-8 md:p-10">
        {/* Platform header */}
        <div className="flex items-center gap-3 mb-6">
          <div className="w-10 h-10 rounded-xl flex items-center justify-center"
            style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-subtle)' }}>
            <current.icon />
          </div>
          <div>
            <h2 className="font-sans text-lg font-semibold" style={{ color: 'var(--text-primary)' }}>
              {t.desktop.title}
            </h2>
            <p className="text-sm font-mono" style={{ color: 'var(--text-muted)' }}>
              {current.label} &middot; {current.arch}
            </p>
          </div>
        </div>

        <p className="text-sm leading-relaxed mb-6" style={{ color: 'var(--text-tertiary)' }}>
          {t.desktop.desc}
        </p>

        {/* Download button */}
        <a
          href={
            current.id === 'windows' ? '/releases/v1.4/laintas_cli.exe' :
            current.id === 'linux' ? '/releases/v1.4/laintas-cli_1.4.0_amd64.deb' :
            `/releases/v1.4/laintas_cli.${current.ext}`
          }
            className="inline-flex items-center gap-2.5 px-8 py-3.5 rounded-xl text-base font-semibold transition-all duration-200"
            style={{
              background: 'var(--accent-soft)',
              color: isDark ? '#121211' : '#fafaf7',
              fontFamily: 'var(--font-sans)',
            }}
            onMouseEnter={e => {
              e.currentTarget.style.transform = 'translateY(-1px)';
              e.currentTarget.style.boxShadow = `0 8px 32px ${isDark ? 'rgba(201,149,62,0.35)' : 'rgba(201,149,62,0.3)'}`;
            }}
            onMouseLeave={e => {
              e.currentTarget.style.transform = '';
              e.currentTarget.style.boxShadow = '';
            }}
          >
            <DownloadIcon />
            {t.download} for {current.label}
          </a>
      </div>

      {/* Bottom strip */}
      <div className="px-8 md:px-10 py-3 flex items-center gap-1.5 text-xs font-mono"
        style={{
          background: 'var(--bg-hover)',
          borderTop: '1px solid var(--border-subtle)',
          color: 'var(--text-muted)',
        }}>
        <ChecksumIcon />
        <span>SHA256 checksum available</span>
      </div>
    </motion.div>
  );
}

/* ─── Web app links ─── */
function WebAppLinks({ t, isDark }) {
  return (
    <motion.div
      className="grid grid-cols-1 gap-3 mb-8"
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay: 0.3 }}
    >
      <a
        href="https://helpwo.laintas.com"
        target="_blank"
        rel="noopener noreferrer"
        className="flex items-center gap-3 p-4 rounded-xl transition-all duration-200 text-left"
        style={{
          background: 'var(--bg-hover)',
          border: '1px solid var(--border-subtle)',
        }}
        onMouseEnter={e => {
          e.currentTarget.style.background = 'var(--bg-raised)';
          e.currentTarget.style.borderColor = 'var(--border-strong)';
        }}
        onMouseLeave={e => {
          e.currentTarget.style.background = 'var(--bg-hover)';
          e.currentTarget.style.borderColor = 'var(--border-subtle)';
        }}
      >
        <div className="w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0"
          style={{ background: 'var(--bg-root)', border: '1px solid var(--border-subtle)' }}>
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
            <path d="M2 3.5h5.5l2 2H14v7H2V3.5z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round"/>
            <path d="M6 8l2 2 4-4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </div>
        <div>
          <p className="text-sm font-medium" style={{ color: 'var(--text-primary)' }}>{t.hero.webApp}</p>
          <p className="text-xs font-mono" style={{ color: 'var(--text-muted)' }}>helpwo.laintas.com</p>
        </div>
        <svg className="ml-auto flex-shrink-0" width="12" height="12" viewBox="0 0 12 12" fill="none">
          <path d="M4 2l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"
            style={{ color: 'var(--text-muted)' }} />
        </svg>
      </a>
    </motion.div>
  );
}

/* ─── Script Install ─── */
function ScriptInstall({ t, isDark, current }) {
  const [copied, setCopied] = useState(false);
  const installCmd = current.id === 'windows'
    ? 'curl -LO https://helpwo.laintas.com/releases/v1.4/laintas_cli.exe'
    : current.id === 'linux'
    ? 'curl -LO https://helpwo.laintas.com/releases/v1.4/laintas-cli_1.4.0_amd64.deb && sudo dpkg -i laintas-cli_1.4.0_amd64.deb'
    : 'curl -fsSL https://cli.laintas.com/install.sh | bash';

  const handleCopy = () => {
    navigator.clipboard.writeText(installCmd).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };

  return (
    <motion.div
      className="rounded-xl overflow-hidden"
      style={{
        border: '1px solid var(--border-subtle)',
        background: isDark ? 'rgba(0,0,0,0.2)' : 'rgba(0,0,0,0.02)',
      }}
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay: 0.35 }}
    >
      <div className="flex items-center justify-between px-4 py-2"
        style={{ borderBottom: '1px solid var(--border-subtle)' }}>
        <span className="text-xs font-mono tracking-wider uppercase" style={{ color: 'var(--text-muted)' }}>
          {t.curl}
        </span>
        <button
          onClick={handleCopy}
          className="flex items-center gap-1.5 text-xs font-mono font-medium transition-colors duration-200 px-2 py-1 rounded-md"
          style={{ color: copied ? 'var(--success)' : 'var(--text-tertiary)' }}
        >
          {copied ? (
            <>
              <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
                <path d="M2 6l3 3 5-6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
              Copied
            </>
          ) : (
            <>
              <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
                <rect x="4" y="4" width="7" height="7" rx="1" stroke="currentColor" strokeWidth="1.2"/>
                <path d="M1 8V2a1 1 0 011-1h6" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
              </svg>
              Copy
            </>
          )}
        </button>
      </div>
      <pre className="px-4 py-3.5 text-sm font-mono overflow-x-auto"
        style={{ color: 'var(--text-secondary)' }}>
        <code>{installCmd}</code>
      </pre>
    </motion.div>
  );
}

/* ─── Footer ─── */
function Footer({ t }) {
  return (
    <footer className="relative z-10 pb-10 text-center">
      <div className="inline-flex items-center gap-3 px-5 py-2 rounded-full"
        style={{
          background: 'var(--card-bg)',
          border: '1px solid var(--card-border)',
        }}>
        <span className="font-mono text-[0.65rem] tracking-widest uppercase"
          style={{ color: 'var(--text-muted)' }}>
          {t.footer.copyright}
        </span>
        <span style={{
          display: 'inline-block', width: '3px', height: '3px',
          borderRadius: '50%', background: 'var(--accent-soft)',
        }} />
        <span className="font-mono text-[0.65rem] tracking-widest uppercase"
          style={{ color: 'var(--text-muted)' }}>
          {t.footer.tagline}
        </span>
      </div>
    </footer>
  );
}

/* ─── Background ─── */
function Background({ isDark }) {
  return (
    <>
      <div className="fixed inset-0 z-0 transition-all duration-1000"
        style={{
          background: isDark
            ? 'radial-gradient(ellipse 80% 50% at 50% -20%, rgba(201,149,62,0.04) 0%, transparent 50%), radial-gradient(ellipse 60% 40% at 80% 80%, rgba(201,149,62,0.03) 0%, transparent 50%), #121211'
            : 'radial-gradient(ellipse 80% 50% at 50% -20%, rgba(201,149,62,0.06) 0%, transparent 50%), radial-gradient(ellipse 60% 40% at 80% 80%, rgba(201,149,62,0.04) 0%, transparent 50%), #fafaf7',
        }}
      />
      <div className="fixed inset-0 z-[1] pointer-events-none opacity-[0.025]"
        style={{
          backgroundImage: `url("data:image/svg+xml,%3Csvg width='60' height='60' viewBox='0 0 60 60' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M60 0H0v60' fill='none' stroke='%23c9953e' stroke-width='0.3'/%3E%3C/svg%3E")`,
          maskImage: 'radial-gradient(ellipse 80% 60% at 50% 40%, black 30%, transparent 70%)',
          WebkitMaskImage: 'radial-gradient(ellipse 80% 60% at 50% 40%, black 30%, transparent 70%)',
        }}
      />
      <div className="fixed z-[1] pointer-events-none transition-all duration-1000"
        style={{
          top: '-15%', left: '50%', transform: 'translateX(-50%)',
          width: '900px', height: '500px',
          background: isDark
            ? 'radial-gradient(ellipse at center, rgba(201,149,62,0.06) 0%, transparent 70%)'
            : 'radial-gradient(ellipse at center, rgba(201,149,62,0.08) 0%, transparent 70%)',
          filter: 'blur(80px)',
        }}
      />
    </>
  );
}

/* ─── OS detection ─── */
function detectOS() {
  if (typeof navigator === 'undefined') return 'linux';
  const p = navigator.platform || '';
  if (p.includes('Win')) return 'windows';
  return 'linux';
}

/* ─── Icons ─── */
function TerminalIcon() {
  return (
    <svg width="28" height="28" viewBox="0 0 28 28" fill="none">
      <rect x="2" y="4" width="24" height="20" rx="3" stroke="currentColor" strokeWidth="1.5"
        style={{ color: 'var(--accent-soft)' }} />
      <path d="M6 9l4 5-4 5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"
        style={{ color: 'var(--accent-soft)' }} />
      <path d="M13 19h5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"
        style={{ color: 'var(--accent-soft)' }} />
    </svg>
  );
}

function WindowsIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
      <path d="M0 2.5l6.5-1v6.5H0V2.5zM7.5 1.5l8-1V8h-8V1.5zM0 9h6.5v6L0 14V9zM7.5 9H15.5v6.5l-8-1V9z" />
    </svg>
  );
}

function LinuxIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
      <path d="M8 1c-.5 0-1 .2-1.3.5-.2.2-.4.5-.4.8 0 .3.1.6.2.9-.5.2-1 .5-1.3 1-.2.3-.4.6-.4 1s.2.7.4 1c.3.5.7.8 1.2 1-.1.3-.1.5 0 .8.2.3.5.5.9.5.4 0 .7-.2.9-.5.1-.3.1-.5 0-.8.5-.2.9-.5 1.2-1 .2-.3.4-.6.4-1s-.2-.7-.4-1c-.3-.5-.8-.8-1.3-1 .1-.3.2-.6.2-.9 0-.3-.1-.6-.4-.8C9 1.2 8.5 1 8 1z" />
      <circle cx="7" cy="6.5" r="1" />
      <circle cx="9" cy="6.5" r="1" />
      <path d="M7.5 8c-.3 0-.5.2-.5.5v1c0 .3.2.5.5.5s.5-.2.5-.5v-1c0-.3-.2-.5-.5-.5z" />
      <path d="M4 11c-.5 1.5.5 3 2 3h4c1.5 0 2.5-1.5 2-3" stroke="currentColor" strokeWidth="1.2" fill="none" />
    </svg>
  );
}

function DownloadIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <path d="M8 2v8M4 7l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
      <path d="M2 11.5v1a1 1 0 001 1h10a1 1 0 001-1v-1" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
    </svg>
  );
}

function ChecksumIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
      <path d="M3 6l2 2 4-4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}
