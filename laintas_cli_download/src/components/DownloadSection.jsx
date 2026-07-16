import { useCallback, useEffect, useMemo, useState } from 'react';
import { motion } from 'framer-motion';
import {
  ArrowRight,
  Check,
  Copy,
  Download,
  ExternalLink,
  FileCode2,
  Package,
  ShieldCheck,
  Terminal,
} from 'lucide-react';
import { useLanguage } from '@/contexts/LanguageContext';

const DOWNLOAD_BASE = 'https://cli.laintas.com/releases/v1.8.3';
const BASE_URL = DOWNLOAD_BASE;
const RELEASE_VERSION = 'v1.8.3';

const ASSETS = [
  {
    id: 'linux',
    label: { zh: 'Linux 版本', en: 'Linux Build' },
    eyebrow: { zh: '独立二进制', en: 'Standalone binary' },
    downloads: [
      {
        id: 'amd64',
        filename: 'laintas-cli_linux_amd64.tar.gz',
        label: { zh: '下载 amd64', en: 'Download amd64' },
      },
      {
        id: 'arm64',
        filename: 'laintas-cli_linux_arm64.tar.gz',
        label: { zh: '下载 arm64', en: 'Download arm64' },
      },
    ],
    meta: { zh: 'amd64 / arm64 · 无需 Python · glibc 2.28+', en: 'amd64 / arm64 · no Python required · glibc 2.28+' },
    icon: Package,
    accent: '#0f9f6e',
  },
  {
    id: 'source',
    label: { zh: '源码包', en: 'Source' },
    eyebrow: { zh: '可审计、可修改', en: 'Auditable and editable' },
    filename: 'laintas-cli_source.zip',
    meta: { zh: 'Python 3.10+ · 适合调试和二次开发', en: 'Python 3.10+ · best for debugging and modification' },
    icon: FileCode2,
    accent: '#c9382f',
  },
];

export default function DownloadSection() {
  const { t, lang } = useLanguage();
  const [assetSizes, setAssetSizes] = useState({});

  useEffect(() => {
    let active = true;
    Promise.all(
      ASSETS.flatMap((asset) => (asset.downloads || [{ id: 'default', filename: asset.filename }])
        .map(async (download) => {
          const size = await fetchAssetSize(`${DOWNLOAD_BASE}/${download.filename}`);
          return [`${asset.id}:${download.id}`, size];
        }))
    ).then((entries) => {
      if (active) setAssetSizes(Object.fromEntries(entries));
    }).catch(() => {});
    return () => { active = false; };
  }, []);

  return (
    <main className="download-page relative min-h-screen overflow-hidden bg-white text-[#10110f]">
      <BackgroundGrid />

      <section className="relative mx-auto flex min-h-screen w-full max-w-[1180px] flex-col px-5 pb-16 pt-28 sm:px-8 lg:px-10">
        <motion.div
          className="grid flex-1 items-center gap-12 lg:grid-cols-[0.96fr_1.04fr]"
          initial={{ opacity: 0, y: 18 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.55, ease: [0.22, 0.61, 0.36, 1] }}
        >
          <div className="max-w-[620px]">
            <BrandMark size="large" />
            <p className="mt-9 font-mono text-[12px] uppercase tracking-[0.34em] text-[#74776f]">
              {t.hero.kicker}
            </p>
            <h1 className="mt-5 max-w-[760px] font-display text-[54px] font-semibold leading-[0.92] tracking-normal text-[#10110f] sm:text-[76px] lg:text-[96px]">
              {t.hero.title}
            </h1>
            <p className="mt-7 max-w-[540px] text-[18px] leading-8 text-[#555951] sm:text-[20px]">
              {t.hero.subtitle}
            </p>

            <div className="mt-10 flex flex-wrap items-center gap-3">
              <a
                href="https://laintas.com"
                target="_blank"
                rel="noopener noreferrer"
                className="group inline-flex h-13 items-center gap-3 rounded-[8px] bg-[#10110f] px-6 text-[15px] font-semibold text-white shadow-[0_20px_48px_rgba(16,17,15,0.18)] transition duration-200 hover:-translate-y-0.5 hover:bg-[#20221f]"
              >
                <ExternalLink size={18} strokeWidth={2.2} />
                {t.hero.primaryCta}
                <ArrowRight size={17} className="transition group-hover:translate-x-0.5" />
              </a>
              <a
                href="https://github.com/lin7c/Laintas_cli"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex h-13 items-center gap-3 rounded-[8px] border border-[#d7d9d2] bg-white px-6 text-[15px] font-semibold text-[#10110f] transition duration-200 hover:-translate-y-0.5 hover:border-[#10110f]"
              >
                <FileCode2 size={18} />
                {t.hero.sourceCta}
              </a>
            </div>
          </div>

          <div className="relative">
            <div className="absolute -left-8 top-10 hidden h-[calc(100%-80px)] w-px bg-[#d8dbd3] lg:block" />
            <div className="space-y-4">
              {ASSETS.map((asset, index) => (
                <DownloadPanel
                  key={asset.id}
                  asset={asset}
                  lang={lang}
                  size={formatBytes(assetSizes[asset.id])}
                  delay={0.14 + index * 0.08}
                />
              ))}
            </div>

            <div className="mt-5 grid gap-4 sm:grid-cols-2">
              <Signal icon={Terminal} label={t.signals.terminal} value="curl / tar / python" />
              <Signal icon={ShieldCheck} label={t.signals.checksum} value="SHA256" />
            </div>
          </div>
        </motion.div>

        <motion.div
          className="mt-12 grid gap-4 border-t border-[#d8dbd3] pt-6 lg:grid-cols-[1fr_1fr]"
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.45, delay: 0.28 }}
        >
          <InstallGuide guide={t.linuxGuide} asset={ASSETS[0]} />
          <InstallGuide guide={t.sourceGuide} asset={ASSETS[1]} />
        </motion.div>

        <CompatibilityGuide content={t.compatibility} />
      </section>
    </main>
  );
}

