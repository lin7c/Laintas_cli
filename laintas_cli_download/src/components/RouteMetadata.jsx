import { useLayoutEffect } from 'react';
import { useLocation } from 'react-router-dom';
import { useLanguage } from '../contexts/LanguageContext';

const pages = {
  '/': ['Laintas CLI — Agent Runtime for Terminal Operations', 'An autonomous coding agent in your terminal. Run commands, edit files, and manage agent workflows with observable controls.'],
  '/docs': ['Laintas CLI Documentation', 'Install Laintas CLI and learn terminal commands, agent workflows, permissions, plugins, and local runtime integration.'],
  '/plugins': ['Laintas CLI Plugins', 'Explore extensions and integrations for the Laintas CLI autonomous terminal agent.'],
};
const chinesePages = {
  '/': ['Laintas CLI — 命令照常敲，其余交给 agent', '在熟悉的终端里运行命令，让 agent 读代码、改文件、跑测试。执行过程看得见，权限由你掌握。'],
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
