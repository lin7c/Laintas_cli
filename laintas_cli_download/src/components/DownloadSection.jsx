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
    id: 'linux',
    labels: { zh: 'Linux', en: 'Linux' },
    filename: 'laintas-cli_linux.tar.gz',
    arch: 'x86-64 · standalone',
    icon: LinuxIcon,
    desc: 'Ubuntu / Debian / RHEL · 无需 Python',
  },
  {
    id: 'macos',
    labels: { zh: 'macOS', en: 'macOS' },
    filename: 'laintas-cli_macos.tar.gz',
    arch: 'Apple Silicon · Intel',
    icon: MacIcon,
    desc: 'macOS 12+ · 需要 Python 3.10+',
  },
  {
    id: 'source',
    labels: { zh: '源码', en: 'Source' },
    filename: 'laintas-cli_source.zip',
    arch: 'macOS · Linux',
    icon: SourceIcon,
    desc: 'Python 3.10+ · source package',
  },
];

const DOWNLOAD_BASE = '/releases/v1.6';

function detectOS() {
  if (typeof navigator === 'undefined') return 'linux';
  const p = navigator.platform || '';
  const ua = navigator.userAgent || '';
  if (p.includes('Mac') || ua.includes('Mac OS X') || ua.includes('Macintosh')) return 'macos';
  return 'linux';
}

export default function DownloadSection() {
  const { t, lang } = useLanguage();
  const { theme } = useTheme();
  const isDark = theme === 'dark';
  const [selectedOS, setSelectedOS] = useState(() => {
    try { return localStorage.getItem('laintas-os') || detectOS(); }
    catch { return detectOS(); }
  });
  const [assetSizes, setAssetSizes] = useState({});

  useEffect(() => {
    try { localStorage.setItem('laintas-os', selectedOS); } catch {}
  }, [selectedOS]);

  useEffect(() => {
    let active = true;

    Promise.all(
      PLATFORMS.map(async (platform) => {
        const size = await fetchAssetSize(`${DOWNLOAD_BASE}/${platform.filename}`);
        return [platform.id, size];
      })
    ).then((entries) => {
      if (!active) return;
      setAssetSizes(Object.fromEntries(entries));
    }).catch(() => {});

    return () => {
      active = false;
    };
  }, []);

  const current = PLATFORMS.find((p) => p.id === selectedOS) || PLATFORMS[0];
  const currentLabel = getPlatformLabel(current, lang);
  const currentSize = formatBytes(assetSizes[current.id]);

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
          <h1 className="font-display text-[28px] sm:text-[38px] md:text-[52px] font-bold italic leading-[1.08] tracking-[-0.03em] mb-4"
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
                  <span className="hidden sm:inline">{getPlatformLabel(os, lang)}</span>
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
          className="grid grid-cols-1 md:grid-cols-[1fr_300px] gap-3 mb-8"
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
            <div className="p-5 sm:p-8 md:p-10">
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
                    {currentLabel} · {current.arch}
                  </p>
                </div>
              </div>

              <p className="text-[14px] leading-relaxed mb-6" style={{ color: 'var(--text-tertiary)' }}>
                {t.desktop.desc}
              </p>

              <div className="flex flex-wrap items-center gap-3">
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
                    window.open(`${DOWNLOAD_BASE}/${current.filename}`, '_self');
                  }}
                >
                  <DownloadIcon />
                  {t.download} for {currentLabel}
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
                  {currentSize}
                </Badge>
              </div>
            </div>

            <div
              className="px-5 sm:px-8 md:px-10 py-3 flex items-center gap-2 text-[12px] font-mono"
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

        {/* Platform Installation Guide */}
        {selectedOS === 'macos' && <MacGuide isDark={isDark} />}
        {selectedOS === 'linux' && <LinuxGuide isDark={isDark} />}
        {selectedOS === 'source' && <SourceGuide isDark={isDark} />}
      </div>
    </section>
  );
}

/* Curl download commands */
const BASE_URL = 'https://cli.laintas.com/releases/v1.6';

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
        className="flex items-center gap-3 p-4 rounded-xl transition-all duration-200 group text-left w-full cursor-pointer"
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

