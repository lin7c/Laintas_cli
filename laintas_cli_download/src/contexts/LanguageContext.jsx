import { createContext, useContext, useState, useEffect } from 'react';

const translations = {
  zh: {
    nav: { contact: '联系', support: '支持', github: 'GitHub' },
    user: { guest: '访客', login: '登录', register: '注册', settings: '设置', logout: '退出登录' },
    theme: { light: '切换暗色主题', dark: '切换亮色主题' },
    modal: { contactTitle: '联系我们', supportTitle: '技术支持', supportUnavailable: '技术支持暂不可用，请发送邮件联系。', close: '关闭' },
    hero: { title: '下载 Laintas CLI', subtitle: '适用于 Windows 和 Linux。', webApp: '打开网页端' },
    desktop: { title: '桌面应用', desc: '快速、专注的 AI 终端体验。' },
    mobile: { title: '移动端', desc: '随时随地管理你的工作空间。' },
    download: '下载',
    curl: '或通过终端安装：',
    linux: { title: 'Linux 安装包', desc: '通过 .deb 包或脚本安装。' },
    footer: { copyright: '© 2026 Laintas', tagline: 'Precision Engineering' }
  },
  en: {
    nav: { contact: 'Contact', support: 'Support', github: 'GitHub' },
    user: { guest: 'Guest', login: 'Log in', register: 'Sign up', settings: 'Settings', logout: 'Sign out' },
    theme: { light: 'Switch to light theme', dark: 'Switch to dark theme' },
    modal: { contactTitle: 'Contact Us', supportTitle: 'Support', supportUnavailable: 'Support is currently unavailable. Please reach out via email.', close: 'Close' },
    hero: { title: 'Download Laintas CLI', subtitle: 'Available for Windows and Linux.', webApp: 'Open Web App' },
    desktop: { title: 'Desktop', desc: 'A fast and focused AI terminal experience.' },
    mobile: { title: 'Mobile', desc: 'Manage your workspace from anywhere.' },
    download: 'Download',
    curl: 'Or install via terminal:',
    linux: { title: 'Linux Package', desc: 'Install via .deb package or script.' },
    footer: { copyright: '© 2026 Laintas', tagline: 'Precision Engineering' }
  }
};

const LanguageContext = createContext(null);

export function LanguageProvider({ children }) {
  const [lang, setLang] = useState(() => {
    try { return localStorage.getItem('laintas-lang') || 'zh'; }
    catch { return 'zh'; }
  });

  useEffect(() => {
    try { localStorage.setItem('laintas-lang', lang); } catch {}
  }, [lang]);

  const toggleLang = () => setLang(l => l === 'zh' ? 'en' : 'zh');
  const t = translations[lang];

  return (
    <LanguageContext.Provider value={{ lang, toggleLang, t }}>
      {children}
    </LanguageContext.Provider>
  );
}

export function useLanguage() {
  const ctx = useContext(LanguageContext);
  if (!ctx) throw new Error('useLanguage must be used within LanguageProvider');
  return ctx;
}