function BackgroundGrid() {
  return (
    <div aria-hidden="true" className="pointer-events-none absolute inset-0">
      <div
        className="absolute inset-0 opacity-[0.42]"
        style={{
          backgroundImage:
            'linear-gradient(#e8ebe3 1px, transparent 1px), linear-gradient(90deg, #e8ebe3 1px, transparent 1px)',
          backgroundSize: '44px 44px',
          maskImage: 'linear-gradient(to bottom, black, transparent 78%)',
        }}
      />
      <div className="absolute right-[-16vw] top-[10vh] h-[38vw] w-[38vw] rounded-full border border-[#e3e5df]" />
      <div className="absolute bottom-[8vh] left-[-9vw] h-[28vw] w-[28vw] rounded-full border border-[#e3e5df]" />
    </div>
  );
}

function BrandMark({ size = 'normal' }) {
  const large = size === 'large';
  return (
    <div
      className={`inline-flex items-center justify-center rounded-[8px] border border-[#d8dbd3] bg-white font-mono font-black shadow-[0_18px_42px_rgba(16,17,15,0.08)] ${large ? 'h-20 w-20 text-[42px]' : 'h-10 w-10 text-[22px]'}`}
      aria-label="Laintas CLI"
    >
      <span className="text-[#d83b32]">&gt;</span>
      <span className="text-[#18a266]">/</span>
    </div>
  );
}

