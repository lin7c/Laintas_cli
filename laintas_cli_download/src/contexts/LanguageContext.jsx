import { createContext, useContext, useState, useEffect } from 'react';

const translations = {
  zh: {
    common: { copy: '复制', copied: '已复制' },
    nav: { contact: '联系', support: '支持', github: 'GitHub' },
    user: { guest: '访客', login: '登录', register: '注册', settings: '设置', logout: '退出登录' },
    theme: { light: '切换暗色主题', dark: '切换亮色主题' },
    modal: { contactTitle: '联系我们', supportTitle: '技术支持', supportUnavailable: '技术支持暂不可用，请发送邮件联系。', close: '关闭' },
    hero: { title: '下载 Laintas CLI', subtitle: '适用于 Windows、macOS 和 Linux。', webApp: '打开网页端' },
    desktop: { title: '桌面应用', desc: '快速、专注的 AI 终端体验。' },
    mobile: { title: '移动端', desc: '随时随地管理你的工作空间。' },
    download: '下载',
    curl: '或通过终端安装：',
    linux: { title: 'Linux 安装包', desc: '通过 .deb 包或脚本安装。' },
    footer: { copyright: '© 2026 Laintas', tagline: 'Precision Engineering' },
    linuxGuide: {
      title: 'Linux 安装指南',
      subtitle: '独立二进制，无需 Python',
      steps: [
        {
          title: '下载并解压',
          desc: '下载 tar.gz 后在终端中解压：',
          code: 'tar xzf laintas-cli_linux.tar.gz',
        },
        {
          title: '安装（推荐）',
          desc: '运行安装脚本，将二进制文件复制到 /usr/local/bin：',
          code: 'sudo ./laintas-cli/install.sh',
        },
        {
          title: '或直接运行（无需安装）',
          desc: '也可以不安装，直接在解压目录运行：',
          code: './laintas-cli/laintas-cli',
        },
        {
          title: '启动',
          desc: '安装后直接运行：',
          code: 'laintas-cli',
        },
      ],
      shellLabel: '一键下载并安装：',
      shellCmd: 'curl -fsSL https://cli.laintas.com/install.sh | bash',
      troubleshootTitle: '常见问题',
      troubleshootItems: [
        { problem: '"Permission denied"', solution: '运行 chmod +x laintas-cli/laintas-cli 后重试。' },
        { problem: '"laintas-cli: command not found"', solution: '确认 /usr/local/bin 在 PATH 中，或重新打开终端。' },
        { problem: '"cannot execute binary file"', solution: '此包为 x86-64 架构，ARM 架构暂不支持独立二进制，请从源码运行。' },
        { problem: 'glibc 版本错误', solution: '系统 glibc 需 2.17+（CentOS 7+ / Ubuntu 14.04+）。' },
      ],
    },
    macGuide: {
      title: 'macOS 安装指南',
      subtitle: '源码包，需要 Python 3.10+',
      steps: [
        {
          title: '前置要求：安装 Python 3.10+',
          desc: '推荐通过 Homebrew 安装（或从 python.org 下载安装包）：',
          code: 'brew install python',
        },
        {
          title: '解压下载的压缩包',
          desc: '在终端中解压：',
          code: 'tar xzf laintas-cli_macos.tar.gz',
        },
        {
          title: '安装',
          desc: '运行安装脚本，安装到 /usr/local/bin 并自动安装依赖：',
          code: 'chmod +x laintas-cli/install.sh && sudo ./laintas-cli/install.sh',
        },
        {
          title: '或直接运行（无需安装）',
          desc: '先安装依赖，然后直接运行：',
          code: 'pip3 install requests rich prompt_toolkit mcp\npython3 laintas-cli/laintas_cli.py',
        },
        {
          title: '验证',
          desc: '安装完成后，打开新终端运行：',
          code: 'laintas-cli',
        },
      ],
      shellLabel: '手动一行安装依赖并运行：',
      shellCmd: 'pip3 install requests rich prompt_toolkit mcp && python3 laintas-cli/laintas_cli.py',
      troubleshootTitle: '常见问题',
      troubleshootItems: [
        { problem: '"laintas-cli: command not found"', solution: '确保 /usr/local/bin 在 PATH 中。zsh 用户可在 ~/.zprofile 添加：export PATH="/usr/local/bin:$PATH"。' },
        { problem: '"python3 not found"', solution: '运行 brew install python 或从 python.org 下载安装。' },
        { problem: '"pip3 未找到"', solution: '运行 python3 -m ensurepip --upgrade。' },
        { problem: '依赖安装失败', solution: '单独安装核心依赖：pip3 install requests rich prompt_toolkit（mcp 为可选）。' },
      ],
    },
    winGuide: {
      title: 'Windows 安装指南',
      subtitle: '独立可执行文件，无需 Python',
      steps: [
        {
          title: '下载可执行文件',
          desc: '将下载的 exe 放到一个固定目录，例如：',
          code: 'C:\\laintas-cli\\laintas_cli.exe',
        },
        {
          title: '在 PowerShell 中进入目录',
          desc: '打开 PowerShell，进入保存 exe 的目录：',
          code: 'cd C:\\laintas-cli',
        },
        {
          title: '启动',
          desc: '直接运行程序：',
          code: '.\\laintas_cli.exe',
        },
        {
          title: '可选：加入 PATH',
          desc: '这样就可以在任意目录启动：',
          code: 'setx PATH "$env:PATH;C:\\laintas-cli"',
        },
      ],
      shellLabel: 'PowerShell 下载命令：',
      shellCmd: 'Invoke-WebRequest -Uri https://cli.laintas.com/releases/v1.1/laintas_cli.exe -OutFile .\\laintas_cli.exe',
      troubleshootTitle: '常见问题',
      troubleshootItems: [
        { problem: 'Windows Defender / SmartScreen 提示', solution: '确认来源后选择“仍要运行”，或将文件放到白名单目录。' },
        { problem: '"无法打开，因为来自未知发布者"', solution: '右键文件属性，勾选“解除锁定”后重试。' },
        { problem: 'PTY 相关报错', solution: 'Windows 不支持 PTY，vi/vim 等交互程序无法使用，其他 AI Agent 功能正常。' },
        { problem: '命令无法全局使用', solution: '重新打开终端，或确认 `C:\\laintas-cli` 已加入 PATH。' },
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
      troubleshootTitle: '常见问题',
      troubleshootItems: [
        { problem: '"python not found"', solution: 'Windows 通常使用 `python`，Linux/macOS 如无该命令请改用 `python3`。' },
        { problem: '依赖安装失败', solution: '先升级 pip：`python -m pip install --upgrade pip`，再重试。' },
        { problem: '缺少可选功能', solution: 'MCP 相关功能依赖 `mcp`，如需使用请单独执行 `python -m pip install mcp`。' },
        { problem: '仍需平台专用包', solution: 'Linux 推荐独立二进制包，Windows 推荐 exe 包，源码包适合调试和二次开发。' },
      ],
    },
  },
  en: {
    common: { copy: 'Copy', copied: 'Copied' },
    nav: { contact: 'Contact', support: 'Support', github: 'GitHub' },
    user: { guest: 'Guest', login: 'Log in', register: 'Sign up', settings: 'Settings', logout: 'Sign out' },
    theme: { light: 'Switch to light theme', dark: 'Switch to dark theme' },
    modal: { contactTitle: 'Contact Us', supportTitle: 'Support', supportUnavailable: 'Support is currently unavailable. Please reach out via email.', close: 'Close' },
    hero: { title: 'Download Laintas CLI', subtitle: 'Available for Windows, macOS, and Linux.', webApp: 'Open Web App' },
    desktop: { title: 'Desktop', desc: 'A fast and focused AI terminal experience.' },
    mobile: { title: 'Mobile', desc: 'Manage your workspace from anywhere.' },
    download: 'Download',
    curl: 'Or install via terminal:',
    linux: { title: 'Linux Package', desc: 'Install via .deb package or script.' },
    footer: { copyright: '© 2026 Laintas', tagline: 'Precision Engineering' },
    linuxGuide: {
      title: 'Linux Installation Guide',
      subtitle: 'Standalone binary — no Python required',
      steps: [
        {
          title: 'Extract the archive',
          desc: 'After downloading, extract in your terminal:',
          code: 'tar xzf laintas-cli_linux.tar.gz',
        },
        {
          title: 'Install (recommended)',
          desc: 'Run the install script to copy the binary to /usr/local/bin:',
          code: 'sudo ./laintas-cli/install.sh',
        },
        {
          title: 'Or run directly (no install needed)',
          desc: 'You can also run it straight from the extracted directory:',
          code: './laintas-cli/laintas-cli',
        },
        {
          title: 'Launch',
          desc: 'Once installed, run from anywhere:',
          code: 'laintas-cli',
        },
      ],
      shellLabel: 'One-liner download and install:',
      shellCmd: 'curl -fsSL https://cli.laintas.com/install.sh | bash',
      troubleshootTitle: 'Troubleshooting',
      troubleshootItems: [
        { problem: '"Permission denied"', solution: 'Run chmod +x laintas-cli/laintas-cli and try again.' },
        { problem: '"laintas-cli: command not found"', solution: 'Ensure /usr/local/bin is in your PATH, or open a new terminal.' },
        { problem: '"cannot execute binary file"', solution: 'This package is x86-64 only. ARM is not yet supported as a standalone binary — run from source instead.' },
        { problem: 'glibc version error', solution: 'Requires glibc 2.17+ (CentOS 7+ / Ubuntu 14.04+).' },
      ],
    },
    macGuide: {
      title: 'macOS Installation Guide',
      subtitle: 'Source package — requires Python 3.10+',
      steps: [
        {
          title: 'Install Python 3.10+',
          desc: 'Install via Homebrew (recommended) or download from python.org:',
          code: 'brew install python',
        },
        {
          title: 'Extract the archive',
          desc: 'Extract the downloaded tarball:',
          code: 'tar xzf laintas-cli_macos.tar.gz',
        },
        {
          title: 'Install',
          desc: 'Run the install script — installs to /usr/local/bin and sets up dependencies:',
          code: 'chmod +x laintas-cli/install.sh && sudo ./laintas-cli/install.sh',
        },
        {
          title: 'Or run directly (no install needed)',
          desc: 'Install deps and run in place:',
          code: 'pip3 install requests rich prompt_toolkit mcp\npython3 laintas-cli/laintas_cli.py',
        },
        {
          title: 'Verify',
          desc: 'Open a new terminal and run:',
          code: 'laintas-cli',
        },
      ],
      shellLabel: 'Install deps and run in one line:',
      shellCmd: 'pip3 install requests rich prompt_toolkit mcp && python3 laintas-cli/laintas_cli.py',
      troubleshootTitle: 'Troubleshooting',
      troubleshootItems: [
        { problem: '"laintas-cli: command not found"', solution: 'Ensure /usr/local/bin is in PATH. For zsh, add export PATH="/usr/local/bin:$PATH" to ~/.zprofile.' },
        { problem: '"python3 not found"', solution: 'Run brew install python or download from python.org.' },
        { problem: '"pip3 not found"', solution: 'Run python3 -m ensurepip --upgrade.' },
        { problem: 'Dependency install fails', solution: 'Install core deps individually: pip3 install requests rich prompt_toolkit (mcp is optional).' },
      ],
    },
    winGuide: {
      title: 'Windows Installation Guide',
      subtitle: 'Standalone executable — no Python required',
      steps: [
        {
          title: 'Download the executable',
          desc: 'Save the downloaded exe to a permanent directory, for example:',
          code: 'C:\\laintas-cli\\laintas_cli.exe',
        },
        {
          title: 'Open PowerShell in that directory',
          desc: 'Switch to the directory where the exe is stored:',
          code: 'cd C:\\laintas-cli',
        },
        {
          title: 'Launch',
          desc: 'Run the executable directly:',
          code: '.\\laintas_cli.exe',
        },
        {
          title: 'Optional: add to PATH',
          desc: 'This lets you launch it from any directory:',
          code: 'setx PATH "$env:PATH;C:\\laintas-cli"',
        },
      ],
      shellLabel: 'PowerShell download command:',
      shellCmd: 'Invoke-WebRequest -Uri https://cli.laintas.com/releases/v1.1/laintas_cli.exe -OutFile .\\laintas_cli.exe',
      troubleshootTitle: 'Troubleshooting',
      troubleshootItems: [
        { problem: 'Windows Defender / SmartScreen warning', solution: 'Verify the file source, then choose Run anyway or allow-list the directory.' },
        { problem: '"cannot open because publisher is unknown"', solution: 'Open file Properties, unblock the file, then try again.' },
        { problem: 'PTY-related errors', solution: 'PTY is not supported on Windows. Interactive programs (vim etc.) will not work, but the rest of the AI agent features work normally.' },
        { problem: 'Command is not available globally', solution: 'Open a new terminal or confirm that `C:\\laintas-cli` is in PATH.' },
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
      troubleshootTitle: 'Troubleshooting',
      troubleshootItems: [
        { problem: '"python not found"', solution: 'Windows usually uses `python`; on Linux/macOS, use `python3` if `python` is unavailable.' },
        { problem: 'Dependency install fails', solution: 'Upgrade pip first with `python -m pip install --upgrade pip`, then retry.' },
        { problem: 'Optional features are missing', solution: 'MCP-related features require `mcp`; install it separately with `python -m pip install mcp`.' },
        { problem: 'You need a platform package instead', solution: 'Linux is best served by the standalone binary, Windows by the exe, and the source package is best for debugging or local modification.' },
      ],
    },
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
