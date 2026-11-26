import * as NavReact from 'react';
import { createNavigationPreferences } from '../../../../laintas/src/components/navigation-preferences/react.js';
import { useEffect, useRef, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { ArrowUpRight, ChevronDown, LogIn, LogOut, Menu, Settings, UserRound, X } from 'lucide-react';
import { useLanguage } from '../contexts/LanguageContext';
import { useTheme } from '../contexts/ThemeContext';
import { useAuth } from '../contexts/AuthContext';
import { useRelease } from '../hooks/useRelease';
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel } from './ui/dropdown-menu';
import BrandMark from './BrandMark';

const NavigationPreferences = createNavigationPreferences(NavReact);
const NAV = {
  zh: [['功能', '/#operations'], ['安全', '/#security'], ['价格', '/#pricing'], ['下载', '/#download'], ['文档', '/docs'], ['插件市场', '/plugins']],
  en: [['Features', '/#operations'], ['Security', '/#security'], ['Pricing', '/#pricing'], ['Download', '/#download'], ['Docs', '/docs'], ['Plugins', '/plugins']],
};

export default function Header() {
  const { lang, toggleLang, t } = useLanguage();
  const { theme, toggleTheme } = useTheme();
  const auth = useAuth();
  const { data: session, isPending } = auth.useSession();
  const [balance, setBalance] = useState(null);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [userOpen, setUserOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const version = useRelease();
  const location = useLocation();
  const headerRef = useRef(null);
  const mobileTrigger = useRef(null);

  useEffect(() => { setMobileOpen(false); setUserOpen(false); }, [location.pathname, location.hash]);
  useEffect(() => {
    if (!mobileOpen) return;
    const close = event => {
      if (event.type === 'keydown') {
        if (event.key !== 'Escape') return;
        mobileTrigger.current?.focus();
      } else if (headerRef.current?.contains(event.target)) return;
      setMobileOpen(false);
    };
    const desktop = window.matchMedia('(min-width: 1101px)');
    const resize = () => { if (desktop.matches) setMobileOpen(false); };
    document.addEventListener('pointerdown', close);
    document.addEventListener('focusin', close);
    document.addEventListener('keydown', close);
    desktop.addEventListener('change', resize);
    return () => {
      document.removeEventListener('pointerdown', close);
      document.removeEventListener('focusin', close);
      document.removeEventListener('keydown', close);
      desktop.removeEventListener('change', resize);
    };
  }, [mobileOpen]);

  useEffect(() => {
    setBalance(null);
    if (!session) return;
    const controller = new AbortController();
    fetch('/api/balance', { credentials: 'include', signal: controller.signal })
      .then(response => response.ok ? response.json() : Promise.reject())
      .then(data => { if (!controller.signal.aborted && data.balance != null) setBalance(data.balance); }).catch(() => {});
    return () => controller.abort();
  }, [session]);

  async function signOut() {
    setSigningOut(true);
    try { await auth.signOut(); } finally { window.location.replace('https://accounts.laintas.com/login'); }
  }

  const navLinks = NAV[lang].map(([label, href]) => {
    const current = href.includes('#') ? location.pathname + location.hash === href : location.pathname === href;
    return <Link to={href} key={href} aria-current={current ? 'location' : undefined} onClick={() => {
      setMobileOpen(false);
      if (current && href.includes('#')) document.getElementById(href.split('#')[1])?.scrollIntoView({ block: 'start' });
    }}>{label}<ArrowUpRight aria-hidden="true" size={15} /></Link>;
  });
  const returnTo = encodeURIComponent(location.pathname + location.search + location.hash);
  const loggedIn = Boolean(session);
  return <header className="site-header" ref={headerRef}>
    <div className="header-inner">
      <Link to="/" className="header-brand" aria-label="Laintas CLI"><BrandMark compact /><span translate="no">laintas_cli</span><small>{version}</small></Link>
      <nav className="header-nav" aria-label={lang === 'zh' ? '主导航' : 'Main navigation'}>{navLinks}</nav>
      <div className="header-actions">
        <NavigationPreferences language={lang} theme={theme} onLanguageChange={next => { if (next !== lang) toggleLang(); }} onThemeChange={toggleTheme} />
        <DropdownMenu open={userOpen} onOpenChange={open => { setUserOpen(open); if (open) setMobileOpen(false); }}>
          <DropdownMenuTrigger asChild>
            <button type="button" className="user-trigger" aria-label={lang === 'zh' ? '账户菜单' : 'Account menu'}>
              {isPending ? <span className="loading-dot" /> : loggedIn ? <span className="user-initial">{(session.user?.name || session.user?.email || '?').charAt(0).toUpperCase()}</span> : <UserRound aria-hidden="true" size={16} />}
              <ChevronDown aria-hidden="true" size={12} />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" sideOffset={12}>
            <DropdownMenuLabel><div className="account-meta"><strong>{loggedIn ? session.user?.name || t.user.guest : t.user.guest}</strong><span>{loggedIn ? session.user?.email : lang === 'zh' ? '登录后查看账户与用量' : 'Sign in to view your account'}</span>{balance != null && <code>{new Intl.NumberFormat(lang === 'zh' ? 'zh-CN' : 'en-US', { style: 'currency', currency: 'USD' }).format(balance / 100)}</code>}</div></DropdownMenuLabel>
            <DropdownMenuGroup>
              {loggedIn ? <>
                <DropdownMenuItem asChild><a href="https://laintas.com/settings"><Settings aria-hidden="true" />{t.user.settings}</a></DropdownMenuItem>
                <DropdownMenuItem disabled={signingOut} onSelect={event => { event.preventDefault(); void signOut(); }}><LogOut aria-hidden="true" />{signingOut ? (lang === 'zh' ? '正在退出…' : 'Signing out…') : t.user.logout}</DropdownMenuItem>
              </> : <>
                <DropdownMenuItem asChild><a href={`/api/sso/login?return_to=${returnTo}`}><LogIn aria-hidden="true" />{t.user.login}</a></DropdownMenuItem>
                <DropdownMenuItem asChild><a href={`/api/sso/login?mode=register&return_to=${returnTo}`}><UserRound aria-hidden="true" />{t.user.register}</a></DropdownMenuItem>
              </>}
            </DropdownMenuGroup>
          </DropdownMenuContent>
        </DropdownMenu>
        <button type="button" ref={mobileTrigger} className="mobile-nav-trigger" aria-expanded={mobileOpen} aria-controls="mobile-navigation" aria-label={lang === 'zh' ? (mobileOpen ? '关闭导航' : '打开导航') : (mobileOpen ? 'Close navigation' : 'Open navigation')} onClick={() => { setMobileOpen(open => !open); setUserOpen(false); }}>{mobileOpen ? <X size={20} /> : <Menu size={20} />}</button>
      </div>
    </div>
    <nav id="mobile-navigation" className="mobile-navigation" hidden={!mobileOpen} aria-label={lang === 'zh' ? '移动端导航' : 'Mobile navigation'}>{navLinks}</nav>
  </header>;
}
