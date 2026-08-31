import ExtensionsSection from '../components/ExtensionsSection';
import { BrandMark } from '../components/DownloadSection';
import { useLanguage } from '../contexts/LanguageContext';
import { ExternalLink } from 'lucide-react';

const COPY = {
  zh: { footer: '插件市场 · 官方与社区扩展', docs: '文档', source: '源码', pricing: '定价' },
  en: { footer: 'Plugin market · official and community extensions', docs: 'Docs', source: 'Source', pricing: 'Pricing' },
};

export default function ExtensionsPage() {
  const { lang } = useLanguage();
  const c = COPY[lang] || COPY.en;

  return (
    <main className="product-page">
      <ExtensionsSection standalone />
      <footer className="product-footer page-shell">
        <BrandMark compact />
        <p>{c.footer}</p>
        <nav>
          <a href="https://laintas.com/docs" target="_blank" rel="noreferrer">{c.docs}</a>
          <a href="https://github.com/lin7c/Laintas_cli" target="_blank" rel="noreferrer">{c.source}<ExternalLink size={12} /></a>
          <a href="https://laintas.com/pricing" target="_blank" rel="noreferrer">{c.pricing}</a>
        </nav>
      </footer>
    </main>
  );
}
