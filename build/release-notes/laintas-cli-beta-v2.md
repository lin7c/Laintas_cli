# Laintas CLI beta v2 (1.32.5b2)

Laintas CLI beta v2（包版本 1.32.5b2）现已发布。

- Thinking 期间可以直接输入并回车追加指令；连续提交的 append 内容以分支保留显示，避免额外执行信息打断连线。
- 新增多账户工作环境：账户各自拥有会话、设置与运行数据，当前终端的账户选择独立保存。可使用 /account switch 切换，或在不同 CLI 中同时使用 A、B 账户。
- 新增显式任务交接与旧任务认领：通过 /handoff 保存和接收任务上下文，接收方创建自己的续做会话，原账户的任务保留；旧任务通过 /account legacy 与 /account adopt 明确认领。
- 在 /term 上扩展两层终端关系认定：控制终端提出邀请，执行终端明确接受后，其 Agent 以终端前缀纳入现有父子调度。保持两层结构，禁止继续创建第三层终端。
- 同机终端配对可在明确接受后跨账户使用；跨服务器配对要求同一账户，并需要配套 Helpwo Kernel/Gateway 支持。普通本地终端创建不依赖远端服务，配对不可用时可退回本地 shell。
- 修复并发文件锁、账户路径初始化与错账户写入问题；账户切换先验证重启命令并保存任务，准备失败保留当前会话，拆卸后重启失败则恢复账户选择并提示重新启动。
- 修复切换时重复 autosave、快照归属冲突缺少诊断，以及旧任务/应用启动参数遗留的问题。首次登录后的重启保留原启动任务。
- Linux 独立包按声明安装完整核心依赖；Linux 与 macOS 构建增加 --version、--help 启动检查。

下载与安装：https://cli.laintas.com/
已安装用户可运行 /v update 更新。

## English

Laintas CLI beta v2 (package version 1.32.5b2) is now available.

- Type and press Enter to append instructions while an Agent is thinking. Consecutive append messages remain visible as branches, with execution messages kept from interrupting the connecting lines.
- Added account workspaces with separate sessions, settings and runtime data. Account selection is saved per terminal. Use /account switch, or run separate CLIs for concurrent A/B account work.
- Added explicit task handoff and legacy-task adoption. /handoff transfers task context into a receiving account's own continuation session while preserving the source task. Use /account legacy and /account adopt to claim old tasks explicitly.
- Extended /term with a two-level controller/executor relationship. An executor must explicitly accept the controller's invitation before its Agents join the existing parent/child scheduling structure under a terminal prefix. A third terminal level is refused.
- Local pairing supports separate accounts with explicit acceptance. Cross-server pairing requires the same account and compatible Helpwo Kernel/Gateway support. Ordinary local terminal creation does not require remote services and can fall back to a shell if pairing is unavailable.
- Fixed concurrent file locking, uninitialized account paths and writes to the wrong profile. Account switches validate the restart command and save tasks before shutdown. Preparation failures retain the current session; restart failures after shutdown restore account selection and provide recovery instructions.
- Fixed extra autosaves during account switching, missing snapshot ownership diagnostics, and retained task/application launch arguments. Restarts immediately after the first login preserve the original launch task.
- Linux standalone builds install the complete declared core dependency set. Linux and macOS builds now check both --version and --help.

Download and install: https://cli.laintas.com/
Existing users can update with /v update.
