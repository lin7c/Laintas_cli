import ExtensionsSection from '../components/ExtensionsSection';
import SiteFooter from '../components/SiteFooter';

export default function ExtensionsPage() {
  return (
    <main id="main-content" tabIndex={-1} className="product-page">
      <ExtensionsSection standalone />
      <SiteFooter compact />
    </main>
  );
}