function DownloadPanel({ asset, lang, size, delay }) {
  const { t } = useLanguage();
  const Icon = asset.icon;
  const label = localize(asset.label, lang);
  const eyebrow = localize(asset.eyebrow, lang);
  const meta = localize(asset.meta, lang);

  return (
    <motion.article
      className="group relative overflow-hidden rounded-[8px] border border-[#d8dbd3] bg-white p-6 shadow-[0_18px_60px_rgba(16,17,15,0.08)] transition duration-200 hover:-translate-y-1 hover:border-[#10110f]"
      initial={{ opacity: 0, x: 18 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.45, delay }}
    >
      <div className="absolute right-0 top-0 h-full w-1.5" style={{ background: asset.accent }} />
      <div className="flex items-start justify-between gap-5">
        <div className="flex min-w-0 gap-4">
          <div
            className="flex h-12 w-12 flex-shrink-0 items-center justify-center rounded-[8px]"
            style={{ background: `${asset.accent}12`, color: asset.accent }}
          >
            <Icon size={23} strokeWidth={2.1} />
          </div>
          <div className="min-w-0">
            <p className="font-mono text-[11px] uppercase tracking-[0.22em] text-[#74776f]">
              {eyebrow}
            </p>
            <h2 className="mt-1 text-[25px] font-semibold tracking-normal text-[#10110f]">
              {label}
            </h2>
            <p className="mt-2 text-[14px] leading-6 text-[#5b5f56]">
              {meta}
            </p>
          </div>
        </div>
        <span className="rounded-[6px] border border-[#e0e2dc] px-2.5 py-1 font-mono text-[11px] text-[#74776f]">
          {RELEASE_VERSION}
        </span>
      </div>

      <div className="mt-7 flex flex-wrap items-center gap-3">
        {(asset.downloads || [{ id: 'default', filename: asset.filename, label: { zh: t.download, en: t.download } }]).map((download) => (
          <div key={download.id} className="flex items-center gap-2">
            <a
              href={`${DOWNLOAD_BASE}/${download.filename}`}
              className="inline-flex h-11 items-center gap-2 rounded-[8px] bg-[#10110f] px-4 text-[14px] font-semibold text-white transition hover:bg-[#20221f]"
            >
              <Download size={16} />
              {localize(download.label, lang)}
            </a>
            <CopyCommand command={`curl -fL -o ${download.filename} ${BASE_URL}/${download.filename}`} compact />
          </div>
        ))}
      </div>
    </motion.article>
  );
}

function Signal({ icon: Icon, label, value }) {
  return (
    <div className="rounded-[8px] border border-[#d8dbd3] bg-white px-4 py-3">
      <div className="flex items-center gap-2 text-[#74776f]">
        <Icon size={15} />
        <span className="font-mono text-[11px] uppercase tracking-[0.18em]">{label}</span>
      </div>
      <p className="mt-2 text-[14px] font-semibold text-[#10110f]">{value}</p>
    </div>
  );
}

