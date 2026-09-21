# laintas-cli 代码复用机会分析报告

日期：2026-09-21 · 范围：/root/laintas_cli（不含 venv/build/tests）· 方法：5 个并行子代理按子系统审计 + 主代理抽查验证

> 验证说明：hwo/hwg 的 `_literal_value` 重复、`cosine()` 唯一性等关键论断已由主代理直接读源码核实；各发现的 path:line 引用来自子代理报告，未逐条复核，但引用行号均已确认存在。

## 一、优先级最高的合并项（两组功能高度相似）

### 1. HWO runner 与 HWG runner —— 最大的一对"双胞胎"模块
`hwo_runner.py`（1481 行）与 `hwg_runner.py`（1348 行）结构平行、大量函数逐行近似：

| 重复内容 | HWO 位置 | HWG 位置 |
|---|---|---|
| `_literal_value()` 字面量解析，几乎逐行相同 | hwo_runner.py:130 | hwg_runner.py:136 |
| Markdown front-matter/阶段块解析 | hwo_runner.py:88 | hwg_runner.py:92 |
| 状态 JSON 读写 + 原子写 + 文件锁 | hwo_runner.py:211 | hwg_runner.py:238 |
| 阶段进度/完成度计算 | hwo_runner.py:388 | hwg_runner.py:421 |
| 阶段执行循环（前置条件→执行→状态落盘） | hwo_runner.py:562 | hwg_runner.py:590 |
| runner 注册/查找入口 | hwo_runner.py:1051 | hwg_runner.py:1102 |

**建议**：提取 `workflow_common.py`（字面量解析、状态存储、进度计算），两个 runner 保留各自的路由/条件/重试语义。预计可消除 400–600 行重复。

### 2. Windows 三件套 + 桥接
`windows_host.py` / `windows_kernel.py` / `windows_tools.py` 三个文件各约 15KB，存在：
- 三份近似重复的"连接建立 + 命令下发 + 输出收集"流程（windows_host.py:102、windows_kernel.py:115、windows_tools.py:98）
- winbridge.py 又实现了一份跨平台传输封装（winbridge.py:60）

**建议**：统一为一个 `windows_transport` 层（连接/编码/超时/重试），三个模块只保留各自领域逻辑。

### 3. branch.py 与 branches.py
`branch.py`（23KB）单分支操作、`branches.py`（28KB）多分支视图，共享同样的分支查找、git 调用包装、状态渲染逻辑（branch.py:88 与 branches.py:112 起的分支定位逻辑高度相似）。

**建议**：branches.py 改为调用 branch.py 的原语，删除复制的查找/渲染代码。

## 二、可提取的公共模块（R2：独立功能）

### 4. 存储层：9 个 store 各写一份"读-改-原子写-锁"
`json_store.py` 本应是公共基座，但 `session_store.py`、`contract_store.py`、`cookie_store.py`、`trust_store.py`、`identity_store.py`、`shared_storage.py`、`agent_persistence.py`、`event_log.py` 中各自实现了近似相同的模式：
- `os.replace` 临时文件原子写 + `threading.Lock`：contract_store.py:45、cookie_store.py:38、trust_store.py:52、identity_store.py:41 等处近似重复
- 加载时 `if not exists: write default` 的迁移骨架逻辑重复出现 5+ 次

**建议**：把"路径解析 + 锁 + 原子写 + 默认值迁移"收进 `json_store.py` 一个 `JsonStore` 类，各 store 只声明路径和 schema。预计消除 300+ 行并统一并发安全行为（目前有的 store 用锁、有的没有，是潜在 bug 源）。

### 5. 信号/记忆子系统：重复的打分与嵌入调用
- `_score_*` 打分头模式在 mem_signals.py、stuck_signals.py、repair_signals.py、rag_signals.py 中各自实现"事件窗口 → 特征 → 0–1 分数"的相同骨架（stuck_signals.py 与 repair_signals.py 的窗口聚合尤为相似）
- `cosine()` 向量相似度在 `embeddings.py:324` 已有权威实现，且全库仅此一处（已验证），但多个调用方各自做归一化/点积拼接
- 记忆检索的"查询 → 嵌入 → top-k 召回"流程在 mem_recall.py 与 memory_system.py 各有一份

**建议**：提取 `signal_head.py`（统一事件窗口特征骨架）和把 top-k 召回统一到 memory_system.py 一处。

### 6. 终端/会话与 UI 模块
- terminal_arbiter.py、station_service.py、session_lifecycle.py 中重复的"按 name/id 查找终端 + 存在性校验"逻辑（terminal_arbiter.py:78 与 station_service.py:112 附近近似）
- 表格渲染/分页展示代码在 station_ui.py、budget_page.py、resource_ui.py、prop_ui.py、transcript_view.py、retask_view.py 中各写一份列对齐/截断/标题逻辑

**建议**：提取 `terminal_lookup()` 公共函数和一个 `render_table()` 工具（这个是典型的 6 处重复）。

## 三、总体建议与排序

| # | 合并/抽取 | 预计消除重复 | 风险 |
|---|---|---|---|
| 1 | hwo/hwg → workflow_common.py | 400–600 行 | 中（两 runner 语义有细微差异，需逐段比对）|
| 2 | 9 个 store → JsonStore 基类 | 300+ 行 | 低，且顺带修复锁不一致问题 |
| 3 | windows_* → 统一 transport 层 | 200+ 行 | 低 |
| 4 | UI 表格渲染 → render_table() | 150+ 行 | 极低，纯展示 |
| 5 | branch/branches 合并 | 100+ 行 | 低 |
| 6 | 信号打分骨架统一 | 100+ 行 | 中（打分语义需保持兼容）|

注意：`laintas_cli.py`（1.3MB）、`agent_loop.py`（700KB）、`tools.py`（550KB）三个巨型文件未纳入本轮逐行审计，它们内部大概率还有更多重复，建议作为第二轮专项分析对象。
