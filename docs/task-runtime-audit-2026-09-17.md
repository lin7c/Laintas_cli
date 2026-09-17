# 近期任务耗时与工具错误审计（2026-09-17）

本次只做分析和最小复现，未修改运行逻辑。此前上下文压缩修复仍保留在工作区。

## 范围与统计口径

- 时间：2026-09-15 00:00 至 2026-09-17 13:25 UTC。
- 来源：`/root/.laintas/events.jsonl`、各项目 `.laintas/events.jsonl`、持久化会话、主模型请求上下文快照，以及网关 `ai-gateway-error-5.log`。
- 按 event_id 去重，关联 session_id / run_id / call_id；核对持久化会话，避免将无工具执行的测试会话作为真实任务样本。
- 记录到 2,180 个工具结果，75 个 ok=false（3.44%）。其中 shell.exec 51、fs.read 12、task.complete 4、fs.ls 2、fs.edit 2、fs.multi_edit 2、await_spawns 1、agent.spawn 1。
- 75 个非成功结果含 10 个 advisory（重复读取 7、完成前测试提醒 3）、5 个策略拒绝、46 个 shell 非零退出和 14 个其他结果。非零退出包含正常无匹配、验证探针和真实失败，不能全部解释成框架故障。反之，搜索漏扫却返回成功不会计入失败率。
- tool_call → tool_result 是工具阶段墙钟时间，可能包括审批，不能直接视为 CPU/磁盘执行时间。tool_result → 下一 ai_response 包含模型、网络、压缩和本地处理，不能直接视为模型推理时间。
- 网关统计跨请求来源，不能把全部重试或辅助调用时长直接归入某一个 CLI 任务。

## 最明确的慢任务案例

会话 `8ebbd7eb01834d9a`，run `74a7b8296b36495090df9dc5c83fa528`：询问 GLM-5.3 为什么显示约 151.5k 可用上下文。

- 30 轮模型响应，58 次工具调用，其中 grep 34、read 17。
- 墙钟约 726.9 秒（12.12 分钟），配对工具阶段合计仅 8.57 秒。
- 请求周期等待合计约 718.55 秒，单次中位数约 16.95 秒。
- 第 6、7、9 轮重复读错误路径；第 11—20 轮已读取预算相关函数，之后仍持续搜索显示文案，最终因 30 轮上限结束。
- 关键显示代码在 `laintas_cli.py:22415`，该文件超过 grep 默认大小限制，搜索工具静默漏扫。PWD 只解释其中一部分重复，不能解释后半段持续找不到文案。
- 主要日志入口：`/root/.laintas/events.jsonl:24484`；第 13 轮搜索 `summarized` 位于同文件 24553 行。

全样本只读工具中位耗时：fs.read 0.047 秒、fs.grep 0.083 秒。shell.exec 中位 0.20 秒、P90 约 14.58 秒（成功配对样本）。优化重点应是减少无效请求轮数和阻塞等待，而非先优化本地文件 I/O。

## 已确认的问题

### 1. grep 将漏扫伪装成无匹配（最高优先级）

`tools.py:_bi_fs_grep` 默认 `max_file_size=1048576`，超限直接 continue，不返回跳过文件或 incomplete 提示；工具 schema 未暴露这个参数。

当前 `laintas_cli.py` 为 1,271,845 字节。对这个明确指定的文件搜索 `summarized`：

- 默认参数：ok=true、matches=0、files_scanned=0、truncated=false。
- 内部调用增加 max_file_size=5000000：matches=3、files_scanned=1。

这不是模型漏看结果，而是工具向模型提供了不完整的否定证据。应优先改为流式扫描明确指定的文本文件，并将任何跳过、预算截止或读取失败报告为不完整搜索。大小限制如保留，应暴露在 schema 中。

### 2. grep 的 include 语义与模型用法不一致

`_glob_matcher` / `_glob_walk_plan` 将 `*.ts` 限制为当前层，递归必须用 `**/*.ts`。描述只有“comma-separated glob patterns”，没有强调区别。

