# Laintas CLI Agent 能力优化方案

## 目标

在不破坏现有会话、工具别名和项目配置的前提下，提高工具选择准确率、长期记忆召回率、执行可靠性和故障可恢复性。所有阶段均采用兼容迁移、独立开关、回归测试和可回滚提交。

## 基线指标

每个版本记录以下指标，避免仅凭主观体验优化：

- 每个任务的输入/输出 token、模型调用次数和工具调用次数
- 工具选择成功率、schema 校验失败率、策略拦截率
- 重复文件读取率、相同失败重复率、循环退出原因
- 记忆召回命中率、用户纠正率、过期记忆使用率
- 任务完成率、恢复成功率、P50/P95 工具耗时

事件只记录结构化元数据和截断摘要，不记录认证信息或完整敏感内容。

## 阶段 1：能力边界一致性

状态：已实施。

1. 模型可见工具与运行时可执行工具使用同一个过滤结果。
2. `mem.read/save/list/delete` 统一使用 `memory_system.py`。
3. 记忆名称强制为安全 slug，解析后的路径不得逃逸存储目录。
4. 内建 schema 校验覆盖嵌套对象、数组、enum、范围、pattern 和额外字段。
5. 需要审批的文件写入在无审批通道或策略异常时 fail closed。

验收：定向能力、安全和终止语义测试全部通过；全量测试无新增失败。

## 阶段 2：ToolExecutor

新增单一执行入口，调度循环不再直接调用各工具：

```text
ToolExecutor.execute(call, context)
  -> validate
  -> authorize
  -> acquire concurrency group
  -> execute with deadline/cancellation
  -> normalize result
  -> emit telemetry
```

工具元数据增加：

- `side_effect`: none / local_write / process / network_write / control
- `idempotent`: 是否允许自动重试
- `timeout_seconds`: 默认 deadline
- `concurrency_group`: terminal、browser-session、filesystem-path 等互斥域
- `output_limit`: 返回给模型的最大输出

只读、幂等工具可对瞬时错误重试一次。写操作、终端输入、浏览器点击和 Agent 控制不自动重试。Python 扩展若需要强制终止，必须放入隔离子进程；线程超时只能停止等待，不能安全停止副作用，因此不作为最终方案。

验收：超时、取消、重试、同终端串行和异常扩展隔离均有测试。

## 阶段 3：上下文与文件缓存

建立统一 `ContextBudget`，预算包含 system prompt、native tool schemas、message thread、临时 live state 和预留输出，而非只估算 thread。

文件读取缓存键：

```text
(realpath, offset, limit, content_hash)
```

写入、编辑、移动、删除后按路径失效；外部修改通过 stat/hash 变化失效。压缩摘要保存目标、已完成工作、关键决策、文件版本、错误和下一步，并校验 assistant tool call 与 tool result 不被拆开。

验收：长任务压缩后的事实保留率、重复读取率和真实 provider token 偏差进入基准测试。

## 阶段 4：长期记忆

统一接口：

```text
memory.search(query, scope, type, limit)
memory.get(name)
memory.upsert(name, body, provenance, confidence, expires_at)
memory.forget(name)
```

召回排序结合 scope、关键词、importance、recency；embedding 为可选增强，不作为基础运行依赖。记忆保存 provenance、created_at、updated_at、last_verified_at、confidence 和 expires_at。写入冲突时保留历史版本并要求模型显式处理矛盾。

用户偏好和反馈可跨项目；项目事实默认仅当前项目可见。外部网页、工具输出和项目文件中的指令不得自动升级为用户记忆。

验收：跨项目隔离、冲突、过期、删除、完整正文召回和 prompt injection 测试通过。

## 阶段 5：持久化收敛

`event_log` 作为追加式事实来源，session、resume、agent state 和 WorkGraph 是带 revision 的投影。每次写入包含 `session_id/run_id/revision`；恢复时检测冲突，禁止按文件时间隐式覆盖。

迁移步骤：

1. 新格式双写并校验投影一致性。
2. 读取优先新格式，失败时回退旧格式并产生诊断。
3. 稳定两个版本后停止旧格式写入。
4. 最后提供显式迁移/清理命令，不自动删除旧数据。

验收：在 prompt admission、模型响应、工具调用前后和投影写入期间注入崩溃，均可恢复到明确状态且不重复副作用。

## 阶段 6：安全并行

仅并行执行无依赖、无副作用且 concurrency group 不冲突的调用，例如不同文件的 read/grep/glob。写操作默认按模型顺序执行；同一终端、浏览器 session 和目标文件始终串行。

一轮超过调用上限时，为未执行调用生成明确 tool result，保证协议配对完整。

验收：结果顺序稳定、取消可传播、部分失败可见，不出现交叉终端输出或文件写竞争。

## 发布与回滚规则

- 每阶段独立提交，不混合无关 UI 或发布改动。
- 新执行路径先由配置开关控制，旧路径保留一个版本。
- 合并前运行全量 unittest、语法编译和关键端到端模拟。
- 指标恶化或出现恢复不一致时关闭开关，不进行破坏性数据回滚。
- 不承诺“绝对无 bug”；以失败封闭、回归覆盖、观测和可回滚性控制风险。