/* Shared installation guide card (used by all three platforms) */
function InstallGuide({ isDark, guide }) {
  const [expanded, setExpanded] = useState(false);

  const cardStyle = {
    background: isDark
      ? 'linear-gradient(160deg, rgba(255,255,255,0.015) 0%, rgba(255,255,255,0.005) 100%)'
      : 'linear-gradient(160deg, rgba(0,0,0,0.01) 0%, transparent 100%)',
    border: '0.5px solid rgba(255,255,255,0.06)',
    backdropFilter: 'blur(24px)',
  };

  const stepNumStyle = {
    background: 'linear-gradient(135deg, rgba(255,255,255,0.06) 0%, rgba(255,255,255,0.02) 100%)',
    border: '0.5px solid rgba(255,255,255,0.08)',
    color: 'var(--text-primary)',
  };

  const codeStyle = {
    background: isDark ? 'rgba(0,0,0,0.4)' : 'rgba(0,0,0,0.06)',
    border: `0.5px solid ${isDark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.08)'}`,
    color: 'var(--text-secondary)',
    fontFamily: 'var(--font-mono)',
  };

  const dividerStyle = `0.5px solid ${isDark ? 'rgba(255,255,255,0.05)' : 'rgba(0,0,0,0.06)'}`;

  return (
    <motion.div
      className="mt-6"
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay: 0.1 }}
    >
      <Card className="rounded-2xl overflow-hidden" style={cardStyle}>
        {/* Header — clickable to toggle */}
        <button
          onClick={() => setExpanded(!expanded)}
          className="w-full px-5 sm:px-8 md:px-10 py-5 flex items-center justify-between text-left cursor-pointer transition-colors duration-150"
          style={{ borderBottom: expanded ? dividerStyle : 'none' }}
        >
          <div className="flex items-center gap-3.5">
            <div className="w-9 h-9 rounded-lg flex items-center justify-center" style={stepNumStyle}>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                <path d="M8 1v14M1 8h14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
            </div>
            <div>
              <h3 className="text-[15px] font-semibold tracking-[-0.01em]" style={{ color: 'var(--text-primary)' }}>
                {guide.title}
              </h3>
              <p className="text-[12px] font-mono mt-0.5" style={{ color: 'var(--text-muted)' }}>
                {guide.subtitle}
              </p>
            </div>
          </div>
          <svg
            width="16" height="16" viewBox="0 0 16 16" fill="none"
            className="transition-transform duration-200 flex-shrink-0"
            style={{ transform: expanded ? 'rotate(180deg)' : 'rotate(0deg)', color: 'var(--text-muted)' }}
          >
            <path d="M4 6l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>

        {/* Steps — collapsible */}
        {expanded && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            transition={{ duration: 0.25 }}
          >
            <div className="px-5 sm:px-8 md:px-10 py-6 space-y-5">
              {guide.steps.map((step, i) => (
                <div key={i} className="flex gap-4">
                  <div
                    className="w-7 h-7 rounded-lg flex items-center justify-center flex-shrink-0 text-[12px] font-mono font-semibold mt-0.5"
                    style={stepNumStyle}
                  >
                    {i + 1}
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="text-[14px] font-medium mb-1" style={{ color: 'var(--text-primary)' }}>
                      {step.title}
                    </p>
                    <p className="text-[13px] leading-relaxed" style={{ color: 'var(--text-tertiary)' }}>
                      {step.desc}
                    </p>
                    {step.code && (
                      <CodeBlock className="mt-2" code={step.code} style={codeStyle} />
                    )}
                  </div>
                </div>
              ))}
            </div>

            {/* Shell one-liner */}
            <div
              className="px-5 sm:px-8 md:px-10 py-5"
              style={{
                background: isDark ? 'rgba(255,255,255,0.015)' : 'rgba(0,0,0,0.015)',
                borderTop: dividerStyle,
              }}
            >
              <p className="text-[12px] font-medium mb-2" style={{ color: 'var(--text-secondary)' }}>
                {guide.shellLabel}
              </p>
              <CodeBlock code={guide.shellCmd} style={codeStyle} textClassName="text-[11px] leading-relaxed whitespace-pre-wrap break-all" />
            </div>

            {/* Troubleshooting */}
            <div className="px-5 sm:px-8 md:px-10 py-5" style={{ borderTop: dividerStyle }}>
              <p className="text-[12px] font-medium mb-3" style={{ color: 'var(--text-secondary)' }}>
                {guide.troubleshootTitle}
              </p>
              <div className="space-y-2">
                {guide.troubleshootItems.map((item, i) => (
                  <div key={i} className="text-[12px] leading-relaxed" style={{ color: 'var(--text-tertiary)' }}>
                    <span className="font-mono" style={{ color: 'var(--text-secondary)' }}>{item.problem}</span>
                    <span style={{ color: 'var(--text-muted)' }}> — {item.solution}</span>
                  </div>
                ))}
              </div>
            </div>
          </motion.div>
        )}
      </Card>
    </motion.div>
  );
}