在 `/root/Helpwo/src` 搜索 `usable`：`include=*.ts` 仅扫描 1 个文件，返回零匹配；`include=**/*.ts` 能找到子目录结果。日志中真实存在前一种调用。

这是接口可用性问题，不是 glob 实现必然错误。需明确模式语义、返回搜索范围和扫描数量，并对明显空范围给出可操作建议。

另：结果 file 相对于 ctx.cwd，而输入 path 可以是另一个搜索根；结果缺少明确的基准字段，容易导致重复拼接目录。这与用户已发现的 PWD 问题相关。

### 3. 持久 shell 的选项会污染后续调用（最高优先级）

`shell_payload_for_pty` 将命令放进函数，多行命令经 eval 执行，仍共享当前 shell。`set -e`、trap、shell options 没有按调用隔离或恢复。

独立 bash 子进程执行现有包装函数的最小复现：

- `true`：本次和下一条命令结束标记均可输出。
- `set -e; false`：shell 退出，连本次结束标记也没有。
- `set -e; true`：本次正常，但后续普通 false 让 shell 退出，下一条标记消失。

真实日志有两次 `Deployment terminal exited`，其中一次命令显式包含 `set -e`；另有一次 120 秒静默超时。最小复现确认机制缺陷，但不能据此将所有终端退出/超时都归因于 set -e。

修复应定义允许持久化的 cwd/env 状态，隔离其余 shell 控制状态；测试需覆盖本次失败和跨调用污染，不能简单吞掉非零退出码。

### 4. 安全规则误把命令参数当作危险程序

`policy.py:165` 的 `\b(?:format|diskpart|bcdedit|bootrec)(?:\.exe)?\s`，配合 `rule.search(variant)`，会命中 `docker ps --format` 和 `docker inspect ... --format`。

两条真实 docker 命令均被拒；正则最小复现也确认命中。应匹配解析后的可执行命令位置，并保留对实际磁盘格式化命令的拦截，而不是关闭策略。

### 5. 辅助模型额度耗尽未被识别，持续重复重试

网关 `_quota_exhausted` 的 marker 列表不含 Cloudflare 的 `used up your daily free allocation of 10,000 neurons`。抽取当前纯函数后输入该真实错误文案，结果为 False。

因此该 429 被当成短期拥堵，先进行 3 次退避，再 failover；下一次辅助请求又重复访问同一失效上游。

网关日志中额度耗尽后 failover 次数：9 月 15 日 53、16 日 52、17 日截至 13:25 为 193，共 298 次。同期 Retry 日志共 897 条，不能假设它们全属于同一错误或同一客户端。

应按供应商识别不可短时恢复的额度错误，进入冷却并及时选可用上游；不能将所有 429 一律熔断。

### 6. “非成功”与真正错误混用，恢复动作缺少针对性

- grep 无匹配导致 shell exit=1，部分清理验证本来就期望无匹配。
- 7 次 fs.read 是“已经可见”的 advisory，其中一个范围连续尝试 3 次；多次拒绝没有帮助模型改变策略。
- 3 次 task.complete 是测试提醒，另一次被未完成 TASK 拦截。
- 手写验证脚本存在真实错误：假设 pyte 有 `__version__`、假设 Catalog 可迭代、把 pg 检查脚本放在 /tmp 后依赖解析失败等。这些不是 PWD 修复能全部解决的问题，也不都属于工具框架 bug。

建议保留真实退出码，同时区分 success / no_match / advisory / blocked / execution_error；向恢复策略传结构化 error_code 和明确下一步，避免同参数重复重试。不能把所有 exit=1 自动改成成功。

## 其他耗时来源及证据边界