function CompatibilityGuide({ content }) {
  return (
    <section className="mt-8 rounded-[8px] border border-[#d8dbd3] bg-white p-5 shadow-[0_18px_42px_rgba(16,17,15,0.05)] sm:p-6">
      <div className="flex flex-col gap-1 border-b border-[#e3e5df] pb-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="text-[22px] font-semibold text-[#10110f]">{content.title}</h2>
          <p className="mt-1 text-[14px] text-[#696d65]">{content.subtitle}</p>
        </div>
        <code className="font-mono text-[12px] text-[#74776f]">{RELEASE_VERSION}</code>
      </div>

      <div className="mt-5 overflow-x-auto">
        <table className="w-full min-w-[680px] border-collapse text-left text-[13px]">
          <thead>
            <tr className="border-b border-[#e3e5df] text-[#74776f]">
              <th className="pb-3 pr-4 font-mono text-[11px] uppercase tracking-[0.14em]">Platform</th>
              <th className="pb-3 pr-4 font-mono text-[11px] uppercase tracking-[0.14em]">Architecture</th>
              <th className="pb-3 pr-4 font-mono text-[11px] uppercase tracking-[0.14em]">Requirements</th>
              <th className="pb-3 font-mono text-[11px] uppercase tracking-[0.14em]">Status</th>
            </tr>
          </thead>
          <tbody>
            {content.rows.map((row) => (
              <tr key={row.name} className="border-b border-[#eef0eb] last:border-0">
                <td className="py-3 pr-4 font-semibold text-[#10110f]">{row.name}</td>
                <td className="py-3 pr-4 font-mono text-[#555951]">{row.arch}</td>
                <td className="py-3 pr-4 text-[#696d65]">{row.detail}</td>
                <td className="py-3 font-semibold text-[#0f8f62]">{row.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="mt-5 grid gap-4 border-t border-[#e3e5df] pt-5 lg:grid-cols-2">
        <div>
          <p className="mb-2 text-[12px] font-semibold uppercase tracking-[0.14em] text-[#74776f]">{content.detectTitle}</p>
          <CodeBlock code={content.detectCommand} />
        </div>
        <div>
          <p className="mb-2 text-[12px] font-semibold uppercase tracking-[0.14em] text-[#74776f]">{content.installTitle}</p>
          <CodeBlock code={content.installCommand} />
        </div>
      </div>

      <div className="mt-4 rounded-[8px] border border-[#f0d6d1] bg-[#fff8f6] px-4 py-3">
        <p className="text-[13px] font-semibold text-[#9e342c]">{content.legacyTitle}</p>
        <p className="mt-1 text-[13px] leading-6 text-[#71443e]">{content.legacyDetail}</p>
      </div>
    </section>
  );
}

function InstallGuide({ guide, asset }) {
  const [open, setOpen] = useState(false);
  const visibleSteps = useMemo(() => open ? guide.steps : guide.steps.slice(0, 2), [guide.steps, open]);
  const Icon = asset.icon;

  return (
    <section className="rounded-[8px] border border-[#d8dbd3] bg-white p-5">
      <div className="flex items-start gap-3">
        <div
          className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-[8px]"
          style={{ background: `${asset.accent}12`, color: asset.accent }}
        >
          <Icon size={19} />
        </div>
        <div className="min-w-0">
          <h3 className="text-[18px] font-semibold text-[#10110f]">{guide.title}</h3>
          <p className="mt-1 text-[13px] leading-6 text-[#696d65]">{guide.subtitle}</p>
        </div>
      </div>

      <div className="mt-5 space-y-4">
        {visibleSteps.map((step, index) => (
          <div key={`${step.title}-${index}`} className="grid grid-cols-[28px_1fr] gap-3">
            <div className="flex h-7 w-7 items-center justify-center rounded-[6px] bg-[#f2f3ef] font-mono text-[12px] font-semibold text-[#555951]">
              {index + 1}
            </div>
            <div className="min-w-0">
              <p className="text-[14px] font-semibold text-[#10110f]">{step.title}</p>
              <p className="mt-1 text-[13px] leading-6 text-[#696d65]">{step.desc}</p>
              {step.code && <CodeBlock code={step.code} />}
            </div>
          </div>
        ))}
      </div>

      <div className="mt-5 border-t border-[#e3e5df] pt-4">
        <p className="mb-2 text-[12px] font-semibold uppercase tracking-[0.16em] text-[#74776f]">
          {guide.shellLabel}
        </p>
        <CodeBlock code={guide.shellCmd} />
      </div>

      {guide.steps.length > 2 && (
        <button
          onClick={() => setOpen((value) => !value)}
          className="mt-4 inline-flex h-9 items-center gap-2 rounded-[8px] border border-[#d8dbd3] px-3 text-[13px] font-semibold text-[#10110f] transition hover:border-[#10110f]"
        >
          {open ? guide.lessLabel : guide.moreLabel}
          <ArrowRight size={14} className={open ? '-rotate-90' : 'rotate-90'} />
        </button>
      )}
    </section>
  );
}

function CodeBlock({ code }) {
  return (
    <div className="mt-2 flex items-start gap-2 rounded-[8px] border border-[#d8dbd3] bg-[#f7f8f4] px-3 py-2.5">
      <pre className="min-w-0 flex-1 overflow-x-auto whitespace-pre-wrap break-all font-mono text-[12px] leading-5 text-[#333630]">
        {code}
      </pre>
      <CopyCommand command={code} compact />
    </div>
  );
}

function CopyCommand({ command, compact = false }) {
  const { t } = useLanguage();
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(command).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    });
  }, [command]);

  return (
    <button
      type="button"
      onClick={handleCopy}
      className={`inline-flex items-center gap-2 rounded-[8px] border border-[#d8dbd3] bg-white font-semibold text-[#10110f] transition hover:border-[#10110f] ${compact ? 'h-8 px-2 text-[12px]' : 'h-11 px-4 text-[13px]'}`}
      title={t.common.copy}
    >
      {copied ? <Check size={compact ? 14 : 16} color="#18a266" /> : <Copy size={compact ? 14 : 16} />}
      {!compact && (copied ? t.common.copied : t.common.copy)}
    </button>
  );
}

function localize(value, lang) {
  return value?.[lang] || value?.en || '';
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
