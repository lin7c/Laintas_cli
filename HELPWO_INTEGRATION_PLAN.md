# laintas_cli ↔ HelpwoAI 集成方案

> **状态：HelpwoAI 端已就绪 (2026-05-19)**，等 laintas_cli 升级到本协议后即可端到端工作。
> 本文件是 laintas_cli 这一侧改造的需求与施工图。HelpwoAI 端对应已落地代码：
> `/root/Helpwo/Helpwo/src/tools/remote.ts`、`/root/Helpwo/Helpwo.py` 的 `/api/agents/<id>/send`。

---

## 0. 角色定位回顾

laintas_cli 同时是 **监控者（Monitor）** + **执行者（Executor）**。HelpwoAI 不再把它当成"另一个 AI"，而是当成自己的"远程手脚"：

| 模式 | 触发 | 行为 |
|---|---|---|
| Monitor（默认） | CLI 启动即进入 | 注册 → 心跳 → 周期性汇报本机状态 → 等待 HelpwoAI 指令 |
| Executor | 收到 `kind: exec / query / delegate` | 解析任务包 → 执行 → 推流事件 → 终态 `final` |
| Local Loop | 用户在 CLI 本地输入自然语言 | 沿用现有 `run_agent_loop`（兼容） |

CLI 启动开关：
```
laintas-cli                  # Monitor + Local Loop 都开
laintas-cli --monitor-only   # 只监控、只接受 HelpwoAI 任务、禁用本地 agent loop
```

---

## 1. 通信协议（HelpwoAI 已实现这一侧的客户端）

### 1.1 入站请求（`/poll` 返回的消息体）

旧版 laintas_cli 看到的：
```json
{"id":"abc","content":"...","timestamp":1747...}
```

新版应当看到（HelpwoAI 已经在发了）：
```json
{
  "id":      "req-l3kxx-2",         // 请求唯一 id（= reqId）
  "reqId":   "req-l3kxx-2",
  "kind":    "exec" | "query" | "delegate" | "abort" | "approval-response" | "chat",
  "payload": { ... },               // 见下表
  "ack":     { "needFinal": true, "needStream": true },
  "content": "<人类可读摘要>",       // 兼容老解析器，新代码应忽略
  "timestamp": 1747...
}
```

| kind | payload 字段 | 期望行为 |
|---|---|---|
| `exec` | `command: string`, `cwd?: string`, `timeout?: number(seconds)` | 在 PTY 里执行；推流 stdout/stderr/cmd-start/cmd-end，最后推 `final` |
| `query` | `what: "cwd"\|"files"\|"env"\|"processes"\|"term-snapshot"`, `target?: string` | 只读侦察。立刻收集结果，推一个 `final` 包含数据 |
| `delegate` | `goal: string`, `maxLoops?: number`, `plan?: string[]` | 启动一次本地 agent loop；每步把 ai-command/ai-reply 推流；done 时 `final` |
| `abort` | `targetReqId: string`, `reason?: string` | 中止指定 reqId 的请求；推 `final{status:"aborted"}` |
| `approval-response` | `targetReqId: string`, `decision: "approve"\|"reject"\|"modify"`, `feedback?: string` | 用户对早先 `needs-approval` 的回应；恢复或终止暂停的请求 |
| `chat` | `message: string` | 向后兼容旧版本，等同于现在的处理 |

### 1.2 出站事件（POST `/api/agents/<id>/events`）

`events` 数组里每个事件**必须**带 `reqId`，HelpwoAI 用它做事件归属。

```json
{
  "events": [
    {
      "reqId": "req-l3kxx-2",
      "type":  "stdout" | "stderr" | "ai-reply" | "ai-command" |
               "cmd-start" | "cmd-end" | "metric" | "error" |
               "memory-update" | "terminal-snapshot" |
               "needs-approval" | "final",
      "content": "...",
      "meta": { /* 视 type 而定，见下表 */ }
    }
  ],
  "state": { "cwd": "...", "status": "running" }   // 可选，会被合并进 agent 状态
}
```

| type | meta 关键字段 | 用途 |
|---|---|---|
| `stdout` / `stderr` | — | 实时输出块；HelpwoAI 会做截断聚合 |
| `cmd-start` | `command, cwd` | 标识 PTY 命令开始 |
| `cmd-end` | `exitCode, durationMs` | 标识 PTY 命令结束 |
| `ai-reply` / `ai-command` | — | delegate 模式下本地 loop 的中间产物 |
| `metric` | `loadAvg, memFree, disk` | 60s 一次的本机指标 |
| `terminal-snapshot` | `name, lines[]` | sub-terminal 最近 N 行 |
| `needs-approval` | `summary, steps[], rationale?, command?` | 暂停请求并请用户确认；HelpwoAI 会用 PlanCard 弹窗，用户决定后会回发 `kind: approval-response` |
| `error` | — | 任何运行时错误；不一定终结请求，但建议紧跟一个 `final` |
| `final` | `status: "success"\|"fail"\|"aborted", summary, artifacts?: [{path, size?}]` | **唯一的请求终结信号**。HelpwoAI 收到它才会 resolve 工具调用 |

