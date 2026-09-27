// cli.laintas.com/docs — Laintas CLI documentation.
//
// Written against the released CLI (the latest GitHub release), not the
// working tree: a command that exists only in an unreleased checkout is not
// documented here until it ships. The command reference mirrors the
// CommandSpec registry in laintas_cli.py (`/help` prints the same list), and
// the state layout mirrors README.md — update them together.
//
// Account, balance, allowance and refunds are platform topics and are linked
// to https://laintas.com/docs rather than repeated here.

const L = (zh, en) => ({ zh, en });

const PLATFORM = 'https://laintas.com/docs';

export const GROUPS = [
  // ────────────────────────────────────────────────────────── start
  {
    title: L('开始', 'Get started'),
    sections: [
      {
        id: 'overview',
        title: L('概述', 'Overview'),
        lead: L(
          'Laintas CLI 把一个普通的交互式 Shell 和一个 AI Agent 运行时放在同一个终端里。你照常输入命令，命令就照常执行；输入一句自然语言，Agent 会读取工作区、调用工具、执行命令并根据结果继续，直到任务完成。',
          'Laintas CLI puts an ordinary interactive shell and an AI agent runtime in the same terminal. Type a command and it runs as it always would; type a sentence and the agent inspects the workspace, calls tools, runs commands and continues from the results until the task is done.',
        ),
        blocks: [
          { t: 'h', ...L('每一行输入怎么处理', 'How each line is handled') },
          {
            t: 'table',
            head: L(['输入', '处理方式', '经过模型？'], ['Input', 'What happens', 'Model involved?']),
            rows: L([
              ['以 `/` 开头', '内置命令或扩展命令，在本地执行', '否'],
              ['首个单词是 PATH 上的程序或 Shell 内建命令', '直接在主终端 `term0` 里运行，就像普通终端：实时输出、键盘输入、`Ctrl+C` 只中断这条命令；`Ctrl+]` 让它转入后台，`/fg` 切回来', '否'],
              ['其他内容', '作为任务交给 Agent：可观察、可随时打断的工具调用循环', '是'],
            ], [
              ['Starts with `/`', 'A built-in or extension command, handled locally', 'No'],
              ['First word is a program on PATH or a shell builtin', 'Runs directly in the main terminal `term0`, like any terminal: live output, keyboard input, and `Ctrl+C` interrupts only that command; `Ctrl+]` detaches it and `/fg` brings it back', 'No'],
              ['Anything else', 'Becomes a task for the agent: an observable, interruptible tool-use loop', 'Yes'],
            ]),
          },
          { t: 'h', ...L('它适合做什么', 'What it is for') },
          {
            t: 'list', ...L([
              '在代码仓库里修 bug、跑测试、改配置——Agent 离文件系统和终端最近，不需要来回复制粘贴。',
              '服务器运维：查日志、看进程、改 nginx 配置，每一步命令都经过安全策略判断。',
              '长时间运行的工作：命名子终端、多个 Agent 并行、可恢复的工作流。',
            ], [
              'Fixing bugs, running tests and changing configuration inside a repository — the agent sits next to the files and the terminal, so there is nothing to copy back and forth.',
              'Server operations: reading logs, inspecting processes, editing an nginx config — with every command checked by the security policy.',
              'Long-running work: named sub-terminals, several agents in parallel, resumable workflows.',
            ]),
          },
          {
            t: 'note', title: L('账户与计费', 'Account and billing'),
            ...L(`CLI 本身免费。使用 Laintas 官方后端时，AI 调用从你的 Laintas 账户付款（会员额度、试用次数或余额）。规则见 [平台文档](${PLATFORM}#how-billing-works)。`,
              `The CLI itself is free. With the official Laintas backend, AI calls are paid from your Laintas account — allowance, trial calls or balance. See the [platform documentation](${PLATFORM}#how-billing-works).`),
          },
        ],
      },
      {
        id: 'install',
        title: L('安装', 'Install'),
        blocks: [
          { t: 'h', ...L('系统要求', 'Requirements') },
          {
            t: 'table',
            head: L(['平台', '要求', '安装包'], ['Platform', 'Requirements', 'Package']),
            rows: L([
              ['Linux x86_64', '64 位，glibc', '独立二进制，无需 Python'],
              ['Linux aarch64', '64 位 ARM，glibc', '独立二进制，无需 Python'],
              ['macOS Apple Silicon / Intel', 'arm64 / x86_64', '原生终端二进制，无需 Python'],
              ['Windows', 'Windows 10 2004+ 或 Windows 11，64 位', '单文件安装程序（自带私有 WSL 2 发行版）'],
              ['其他（Alpine/musl、32 位等）', 'Python 3.10+', '源码包'],
            ], [
              ['Linux x86_64', '64-bit, glibc', 'Standalone binary, no Python needed'],
              ['Linux aarch64', '64-bit ARM, glibc', 'Standalone binary, no Python needed'],
              ['macOS Apple Silicon / Intel', 'arm64 / x86_64', 'Native terminal binary, no Python needed'],
              ['Windows', 'Windows 10 2004+ or Windows 11, 64-bit', 'Single-file installer (ships a private WSL 2 distribution)'],
              ['Anything else (Alpine/musl, 32-bit …)', 'Python 3.10+', 'Source package'],
            ]),
          },
          { t: 'h', ...L('Linux', 'Linux') },
          {
            t: 'p', ...L('安装脚本会根据 CPU 架构自动选择对应的二进制包：', 'The installer picks the binary that matches your CPU architecture:'),
          },
          { t: 'code', label: 'bash', code: 'curl -fsSL https://cli.laintas.com/install.sh | bash\nlaintas-cli' },
          {
            t: 'p', ...L('不确定机器是否兼容时，先检查架构、位数和 glibc 版本：', 'If you are unsure the machine is compatible, check the architecture, word size and glibc version first:'),
          },
          { t: 'code', label: 'bash', code: 'uname -m\ngetconf LONG_BIT\nldd --version' },
          { t: 'h', ...L('macOS', 'macOS') },
          { t: 'code', label: 'bash', code: 'curl -fsSL https://cli.laintas.com/install.sh | bash\nlaintas-cli' },
          {
            t: 'p', ...L(
              '安装脚本会选择 Apple Silicon 或 Intel 安装包，校验 SHA-256 后安装到 `~/.local/bin`。Mac 版不包含 Helpwo Kernel。',
              'The installer chooses the Apple Silicon or Intel archive, verifies its SHA-256 checksum and installs to `~/.local/bin`. The Mac build does not include Helpwo Kernel.',
            ),
          },
          { t: 'h', ...L('Windows', 'Windows') },
          { t: 'code', label: 'PowerShell', code: 'irm https://cli.laintas.com/install.ps1 | iex\nlaintas-cli' },
          {
            t: 'p', ...L(
              '安装程序会导入一个名为 `Laintas-CLI` 的私有 WSL 2 发行版，并安装原生的 `laintas-cli.exe` 启动器。它不会修改你的默认 WSL 发行版，日常启动也不调用 `wsl.exe`。安装位置所在的磁盘可以在安装时选择。',
              'The installer imports a private WSL 2 distribution named `Laintas-CLI` and installs a native `laintas-cli.exe` launcher. Your default WSL distribution is left alone, and normal startup does not run `wsl.exe`. You can choose the drive to install to.',
            ),
          },
          {
            t: 'note', title: L('SmartScreen 提示', 'SmartScreen'),
            ...L('安装程序暂未代码签名，首次运行时 Windows 可能提示「已保护你的电脑」。选择「更多信息 → 仍要运行」；如需确认文件来源，请对照发布页的 SHA-256 校验值。',
              'The installer is not code-signed yet, so Windows may say it protected your PC on first run. Choose "More info → Run anyway"; to be sure the file is ours, compare it with the SHA-256 checksum on the release page.'),
          },
          { t: 'h', ...L('源码包', 'Source package') },
          {
            t: 'p', ...L('用于不支持独立二进制的平台，或者你想审计、调试、修改 CLI：', 'For platforms without a standalone build, or when you want to audit, debug or modify the CLI:'),
          },
          { t: 'code', label: 'bash', code: 'unzip laintas-cli_source.zip\ncd laintas-cli-source\npython3 -m pip install -r requirements.txt\npython3 laintas_cli.py' },
          {
            t: 'p', ...L(
              'Linux 和 macOS 双架构安装包、Debian 包、Windows 安装程序、源码包以及 SHA-256 校验文件都发布在 [GitHub Releases](https://github.com/lin7c/Laintas_cli/releases)，并同步到 `cli.laintas.com/releases/latest/`；[下载页](/#download) 的安装脚本会自动选择 Linux 或 Mac 架构。',
              'Linux and macOS archives for both architectures, the Debian package, Windows installer, source package and SHA-256 checksums are published on [GitHub Releases](https://github.com/lin7c/Laintas_cli/releases) and mirrored at `cli.laintas.com/releases/latest/`; the [download section](/#download) offers an installer that picks the Linux or Mac architecture automatically.',
            ),
          },
        ],
      },
      {
        id: 'first-run',
        title: L('首次运行与登录', 'First run and sign-in'),
        blocks: [
          {
            t: 'steps', ...L([
              '在想要工作的目录里运行 `laintas-cli`。启动时所在的目录就是 Agent 的工作区。',
              '第一次启动会打印一个授权链接。在任意设备的浏览器中打开它，登录 Laintas 账户并批准。',
              '批准后 CLI 自动完成登录，出现输入提示符。直接输入命令或任务即可开始。',
            ], [
              'Run `laintas-cli` in the directory you want to work in. The directory it starts in is the agent’s workspace.',
              'The first start prints an authorization link. Open it in a browser on any device, sign in to your Laintas account and approve.',
              'The CLI finishes signing in by itself and shows its prompt. Type a command or a task to begin.',
            ]),
          },
          {
            t: 'p', ...L(
              `登录状态保存在 \`~/.laintas/session.json\`，只有当前用户可读。设备授权的细节见 [平台文档](${PLATFORM}#device-login)。`,
              `The session is stored in \`~/.laintas/session.json\`, readable only by your user. See the [platform documentation](${PLATFORM}#device-login) for how device authorization works.`,
            ),
          },
          {
            t: 'table',
            head: L(['命令', '作用'], ['Command', 'Effect']),
            rows: L([
              ['`/login`', '重新登录（例如换账户）'],
              ['`/quit`、`/q`', '退出，**保留**登录状态'],
              ['`/exit`', '**退出登录**并退出'],
            ], [
              ['`/login`', 'Sign in again, e.g. to switch accounts'],
              ['`/quit`, `/q`', 'Exit and **keep** the session'],
              ['`/exit`', '**Sign out** and exit'],
            ]),
          },
          {
            t: 'note', title: L('不用 Laintas 账户', 'Without a Laintas account'),
            ...L('CLI 也可以连接你自己的模型端点（见 [后端配置](#backends)）。本地回环后端不需要登录，自定义后端的调用不经过 Laintas 计费。',
              'The CLI can also talk to a model endpoint of your own (see [Backends](#backends)). A loopback backend needs no sign-in, and calls to a custom backend are not billed by Laintas.'),
          },
        ],
      },
      {
        id: 'update',
        title: L('更新', 'Updating'),
        blocks: [
          {
            t: 'table',
            head: L(['命令', '作用'], ['Command', 'Effect']),
            rows: L([
              ['`/v`', '显示当前版本'],
              ['`/v check`', '检查是否有新版本'],
              ['`/v update`', '下载并安装最新版本；`--force` 在版本相同时也重新安装'],
            ], [
              ['`/v`', 'Show the installed version'],
              ['`/v check`', 'Check for a newer release'],
              ['`/v update`', 'Download and install the latest release; `--force` reinstalls even when the version matches'],
            ]),
          },
          {
            t: 'p', ...L(
              '更新、下载页和安装脚本都读取同一个发布渠道：GitHub Releases。Windows 上 CLI 实际运行在私有 WSL 发行版内，所以 `/v update` 更新的是其中的 Linux 程序；启动器 `laintas-cli.exe` 和发行版本身需要重新运行安装程序来更新，已有的数据会被保留。',
              'Updates, the download page and the install scripts all read one channel: GitHub Releases. On Windows the CLI actually runs inside its private WSL distribution, so `/v update` replaces the Linux program there; the `laintas-cli.exe` launcher and the distribution itself are updated by re-running the installer, which keeps existing data.',
            ),
          },
        ],
      },
    ],
  },

  // ────────────────────────────────────────────────────────── use
  {
    title: L('日常使用', 'Everyday use'),
    sections: [
      {
        id: 'tasks',
        title: L('给 Agent 下任务', 'Giving the agent a task'),
        blocks: [
          {
            t: 'p', ...L(
              '用自然语言描述你要的结果，例如「跑一下测试，修掉失败的那个」。Agent 会逐步读取文件、执行命令、修改代码；每一步都会显示在终端里。',
              'Describe the outcome you want in plain language — for example "run the tests and fix the one that fails". The agent reads files, runs commands and edits code step by step, and every step is shown in the terminal.',
            ),
          },
          {
            t: 'list', ...L([
              '**补充**：任务运行时可以直接输入新的说明，Agent 会在下一步看到它。',
              '**打断**：按 `Esc` 停止当前任务（模型思考中也有效；已经在运行的工具会先执行完）。运行期间单按一次 `Ctrl+C` 会被忽略，快速连按两次则强制退出 CLI。',
              '**审批**：需要审批的操作会暂停并询问你批准或拒绝。哪些操作需要审批由 [模式](#modes) 和 [安全策略](#security) 决定。',
              '**为什么失败**：`/why` 解释最近一次工具失败的原因；`/detail on` 记录每一轮的完整执行细节，之后用 `/detail trace` 浏览。',
              '**上下文**：`/prop` 查看这一轮实际发送给模型的完整上下文和系统提示词。',
            ], [
              '**Add to it** — type a new instruction while a task runs; the agent sees it at its next step.',
              '**Interrupt** — press `Esc` to stop the current task (this works while the model is thinking; a tool already running finishes first). A single `Ctrl+C` is ignored during a run; pressing it twice quickly force-exits the CLI.',
              '**Approve** — an action that needs approval pauses and asks you to approve or reject it. [Modes](#modes) and the [security policy](#security) decide what needs approval.',
              '**Why did it fail** — `/why` explains the most recent tool failure; `/detail on` records full execution detail for each turn, browsable later with `/detail trace`.',
              '**Context** — `/prop` shows the exact context and system prompt sent to the model for a turn.',
            ]),
          },
          { t: 'h', ...L('项目说明', 'Project instructions') },
          {
            t: 'p', ...L(
              '在项目根目录的 `.laintas/cli.prop` 中写下这个仓库的约定——测试命令、代码风格、不要碰的目录。它会附加到每一轮的项目指令里。它是纯文本，不执行任何代码。',
              'Write the repository’s conventions — test commands, code style, directories to leave alone — in `.laintas/cli.prop` at the project root. It is appended to the project instructions every turn. It is plain text and executes nothing.',
            ),
          },
          { t: 'h', ...L('记忆与规则', 'Memory and rules') },
          {
            t: 'p', ...L(
              '`/memory` 管理跨会话保留的事实（分为全局和项目两级），`/rule` 管理需要一直遵守的约束。它们每一轮都会占用上下文，所以写得越精炼越好；它们是行为指导，不是权限控制——要限制 Agent 能做什么，请用 [模式](#modes) 和 [安全策略](#security)。',
              '`/memory` manages facts kept across sessions (global and per-project), and `/rule` manages constraints to follow every time. Both take up context every turn, so keep them short. They guide behaviour; they do not grant or remove permissions — use [modes](#modes) and the [security policy](#security) for that.',
            ),
          },
        ],
      },
      {
        id: 'sessions',
        title: L('会话与上下文', 'Sessions and context'),
        blocks: [
          {
            t: 'table',
            head: L(['命令', '作用'], ['Command', 'Effect']),
            rows: L([
              ['`/new`', '开始一个新会话（别名 `/clear`）'],
              ['`/resume [N|all|latest]`', '打开会话选择器，切换到已保存的会话；不会创建分支'],
              ['`/fork [name]`', '从当前上下文分出一个独立分支'],
              ['`/compact`', '立即压缩当前上下文；`/compact status` 查看阈值与后台压缩状态'],
              ['`/told`', '回放之前的提示词或某个 Agent 的对话'],
            ], [
              ['`/new`', 'Start a new session (alias `/clear`)'],
              ['`/resume [N|all|latest]`', 'Open the session picker and switch to a saved session, without creating a branch'],
              ['`/fork [name]`', 'Branch an independent session off the current context'],
              ['`/compact`', 'Compact the current context now; `/compact status` shows thresholds and the background worker'],
              ['`/told`', 'Replay earlier prompts or one agent’s conversation'],
            ]),
          },
          {
            t: 'p', ...L(
              '上下文达到可用预算的 70% 时，CLI 会在后台开始压缩；达到 90% 时，Agent 会等待后台结果或在前台压缩。压缩期间新输入的消息会被保留。启动时的 `--resume` 与 `--continue` 参数等同于 `/resume`。',
              'When the context reaches 70% of the usable budget, the CLI starts compacting in the background; at 90% the agent waits for that result or compacts in the foreground. Messages you type meanwhile are preserved. The `--resume` and `--continue` flags at startup behave like `/resume`.',
            ),
          },
          {
            t: 'warn', title: L('同一会话不能同时打开两次', 'One session, one terminal'),
            ...L('正在另一个终端里打开的会话不能同时被恢复。先在那个终端切走或关闭它。',
              'A session that is open in another terminal cannot be resumed at the same time. Switch away from it or close that terminal first.'),
          },
        ],
      },
      {
        id: 'models',
        title: L('模型与用量', 'Models and usage'),
        blocks: [
          {
            t: 'table',
            head: L(['命令', '作用'], ['Command', 'Effect']),
            rows: L([
              ['`/model`', '列出可用模型并为当前终端选择模型；`/model reset` 恢复默认'],
              ['`/model aux`', '选择辅助任务（如压缩摘要）使用的模型'],
              ['`/usage [7d|30d|90d]`', '本地 token 统计加上 Laintas 账户的用量与额度'],
              ['`/usage buy calls|storage`', '购买调用包或存储'],
            ], [
              ['`/model`', 'List available models and choose one for this terminal; `/model reset` restores the default'],
              ['`/model aux`', 'Choose the model used for auxiliary work such as compaction summaries'],
              ['`/usage [7d|30d|90d]`', 'Local token statistics plus usage and allowance from your Laintas account'],
              ['`/usage buy calls|storage`', 'Buy a call pack or storage'],
            ]),
          },
          {
            t: 'p', ...L(
              `模型按档位计费，档位越高，一次调用从会员额度里扣的单位越多。各档位包含的模型和价格见 [定价页](https://laintas.com/pricing#token-pricing)。`,
              `Models are billed by tier; a higher tier draws more units from the allowance per call. Which models are in each tier, and their prices, are on the [pricing page](https://laintas.com/pricing#token-pricing).`,
            ),
          },
        ],
      },
      {
        id: 'terminals',
        title: L('终端与子终端', 'Terminals and sub-terminals'),
        lead: L(
          '主终端叫 `term0`。需要长时间运行的程序（开发服务器、日志跟踪、交互式 REPL）放进命名子终端，它们跨任务存活，Agent 每一轮都能看到它们最新的输出。',
          'The main terminal is `term0`. Put long-running programs — dev servers, log tails, interactive REPLs — in named sub-terminals. They survive across tasks, and the agent sees their latest output every turn.',
        ),
        blocks: [
          {
            t: 'table',
            head: L(['命令', '作用'], ['Command', 'Effect']),
            rows: L([
              ['`/t`', '打开终端浏览器：`n` 新建、`e` 进入、`o` 观察、连按两次 `x` 关闭'],
              ['`/term <name>`', '新建一个命名子终端；`/term rename <old> <new>` 改名'],
              ['`/back`', '从子终端回到父终端，子终端继续运行'],
              ['`/send <name> <command>`', '向某个终端发送输入；`--wait <秒>` 等待输出'],
              ['`/terminate <name>`', '关闭终端及其下的所有资源'],
              ['`/bash <command>`', '通过 `term0` 执行一条命令'],
            ], [
              ['`/t`', 'Open the terminal browser: `n` new, `e` enter, `o` observe, `x` twice to close'],
              ['`/term <name>`', 'Create a named sub-terminal; `/term rename <old> <new>` renames one'],
              ['`/back`', 'Return from a sub-terminal to its parent; the child keeps running'],
              ['`/send <name> <command>`', 'Send input to a terminal; `--wait <seconds>` waits for output'],
              ['`/terminate <name>`', 'Close a terminal and everything under it'],
              ['`/bash <command>`', 'Run one command through `term0`'],
            ]),
          },
          {
            t: 'p', ...L('在子终端里 `Ctrl+\\` 是强制脱离的快捷键。在 tmux 中运行时，交互程序会在新的 tmux 窗口里原生运行。',
              'Inside a sub-terminal, `Ctrl+\\` force-detaches. When running under tmux, interactive programs open natively in a new tmux window.'),
          },
          {
            t: 'p', ...L('直接输入的命令在 `term0` 里以「附着」方式运行，需要真实终端和 bash 4.4+ 或 zsh；在管道、`/agents` 视图等没有终端可附着的场合，会退回到逐条执行、执行完再显示输出的方式。',
              'Commands you type run attached to `term0`, which needs a real terminal and bash 4.4+ or zsh; where there is no terminal to attach to (pipes, the `/agents` view), they fall back to running one at a time and showing the output when done.'),
          },
        ],
      },
      {
        id: 'agents',
        title: L('多个 Agent', 'Multiple agents'),
        lead: L(
          '一个 CLI 进程里可以有多个持久的 Agent（「员工」）。每个 Agent 有自己的对话历史、终端、模型和工具策略；工作目录、项目记忆和工具注册表是共享的。',
          'One CLI process can hold several persistent agents ("employees"). Each has its own conversation, terminals, model and tool policy; the working directory, project memory and tool registry are shared.',
        ),
        blocks: [
          {
            t: 'table',
            head: L(['命令', '作用'], ['Command', 'Effect']),
            rows: L([
              ['`/hire [name]`', '雇佣一个 Agent，可指定 `--profile`、`--model`、`--tools`、`--terminal`；雇佣不会立即开始工作'],
              ['`/agent <name>`', '把前台对话切换到该 Agent；它在后台完成的工作会保留'],
              ['`/agents`', '全屏查看所有 Agent 的活动；回车给正在工作的 Agent 补充信息，或给空闲的 Agent 布置新任务'],
              ['`/spawn [name:] <task>`', '派生一个子 Agent 处理子任务'],
              ['`/tell <agent> <message>`', '给某个 Agent 发消息'],
              ['`/abort <agent>`', '中止某个 Agent'],
              ['`/station`', '实时的 Agent 与终端管理器：分派任务、绑定终端、查看路由建议'],
            ], [
              ['`/hire [name]`', 'Hire an agent, optionally with `--profile`, `--model`, `--tools`, `--terminal`; hiring does not start any work'],
              ['`/agent <name>`', 'Switch the foreground conversation to that agent; work it finished in the background is kept'],
              ['`/agents`', 'Full-screen view of every agent’s activity; Enter sends an update to a working agent or a new task to an idle one'],
              ['`/spawn [name:] <task>`', 'Spawn a sub-agent for a subtask'],
              ['`/tell <agent> <message>`', 'Send a message to an agent'],
              ['`/abort <agent>`', 'Abort an agent'],
              ['`/station`', 'Live manager for agents and terminals: assign work, bind terminals, preview routing'],
            ]),
          },
          {
            t: 'note', title: L('权限不会扩大', 'Permissions never widen'),
            ...L('子 Agent 和自动分派的任务只能在父级权限范围内工作；角色选择和路由永远不会给它们比父级更多的工具。自动执行的写入任务需要 Git worktree 隔离。',
              'Sub-agents and auto-routed work run inside their parent’s permissions; role selection and routing never grant more tools than the parent has. Automatic writing tasks require Git worktree isolation.'),
          },
        ],
      },
      {
        id: 'planning',
        title: L('计划、任务与工作流', 'Plans, tasks and workflows'),
        blocks: [
          {
            t: 'table',
            head: L(['命令', '作用'], ['Command', 'Effect']),
            rows: L([
              ['`/plan enter <task>`', '进入计划模式：Agent 先只读探索并写出计划，你审阅（`revise`）、批准（`approve`）后才开始执行'],
              ['`/task`', '项目任务清单：添加、开始、完成、子任务、进度'],
              ['`/retask`', '打开 AI 交给你来完成的待办清单（`Alt+R`）'],
              ['`/workflow`', '多阶段工作流：每个阶段有自己的工具范围，按阶段推进和审批'],
              ['`/hwo <file>`', '运行 HWO 编排文件：多个 Agent 的实时协作'],
              ['`/hwg <file.hwg>`', '编译并运行 HWG 图工作流：持久、可恢复，`/hwg resume` 从中断处继续'],
              ['`/work`', '查看或恢复统一的工作图状态'],
            ], [
              ['`/plan enter <task>`', 'Enter plan mode: the agent explores read-only and writes a plan; it executes only after you revise and approve it'],
              ['`/task`', 'Project task list: add, start, finish, subtasks, progress'],
              ['`/retask`', 'Open the checklist of work the AI handed to you (`Alt+R`)'],
              ['`/workflow`', 'Multi-phase workflows: each phase has its own tool scope and is advanced and approved in turn'],
              ['`/hwo <file>`', 'Run an HWO orchestration file: live collaboration between agents'],
              ['`/hwg <file.hwg>`', 'Compile and run an HWG graph workflow: durable and resumable, `/hwg resume` continues after an interruption'],
              ['`/work`', 'Inspect or resume unified work-graph state'],
            ]),
          },
          { t: 'h', ...L('Git 检查点', 'Git checkpoints') },
          {
            t: 'p', ...L('`/snapshot [label]` 创建检查点，`/snapshots` 列出，`/undo [sha]` 恢复到某个检查点。在让 Agent 做大范围修改前打一个检查点是个好习惯。',
              '`/snapshot [label]` creates a checkpoint, `/snapshots` lists them, and `/undo [sha]` restores one. Taking a checkpoint before a wide-ranging change is a good habit.'),
          },
        ],
      },
    ],
  },

  // ────────────────────────────────────────────────────────── control
  {
    title: L('控制与安全', 'Control and safety'),
    sections: [
      {
        id: 'modes',
        title: L('模式', 'Modes'),
        lead: L('模式决定 Agent 的工作姿态：能用哪些工具、哪些操作自动放行。用 `/mode <name>` 切换。',
          'A mode sets the agent’s working posture — which tools it may use and what is approved automatically. Switch with `/mode <name>`.'),
        blocks: [
          {
            t: 'table',
            head: L(['模式', '用途'], ['Mode', 'Use']),
            rows: L([
              ['`act`', '默认模式，正常执行'],
              ['`review`', '只读的代码与设计审查，不修改工作区'],
              ['`study`', '只读，专注于倾听并把要点写入记忆'],
              ['`auto`', '自主执行，需要确认的操作会给出一个限时确认窗口'],
              ['`step`', '每次只运行一轮模型迭代，按回车（`/continue`）推进'],
            ], [
              ['`act`', 'Default; normal execution'],
              ['`review`', 'Read-only code and design review; the workspace is not modified'],
              ['`study`', 'Read-only; listens and writes what matters to memory'],
              ['`auto`', 'Autonomous execution; actions that need confirmation get a timed confirmation window'],
              ['`step`', 'Runs one model iteration at a time; press Enter (`/continue`) to advance'],
            ]),
          },
          { t: 'h', ...L('自定义模式', 'Custom modes') },
          {
            t: 'p', ...L('用命令创建一个只读的文档审查模式：', 'Create a read-only documentation review mode from the command line:'),
          },
          { t: 'code', label: 'laintas-cli', code: '/mode create docs-review --tools "fs.read,fs.grep,fs.glob" --deny "shell.*,fs.write,fs.edit" --auto-approve none' },
          {
            t: 'p', ...L('也可以直接写进项目的 `.laintas/modes.json`：', 'Or declare it in the project’s `.laintas/modes.json`:'),
          },
          {
            t: 'code', label: '.laintas/modes.json', code: `{
  "version": 1,
  "active": "docs-review",
  "modes": {
    "docs-review": {
      "description": "Review documentation without modifying the workspace",
      "instructions": "Check correctness, structure, examples, and broken references.",
      "allowed_tools": ["fs.read", "fs.grep", "fs.glob"],
      "denied_tools": ["shell.*", "fs.write", "fs.edit"],
      "auto_approve": "none"
    }
  }
}`,
          },
          {
            t: 'list', ...L([
              '`denied_tools` 优先于 `allowed_tools`；工具名支持通配符。',
              '`auto_approve` 可选 `none`、`writes`、`commands`、`all`。',
              '模式只能收窄权限：最终可用的工具还要与工作流阶段、角色、全局策略和信任状态取交集，模式不能放开其他层禁止的东西。',
            ], [
              '`denied_tools` wins over `allowed_tools`; tool names accept glob patterns.',
              '`auto_approve` is one of `none`, `writes`, `commands`, `all`.',
              'A mode can only narrow: the effective tool set is intersected with the workflow phase, role, global policy and trust state, so a mode cannot re-open what another layer denies.',
            ]),
          },
        ],
      },
      {
        id: 'security',
        title: L('安全策略', 'Security policy'),
        lead: L(
          'Agent 要执行的每一条命令，在执行前都会经过策略引擎，得到三种结果之一：放行（allow）、需要审批（needs_approval）或拒绝（deny）。每个决定都会写入审计日志。',
          'Every command the agent wants to run goes through the policy engine first and gets one of three decisions: allow, needs_approval or deny. Every decision is written to an audit log.',
        ),
        blocks: [
          { t: 'h', ...L('策略模式', 'Policy modes') },
          {
            t: 'table',
            head: L(['模式', '行为'], ['Mode', 'Behaviour']),
            rows: L([
              ['`audit`（默认）', '拒绝规则生效；普通的审批规则只记录警告、不打断；下面列出的高风险操作仍然每次都询问'],
              ['`enforce`', '所有审批规则都会暂停等待确认，包括 `sudo` 和工作区之外的写入'],
              ['`disabled`', '关闭策略检查。需要 `--yes` 确认，只建议在一次性的隔离环境里使用'],
            ], [
              ['`audit` (default)', 'Deny rules apply; ordinary approval rules only log a warning; the high-risk actions below still always ask'],
              ['`enforce`', 'Every approval rule pauses for confirmation, including `sudo` and writes outside the workspace'],
              ['`disabled`', 'Turns policy checks off. Requires `--yes`; only for a throwaway, isolated environment'],
            ]),
          },
          { t: 'h', ...L('无论哪种模式都会询问的操作', 'Always asked, in audit and enforce alike') },
          {
            t: 'list', ...L([
              '删除命令（包括包在 `bash -c "…"` 等外壳里的删除）。',
              '破坏性的 git 操作：`reset --hard`、`clean -fdx`、`push --force`、`branch -D`、`stash drop` 等。',
              '读取密钥、Cookie、令牌或凭据存储的命令。读取凭据并在同一条命令里发往外部的，直接拒绝。',
              '在工作目录之外、没有深度限制的递归遍历。',
            ], [
              'Delete commands — including deletes wrapped in `bash -c "…"` and similar shells.',
              'Destructive git: `reset --hard`, `clean -fdx`, `push --force`, `branch -D`, `stash drop` and the like.',
              'Commands that read keys, cookies, tokens or a credential store. Reading credentials and sending them off the machine in the same command is denied outright.',
              'Unbounded recursive walks outside the working directory.',
            ]),
          },
          {
            t: 'p', ...L(
              '判断基于命令解析，而不只是字符串匹配：多余的空格、引号拼接、包装命令等混淆写法会先被还原再检查。',
              'Decisions are made on the parsed command, not only on the typed string: extra spaces, quote splicing, wrapper commands and similar obfuscations are resolved before matching.',
            ),
          },
          {
            t: 'table',
            head: L(['位置 / 命令', '说明'], ['File / command', 'Notes']),
            rows: L([
              ['`~/.laintas/policy.json`', '策略规则（allow / needs_approval / deny 三个正则列表），首次加载时生成安全默认值，修改后无需重启'],
              ['`~/.laintas/audit.log`', 'JSONL 审计日志，每个决定一行，超过 10 MB 自动轮转'],
              ['`/policy [audit|enforce|disabled|reset]`', '查看或切换策略模式；`reset` 恢复默认规则'],
            ], [
              ['`~/.laintas/policy.json`', 'The rules (allow / needs_approval / deny regex lists). Safe defaults are written on first load; edits apply without a restart'],
              ['`~/.laintas/audit.log`', 'JSONL audit log, one line per decision, rotated at 10 MB'],
              ['`/policy [audit|enforce|disabled|reset]`', 'Show or switch the policy mode; `reset` restores the default rules'],
            ]),
          },
          { t: 'h', ...L('项目信任', 'Project trust') },
          {
            t: 'p', ...L(
              '项目里会被执行的文件——`.laintas/commands.py`、`.laintas/loop.py`、项目扩展——只有在你用 `/trust allow` 批准了它们**当前内容的哈希**之后才会运行。文件一改，批准自动失效，需要重新审阅。克隆一个陌生仓库不会让其中的代码在你的 CLI 里静默执行。`/trust status` 查看，`/trust revoke` 撤销。',
              'Executable project files — `.laintas/commands.py`, `.laintas/loop.py` and project extensions — run only after you approve the **hash of their current content** with `/trust allow`. Any change invalidates the approval and needs a fresh review, so cloning an unfamiliar repository cannot make its code run silently in your CLI. `/trust status` shows the state; `/trust revoke` removes it.',
            ),
          },
        ],
      },
      {
        id: 'config',
        title: L('配置与文件位置', 'Configuration and files'),
        blocks: [
          {
            t: 'p', ...L('`/config` 查看全部运行时配置；`/config <key> <value>` 修改；`/config export <file>` 与 `/config import <file>` 在机器之间迁移配置。`/theme dark|light|mono` 切换配色。',
              '`/config` shows every runtime setting; `/config <key> <value>` changes one; `/config export <file>` and `/config import <file>` move settings between machines. `/theme dark|light|mono` switches the colour scheme.'),
          },
          {
            t: 'table',
            head: L(['位置', '内容', '范围'], ['Location', 'Contents', 'Scope']),
            rows: L([
              ['`~/.laintas/config.json`', '运行时偏好', '用户'],
              ['`~/.laintas/session.json`', '登录状态（私有）', '用户'],
              ['`~/.laintas/policy.json`', '全局命令与工具策略', '用户'],
              ['`~/.laintas/backends.json`', '后端配置与凭据引用', '用户'],
              ['`~/.laintas/mcp.json`', 'MCP 服务器定义', '用户'],
              ['`~/.laintas/hooks.json`、`hooks.py`', '生命周期钩子', '用户'],
              ['`~/.laintas/skills/`、`extensions/`', '用户安装的技能与扩展', '用户'],
              ['`~/.laintas/memory/`、`sessions/`、`agents/`', '记忆、会话与 Agent 数据', '用户'],
              ['`.laintas/cli.prop`', '项目说明', '项目'],
              ['`.laintas/memory.json`、`rules.json`', '项目记忆与规则', '项目'],
              ['`.laintas/modes.json`', '自定义模式', '项目'],
              ['`.laintas/commands.py`、`loop.py`、`extensions/`', '项目可执行定制（需信任）', '项目'],
            ], [
              ['`~/.laintas/config.json`', 'Runtime preferences', 'User'],
              ['`~/.laintas/session.json`', 'Sign-in state (private)', 'User'],
              ['`~/.laintas/policy.json`', 'Global command and tool policy', 'User'],
              ['`~/.laintas/backends.json`', 'Backend profiles and credential references', 'User'],
              ['`~/.laintas/mcp.json`', 'MCP server definitions', 'User'],
              ['`~/.laintas/hooks.json`, `hooks.py`', 'Lifecycle hooks', 'User'],
              ['`~/.laintas/skills/`, `extensions/`', 'User-installed skills and extensions', 'User'],
              ['`~/.laintas/memory/`, `sessions/`, `agents/`', 'Memory, sessions and agent data', 'User'],
              ['`.laintas/cli.prop`', 'Project instructions', 'Project'],
              ['`.laintas/memory.json`, `rules.json`', 'Project memory and rules', 'Project'],
              ['`.laintas/modes.json`', 'Custom modes', 'Project'],
              ['`.laintas/commands.py`, `loop.py`, `extensions/`', 'Executable project customisation (trust required)', 'Project'],
            ]),
          },
          {
            t: 'p', ...L('私有文件和目录以受限权限创建；可执行的项目定制会拒绝符号链接和属主不安全的文件。',
              'Private files and directories are created with restrictive permissions; executable project customisation rejects symlinks and unsafe ownership.'),
          },
        ],
      },
    ],
  },

  // ────────────────────────────────────────────────────────── extend
  {
    title: L('定制', 'Customise'),
    sections: [
      {
        id: 'choosing',
        title: L('选择合适的扩展方式', 'Choosing a customisation surface'),
        lead: L('优先选能解决问题的最窄的一层：不执行代码的方式不需要信任审批。',
          'Use the narrowest surface that solves the problem — the ones that execute no code need no trust approval.'),
        blocks: [
          {
            t: 'table',
            head: L(['需求', '推荐方式', '执行代码？'], ['Need', 'Use', 'Runs code?']),
            rows: L([
              ['改变通用行为', '`/config`', '否'],
              ['给项目加说明', '`.laintas/cli.prop`', '否'],
              ['保存事实或约束', '`/memory`、`/rule`', '否'],
              ['限制某种工作方式的工具', '`.laintas/modes.json`', '否'],
              ['可复用的操作指南', '技能 `SKILL.md`', '否'],
              ['小型项目命令', '`.laintas/commands.py`', '是（需信任）'],
              ['可复用的进程内工具', '技能 `skill.py`', '是（需信任）'],
              ['连接外部工具服务', 'MCP', '子进程（需信任）'],
              ['命令 + 工具 + 生命周期', '[扩展](#extensions)', '是（签名或哈希信任）'],
              ['拦截运行时事件', '钩子', '视情况'],
              ['使用别的推理端点', '后端配置', '远程服务'],
            ], [
              ['Change general behaviour', '`/config`', 'No'],
              ['Add project instructions', '`.laintas/cli.prop`', 'No'],
              ['Keep facts or constraints', '`/memory`, `/rule`', 'No'],
              ['Restrict tools for a way of working', '`.laintas/modes.json`', 'No'],
              ['Reusable guidance', 'Skill `SKILL.md`', 'No'],
              ['A small project command', '`.laintas/commands.py`', 'Yes (trusted)'],
              ['Reusable in-process tools', 'Skill `skill.py`', 'Yes (trusted)'],
              ['An external tool server', 'MCP', 'Child process (trusted)'],
              ['Commands, tools and lifecycle', '[Extension](#extensions)', 'Yes (signature or hash trust)'],
              ['Intercept runtime events', 'Hooks', 'Depends'],
              ['Another inference endpoint', 'Backend profile', 'Remote service'],
            ]),
          },
        ],
      },
      {
        id: 'skills',
        title: L('技能', 'Skills'),
        blocks: [
          {
            t: 'p', ...L('技能是 `~/.laintas/skills/<name>/` 下的一个目录。启动时只索引 `SKILL.md` 的简短头信息；只有技能被用到时才加载完整说明，参考资料按需读取——这样技能再多也不会拖慢启动或挤占上下文。',
              'A skill is a directory under `~/.laintas/skills/<name>/`. Only the short front matter of `SKILL.md` is indexed at startup; the full instructions load when the skill is used, and references load on demand — so many skills cost neither startup time nor context.'),
          },
          {
            t: 'code', label: L('目录结构', 'Layout'), code: `my-skill/
├── SKILL.md          # name, description, version, instructions
├── references/       # loaded only when the skill needs them
├── skill.py          # optional: get_tools()
└── extension.json    # required when skill.py provides tools`,
          },
          {
            t: 'p', ...L('只有 `SKILL.md` 的技能不执行任何代码。带 `skill.py` 的技能提供工具，必须在 `extension.json` 中声明能力并通过哈希信任后才会注册。用户技能会覆盖同名的内置技能。管理命令：`/skill`。',
              'A skill with only `SKILL.md` runs no code. A skill with `skill.py` provides tools; it must declare its capabilities in `extension.json` and pass hash trust before it is registered. A user skill overrides a bundled skill of the same name. Manage skills with `/skill`.'),
          },
        ],
      },
      {
        id: 'mcp',
        title: L('MCP 服务器', 'MCP servers'),
        blocks: [
          {
            t: 'p', ...L('已经作为独立服务运行的工具，或需要自己依赖环境的工具，适合用 MCP 接入。在 `~/.laintas/mcp.json` 中配置：',
              'MCP suits tools that already run as a separate service or need their own dependency environment. Configure servers in `~/.laintas/mcp.json`:'),
          },
          {
            t: 'code', label: '~/.laintas/mcp.json', code: `{
  "servers": {
    "example": {
      "command": "/absolute/path/to/example-server",
      "args": ["--stdio"],
      "env": {"EXAMPLE_TOKEN": "..."},
      "cwd": "/absolute/path/to/workspace",
      "enabled": true,
      "call_timeout": 30,
      "capabilities": ["fs.read"]
    }
  }
}`,
          },
          {
            t: 'list', ...L([
              '工具以 `mcp.<server>.<tool>` 的名字出现在统一工具注册表中。',
              '子进程只继承最小环境变量加上你显式配置的变量。',
              '信任绑定在服务器配置的哈希上：修改启动配置后需要重新审阅。',
              '`/mcp list`、`/mcp connect`、`/mcp tools`、`/mcp trust` 管理服务器。',
            ], [
              'Tools appear in the unified registry as `mcp.<server>.<tool>`.',
              'Child processes get a minimal environment plus the variables you configure explicitly.',
              'Trust is bound to the hash of the server configuration: changing how it launches requires a fresh review.',
              'Manage servers with `/mcp list`, `/mcp connect`, `/mcp tools` and `/mcp trust`.',
            ]),
          },
        ],
      },
      {
        id: 'hooks',
        title: L('钩子', 'Hooks'),
        blocks: [
          {
            t: 'p', ...L('钩子可以观察或拦截运行时事件：命令与工具执行、会话开始与结束、错误、记忆变更等。',
              'Hooks observe or gate runtime events: command and tool execution, session start and end, errors, memory changes and more.'),
          },
          {
            t: 'list', ...L([
              '**声明式钩子**（`~/.laintas/hooks.json`）：以参数数组方式运行程序（不经过 Shell），事件 JSON 从标准输入传入，支持条件、超时和 `block_on_failure`。',
              '**Python 钩子**（`~/.laintas/hooks.py`）：进程内回调，能力更强，需要可执行信任。',
            ], [
              '**Declarative hooks** (`~/.laintas/hooks.json`) run a program as an argument vector — no shell — with the event JSON on standard input, and support conditions, timeouts and `block_on_failure`.',
              '**Python hooks** (`~/.laintas/hooks.py`) are in-process callbacks with more power; they require executable trust.',
            ]),
          },
          {
            t: 'p', ...L('审计转发、确定性检查用声明式钩子即可；只有事件确实需要本地程序逻辑时才用 Python。配置为阻塞的钩子失败时按「拒绝」处理。管理命令：`/hooks`。',
              'Declarative hooks are enough for audit forwarding and deterministic checks; use Python only when the event genuinely needs local program logic. A hook configured to block fails closed. Manage hooks with `/hooks`.'),
          },
        ],
      },
      {
        id: 'backends',
        title: L('后端配置', 'Backends'),
        lead: L('后端配置（`~/.laintas/backends.json`）把推理端点分成三个信任域，用 `/backend` 管理。',
          'Backend profiles in `~/.laintas/backends.json` divide inference endpoints into three trust domains. Manage them with `/backend`.'),
        blocks: [
          {
            t: 'table',
            head: L(['信任域', '说明'], ['Domain', 'Meaning']),
            rows: L([
              ['official', '精确匹配的 Laintas 官方地址；可以收到你的 Laintas 会话，按账户计费'],
              ['custom', '你指定的 HTTPS 端点，使用单独的凭据引用（`env:VARIABLE` 或 `keyring:service/user`）；不经过 Laintas 计费'],
              ['local', '本机回环开发端点，不需要认证，也不需要 Laintas 登录'],
            ], [
              ['official', 'Exact Laintas origins; may receive your Laintas session and bills your account'],
              ['custom', 'An HTTPS endpoint you name, with its own credential reference (`env:VARIABLE` or `keyring:service/user`); not billed by Laintas'],
              ['local', 'A loopback development endpoint; no authentication and no Laintas sign-in'],
            ]),
          },
          {
            t: 'list', ...L([
              '自定义端点不能把自己声明为 official。',
              'Laintas 凭据绝不会发往自定义地址，自定义端点的令牌也绝不会发往官方地址；带认证的请求不跟随跨源重定向。',
              '不要把密钥写进 URL 或提交到仓库的文件里。',
            ], [
              'A custom endpoint cannot declare itself official.',
              'Laintas credentials are never sent to a custom host, and a custom token is never sent to an official origin; authenticated requests do not follow cross-origin redirects.',
              'Never put secrets in a URL or in a file you commit.',
            ]),
          },
        ],
      },
    ],
  },

  // ────────────────────────────────────────────────────────── extensions
  // Kept apart from the core on purpose: nothing in this group ships with the
  // CLI. Each extension is installed separately and its commands appear only
  // after it is. The official list mirrors dist/extensions/official-registry.json.
  {
    title: L('扩展（插件）', 'Extensions (plugins)'),
    sections: [
      {
        id: 'extensions',
        title: L('扩展与插件市场', 'Extensions and the plugin market'),
        lead: L('扩展不属于 CLI 本体：它们需要单独安装，安装后才会出现对应的命令和工具，卸载后随之消失。官方扩展和社区扩展都在 [插件市场](/plugins) 中浏览。',
          'Extensions are not part of the CLI itself: each is installed separately, its commands and tools appear only once it is, and they go away when it is removed. Browse official and community extensions in the [plugin market](/plugins).'),
        blocks: [
          {
            t: 'table',
            head: L(['命令', '作用'], ['Command', 'Effect']),
            rows: L([
              ['`/extensions available [official|community]`', '浏览市场中的扩展'],
              ['`/extensions search <keyword>`', '在官方和社区扩展中搜索'],
              ['`/extensions install <source>`', '安装扩展（来源格式见下表）'],
              ['`/extensions list`', '列出本机已安装的扩展'],
              ['`/extensions info <name>`、`remove <name>`', '查看详情、卸载'],
            ], [
              ['`/extensions available [official|community]`', 'Browse the market'],
              ['`/extensions search <keyword>`', 'Search official and community extensions'],
              ['`/extensions install <source>`', 'Install an extension (source formats below)'],
              ['`/extensions list`', 'List extensions installed on this machine'],
              ['`/extensions info <name>`, `remove <name>`', 'Show details, uninstall'],
            ]),
          },
          { t: 'h', ...L('来源与信任', 'Sources and trust') },
          {
            t: 'table',
            head: L(['来源', '安装命令', '信任机制'], ['Source', 'Install with', 'Trust']),
            rows: L([
              ['官方', '`/extensions install laintas/<name>`', '官方注册表哈希 + 你的明确批准'],
              ['社区', '`/extensions install @author/<name>`', '不可变的包哈希、每次安装都重新做静态检查和 AI 源码审查、你的明确批准'],
              ['本地', '`/extensions install <path-or-url>`', '内容哈希 + 你的明确批准'],
            ], [
              ['Official', '`/extensions install laintas/<name>`', 'Official registry hash plus your explicit approval'],
              ['Community', '`/extensions install @author/<name>`', 'Immutable package hash, a fresh static check and AI source review on every install, and your explicit approval'],
              ['Local', '`/extensions install <path-or-url>`', 'Content hash plus your explicit approval'],
            ]),
          },
          {
            t: 'warn', title: L('社区扩展不是沙箱', 'Community extensions are not sandboxed'),
            ...L('AI 审查报告只是参考：社区扩展的 Python 代码仍以你的用户权限运行。发现严重问题会阻止安装，扫描失败也会停止安装，但每次安装前请自己确认来源可信。',
              'The AI review is advisory: community Python still runs with your user’s permissions. Critical findings block installation and a failed scan stops it, but confirm you trust the author before every install.'),
          },
        ],
      },
      {
        id: 'official-extensions',
        title: L('官方扩展', 'Official extensions'),
        lead: L('由 Laintas 维护和发布。它们同样需要手动安装，默认不随 CLI 提供。',
          'Maintained and published by Laintas. They too are installed by hand; none ships with the CLI.'),
        blocks: [
          {
            t: 'table',
            head: L(['扩展', '命令', '用途'], ['Extension', 'Command', 'What it does']),
            rows: L([
              ['`laintas/ai-pow`', '`/pow`', '为每个 Git 提交记录过程评分和可验证的工作日志'],
              ['`laintas/blindpick`', '`/blindpick`', '用两个模型在隔离分支上做同一个任务，盲评比较，只应用你选中的结果'],
              ['`laintas/canvas`', '`/canvas`', '终端里的白板：查看和绘制 `.excalidraw` 画板，也让 Agent 在上面画'],
              ['`laintas/swebench`', '`/swebench`', '用 CLI 生成 SWE-bench 预测结果；评分仍交给官方评测工具'],
              ['`laintas/whatsapp`', '`/whatsapp`', '通过 WhatsApp 给 CLI 发任务，执行后回报结果'],
            ], [
              ['`laintas/ai-pow`', '`/pow`', 'A process score and verifiable work journal for every Git commit'],
              ['`laintas/blindpick`', '`/blindpick`', 'Run the same task with two models on isolated branches, compare blind, apply only the one you pick'],
              ['`laintas/canvas`', '`/canvas`', 'Whiteboards in the terminal: view and draw `.excalidraw` boards, and let the agent draw on them'],
              ['`laintas/swebench`', '`/swebench`', 'Generate SWE-bench predictions with the CLI; scoring stays with the official harness'],
              ['`laintas/whatsapp`', '`/whatsapp`', 'Send the CLI a task over WhatsApp; it runs it and reports back'],
            ]),
          },
          { t: 'code', label: L('安装示例', 'Example'), code: '/extensions install laintas/blindpick\n/blindpick' },
          {
            t: 'p', ...L('每个扩展的具体用法，在安装后用 `/help <command>` 查看，或在 [插件市场](/plugins) 的扩展卡片上查看。',
              'For how to use an extension, run `/help <command>` after installing it, or see its card in the [plugin market](/plugins).'),
          },
        ],
      },
      {
        id: 'writing-extensions',
        title: L('编写与发布扩展', 'Writing and publishing extensions'),
        lead: L('扩展是最完整的定制单元，可以注册斜杠命令、工具、循环处理器和清理逻辑。',
          'An extension is the broadest customisation unit: it can register slash commands, tools, loop handlers and teardown logic.'),
        blocks: [
          {
            t: 'code', label: 'extension.json', code: `{
  "schemaVersion": 2,
  "name": "example",
  "version": "0.1.0",
  "entrypoint": "main.py",
  "description": "Example Laintas extension",
  "author": {"name": "Your Name"},
  "license": "MIT",
  "toolPrefix": "example."
}`,
          },
          {
            t: 'list', ...L([
              '`main.py` 导出 `setup(ctx)`；上下文只提供收窄后的注册与后端调用接口，官方登录凭据不会交给扩展。',
              '`toolPrefix` 必须小写并以点结尾。',
              '项目扩展放在 `.laintas/extensions/<name>/`，全局扩展放在 `~/.laintas/extensions/<name>/`；同名时项目扩展优先。',
              '`/extensions create` 生成脚手架，`/extensions pack` 打包为 `.lext`，`/extensions publish <name>` 发布到社区注册表。社区包不能使用 `laintas` 发布者命名空间。',
              '`/evolve` 可以让 Agent 为当前项目生成、测试并热加载扩展，支持回滚。',
            ], [
              '`main.py` exports `setup(ctx)`; the context offers narrowed registration and backend-call helpers, and official sign-in credentials are never handed to the extension.',
              '`toolPrefix` must be lower-case and end with a dot.',
              'Project extensions live in `.laintas/extensions/<name>/`, global ones in `~/.laintas/extensions/<name>/`; a project extension shadows a global one of the same name.',
              '`/extensions create` scaffolds a package, `/extensions pack` builds a `.lext`, and `/extensions publish <name>` publishes to the community registry. Community packages cannot use the `laintas` publisher namespace.',
              '`/evolve` lets the agent generate, test and hot-load an extension for the current project, with rollback.',
            ]),
          },
        ],
      },
    ],
  },

  // ────────────────────────────────────────────────────────── connect
  {
    title: L('连接', 'Connect'),
    sections: [
      {
        id: 'helpwo',
        title: L('与 Helpwo 一起使用', 'Using it with Helpwo'),
        lead: L('[Helpwo](https://helpwo.laintas.com) 是浏览器里的 AI 工作台。CLI 可以把所在的机器作为 Helpwo 的运行环境，也可以在本地直接运行 Helpwo。',
          '[Helpwo](https://helpwo.laintas.com) is an AI workspace in the browser. The CLI can make its machine a runtime environment for Helpwo, or run Helpwo locally.'),
        blocks: [
          {
            t: 'table',
            head: L(['命令', '作用'], ['Command', 'Effect']),
            rows: L([
              ['`/helpwo --remote`', '让这台机器上线为 Helpwo 的运行环境：在 helpwo.laintas.com 中可以浏览它的文件、打开终端、让 AI 在这里执行命令'],
              ['`/helpwo`', '在名为 `helpwo` 的子终端里本地运行 Helpwo；当前目录就是它的工作区，登录、数据和对话按目录保存'],
              ['`/helpwo stop`', '关闭 Helpwo 子终端及其中的一切'],
              ['`/shared`', '通过 Laintas 存储与 Helpwo 共享文件（push / pull / list）'],
            ], [
              ['`/helpwo --remote`', 'Bring this machine online as a Helpwo runtime environment: on helpwo.laintas.com you can browse its files, open terminals and let the AI run commands here'],
              ['`/helpwo`', 'Run Helpwo locally in a sub-terminal named `helpwo`; the current folder is its workspace, and sign-in, data and conversation are kept per folder'],
              ['`/helpwo stop`', 'Close the Helpwo sub-terminal and everything in it'],
              ['`/shared`', 'Share files with Helpwo through Laintas storage (push / pull / list)'],
            ]),
          },
          {
            t: 'p', ...L('远程模式下，文件内容通过点对点连接直接在浏览器和这台机器之间传输，不经过 Helpwo 服务器。Helpwo 侧的用法见 [Helpwo 文档](https://helpwo.laintas.com/docs/#environments)。',
              'In remote mode, file contents travel peer-to-peer between the browser and this machine, not through Helpwo’s servers. For the Helpwo side, see the [Helpwo docs](https://helpwo.laintas.com/docs/#environments).'),
          },
        ],
      },
      {
        id: 'apps',
        title: L('托管应用', 'Hosted applications'),
        blocks: [
          {
            t: 'p', ...L('`/app` 在独立的子终端里运行一个已登记的应用，并给它配一个专属 Agent。应用清单放在 `~/.laintas/apps/*.json` 或 `./.laintas/apps/*.json`，启动前必须用 `/app trust <name>` 信任，清单一改就要重新信任。',
              '`/app` runs a registered application in its own sub-terminal with a dedicated agent. Manifests live in `~/.laintas/apps/*.json` or `./.laintas/apps/*.json`; each must be trusted with `/app trust <name>` before it starts, and again after it changes.'),
          },
          {
            t: 'code', label: L('应用清单', 'Manifest'), code: `{"name": "notes", "description": "…", "command": "node server.js",
 "app_url": "http://127.0.0.1:3000",
 "prompt": "What this app's agent is for.", "persistence": "none", "port": 8123,
 "session_tools": ["shell.exec"], "auto_approve": false,
 "max_sessions": 10, "session_idle_minutes": 30}`,
          },
          {
            t: 'p', ...L('应用可以为它的每个用户申请一个隔离会话（独立的子终端和 Agent）。CLI 只提供隔离；用户、计费和协议由应用自己负责。',
              'An application can ask for an isolated session per user — its own sub-terminal and agent. The CLI provides the isolation; users, billing and protocol are the application’s to define.'),
          },
          {
            t: 'warn', title: L('隔离的边界', 'Where isolation ends'),
            ...L('所有会话都以运行 CLI 的操作系统用户身份执行。独立的 home 目录只能防止 Agent 互相加载数据，不能阻止 Shell 命令读取别的目录。面向不受信任的用户并开放 `shell.exec` 时，请在容器或专用账户中运行 CLI。',
              'Every session runs as the operating-system user running the CLI. Separate homes keep agents from loading each other’s data; they do not stop a shell command from reading another folder. If end users are untrusted and have `shell.exec`, run the CLI in a container or under a dedicated account.'),
          },
        ],
      },
    ],
  },

  // ────────────────────────────────────────────────────────── reference
  {
    title: L('参考', 'Reference'),
    sections: [
      {
        id: 'commands',
        title: L('命令一览', 'Command reference'),
        lead: L('以下是 CLI 本体的命令。`/help` 会列出你所安装版本的完整命令，包括已安装扩展的命令（扩展命令见 [官方扩展](#official-extensions)）；`/help <command>` 显示某个命令的用法。',
          'These are the CLI’s built-in commands. `/help` lists everything in your installed version, including commands from installed extensions (see [Official extensions](#official-extensions)); `/help <command>` shows one command’s usage.'),
        blocks: [
          { t: 'h', id: 'basics', ...L('基础', 'Basics') },
          {
            t: 'table', head: L(['命令', '说明'], ['Command', 'Description']),
            rows: L([
              ['`/help [command]`', '命令帮助'],
              ['`/cwd`', '显示工作目录'],
              ['`/fg`', '回到用 `Ctrl+]` 转入后台的命令'],
              ['`/messages`', '阅读平台通知（别名 `/msg`）'],
              ['`/scan`', '列出 PATH 上面向用户的命令'],
              ['`/img <path> [question]`', '读取图片：提问或转写文字'],
            ], [
              ['`/help [command]`', 'Command help'],
              ['`/cwd`', 'Show the working directory'],
              ['`/fg`', 'Return to a command detached with `Ctrl+]`'],
              ['`/messages`', 'Read platform notices (alias `/msg`)'],
              ['`/scan`', 'List user-facing commands on PATH'],
              ['`/img <path> [question]`', 'Read an image: ask about it or transcribe it'],
            ]),
          },
          { t: 'h', id: 'account-session', ...L('账户与会话', 'Account and session') },
          {
            t: 'table', head: L(['命令', '说明'], ['Command', 'Description']),
            rows: L([
              ['`/login`', '重新登录'],
              ['`/usage`', 'AI 用量：本地 token 统计 + Laintas 账户用量；`buy` 购买调用包或存储'],
              ['`/resume`、`/fork`、`/new`', '切换、分支、新建会话'],
              ['`/quit`、`/exit`', '退出（保留登录）/ 退出登录并退出'],
              ['`/back`', '从子终端脱离'],
              ['`/v`', '版本与更新'],
              ['`/password`', '本地密码库'],
              ['`/training`', '训练数据共享（云端）与本地采集的开关'],
              ['`/extensions`', '安装、管理和发布扩展'],
            ], [
              ['`/login`', 'Sign in again'],
              ['`/usage`', 'AI usage: local token stats plus your Laintas usage; `buy` buys calls or storage'],
              ['`/resume`, `/fork`, `/new`', 'Switch, branch or start sessions'],
              ['`/quit`, `/exit`', 'Exit keeping the session / sign out and exit'],
              ['`/back`', 'Detach from a sub-terminal'],
              ['`/v`', 'Version and updates'],
              ['`/password`', 'Local password vault'],
              ['`/training`', 'Training-data sharing (cloud) and local capture'],
              ['`/extensions`', 'Install, manage and publish extensions'],
            ]),
          },
          { t: 'h', id: 'agents-terminals', ...L('Agent 与终端', 'Agents and terminals') },
          {
            t: 'table', head: L(['命令', '说明'], ['Command', 'Description']),
            rows: L([
              ['`/name [new-name]`', '查看或设置当前 Agent 名称'],
              ['`/hire`、`/agent`、`/agents`', '雇佣、切换、总览 Agent'],
              ['`/spawn`、`/tell`、`/abort`', '派生子 Agent、发消息、中止'],
              ['`/station`', 'Agent 与终端管理器（别名 `/st`）'],
              ['`/term`、`/send`、`/terminate`', '终端的创建、输入、关闭（`/t` 打开终端浏览器）'],
              ['`/helpwo`、`/app`、`/shared`', 'Helpwo、托管应用、共享文件'],
              ['`/handoff`', '把工作以文件形式交接给下一个人'],
            ], [
              ['`/name [new-name]`', 'Show or set the current agent’s name'],
              ['`/hire`, `/agent`, `/agents`', 'Hire, switch to, and overview agents'],
              ['`/spawn`, `/tell`, `/abort`', 'Spawn a sub-agent, message one, abort one'],
              ['`/station`', 'Agent and terminal manager (alias `/st`)'],
              ['`/term`, `/send`, `/terminate`', 'Create, feed and close terminals (`/t` opens the terminal browser)'],
              ['`/helpwo`, `/app`, `/shared`', 'Helpwo, hosted applications, shared files'],
              ['`/handoff`', 'Hand work to the next person as a file'],
            ]),
          },
          { t: 'h', id: 'planning-tasks', ...L('计划与任务', 'Planning and tasks') },
          {
            t: 'table', head: L(['命令', '说明'], ['Command', 'Description']),
            rows: L([
              ['`/mode`、`/plan`', '模式、计划'],
              ['`/task`、`/retask`、`/work`', '任务清单、交给你的待办、工作图'],
              ['`/workflow`、`/hwo`、`/hwg`', '多阶段工作流、编排、图工作流'],
              ['`/prompt`', 'Prompt Lab：测试过的提示词覆盖层'],
              ['`/evolve`', '生成、测试、热加载项目扩展'],
            ], [
              ['`/mode`, `/plan`', 'Modes and plans'],
              ['`/task`, `/retask`, `/work`', 'Task list, work handed to you, work graph'],
              ['`/workflow`, `/hwo`, `/hwg`', 'Multi-phase workflows, orchestration, graph workflows'],
              ['`/prompt`', 'Prompt Lab: tested prompt overlays'],
              ['`/evolve`', 'Generate, test and hot-load project extensions'],
            ]),
          },
          { t: 'h', id: 'config-tools', ...L('配置与工具', 'Configuration and tools') },
          {
            t: 'table', head: L(['命令', '说明'], ['Command', 'Description']),
            rows: L([
              ['`/model`、`/config`、`/theme`、`/stream`', '模型、配置、配色、流式预览'],
              ['`/policy`、`/trust`、`/hooks`、`/backend`', '安全策略、项目信任、钩子、后端'],
              ['`/web`', '联网搜索与抓取：引擎、代理、Cookie、诊断（别名 `/search`）'],
              ['`/identity`', 'Agent 可用于浏览的已保存登录身份'],
              ['`/windows`', '访问 CLI 所在的 Windows 主机'],
              ['`/tools`、`/tool <name>`', '列出工具、直接调用某个工具'],
              ['`/skill`、`/mcp`', '技能、MCP 服务器'],
              ['`/memory`、`/prop`', '记忆、查看完整上下文'],
              ['`/debug`、`/why`、`/detail`', '调试记录、解释失败、执行细节'],
              ['`/max`', '解除本进程的运行时上限'],
            ], [
              ['`/model`, `/config`, `/theme`, `/stream`', 'Model, settings, colours, streaming preview'],
              ['`/policy`, `/trust`, `/hooks`, `/backend`', 'Security policy, project trust, hooks, backends'],
              ['`/web`', 'Web search and fetch: engines, proxy, cookies, diagnostics (alias `/search`)'],
              ['`/identity`', 'Saved logins the agent may browse as'],
              ['`/windows`', 'Reach the Windows machine the CLI runs inside'],
              ['`/tools`, `/tool <name>`', 'List tools, call one directly'],
              ['`/skill`, `/mcp`', 'Skills, MCP servers'],
              ['`/memory`, `/prop`', 'Memory, inspect the full context'],
              ['`/debug`, `/why`, `/detail`', 'Debug entries, explain a failure, execution detail'],
              ['`/max`', 'Lift runtime limits for this process'],
            ]),
          },
          { t: 'h', id: 'history', ...L('历史', 'History') },
          {
            t: 'table', head: L(['命令', '说明'], ['Command', 'Description']),
            rows: L([
              ['`/snapshot`、`/snapshots`、`/undo`', 'Git 检查点'],
              ['`/compact`、`/continue`、`/told`', '压缩上下文、继续、回放'],
              ['`/reload`', '重新加载默认文件并重启'],
            ], [
              ['`/snapshot`, `/snapshots`, `/undo`', 'Git checkpoints'],
              ['`/compact`, `/continue`, `/told`', 'Compact, continue, replay'],
              ['`/reload`', 'Reload default files and restart'],
            ]),
          },
        ],
      },
      {
        id: 'troubleshooting',
        title: L('常见问题', 'Troubleshooting'),
        blocks: [
          { t: 'h', ...L('调用被拒绝，提示余额或额度', 'A call is refused over balance or allowance') },
          {
            t: 'p', ...L(`这类拒绝都没有扣费。错误码的含义和处理方法见 [平台文档：计费相关的拒绝](${PLATFORM}#errors)。用 \`/usage\` 查看当前额度。`,
              `Nothing is charged for these refusals. The codes and what to do about each are in the [platform documentation](${PLATFORM}#errors). Check your allowance with \`/usage\`.`),
          },
          { t: 'h', ...L('看起来卡住了', 'It looks stuck') },
          {
            t: 'list', ...L([
              '先看是否在等待审批：`auto` 模式下审批有限时窗口，其他模式会一直等你回答。',
              '可能是命令本身在长时间运行（例如大目录上的 `find`）。按 `Esc` 打断，再给出更具体的范围。',
              '上下文接近上限时会先压缩；`/compact status` 可以看到压缩是否在进行。',
            ], [
              'Check whether it is waiting for an approval: in `auto` mode approvals have a timed window; other modes wait for your answer.',
              'The command itself may be long-running — a `find` over a large tree, say. Press `Esc` and give a narrower scope.',
              'Near the context limit it compacts first; `/compact status` shows whether that is running.',
            ]),
          },
          { t: 'h', ...L('Windows 上找不到文件', 'Files not found on Windows') },
          {
            t: 'p', ...L('CLI 运行在私有 WSL 发行版中，Windows 磁盘挂载在 `/mnt/<盘符>/` 下，例如 `C:\\Users\\me\\project` 对应 `/mnt/c/Users/me/project`。',
              'The CLI runs inside its private WSL distribution, where Windows drives are mounted under `/mnt/<drive>/` — `C:\\Users\\me\\project` is `/mnt/c/Users/me/project`.'),
          },
          { t: 'h', ...L('报告问题', 'Reporting a problem') },
          {
            t: 'p', ...L('在 [GitHub](https://github.com/lin7c/Laintas_cli) 提交问题时，附上 `/v` 的版本号、操作系统，以及 `/why` 或 `/debug` 的相关输出（注意先删去其中的敏感信息）。账户与账单问题请通过 [Laintas 联系页](https://laintas.com/contact)。',
              'When filing an issue on [GitHub](https://github.com/lin7c/Laintas_cli), include the version from `/v`, your operating system and the relevant `/why` or `/debug` output — with anything sensitive removed. Account and billing questions go to the [Laintas contact page](https://laintas.com/contact).'),
          },
        ],
      },
    ],
  },
];
