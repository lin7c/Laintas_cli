import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { ThemeProvider, useTheme } from './contexts/ThemeContext';
import { LanguageProvider } from './contexts/LanguageContext';
import { AuthProvider } from './contexts/AuthContext';
import Header from './components/Header';
import DownloadSection from './components/DownloadSection';
import ExtensionsPage from './pages/ExtensionsPage';
import LoginPage from './pages/LoginPage';
import RegisterPage from './pages/RegisterPage';
import SettingsPage from './pages/SettingsPage';

function AppContent() {
  const { theme } = useTheme();
  const isDark = theme === 'dark';

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={
          <div className="relative min-h-screen">
            <Header />
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
            <Header />
            <ExtensionsPage />
          </div>
        } />
        <Route path="/login" element={
          <div className="relative min-h-screen">
            <Header />
            <main className="relative z-10">
              <LoginPage />
            </main>
          </div>
        } />
        <Route path="/register" element={
          <div className="relative min-h-screen">
            <Header />
            <main className="relative z-10">
              <RegisterPage />
            </main>
          </div>
        } />
        <Route path="/settings" element={
          <div className="relative min-h-screen">
            <Header />
            <main className="relative z-10">
              <SettingsPage />
            </main>
          </div>
        } />
      </Routes>
    </BrowserRouter>
  );
}

export default function App() {
  return (
    <ThemeProvider>
      <LanguageProvider>
        <AuthProvider>
          <AppContent />
        </AuthProvider>
      </LanguageProvider>
    </ThemeProvider>
  );
}
