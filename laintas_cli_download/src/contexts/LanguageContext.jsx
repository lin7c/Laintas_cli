import { createContext, useContext, useState, useEffect } from 'react';

const translations = {
  zh: {
    common: { copy: '复制', copied: '已复制' },
    nav: { contact: '联系', support: '支持', github: 'GitHub' },
    user: { guest: '访客', login: '登录', register: '注册', settings: '设置', logout: '退出登录' },
    theme: { light: '切换暗色主题', dark: '切换亮色主题' },
    modal: { contactTitle: '联系我们', supportTitle: '技术支持', supportUnavailable: '技术支持暂不可用，请发送邮件联系。', close: '关闭' },
    hero: {
      kicker: 'Autonomous terminal agent',
      title: '把 AI 放进你的终端',
      subtitle: 'Laintas CLI 为开发者和服务器工作流而生。下载 Linux 独立版本，或使用源码包在自己的 Python 环境中运行。',
      primaryCta: '去 Laintas 官网',
      sourceCta: 'GitHub',
      webApp: '打开网页端',
    },
    desktop: { title: '终端代理', desc: '快速、专注的 AI 终端体验。' },
    mobile: { title: '移动端', desc: '随时随地管理你的工作空间。' },
    download: '下载',
    curl: '或通过终端安装：',
    linux: { title: 'Linux 安装包', desc: '下载独立二进制包。' },
    signals: { terminal: '终端安装', checksum: '校验' },
    compatibility: {
      title: '兼容性与安装说明',
      subtitle: '下载前先确认 CPU 架构和 glibc 版本。',
      rows: [
        { name: 'Linux amd64', arch: 'x86_64', detail: 'Intel / AMD 64 位处理器，glibc 2.28+。', status: '支持独立二进制' },
        { name: 'Linux arm64', arch: 'aarch64', detail: '64 位 ARM 服务器或开发板，glibc 2.28+。', status: '支持独立二进制' },
        { name: 'Linux i686', arch: 'i386 / 32 位 x86', detail: '老式 32 位系统，当前没有对应二进制包。', status: '不支持独立二进制' },
        { name: 'Alpine Linux', arch: 'musl', detail: '独立包依赖 glibc，Alpine 请使用源码包并自行安装 Python 依赖。', status: '使用源码包' },
      ],
      detectTitle: '安装前检测',
      detectCommand: 'uname -m && getconf LONG_BIT && ldd --version',
      installTitle: '推荐安装方式',
      installCommand: 'curl -fsSL https://cli.laintas.com/install.sh | bash',
      legacyTitle: '你的机器如果显示 i686',
      legacyDetail: '这表示当前 32 位 x86 环境。请使用 64 位系统/内核，或下载源码包并使用可用的 32 位 Python 环境；当前 amd64 和 arm64 包都不能运行。',
    },
    footer: { copyright: '© 2026 Laintas', tagline: 'Precision Engineering' },
    linuxGuide: {
      title: 'Linux 安装指南',
      subtitle: '独立二进制，无需 Python',
      steps: [
        {
          title: '运行安装器',
          desc: '安装器会根据机器架构自动选择 amd64 或 arm64：',
          code: 'curl -fsSL https://cli.laintas.com/install.sh | bash',
        },
        {
          title: '检查架构',
          desc: '确认当前机器的 CPU 架构和安装包匹配：',
          code: 'uname -m && file /usr/local/bin/laintas-cli',
        },
        {
          title: '启动',
          desc: '安装完成后直接运行：',
          code: 'laintas-cli',
        },
      ],
      shellLabel: '一键下载并安装：',
      shellCmd: 'curl -fsSL https://cli.laintas.com/install.sh | bash',
      moreLabel: '展开完整步骤',
      lessLabel: '收起步骤',
      troubleshootTitle: '常见问题',
      troubleshootItems: [
        { problem: '"Permission denied"', solution: '运行 chmod +x laintas-cli/laintas-cli 后重试。' },
        { problem: '"laintas-cli: command not found"', solution: '确认 /usr/local/bin 在 PATH 中，或重新打开终端。' },
        { problem: '"cannot execute binary file"', solution: '请确认下载包的架构与机器一致：amd64 或 arm64。' },
        { problem: 'glibc 版本错误', solution: '系统 glibc 需 2.28+。' },
      ],
    },
    sourceGuide: {
      title: '源码安装指南',
      subtitle: '跨平台源码包，需要 Python 3.10+',
      steps: [
        {
          title: '解压源码包',
          desc: '下载后解压到任意工作目录：',
          code: 'unzip laintas-cli_source.zip',
        },
        {
          title: '进入目录',
          desc: '切换到源码目录：',
          code: 'cd laintas-cli-source',
        },
        {
          title: '安装依赖',
          desc: '推荐使用 Python 自带的模块方式安装依赖：',
          code: 'python -m pip install -r requirements.txt',
        },
        {
          title: '直接运行',
          desc: '在源码目录中启动：',
          code: 'python laintas_cli.py',
        },
        {
          title: '可选：安装为命令',
          desc: '安装后即可使用 `laintas-cli` 命令：',
          code: 'python -m pip install .',
        },
      ],
      shellLabel: '直接运行的一行命令：',
      shellCmd: 'python -m pip install -r requirements.txt && python laintas_cli.py',
      moreLabel: '展开完整步骤',
      lessLabel: '收起步骤',
      troubleshootTitle: '常见问题',
      troubleshootItems: [
        { problem: '"python not found"', solution: 'Linux 如无 `python` 命令请改用 `python3`。' },
        { problem: '依赖安装失败', solution: '先升级 pip：`python -m pip install --upgrade pip`，再重试。' },
        { problem: '缺少可选功能', solution: 'MCP 相关功能依赖 `mcp`，如需使用请单独执行 `python -m pip install mcp`。' },
        { problem: '仍需平台专用包', solution: 'Linux 推荐独立二进制包，源码包适合调试和二次开发。' },
      ],
    },
  },
  en: {
    common: { copy: 'Copy', copied: 'Copied' },
    nav: { contact: 'Contact', support: 'Support', github: 'GitHub' },
    user: { guest: 'Guest', login: 'Log in', register: 'Sign up', settings: 'Settings', logout: 'Sign out' },
    theme: { light: 'Switch to light theme', dark: 'Switch to dark theme' },
    modal: { contactTitle: 'Contact Us', supportTitle: 'Support', supportUnavailable: 'Support is currently unavailable. Please reach out via email.', close: 'Close' },
    hero: {
      kicker: 'Autonomous terminal agent',
      title: 'Put AI in your terminal',
      subtitle: 'Laintas CLI is built for developer and server workflows. Download the standalone Linux build, or run the auditable source package in your own Python environment.',
      primaryCta: 'Laintas Website',
      sourceCta: 'GitHub',
      webApp: 'Open Web App',
    },
    desktop: { title: 'Terminal agent', desc: 'A fast and focused AI terminal experience.' },
    mobile: { title: 'Mobile', desc: 'Manage your workspace from anywhere.' },
    download: 'Download',
    curl: 'Or install via terminal:',
    linux: { title: 'Linux Package', desc: 'Download the standalone binary.' },
    signals: { terminal: 'Terminal install', checksum: 'Verification' },
    compatibility: {
      title: 'Compatibility and installation',
      subtitle: 'Check the CPU architecture and glibc version before downloading.',
      rows: [
        { name: 'Linux amd64', arch: 'x86_64', detail: '64-bit Intel or AMD processor, glibc 2.28+.', status: 'Standalone binary supported' },
        { name: 'Linux arm64', arch: 'aarch64', detail: '64-bit ARM server or development board, glibc 2.28+.', status: 'Standalone binary supported' },
        { name: 'Linux i686', arch: 'i386 / 32-bit x86', detail: 'Legacy 32-bit systems do not have a native binary package.', status: 'No standalone binary' },
        { name: 'Alpine Linux', arch: 'musl', detail: 'Standalone packages require glibc. Use the source package and install Python dependencies on Alpine.', status: 'Use source package' },
      ],
      detectTitle: 'Check before installing',
      detectCommand: 'uname -m && getconf LONG_BIT && ldd --version',
      installTitle: 'Recommended installation',
      installCommand: 'curl -fsSL https://cli.laintas.com/install.sh | bash',
      legacyTitle: 'If your machine reports i686',
      legacyDetail: 'This is a 32-bit x86 environment. Use a 64-bit system/kernel, or install from source with a working 32-bit Python environment; neither amd64 nor arm64 binaries can run there.',
    },
    footer: { copyright: '© 2026 Laintas', tagline: 'Precision Engineering' },
    linuxGuide: {
      title: 'Linux Installation Guide',
      subtitle: 'Standalone binary — no Python required',
      steps: [
        {
          title: 'Run the installer',
          desc: 'The installer selects amd64 or arm64 automatically:',
          code: 'curl -fsSL https://cli.laintas.com/install.sh | bash',
        },
        {
          title: 'Check the architecture',
          desc: 'Confirm that the installed binary matches the machine:',
          code: 'uname -m && file /usr/local/bin/laintas-cli',
        },
        {
          title: 'Launch',
          desc: 'Once installed, run from anywhere:',
          code: 'laintas-cli',
        },
      ],
      shellLabel: 'One-liner download and install:',
      shellCmd: 'curl -fsSL https://cli.laintas.com/install.sh | bash',
      moreLabel: 'Show full steps',
      lessLabel: 'Collapse steps',
      troubleshootTitle: 'Troubleshooting',
      troubleshootItems: [
        { problem: '"Permission denied"', solution: 'Run chmod +x laintas-cli/laintas-cli and try again.' },
        { problem: '"laintas-cli: command not found"', solution: 'Ensure /usr/local/bin is in your PATH, or open a new terminal.' },
        { problem: '"cannot execute binary file"', solution: 'Confirm that the package architecture matches the machine: amd64 or arm64.' },
        { problem: 'glibc version error', solution: 'Requires glibc 2.28 or newer.' },
      ],
    },
    sourceGuide: {
      title: 'Source Installation Guide',
      subtitle: 'Cross-platform source package — requires Python 3.10+',
      steps: [
        {
          title: 'Extract the source archive',
          desc: 'After downloading, unpack it into any working directory:',
          code: 'unzip laintas-cli_source.zip',
        },
        {
          title: 'Enter the directory',
          desc: 'Switch into the extracted source tree:',
          code: 'cd laintas-cli-source',
        },
        {
          title: 'Install dependencies',
          desc: 'Use Python module mode to install the runtime dependencies:',
          code: 'python -m pip install -r requirements.txt',
        },
        {
          title: 'Run from source',
          desc: 'Launch directly from the source directory:',
          code: 'python laintas_cli.py',
        },
        {
          title: 'Optional: install as a command',
          desc: 'This installs the `laintas-cli` entry point:',
          code: 'python -m pip install .',
        },
      ],
      shellLabel: 'One-line run command:',
      shellCmd: 'python -m pip install -r requirements.txt && python laintas_cli.py',
      moreLabel: 'Show full steps',
      lessLabel: 'Collapse steps',
      troubleshootTitle: 'Troubleshooting',
      troubleshootItems: [
        { problem: '"python not found"', solution: 'On Linux, use `python3` if `python` is unavailable.' },
        { problem: 'Dependency install fails', solution: 'Upgrade pip first with `python -m pip install --upgrade pip`, then retry.' },
        { problem: 'Optional features are missing', solution: 'MCP-related features require `mcp`; install it separately with `python -m pip install mcp`.' },
        { problem: 'You need a platform package instead', solution: 'Linux is best served by the standalone binary, and the source package is best for debugging or local modification.' },
      ],
    },
  }
};

const LanguageContext = createContext(null);

export function LanguageProvider({ children }) {
  const [lang, setLang] = useState(() => {
    try { return localStorage.getItem('laintas-lang') || 'en'; }
    catch { return 'en'; }
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
