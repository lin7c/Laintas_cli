import { useState, useEffect, useCallback } from 'react';
import { motion } from 'framer-motion';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { useLanguage } from '@/contexts/LanguageContext';
import { useTheme } from '@/contexts/ThemeContext';

const PLATFORMS = [
  {
    id: 'windows',
    label: 'Windows',
    filename: 'laintas_cli.exe',
    arch: 'x64 · ARM64',
    size: '~86 MB',
    icon: WindowsIcon,
    desc: 'Windows 10+',
  },
  {
    id: 'linux',
    label: 'Linux',
    filename: 'laintas-cli_0.1.0_amd64.deb',
    arch: 'amd64',
    size: '~48 KB',
    icon: LinuxIcon,
    desc: 'Ubuntu 20.04+ / Debian 11+',
  },
];

function detectOS() {
  if (typeof navigator === 'undefined') return 'linux';
  const p = navigator.platform || '';
  if (p.includes('Win')) return 'windows';
  return 'linux';
}

export default function DownloadSection() {
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

  const current = PLATFORMS.find((p) => p.id === selectedOS) || PLATFORMS[0];

  return (
    <section className="relative">
      <div className="relative z-10 max-w-[960px] mx-auto px-4 pt-36 pb-20">
        {/* Hero */}
        <motion.div
          className="text-center mb-16"
          initial={{ opacity: 0, y: 24 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, ease: [0.22, 0.61, 0.36, 1] }}
        >
          <h1 className="font-display text-[42px] md:text-[52px] font-bold italic leading-[1.08] tracking-[-0.03em] mb-4"
            style={{ color: 'var(--text-primary)' }}>
            {t.hero.title}
          </h1>
          <p className="text-[17px] leading-relaxed max-w-[520px] mx-auto"
            style={{ color: 'var(--text-tertiary)', fontFamily: 'var(--font-sans)' }}>
            {t.hero.subtitle}
          </p>
        </motion.div>

        {/* Tabs */}
        <motion.div
          className="flex justify-center mb-10"
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4, delay: 0.16 }}
        >
          <Tabs value={selectedOS} onValueChange={setSelectedOS}>
            <TabsList
              className="relative rounded-xl p-1 gap-0.5"
              style={{
                background: isDark ? 'rgba(255,255,255,0.03)' : 'rgba(0,0,0,0.03)',
                border: '0.5px solid rgba(255,255,255,0.06)',
              }}
            >
              {PLATFORMS.map((os) => (
                <TabsTrigger
                  key={os.id}
                  value={os.id}
                  className="relative flex items-center gap-2 px-5 py-2.5 rounded-lg text-[14px] font-medium data-[state=active]:text-foreground"
                  style={{
                    color: os.id === selectedOS ? 'var(--text-primary)' : 'var(--text-tertiary)',
                    background: 'transparent',
                    letterSpacing: '-0.01em',
                  }}
                >
                  <os.icon />
                  <span className="hidden sm:inline">{os.label}</span>
                  {os.id === selectedOS && (
                    <motion.div
                      layoutId="platform-indicator"
                      className="absolute inset-0 rounded-lg"
                      style={{
                        background: isDark ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.04)',
                        border: '0.5px solid rgba(255,255,255,0.10)',
                      }}
                      transition={{ type: 'spring', stiffness: 420, damping: 30 }}
                    />
                  )}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        </motion.div>

        {/* Bento Grid */}
        <motion.div
          className="grid grid-cols-[1fr_300px] gap-3 mb-8"
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.45, delay: 0.22 }}
        >
          {/* Main Card — large */}
          <Card
            className="rounded-2xl overflow-hidden p-0"
            style={{
              background: isDark
                ? 'linear-gradient(160deg, rgba(255,255,255,0.02) 0%, rgba(255,255,255,0.005) 40%, rgba(255,255,255,0.01) 100%)'
                : 'linear-gradient(160deg, rgba(0,0,0,0.01) 0%, rgba(0,0,0,0.005) 40%, transparent 100%)',
              border: '0.5px solid rgba(255,255,255,0.06)',
              backdropFilter: 'blur(24px)',
            }}
          >
            <div className="p-8 md:p-10">
              <div className="flex items-center gap-3.5 mb-5">
                <div
                  className="w-10 h-10 rounded-xl flex items-center justify-center"
                  style={{
                    background: 'linear-gradient(135deg, rgba(255,255,255,0.06) 0%, rgba(255,255,255,0.02) 100%)',
                    border: '0.5px solid rgba(255,255,255,0.06)',
                  }}
                >
                  <current.icon />
                </div>
                <div>
                  <h2 className="text-[15px] font-semibold tracking-[-0.01em]" style={{ color: 'var(--text-primary)' }}>
                    {t.desktop.title}
                  </h2>
                  <p className="text-[13px] font-mono mt-0.5" style={{ color: 'var(--text-muted)' }}>
                    {current.label} · {current.arch}
                  </p>
                </div>
              </div>

              <p className="text-[14px] leading-relaxed mb-6" style={{ color: 'var(--text-tertiary)' }}>
                {t.desktop.desc}
              </p>

              <div className="flex items-center gap-3">
                <Button
                  size="lg"
                  className="rounded-xl text-[15px] font-semibold tracking-[-0.01em] px-7 h-[46px]"
                  style={{
                    background: 'linear-gradient(180deg, rgba(255,255,255,0.95) 0%, rgba(255,255,255,0.88) 100%)',
                    color: '#0c0c0c',
                    border: '0.5px solid rgba(255,255,255,0.15)',
                    boxShadow: '0 1px 2px rgba(0,0,0,0.2), inset 0 1px 0 rgba(255,255,255,0.1)',
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.background = 'linear-gradient(180deg, #fff 0%, rgba(255,255,255,0.95) 100%)';
                    e.currentTarget.style.boxShadow = '0 4px 20px rgba(255,255,255,0.12), 0 1px 2px rgba(0,0,0,0.2)';
                    e.currentTarget.style.transform = 'translateY(-1px)';
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.background = 'linear-gradient(180deg, rgba(255,255,255,0.95) 0%, rgba(255,255,255,0.88) 100%)';
                    e.currentTarget.style.boxShadow = '0 1px 2px rgba(0,0,0,0.2), inset 0 1px 0 rgba(255,255,255,0.1)';
                    e.currentTarget.style.transform = '';
                  }}
                  onClick={() => {
                    window.open(`/releases/latest/${current.filename}`, '_self');
                  }}
                >
                  <DownloadIcon />
                  {t.download} for {current.label}
                </Button>
                <Badge
                  variant="outline"
                  className="rounded-lg py-1.5 px-3 text-[11px] font-mono font-medium"
                  style={{
                    background: 'transparent',
                    border: '0.5px solid rgba(255,255,255,0.06)',
                    color: 'var(--text-muted)',
                  }}
                >
                  {current.size}
                </Badge>
              </div>
            </div>

            <div
              className="px-8 md:px-10 py-3 flex items-center gap-2 text-[12px] font-mono"
              style={{
                background: isDark ? 'rgba(255,255,255,0.015)' : 'rgba(0,0,0,0.015)',
                borderTop: '0.5px solid rgba(255,255,255,0.05)',
                color: 'var(--text-muted)',
              }}
            >
              <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
                <path d="M3 6l2 2 4-4" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              SHA256 checksum available
            </div>
          </Card>

          {/* Side Card — web app link */}
          <div className="flex flex-col gap-3">
            <a
              href="https://helpwo.laintas.com"
              target="_blank"
              rel="noopener noreferrer"
              className="flex-1 rounded-2xl p-5 text-left transition-all duration-200 group"
              style={{
                background: isDark
                  ? 'linear-gradient(160deg, rgba(255,255,255,0.015) 0%, rgba(255,255,255,0.005) 100%)'
                  : 'linear-gradient(160deg, rgba(0,0,0,0.01) 0%, transparent 100%)',
                border: '0.5px solid rgba(255,255,255,0.06)',
                backdropFilter: 'blur(24px)',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.borderColor = 'rgba(255,255,255,0.12)';
                e.currentTarget.style.background = isDark
                  ? 'linear-gradient(160deg, rgba(255,255,255,0.03) 0%, rgba(255,255,255,0.01) 100%)'
                  : 'linear-gradient(160deg, rgba(0,0,0,0.02) 0%, transparent 100%)';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.borderColor = 'rgba(255,255,255,0.06)';
                e.currentTarget.style.background = isDark
                  ? 'linear-gradient(160deg, rgba(255,255,255,0.015) 0%, rgba(255,255,255,0.005) 100%)'
                  : 'linear-gradient(160deg, rgba(0,0,0,0.01) 0%, transparent 100%)';
              }}
            >
              <div className="flex items-center gap-3 mb-3">
                <div
                  className="w-9 h-9 rounded-lg flex items-center justify-center"
                  style={{
                    background: 'linear-gradient(135deg, rgba(255,255,255,0.04) 0%, rgba(255,255,255,0.01) 100%)',
                    border: '0.5px solid rgba(255,255,255,0.06)',
                  }}
                >
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                    <path d="M2 3.5h5.5l2 2H14v7H2V3.5z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round"/>
                    <path d="M6 8l2 2 4-4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                </div>
                <div>
                  <p className="text-[14px] font-semibold tracking-[-0.01em]" style={{ color: 'var(--text-primary)' }}>
                    {t.hero.webApp}
                  </p>
                  <p className="text-[11px] font-mono mt-0.5" style={{ color: 'var(--text-muted)' }}>
                    helpwo.laintas.com
                  </p>
                </div>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-[12px] font-mono" style={{ color: 'var(--text-muted)' }}>
                  {t.hero.webApp}
                </span>
                <svg
                  width="13"
                  height="13"
                  viewBox="0 0 13 13"
                  fill="none"
                  className="transition-transform duration-200 group-hover:translate-x-0.5"
                >
                  <path
                    d="M4.5 2.5l4 4-4 4"
                    stroke="currentColor"
                    strokeWidth="1.2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    style={{ color: 'var(--text-muted)' }}
                  />
                </svg>
              </div>
            </a>
          </div>
        </motion.div>

        {/* Curl download command */}
        <CurlCommands isDark={isDark} selectedOS={selectedOS} />
      </div>
    </section>
  );
}

