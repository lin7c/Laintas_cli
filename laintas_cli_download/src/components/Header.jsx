import { useState, useRef, useEffect } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { motion, AnimatePresence } from 'framer-motion';
import { useLanguage } from '../contexts/LanguageContext';
import { useTheme } from '../contexts/ThemeContext';
import { useAuth } from '../contexts/AuthContext';

export default function Header() {
  const { lang, toggleLang, t } = useLanguage();
  const { theme, toggleTheme } = useTheme();
  const auth = useAuth();
  const { data: session, isPending } = auth.useSession();
  const navigate = useNavigate();

  const [userOpen, setUserOpen] = useState(false);
  const [modalContent, setModalContent] = useState(null);
  const [balance, setBalance] = useState(null);
  const menuRef = useRef(null);

  useEffect(() => {
    const handleClick = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) setUserOpen(false);
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  useEffect(() => {
    if (!session) { setBalance(null); return; }
    fetch('/api/balance', { credentials: 'include' })
      .then(r => r.json())
      .then(d => { if (d.balance != null) setBalance(d.balance); })
      .catch(() => {});
  }, [session]);

  async function handleSignOut() {
    setUserOpen(false);
    try {
      await auth.signOut();
      navigate('/', { replace: true });
    } catch {}
  }

  const navItems = [
    { key: 'contact', action: 'modal', icon: MailIcon },
    { key: 'support', action: 'modal', icon: BookIcon },
    { key: 'github', action: 'external', href: 'https://github.com/lin7c/laintas_cli_pre', icon: GitHubIcon },
  ];

  const isLoggedIn = !!session;

  return (
    <header className="fixed top-0 left-0 right-0 z-50">
      <div className="mx-5 mt-4 px-6 py-2.5 rounded-2xl glass flex items-center justify-between"
        style={{ border: '1px solid var(--card-border)' }}>
        {/* Left: Brand */}
        <Link to="/" className="flex items-center gap-2 flex-shrink-0 group">
          <span style={{
            display: 'inline-block', width: '7px', height: '7px',
            borderRadius: '50%', background: 'var(--accent-soft)',
            boxShadow: '0 0 8px var(--accent-glow)',
          }} />
          <span className="font-display text-lg font-semibold tracking-tight italic"
            style={{ color: 'var(--text-primary)' }}>
            laintas_cli
          </span>
        </Link>


        {/* Right: Controls */}
        <div className="flex items-center gap-1.5">
          <button
            onClick={toggleLang}
            className="w-8 h-8 rounded-full flex items-center justify-center font-mono text-[0.65rem] font-semibold tracking-wider header-link"
            title={lang === 'zh' ? 'Switch to English' : '切换到中文'}
          >
            {lang === 'zh' ? 'EN' : '中'}
          </button>

          <button
            onClick={toggleTheme}
            className="w-8 h-8 rounded-full flex items-center justify-center header-link"
            style={{ color: 'var(--text-tertiary)' }}
            title={theme === 'dark' ? t.theme.light : t.theme.dark}
          >
            {theme === 'dark' ? (
              <svg width="15" height="15" viewBox="0 0 16 16" fill="none">
                <circle cx="8" cy="8" r="3" stroke="currentColor" strokeWidth="1.3"/>
                <path d="M8 1v1.5M8 13.5V15M15 8h-1.5M2.5 8H1M12.95 3.05l-1.06 1.06M4.11 11.89l-1.06 1.06M12.95 12.95l-1.06-1.06M4.11 4.11L3.05 3.05" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round"/>
              </svg>
            ) : (
              <svg width="15" height="15" viewBox="0 0 16 16" fill="none">
                <path d="M13.5 10.5A6 6 0 011.5 6.5 5 5 0 0013.5 10.5z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round"/>
              </svg>
            )}
          </button>

          {/* User menu */}
          <div className="relative" ref={menuRef}>
            <button
              onClick={() => setUserOpen(!userOpen)}
              className="w-8 h-8 rounded-full flex items-center justify-center header-link"
              style={{ color: 'var(--text-tertiary)' }}
            >
              {isPending ? (
                <div className="w-3.5 h-3.5 border-2 border-current border-t-transparent rounded-full animate-spin opacity-40" />
              ) : isLoggedIn ? (
                <div className="w-7 h-7 rounded-full flex items-center justify-center font-mono text-xs font-semibold"
                  style={{
                    background: 'var(--accent-dim)',
                    color: 'var(--accent-soft)',
                    border: '1px solid var(--border-accent)',
                  }}>
                  {(session.user?.name || session.user?.email || '?').charAt(0).toUpperCase()}
                </div>
              ) : (
                <svg width="15" height="15" viewBox="0 0 14 14" fill="none">
                  <circle cx="7" cy="4.5" r="2.5" stroke="currentColor" strokeWidth="1.2"/>
                  <path d="M2 12.5c0-2.5 2.2-4.5 5-4.5s5 2 5 4.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
                </svg>
              )}
            </button>

            <AnimatePresence>
              {userOpen && (
                <motion.div
                  initial={{ opacity: 0, y: 6, scale: 0.97 }}
                  animate={{ opacity: 1, y: 0, scale: 1 }}
                  exit={{ opacity: 0, y: 6, scale: 0.97 }}
                  transition={{ duration: 0.15 }}
                  className="absolute right-0 top-full mt-2 w-52 rounded-xl glass p-1.5"
                  style={{
                    background: 'var(--bg-raised)',
                    border: '1px solid var(--border-strong)',
                    boxShadow: '0 16px 48px rgba(0,0,0,0.4)',
                  }}
                >
                  {isPending ? (
                    <div className="px-3 py-5 text-center">
                      <div className="w-4 h-4 mx-auto border-2 border-current border-t-transparent rounded-full animate-spin opacity-40" />
                    </div>
                  ) : isLoggedIn ? (
                    <>
                      <div className="px-3.5 py-2.5 mb-1"
                        style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                        <p className="text-sm font-medium" style={{ color: 'var(--text-primary)' }}>
                          {session.user?.name || t.user.guest}
                        </p>
                        <p className="text-xs mt-0.5 font-mono" style={{ color: 'var(--text-muted)' }}>
                          {session.user?.email}
                        </p>
                      </div>
                      <a
                        href="https://laintas.com/settings"
                        onClick={() => setUserOpen(false)}
                        className="block w-full text-left px-3.5 py-2.5 rounded-lg text-sm transition-colors"
                        style={{ color: 'var(--text-secondary)' }}
                        onMouseEnter={e => { e.target.style.background = 'var(--bg-hover)'; e.target.style.color = 'var(--text-primary)'; }}
                        onMouseLeave={e => { e.target.style.background = ''; e.target.style.color = 'var(--text-secondary)'; }}
                      >
                        {t.user.settings}
                      </a>
                      <div className="pt-1 mt-1" style={{ borderTop: '1px solid var(--border-subtle)' }}>
                        <button
                          onClick={handleSignOut}
                          className="w-full text-left px-3.5 py-2.5 rounded-lg text-sm transition-colors"
                          style={{ color: 'var(--text-tertiary)' }}
                          onMouseEnter={e => { e.target.style.background = 'var(--bg-hover)'; e.target.style.color = 'var(--danger)'; }}
                          onMouseLeave={e => { e.target.style.background = ''; e.target.style.color = 'var(--text-tertiary)'; }}
                        >
                          {t.user.logout}
                        </button>
                      </div>
                    </>
                  ) : (
                    <>
                      <div className="px-3.5 py-2.5 mb-1"
                        style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                        <p className="text-sm font-medium" style={{ color: 'var(--text-primary)' }}>
                          {t.user.guest}
                        </p>
                        <p className="text-xs mt-0.5 font-mono" style={{ color: 'var(--text-muted)' }}>
                          guest@laintas.com
                        </p>
                      </div>
                      <Link
                        to="/login"
                        onClick={() => setUserOpen(false)}
                        className="block w-full text-left px-3.5 py-2.5 rounded-lg text-sm transition-colors"
                        style={{ color: 'var(--text-secondary)' }}
                        onMouseEnter={e => { e.target.style.background = 'var(--bg-hover)'; e.target.style.color = 'var(--text-primary)'; }}
                        onMouseLeave={e => { e.target.style.background = ''; e.target.style.color = 'var(--text-secondary)'; }}
                      >
                        {t.user.login}
                      </Link>
                      <Link
                        to="/register"
                        onClick={() => setUserOpen(false)}
                        className="block w-full text-left px-3.5 py-2.5 rounded-lg text-sm transition-colors"
                        style={{ color: 'var(--text-secondary)' }}
                        onMouseEnter={e => { e.target.style.background = 'var(--bg-hover)'; e.target.style.color = 'var(--text-primary)'; }}
                        onMouseLeave={e => { e.target.style.background = ''; e.target.style.color = 'var(--text-secondary)'; }}
                      >
                        {t.user.register}
                      </Link>
                    </>
                  )}
                </motion.div>
              )}
            </AnimatePresence>
          </div>

          {isLoggedIn && (
            <div className="hidden sm:flex items-center gap-1 px-2 py-1 rounded-full text-xs font-mono font-semibold"
              style={{ background: 'var(--accent-dim)', color: 'var(--accent-soft)', border: '1px solid var(--border-accent)' }}>
              <span>$</span>
              <span>{balance != null ? (balance / 100).toFixed(2) : '0.00'}</span>
            </div>
          )}
        </div>
      </div>

      {/* Modal */}
      <AnimatePresence>
        {modalContent && (
          <div className="fixed inset-0 z-[100] flex items-center justify-center p-4">
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.15 }}
              className="absolute inset-0 bg-black/60 backdrop-blur-sm"
              onClick={() => setModalContent(null)}
            />
            <motion.div
              initial={{ opacity: 0, scale: 0.96, y: 10 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.96, y: 10 }}
              transition={{ duration: 0.2 }}
              className="relative rounded-2xl p-7 max-w-sm w-full text-center"
              style={{
                background: 'var(--bg-raised)',
                border: '1px solid var(--border-strong)',
                boxShadow: '0 24px 64px rgba(0,0,0,0.5)',
              }}
            >
              <h3 className="font-display text-xl font-semibold italic mb-4"
                style={{ color: 'var(--text-primary)' }}>
                {modalContent === 'contact' ? t.modal.contactTitle : t.modal.supportTitle}
              </h3>
              {modalContent === 'contact' ? (
                <a
                  href="mailto:contact@laintas.com"
                  className="font-mono text-sm break-all transition-colors"
                  style={{ color: 'var(--accent-soft)' }}
                >
                  contact@laintas.com
                </a>
              ) : (
                <p style={{ color: 'var(--text-tertiary)' }}>{t.modal.supportUnavailable}</p>
              )}
              <button
                onClick={() => setModalContent(null)}
                className="mt-5 px-6 py-2 rounded-full text-sm font-medium transition-all"
                style={{
                  background: 'var(--bg-hover)',
                  color: 'var(--text-secondary)',
                  border: '1px solid var(--border-subtle)',
                }}
              >
                {t.modal.close}
              </button>
            </motion.div>
          </div>
        )}
      </AnimatePresence>
    </header>
  );
}

function MailIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <rect x="1.5" y="3" width="11" height="8" rx="1.5" stroke="currentColor" strokeWidth="1.2"/>
      <path d="M1.5 4.5l5.5 4 5.5-4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}
function BookIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M7 11.5V3M2 1.5h5l2.5 2.5L12 1.5h.5a.5.5 0 01.5.5v10a.5.5 0 01-.5.5H2a.5.5 0 01-.5-.5V2a.5.5 0 01.5-.5z" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}
function GitHubIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M8 .5c.7 0 1.3.3 1.7.8 1-.2 2-.4 2.8-.8-.3.8-.8 1.5-1.3 2 .4 3.1-1 5.5-4.2 5.5S3 5.6 3.4 2.5c-.5-.5-1-1.2-1.3-2 .8.4 1.7.6 2.8.8.4-.5 1-.8 1.7-.8z" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}
