# laintas-cli /agents 配色跟随设计方案

> 依据：`/root/laintas_cli/agents_mode.py:160-270`（`/agents` 全屏视图 STYLE 字典）、
> `/root/laintas_cli/laintas_cli.py:5089-5142`（dark/light/mono 三主题调色板）、
> `/root/laintas_cli/laintas_cli.py:21490-21595`（`/agents --plain` 行内输出）。

## 一、/agents 输出配色分析（R1）

`/agents` 全屏视图（agents_mode.py）的整体基调是 **GitHub Primer Dark**：深色画布
`#0d1117`，面板底色 `#0a0e13`/`#161b22`，前景灰白阶 `#f0f6fc → #e6edf3 → #c9d1d9 →
#b1bac4 → #8b949e → #6e7681 → #484f58`。语义色取 GitHub 状态色：成功/运行绿
`#3fb950`（亮绿 `#4ade80` 用于标题与输入光标）、错误红 `#f85149`、警示黄系
`#d29922`（badge、thinking、approval 边框）与 `#e3b341`（approval/警告文字）、
信息蓝 `#58a6ff`、Agent/消息紫 `#a78bfa`/`#d2a8ff`、链接蓝 `#1f6feb`、diff 标注
`#a371f7`。交互背景层：hover `#10151c`、选中 `#161b22`、强调选中 `#21262d`、
通用边框 `#30363d`、分隔线 `#262c36`。

`/agents --plain`（行内列表）用 Rich 标记映射到同一语义：agent ID `bold`（白）、
状态 `dim`、inbox `dim yellow`、指向终端 `cyan`、前景/输入标记 `bold green`、
未读计数 `muted`、找不到时 `red`、用法错误 `yellow`。

CLI 全局还有 light / mono 两个主题（laintas_cli.py:5092-5096），为可访问性提供
无色对照方案。

## 二、设计 token（跟随 /agents 配色）

### 1. 基础层（Surface / Text）
| Token | 值 | 用途 |
|---|---|---|
| `bg.canvas` | `#0d1117` | 根背景 |
| `bg.panel` | `#0a0e13` | 面板/侧栏背景 |
| `bg.card` | `#161b22` | 选中卡片、行内代码背景 |
| `bg.row` | `#11161d` | 工具行背景 |
| `bg.hover` | `#10151c` | 悬停 |
| `bg.selected` | `#21262d` | 强调选中（菜单当前项） |
| `fg.highest` | `#f0f6fc` | 标题、用户文本、光标行 |
| `fg.high` | `#e6edf3` | 正文、工具名 |
| `fg.mid` | `#c9d1d9` | 名称、表头 |
| `fg.body` | `#b1bac4` | 说明正文 |
| `fg.muted` | `#8b949e` | 次要信息（muted/queued/idle） |
| `fg.subtle` | `#6e7681` | 标签、元信息 |
| `fg.dim` | `#484f58` | 行号、gutter、占位符 |

### 2. 语义层（Status）
| Token | 值 | 语义 |
|---|---|---|
| `accent.green` | `#3fb950` | 运行中/成功/done/光标/滚动强调 |
| `accent.green.bright` | `#4ade80` | 品牌标题、输入提示符 |
| `accent.green.deep` | `#2ea043` | 脉冲/按钮主底色（配白字） |
| `error.red` | `#f85149`（浅文本 `#ffa198`） | 错误、diff 删除 |
| `warn.amber` | `#e3b341` | 警示文字、审批 |
| `warn.gold` | `#d29922` | badge 底（配 `#0d1117` 字）、thinking、审批边框 |
| `info.blue` | `#58a6ff` | toast info |
| `link.blue` | `#1f6feb` | 主按钮底（白字）、pill |
| `violet.agent` | `#a78bfa` | Agent 名/消息（bold） |
| `violet.bright` | `#d2a8ff` | Agent 标题 |
| `violet.deep` | `#a371f7` | diff hunk |

### 3. 线与层
边框 `#30363d`、分隔线 `#262c36`、选择高亮 `bg:#264f78 + #ffffff`。

## 三、应用规则（跟随 /agents 的用法约定）

1. **状态优先用绿系，而非蓝**：running/done/输入光标/主按钮都用 `#3fb950`–`#2ea043`
   绿阶；蓝色（`#1f6feb`/`#58a6ff`）只作链接与信息提示，不承载"运行中"语义。
2. **Agent 身份一律紫**：`#a78bfa`（名称，bold）、`#d2a8ff`（标题/强调）、与用户
   `#f0f6fc` 形成对比。
3. **黄有两档**：正文警示 `#e3b341`，带背景的 badge/边框用 `#d29922`（badge 上配
   深底字 `#0d1117` 保证对比）。
4. **信息层级靠灰阶，不靠加色**：次级信息在 `#8b949e → #6e7681 → #484f58` 三档
   之间选，与 /agents 的 muted/subtle/dim 完全对应。
5. **交互背景分四层**：`#0d1117`（画布）→ `#10151c`（hover）→ `#161b22`（选中）
   → `#21262d`（强调选中），只用背景变化，不改前景色。
6. **彩色仅限短语义片段**：整行/整块保持灰白阶，色只落在状态标记、名称、按钮上
   （与终端输出风格规范一致）。
7. **对比度**：主要前景对 `#0d1117` 均满足 WCAG AA；`#484f58` 仅用于装饰性
   gutter，不用于必读文本。

## 四、明暗与无色主题

跟随 CLI 的三主题结构：
- **dark**（默认）：上表全部 token。
- **light**：换用 CLI light 调色板（`#24292f` 正文、`#176f2c`/`#116329` 绿、
  `#9a6700` 黄、`#57606a`/`#6e7781` 灰、`#8250df` 紫、`#f6f8fa` 画布、
  `#d0d7de` 选中底、错误红 `#cf222e`）。
- **mono**：全部去色，仅以 bold / reverse 表达层级，语义不依赖颜色（可访问性）。

## 五、落地方式

- Rich 标记语法中的近似映射：`accent.green`→`green`、`warn`→`yellow`、
  `error.red`→`red`、终端→`cyan`、次要→`dim`、身份→`magenta`。
- Web/桌面 UI 直接消费第二节 token；建议导出为 CSS variables / design tokens
  JSON，键名与上表一致，便于与 agents_mode.py 的 STYLE 字典逐条对照维护。