/* Curl download commands */
const BASE_URL = 'https://cli.laintas.com/releases/latest';

function CurlCommands({ isDark, selectedOS }) {
  const [copied, setCopied] = useState(false);
  const os = PLATFORMS.find((p) => p.id === selectedOS) || PLATFORMS[0];
  const curlCmd = `curl -o ${os.filename} ${BASE_URL}/${os.filename}`;

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(curlCmd).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [curlCmd]);

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay: 0.3 }}
    >
      <button
        onClick={handleCopy}
        className="flex items-center gap-3 p-4 rounded-xl transition-all duration-200 group text-left w-full"
        style={{
          background: isDark
            ? 'linear-gradient(160deg, rgba(255,255,255,0.015) 0%, rgba(255,255,255,0.005) 100%)'
            : 'linear-gradient(160deg, rgba(0,0,0,0.01) 0%, transparent 100%)',
          border: '0.5px solid rgba(255,255,255,0.06)',
          backdropFilter: 'blur(24px)',
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.borderColor = 'rgba(255,255,255,0.12)';
          e.currentTarget.style.background = isDark
            ? 'linear-gradient(160deg, rgba(255,255,255,0.025) 0%, rgba(255,255,255,0.01) 100%)'
            : '';
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.borderColor = 'rgba(255,255,255,0.06)';
          e.currentTarget.style.background = isDark
            ? 'linear-gradient(160deg, rgba(255,255,255,0.015) 0%, rgba(255,255,255,0.005) 100%)'
            : '';
        }}
      >
        <div
          className="w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0"
          style={{
            background: 'linear-gradient(135deg, rgba(255,255,255,0.04) 0%, rgba(255,255,255,0.01) 100%)',
            border: '0.5px solid rgba(255,255,255,0.06)',
          }}
        >
          <os.icon />
        </div>
        <div className="min-w-0 flex-1">
          <p className="text-[12px] font-mono leading-relaxed break-all" style={{ color: 'var(--text-primary)' }}>
            {curlCmd}
          </p>
        </div>
        {copied ? (
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="flex-shrink-0">
            <path d="M3 8l3 3 6-6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" style={{ color: '#10b981' }} />
          </svg>
        ) : (
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="flex-shrink-0" style={{ color: 'var(--text-muted)' }}>
            <rect x="5.5" y="3.5" width="8" height="10" rx="1" stroke="currentColor" strokeWidth="1.2" />
            <path d="M3 5.5h-.5a1 1 0 00-1 1v6a1 1 0 001 1h7a1 1 0 001-1v-.5" stroke="currentColor" strokeWidth="1.1" />
          </svg>
        )}
      </button>
    </motion.div>
  );
}

/* Icons */
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
      <path d="M4 11c-.5 1.5.5 3 2 3h4c1.5 0 2.5-1.5 2-3" stroke="currentColor" strokeWidth="1.1" fill="none" />
    </svg>
  );
}

function DownloadIcon({ className }) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className={className}>
      <path d="M8 2v8M4 7l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
      <path d="M2 11.5v1a1 1 0 001 1h10a1 1 0 001-1v-1" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
    </svg>
  );
}