**关键约定**：
- 一个 `reqId` 对应**恰好一个** `final`。漏发 = HelpwoAI 等到超时（默认 60s，硬上限 5min）才会强制中止。
- 没有 `reqId` 的事件 HelpwoAI 会忽略掉（只保留它们用于 UI agent panel 的实时滚动）。

### 1.3 心跳扩展（POST `/api/agents/heartbeat`）

现行只发 `agentId + cwd`。建议扩展（HelpwoAI 端已经能消费）：
```json
{
  "agentId": "remote-xxx",
  "cwd": "/root/...",
  "shell": "/bin/bash",
  "runningTerminals": [{"name":"srv","alive":true,"cmd":"npm run dev"}],
  "metrics": {"loadAvg":[0.2,0.1,0.0], "memFreeMB":2048, "diskFreeGB":50}
}
```

---

## 2. 监控者职责（默认行为，与 HelpwoAI 解耦）

| 项 | 频率 | 已有？ | 备注 |
|---|---|---|---|
| 注册 | 启动一次 | ✅ | `/api/agents/register` |
| 心跳 | 30s | ✅ | 仅扩展 payload |
| metric 事件 | 60s | ❌ 新增 | `psutil` 取 load/mem/disk |
| terminal-snapshot | 5s 节流，或终端有新输出 | 部分 | 已有 `terminal_tail_lines`，要包成事件推 |
| cwd 同步 | 用户每次 cd / 命令结束 | 部分 | 心跳里带就够 |

监控事件不带 `reqId`，HelpwoAI 用它们刷新 UI 但不会触发任何工具调用。

---

## 3. 执行者职责（推荐"服从模式"）

**强烈建议 laintas_cli 不要自己跑 LLM**，让 HelpwoAI 来当大脑。理由：
- HelpwoAI 已经有完整的 plan/approve、AST 工具、模式控制
- laintas_cli 自己跑 LLM 会双重计费、双重决策、双重 bug
- 让 laintas_cli 退化为"输入命令 → 跑 PTY → 推事件"，bug 面最小

### 3.1 `kind: exec` 最小实现
```python
def handle_exec(req: dict, agent_registry: AgentRegistry):
    cmd     = req["payload"]["command"]
    cwd     = req["payload"].get("cwd") or os.getcwd()
    timeout = req["payload"].get("timeout", 30)
    req_id  = req["reqId"]

    # 1. 策略检查（白/灰/黑名单），灰名单 → push needs-approval 后挂起
    decision = policy.evaluate(cmd, cwd)
    if decision == "block":
        push_final(agent_registry, req_id, "fail", f"Blocked by policy: {cmd}")
        return
    if decision == "needs-approval":
        push_needs_approval(agent_registry, req_id, cmd, cwd)
        approval = wait_for_approval(req_id, timeout=300)   # 阻塞或异步状态机
        if approval != "approve":
            push_final(agent_registry, req_id, "aborted", f"User {approval}")
            return

    # 2. 跑 PTY
    push(agent_registry, req_id, "cmd-start", "", {"command": cmd, "cwd": cwd})
    session = InteractiveSession(cmd, cwd=cwd)
    session.start()
    while session.is_alive():
        chunk = session.read_output(timeout=0.2)
        if chunk:
            push(agent_registry, req_id, "stdout", chunk)
    exit_code = session.close()
    push(agent_registry, req_id, "cmd-end", "", {"exitCode": exit_code})

    # 3. 终态
    status = "success" if exit_code == 0 else "fail"
    push_final(agent_registry, req_id, status, f"exit={exit_code}")
```

### 3.2 `kind: query` 最小实现
```python
def handle_query(req, agent_registry):
    what = req["payload"]["what"]
    req_id = req["reqId"]
    if what == "cwd":      data = os.getcwd()
    elif what == "files":  data = os.listdir(os.getcwd())
    elif what == "env":    data = dict(os.environ)
    elif what == "processes":
        data = [{"pid": p.pid, "name": p.name()} for p in psutil.process_iter()]
    elif what == "term-snapshot":
        name = req["payload"].get("target")
        term = get_terminal(name)
        data = term.tail(20) if term else None
    push_final(agent_registry, req_id, "success", "", artifacts=[],
               meta={"what": what, "data": data})
```

### 3.3 `kind: delegate` 推荐做法
直接复用现有 `run_agent_loop`，但把 `events_cb` 中每个事件加上 `reqId = req["reqId"]`，并在 done 时多推一个 `type: final`。其余逻辑不动。

### 3.4 `kind: abort`
维护 `_active_requests: dict[reqId, abort_token]`。收到 abort 时 set token，所有 handler 在 PTY 读循环里检查 token，及时 SIGTERM。

### 3.5 `kind: approval-response`
维护 `_pending_approvals: dict[reqId, threading.Event + decision]`。收到响应时存 decision 并 `event.set()`，被阻塞的 handler 唤醒后继续/退出。

---