function MacGuide({ isDark }) {
  const { t } = useLanguage();
  return <InstallGuide isDark={isDark} guide={t.macGuide} />;
}

function LinuxGuide({ isDark }) {
  const { t } = useLanguage();
  return <InstallGuide isDark={isDark} guide={t.linuxGuide} />;
}

function SourceGuide({ isDark }) {
  const { t } = useLanguage();
  return <InstallGuide isDark={isDark} guide={t.sourceGuide} />;
}

function CodeBlock({ className = '', code, style, textClassName = 'text-[12px] leading-relaxed whitespace-pre-wrap break-all' }) {
  return (
    <div className={`mt-2 rounded-lg px-4 py-2.5 ${className}`.trim()} style={style}>
      <div className="flex items-start justify-between gap-3">
        <pre className={`min-w-0 flex-1 overflow-x-auto ${textClassName}`}>{code}</pre>
        <CopyButton text={code} />
      </div>
    </div>
  );
}

function CopyButton({ text }) {
  const { t } = useLanguage();
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [text]);

  return (
    <button
      onClick={handleCopy}
      className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-mono transition-colors flex-shrink-0"
      style={{ color: copied ? '#10b981' : 'var(--text-muted)' }}
    >
      {copied ? (
        <>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path d="M2 6l3 3 5-6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          {t.common.copied}
        </>
      ) : (
        <>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <rect x="4" y="4" width="7" height="7" rx="1" stroke="currentColor" strokeWidth="1.2" />
            <path d="M1 8V2a1 1 0 011-1h6" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
          {t.common.copy}
        </>
      )}
    </button>
  );
}

function getPlatformLabel(platform, lang) {
  return platform.labels?.[lang] || platform.labels?.en || platform.id;
}

async function fetchAssetSize(url) {
  try {
    const response = await fetch(url, { method: 'HEAD' });
    const size = Number(response.headers.get('content-length'));
    if (Number.isFinite(size) && size > 0) return size;
  } catch {}
  return null;
}

function formatBytes(bytes) {
  if (!bytes) return '--';
  if (bytes < 1000) return `${bytes} B`;
  if (bytes < 1000 * 1000) return `${(bytes / 1000).toFixed(1)} KB`;
  return `${(bytes / (1000 * 1000)).toFixed(1)} MB`;
}

/* Icons */
function SourceIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <path d="M8 2v8M5 7l3 3 3-3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M3 11.5v1a1 1 0 001 1h8a1 1 0 001-1v-1" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
      <path d="M3 4.5l5-2 5 2" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
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

function MacIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
      <path d="M11.2 1.6c-.7.8-1.8 1.4-2.9 1.3-.1-1.1.4-2.2 1-2.9.7-.8 1.9-1.4 2.8-1.4.1 1.1-.3 2.2-.9 3zM12.8 4.4c-1.6-.1-3 .9-3.7.9-.8 0-1.9-.9-3.2-.8-1.6 0-3.1.9-4 2.4-1.7 2.9-.4 7.2 1.2 9.6.8 1.2 1.8 2.5 3.1 2.4 1.2-.1 1.7-.8 3.2-.8s1.9.8 3.2.8c1.3 0 2.2-1.2 3-2.4.6-.9 1-1.8 1.3-2.8-3.1-1.2-3.6-5.7-.5-7.4-.9-1.2-2.3-1.9-3.6-1.9z" transform="translate(1.5, 3.5) scale(0.72)" />
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
