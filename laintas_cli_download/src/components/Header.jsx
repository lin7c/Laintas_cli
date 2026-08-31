import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { AnimatePresence, motion } from 'framer-motion';
import { ChevronDown, LogIn, LogOut, Moon, Settings, Sun, UserRound } from 'lucide-react';
import { useLanguage } from '../contexts/LanguageContext';
import { useTheme } from '../contexts/ThemeContext';
import { useAuth } from '../contexts/AuthContext';
import { BrandMark } from './DownloadSection';

const NAV = {
  zh: [['流程', '/#workflow'], ['运维能力', '/#operations'], ['安全边界', '/#security'], ['下载', '/#download'], ['插件市场', '/plugins']],
  en: [['Workflow', '/#workflow'], ['Operations', '/#operations'], ['Security', '/#security'], ['Download', '/#download'], ['Plugin Market', '/plugins']],
};

export default function Header() {
  const { lang, toggleLang, t } = useLanguage();
  const { theme, toggleTheme } = useTheme();
  const auth = useAuth();
  const { data: session, isPending } = auth.useSession();
  const [userOpen, setUserOpen] = useState(false);
  const [balance, setBalance] = useState(null);
  const menuRef = useRef(null);

  useEffect(() => {
    const close = (event) => { if (menuRef.current && !menuRef.current.contains(event.target)) setUserOpen(false); };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, []);

  useEffect(() => {
    if (!session) { setBalance(null); return; }
    fetch('/api/balance', { credentials: 'include' }).then((response) => response.json()).then((data) => {
      if (data.balance != null) setBalance(data.balance);
    }).catch(() => {});
  }, [session]);

  async function signOut() {
    setUserOpen(false);
    try { await auth.signOut(); } finally { window.location.replace('https://accounts.laintas.com/login'); }
  }

  const loggedIn = Boolean(session);
  return (
    <header className="site-header">
      <div className="header-inner">
        <Link to="/" className="header-brand"><BrandMark compact /><span>laintas_cli</span><small>v1.18</small></Link>
        <nav className="header-nav" aria-label="Product navigation">
          {NAV[lang].map(([label, href]) => (href.startsWith('/#')
            ? <a href={href} key={href}>{label}</a>
            : <Link to={href} key={href}>{label}</Link>))}
        </nav>
        <div className="header-actions">
          <a className="pricing-link" href="https://laintas.com/pricing" target="_blank" rel="noreferrer">{lang === 'zh' ? '定价' : 'Pricing'}</a>
          <button className="round-control text-control" onClick={toggleLang} aria-label={lang === 'zh' ? 'Switch to English' : '切换到中文'}>{lang === 'zh' ? 'EN' : '中'}</button>
          <button className="round-control" onClick={toggleTheme} aria-label={theme === 'dark' ? t.theme.light : t.theme.dark}>{theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}</button>
          <div className="user-control" ref={menuRef}>
            <button className="user-trigger" onClick={() => setUserOpen((open) => !open)} aria-label={t.user.guest} aria-expanded={userOpen}>
              {isPending ? <span className="loading-dot" /> : loggedIn ? <span className="user-initial">{(session.user?.name || session.user?.email || '?').charAt(0).toUpperCase()}</span> : <UserRound size={15} />}
              <ChevronDown size={12} />
            </button>
            <AnimatePresence>
              {userOpen && <motion.div className="user-menu" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 6 }} transition={{ duration: 0.15 }}>
                {loggedIn ? <>
                  <div className="user-meta"><strong>{session.user?.name || t.user.guest}</strong><span>{session.user?.email}</span>{balance != null && <code>${(balance / 100).toFixed(2)}</code>}</div>
                  <a href="https://laintas.com/settings" onClick={() => setUserOpen(false)}><Settings size={14} />{t.user.settings}</a>
                  <button onClick={signOut}><LogOut size={14} />{t.user.logout}</button>
                </> : <>
                  <div className="user-meta"><strong>{t.user.guest}</strong><span>guest@laintas.com</span></div>
                  <a href="/api/sso/login?return_to=%2F"><LogIn size={14} />{t.user.login}</a>
                  <a href="/api/sso/login?mode=register&return_to=%2F"><UserRound size={14} />{t.user.register}</a>
                </>}
              </motion.div>}
            </AnimatePresence>
          </div>
        </div>
      </div>
    </header>
  );
}
