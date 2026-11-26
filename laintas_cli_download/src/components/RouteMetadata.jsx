import { useLayoutEffect } from 'react';
import { useLocation } from 'react-router-dom';
import { useLanguage } from '../contexts/LanguageContext';

const pages = {
  '/': ['Download Laintas CLI', 'Download Laintas CLI for Linux, macOS and Windows: one install command, or a direct package.'],
  '/docs': ['Laintas CLI Documentation', 'Install Laintas CLI and learn terminal commands, agent workflows, permissions, plugins, and local runtime integration.'],
  '/plugins': ['Laintas CLI Plugins', 'Explore extensions and integrations for the Laintas CLI autonomous terminal agent.'],
};
const chinesePages = {
  '/': ['下载 Laintas CLI', '下载 Laintas CLI：Linux、macOS、Windows 一行命令安装，或直接下载安装包。'],
  '/docs': ['Laintas CLI 文档', '安装、登录、命令、工作流、安全策略与插件扩展。查找 Laintas CLI 的使用说明。'],
  '/plugins': ['Laintas CLI 插件市场', '查找官方与社区插件，复制安装命令，为 Laintas CLI 添加需要的能力。'],
};

function setMeta(selector, attrs) {
  let element = document.head.querySelector(selector);
  if (!element) {
    element = document.createElement('meta');
    document.head.appendChild(element);
  }
  for (const [name, value] of Object.entries(attrs)) element.setAttribute(name, value);
}

export default function RouteMetadata() {
  const { pathname } = useLocation();
  const { lang } = useLanguage();
  useLayoutEffect(() => {
    const page = (lang === 'zh' ? chinesePages : pages)[pathname];
    if (!page) return;
    const [title, description] = page;
    const url = `https://cli.laintas.com${pathname}`;
    document.title = title;
    setMeta('meta[name="description"]', { name: 'description', content: description });
    setMeta('meta[property="og:title"]', { property: 'og:title', content: title });
    setMeta('meta[property="og:description"]', { property: 'og:description', content: description });
    setMeta('meta[property="og:url"]', { property: 'og:url', content: url });
    document.head.querySelector('link[rel="canonical"]')?.setAttribute('href', url);
  }, [pathname, lang]);
  return null;
}
