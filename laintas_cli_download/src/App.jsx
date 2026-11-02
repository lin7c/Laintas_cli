import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { lazy, Suspense } from 'react';
import RouteMetadata from './components/RouteMetadata';
import RouteNavigation from './components/RouteNavigation';
import { MotionConfig } from 'framer-motion';
import { ThemeProvider } from './contexts/ThemeContext';
import { LanguageProvider } from './contexts/LanguageContext';
import { AuthProvider } from './contexts/AuthContext';
import Header from './components/Header';
import DownloadSection from './components/DownloadSection';
import { useLanguage } from './contexts/LanguageContext';
import { useMotionPreference } from './hooks/useMotionPreference';

const ExtensionsPage = lazy(() => import('./pages/ExtensionsPage'));
const DocsPage = lazy(() => import('./pages/DocsPage'));
const LoginPage = lazy(() => import('./pages/LoginPage'));
const RegisterPage = lazy(() => import('./pages/RegisterPage'));
const SettingsPage = lazy(() => import('./pages/SettingsPage'));

function AppContent() {
  const { lang } = useLanguage();
  return (
    <BrowserRouter>
      <RouteMetadata />
      <RouteNavigation />
      <Header />
      <Suspense fallback={<div className="route-loading" role="status"><span className="loading-dot" aria-hidden="true" />{lang === 'zh' ? '正在打开页面…' : 'Opening page…'}</div>}><Routes>
        <Route path="/" element={
          <div className="relative min-h-screen">
            <DownloadSection />
          </div>
        } />
        {/* /plugins, not /extensions: the registry and the extension packages
            themselves are served from the static /extensions/ directory, so
            that path resolves to a directory listing (403), not this page —
            client-side navigation would work and a refresh or a shared link
            would not. */}
        <Route path="/plugins" element={
          <div className="relative min-h-screen">
            <ExtensionsPage />
          </div>
        } />
        <Route path="/docs" element={
          <div className="relative min-h-screen">
            <DocsPage />
          </div>
        } />
        <Route path="/login" element={
          <div className="relative min-h-screen">
            <main id="main-content" tabIndex={-1} className="relative z-10">
              <LoginPage />
            </main>
          </div>
        } />
        <Route path="/register" element={
          <div className="relative min-h-screen">
            <main id="main-content" tabIndex={-1} className="relative z-10">
              <RegisterPage />
            </main>
          </div>
        } />
        <Route path="/settings" element={
          <div className="relative min-h-screen">
            <main id="main-content" tabIndex={-1} className="relative z-10">
              <SettingsPage />
            </main>
          </div>
        } />
      </Routes></Suspense>
    </BrowserRouter>
  );
}

export default function App() {
  const reduceMotion = useMotionPreference();
  return (
    <MotionConfig reducedMotion={reduceMotion ? 'always' : 'never'}><ThemeProvider>
      <LanguageProvider>
        <AuthProvider>
          <AppContent />
        </AuthProvider>
      </LanguageProvider>
    </ThemeProvider></MotionConfig>
  );
}