- 同期网关记录 1,738 次 GLM-5.3 请求开始；其中 471 次为 max 档。Gemma 辅助请求中也有 120 次 max 档。固定思考档位会影响辅助任务；应按任务类型单独限定，而非一律跟随主模型。
- 1,009 条 GLM-5.3 SI_ROUTE 记录中，输入中位 69,329 tokens、最大 129,907；177 条发生多次供应商尝试。大上下文和重试会提高单轮成本，但日志不足以量化各自增加多少秒。
- 三个最近真实会话按 run_id 关联得到 89 次 critic_assessment、15 次 intent_started。部分辅助流程已在后台；不能把辅助调用耗时全部加到主任务关键路径。
- 一次 await_spawns 工具阶段约 1,200.5 秒。多次 gh run watch 等待 CI 约 642—746 秒。这些是实际等待外部工作，应区分于推理慢；可通过后台状态与完成通知改善交互。
- 某些 fs.edit 阶段超过十分钟，可能包括审批或用户停顿。没有独立 approval_wait_ms / execute_ms，不能将其当成磁盘写入慢。
- 当前上下文快照保留了最近 170 个 main_loop 请求，均为 GLM-5.3；没有完整的每阶段延迟、TTFT 和辅助请求关联，不能据此作不同模型速度排名。

## 建议处理顺序

1. 修 grep 漏扫及不完整结果表达，补 >1 MB 文件与递归 glob 回归。
2. 隔离持久 shell 控制状态，补 set -e / pipefail / trap / exit 回归。
3. 修网关 Cloudflare 额度错误识别和冷却，避免每次辅助调用重新支付退避成本。
4. 修策略对 --format 的误匹配，保留真实危险命令防护。
5. 统一工具结果分类及重复失败恢复，澄清路径和 include 语义。
6. 补全 request_id 贯通的模型等待、首 token、生成、压缩、审批、工具执行和外部等待耗时，再决定辅助调用频率、思考档位及并行调度调整。

本次没有更改模型配置、停止用户服务或重放真实任务；最小复现仅启动短生命周期的独立 bash 子进程。

## 修复状态（2026-09-17 同日）

| # | 问题 | 处理 | 位置 |
|---|------|------|------|
| 1 | grep 漏扫伪装成无匹配 | 显式指定的文件不限大小；目录遍历默认上限 1 MB→8 MB，跳过/读失败的文件列入 `skipped_files` 并置 `truncated`+`incomplete`；schema 暴露 `max_file_size`；结果带 `paths_relative_to` | `tools.py:_bi_fs_grep` |
| 2 | `include=*.ts` 只搜一层 | 不含 `/` 的模式按任意深度匹配（同 ripgrep）；含 `/` 的保持锚定；schema 写明 | 同上 |
| 3 | 持久 shell 被 `set -e` 污染 | 开启 errexit 的命令在子 shell 里跑（本次仍按 set -e 中止、退出码真实，但 cd/export 不持久）；每次调用前后快照并恢复 errexit/nounset/pipefail/xtrace 等选项及 ERR/RETURN/DEBUG trap，兜住 `source x.sh` 这类识别不到的情况 | `tools.py:shell_payload_for_pty` |
| 4 | `--format` 被当成磁盘格式化 | 规则只匹配程序位置；`_migrate_config` 从已保存的 `~/.laintas/policy.json` 移除旧规则；已 sync 到 Helpwo kernel vendor | `policy.py` |
| 5 | Cloudflare 日额度耗尽被当拥堵 | 加 `daily free allocation` 标记：不再退避重试，直接 park（上限 30 分钟）并 failover；ai-gateway 已重启上线 | `agent_gateway/gateway.py` |
| 6a | grep 无匹配 exit 1 算失败 | 最后一段是 grep/rg/git grep 且 exit 1、无输出 → `ok=true, outcome=no_match`，保留 `returncode=1` | `tools.py:_classify_shell_result` |
| 6b | "已可见"重复拒绝形成循环 | 同一范围只拒一次，第二次直接返回内容 | `tools.py:_read_already_visible` |

未做：完整的 success/no_match/advisory/blocked/execution_error 统一分类、辅助调用思考档位按任务限定、await_spawns / CI 等待改后台通知、request_id 贯通的分阶段耗时。
