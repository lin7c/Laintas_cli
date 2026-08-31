import { useLanguage } from '../contexts/LanguageContext';
import BrandMark from './BrandMark';

// One footer for every page. It lived inside DownloadSection, so the plugin
// market grew a hand-written copy of it that drifted immediately — different
// text, an icon the original does not have. Anything added here now shows up
// on both pages, which is the only way two footers stay one footer.
const COPY = {
  zh: { docs: '阅读文档', source: '查看源码', pricing: '查看 Laintas 定价',
        footer: 'Local runtime. Observable work. Controlled execution.' },
  en: { docs: 'Read the docs', source: 'View source', pricing: 'View Laintas pricing',
        footer: 'Local runtime. Observable work. Controlled execution.' },
};

export default function SiteFooter() {
  const { lang } = useLanguage();
  const c = COPY[lang] || COPY.en;
  return (
    <footer className="product-footer page-shell">
      <BrandMark compact />
      <p>{c.footer}</p>
      <nav>
        <a href="https://laintas.com/docs" target="_blank" rel="noreferrer">{c.docs}</a>
        <a href="https://github.com/lin7c/Laintas_cli" target="_blank" rel="noreferrer">{c.source}</a>
        <a href="https://laintas.com/pricing" target="_blank" rel="noreferrer">{c.pricing}</a>
      </nav>
    </footer>
  );
}