## 4. 安全策略

新增 `~/.laintas_cli_policy.json`：
```json
{
  "allow":   ["^ls( |$)", "^cat ", "^grep ", "^git status", "^pwd$"],
  "needs_approval": ["^git push", "^npm install", "^pip install"],
  "deny":    ["^rm\\s+-rf\\s+/", "^sudo ", "^chmod\\s+777", "^:\\(\\)\\{\\s*:"],
  "allowedRoots": ["/root/Helpwo", "/tmp", "/home"]
}
```

加载点：在 `policy.py` 模块里，复用 `.extra_command.py` 的 mtime 缓存模式。每条 exec 跑前 evaluate，写操作越界（路径）直接 deny。

---

## 5. 文件落点（laintas_cli 仓库）

| 路径 | 改动 | 备注 |
|---|---|---|
| `laintas_cli.py` | `_poll_loop` 增加 kind 路由 | ~50 行 |
| `laintas_cli.py` | `AgentRegistry._push_events` 接受 `reqId/meta` | ~10 行 |
| `laintas_cli.py` | 心跳 payload 扩展（cwd/shell/runningTerminals/metrics） | ~30 行 |
| `agent_loop.py` | `events_cb` 调用点加 `reqId` 参数 | ~20 行散点 |
| `request_handlers.py` (新) | `handle_exec / handle_query / handle_delegate / handle_abort / handle_approval` | ~250 行 |
| `policy.py` (新) | 白/灰/黑名单引擎 | ~80 行 |
| `~/.laintas_cli_policy.json` (新) | 默认模板，启动时若不存在自动写入 | — |
| `~/.laintas_cli_audit.log` (新) | 每条 exec 落盘（reqId + 时间 + cmd + exitCode） | — |
| `setup.py` | 增加 `psutil>=5.9` 到 `install_requires` | — |
| `PROJECT.md` | 更新协议章节 | — |

---

## 6. 分阶段路线（建议顺序）

| Phase | 任务 | 验收 |
|---|---|---|
| **A1** | `_poll_loop` 支持 kind 路由（exec/query 先打通，其余先 stub） | curl 灰测：HelpwoAI 端 `remote_exec` 工具能调到 `ls` 并拿回 stdout |
| **A2** | `events_cb` 增加 `reqId`，确保 stdout/cmd-start/cmd-end/final 都正确归属 | HelpwoAI UI 上 agent panel 滚动显示了对应事件 |
| **B1** | 心跳扩展 + metric 事件 | HelpwoAI UI 上 agent 行能看到 cwd、shell、负载 |
| **B2** | terminal-snapshot 事件 | UI 能看到 sub-terminal 最近输出 |
| **C1** | policy 白名单 + audit log | 跑 `ls` 走白名单直通，跑 `rm -rf /` 被 deny 不上事件流 |
| **C2** | `needs-approval` + `approval-response` 闭环 | HelpwoAI 端 PlanCard 弹出 → 用户 approve → laintas_cli 真的执行 |
| **C3** | `kind: delegate`（复用 run_agent_loop） | HelpwoAI 端委派"找出当前目录最大的文件"能跑成 |
| **C4** | `kind: abort`（HelpwoAI 端工具支持 AbortSignal） | HelpwoAI 端取消 → laintas_cli 立刻 SIGTERM 子进程 |
| **D**（可选）| `--monitor-only` 启动开关 + Windows 兜底 | — |

---

## 7. 端到端 demo 路径（A1 完成时就能跑）

1. `cd /root/laintas_cli && source venv/bin/activate && python laintas_cli.py`
   → 看见 "Registered as remote-xxxx"
2. 在 HelpwoAI UI 切到 **Agent 模式**，输入：
   > 调用 remote_exec 在远程跑 `ls -la /tmp` 并告诉我结果
3. AI 应当调用 `remote_exec(command="ls -la /tmp")`
4. HelpwoAI 后端把结构化包入队 → laintas_cli `/poll` 拿到 → 解析 kind=exec → 跑 PTY → 推 stdout/final
5. HelpwoAI 端收到 final，把 output 喂回模型 → AI 写一段总结回复用户

---

## 8. HelpwoAI 端已就绪（你这次会话不用动它）

| 文件 | 内容 |
|---|---|
| `src/tools/remote.ts` | `remote_exec` / `remote_query` 工具：自动选 agent、POST send、polling /updates、reqId 过滤、needs-approval→PlanCard 桥、final 事件 resolve |
| `src/tools/index.ts` | 注册 remoteTools |
| `src/utils/AutonomousKernel.ts` | Edit 模式放行 `remote_query`，Agent 模式 modeNote 增加"远程委派"说明 |
| `Helpwo.py` `/api/agents/<id>/send` | 接受 `{kind,reqId,payload,ack}`，仍兼容老的 `{message}` |
| `Helpwo.py` `_push_agent_event` | 事件支持 `reqId` 和 `meta` 字段 |

HelpwoAI 端 polling 间隔 600ms，默认超时 60s（硬上限 5min），事件输出做 6000 字符截断。
