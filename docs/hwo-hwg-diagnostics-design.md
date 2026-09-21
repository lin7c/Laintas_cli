# HWO / HWG 编译诊断机制设计（对标 GCC）

Status: 设计定稿，未实施。全部现状结论来自 2026-09-20 的程序化实测（复现命令与断言见附录 A），行号引用均已复核；3 处多行调用的锚点行差异已在复核中确认（编号表锚定 raise/append 行，文案在其下一行，引用本身正确）。

目标：把 `.hwo` / `.hwg` 的编译报错从"一段描述性字符串"升级为具备 **位置（file:line:col + span）、稳定编号、severity、note、fix-it、多错误恢复、颜色/纯文本/JSON 三种渲染** 的诊断体系，达到 GCC/Clang 的成熟度标准。

实施边界：`hwo_adapter/adapter.py` 与 `hwg_adapter/adapter.py` 是从 `/root/agent_gateway/{hwo,hwg}/` vendored 的纯语法模块（`sync_hwo.sh` / `sync_hwg.sh` 分发到 `Helpwo/src/tools/{hwo,hwg}-core.ts`，602 / 1348 行），`test_parity.py` 快照同时锁定 AST 与 errors 文本。因此分期从**产品层旁路**开始，canonical 侧只做**加法**（新函数、可选字段），`validate()` 的旧签名与文本全程保留。

## 1. 现状实测（缺陷清单）

实测入口：`hwo_runner.compile_hwo_file` / `hwg_runner.compile_hwg_file`（`hwo_runner.py:1412` / `hwg_runner.py:578`）。

| # | 真实输出（现状） | 缺陷 |
|---|---|---|
| 1 | `hwo: parse error — Unclosed body for agent "a", expected } at 13` | 无 file/line/col；`13` 是字符偏移且指向 EOF，不是未闭合的 `{` 的位置 |
| 2 | `Unknown thinking gear 'medum' — use one of: … at 19` | offset 指向文档末尾，无行列概念 |
| 3 | `HWG parse error: Unexpected token "{ on: verdict ==" at 11` | 16 字符原始片段含换行直接塞进引号，无法判读 |
| 4 | `root#writer#: input references #reseracher.notes before that agent has completed in this scope.` | **原因错误**：`#reseracher#` 根本未声明，应报"未声明 + 拼写建议"，却报成顺序问题 |
| 5 | `root: duplicate agent name "#a#" …` | `root` 是伪路径冒充位置 |
| 6 | `root#writer#: input references undeclared output #researcher.notez.` | 无 `did you mean 'notes'?`（候选集就在 AST 里） |
| 7 | 任一解析错误即中止 | 无多错误恢复（GCC 实测 3/3 全报） |
| 8 | 全部按 error 处理，无 `[-Wxxx]` 类标签 | 无稳定编号、无 severity、无抑制/升级开关 |

点位清点（grep 实测并程序复核）：HWO 解析 14 处 `raise HwoParseError`、校验 11 处 `errors.append`；HWG 解析 18 处 `raise HwgParseError`、`errors.append` 37 处、`_CondError` 11 处。

结构定义：`HwoParseError(message, index)`（`hwo_adapter/adapter.py:159`）、`HwgParseError(message, index)`（`hwg_adapter/adapter.py:50`），`str()` 拼成 `"{message} at {index}"`；两语言 `validate()` 均返回 `list[str]`。

调用面（4 处 + 1 处展示）：`tools.py:4596`（`hwo` 工具 compile）、`tools.py:4630`（`hwg` 工具）、`laintas_cli.py:23449`（`/hwo compile`）、`laintas_cli.py:23544`（`/hwg compile`）、`hwo_ui.py:440`（`load_hwo_file` → `f"Parse error: {e}"`）。全部经 `{"ok": False, "msg": ...}` 传递。

## 2. GCC 基线（本机 gcc 13.3.0 实测）

```
br.c:3:3: error: expected declaration or statement at end of input
    3 |   return x;
      |   ^~~~~~
m2.c:2:38: error: 'struct S' has no member named 'alph'; did you mean 'alpha'?
    2 | int main(void){ struct S s; return s.alph; }
      |                                      ^~~~
      |                                      alpha
```

- 三个独立错误全部报出（实测 3/3），不因首个错误中止；
- `-Wall` 下 warning 携带 `[-Wunused-variable]` 选项标签；
- `-fno-diagnostics-show-caret` 去掉 caret 但保留 `file:line:col: error:` 行；
- 退出码 `1`；`GCC_COLORS` 默认 `error=01;31:warning=01;35:note=01;36:locus=01:quote=01:fixit-insert=32:fixit-delete=31`；
- `-fdiagnostics-plain-output` ≡ caret/行号/颜色/URL 全关的稳定集合（dejagnu 类工具解析用）；
- "each undeclared identifier is reported only once" —— 重复抑制以 note 形式回指首报。

## 3. 目标能力矩阵

| GCC/Clang 能力 | 本设计 |
|---|---|
| `file:line:col: error:` 定位行 | `Locus`（1-based 行/列，跨语言同构） |
| caret + `^~~~` 区间下划线 | `PrettyRenderer`，宽度用 `wcwidth`（已验证可用） |
| `note:` / 关联信息 | `Diagnostic.notes[]` |
| fix-it hints | `Diagnostic.fixits[]`，可机器应用 |
| `did you mean` | 作用域候选集 + `difflib.get_close_matches` |
| 多错误恢复 + `-ferror-limit` | parser `_synchronize()` + `--error-limit`（默认 20）+ 抑制计数 |
| `-fdiagnostics-color` / `-fdiagnostics-plain-output` | `--color=auto\|always\|never` / `--plain`；复用 `laintas_cli.py:436` 的 `NO_COLOR` 判定 |
| 诊断分层 + 稳定编号 | `phase`（lex/parse/sema/include）+ `HWO/HWG` × `1xxx/2xxx/3xxx/4xxx`（Rust `E0308` 式；新 DSL 无历史 `-Wfoo` 包袱） |
| `-Werror=` / `-Wno-` | `--deny=<code>` / `--allow=<code>` |
| `-fmessage-length=n` | `--message-length=n`（TTY 默认终端宽；非 TTY 默认 0=不换行） |
| 机器可读 | `--json` 输出结构化诊断数组 |

## 4. 数据模型（纯数据、可 JSON 序列化，TS 镜像可逐字节对齐）

```python
Severity = "error" | "warning" | "note"
Phase    = "lex" | "parse" | "sema" | "include"

@dataclass(frozen=True)
class Locus:
    file: str
    line: int            # 1-based
    column: int          # 1-based，按 Unicode 码点数
    end_line: int
    end_column: int
    byte_offset: int     # 供编辑器跳转，不进展示

@dataclass(frozen=True)
class Note:
    message: str
    locus: Locus | None

@dataclass(frozen=True)
class FixIt:
    kind: "insert" | "replace" | "delete"
    locus: Locus
    text: str = ""       # delete 时为空

@dataclass(frozen=True)
class Diagnostic:
    code: str            # "HWO2010" / "HWG3024"
    severity: Severity
    phase: Phase
    message: str         # 单行，不含位置信息，不含控制字符
    locus: Locus
    notes: tuple[Note, ...] = ()
    fixits: tuple[FixIt, ...] = ()
    meta: Mapping[str, str] = field(default_factory=dict)  # agent/field 等实体，供渲染与 suppress

@dataclass(frozen=True)
class DiagnosticSet:
    diagnostics: tuple[Diagnostic, ...]
    suppressed: int = 0
```

**列的口径定死**：`column` = 1-based 码点数（非字节、非显示宽度）；`wcwidth` 只用于 caret 对齐。中文标识符/注释会使字节列与显示列严重错位（实测 `wcswidth("中文ab")==6` 而码点数 4），二者必须分离。

## 5. 诊断编号表（锚点 = raise/append 所在行；多行调用的文案在下一行）

### HWO

- 词法 `1xxx`：`HWO1001` 未闭合 ` ``` ` 注释块（adapter.py:355）
- 语法 `2xxx`：`2001`:134 空 gear ｜ `2002`:136 未知 gear ｜ `2003`:178 意外 token ｜ `2004`:219 未闭合 `//` ｜ `2005`:230 `@line` 后缺 IO ｜ `2006`:247 未闭合 agent 名 ｜ `2007`:264 空 agent 名 ｜ `2008`:266 `@` 后空 model ｜ `2009`:282 agent 名后缺 `{` ｜ `2010`:290 body 未闭合 ｜ `2011`:320 未闭合 `[` ｜ `2012`:328 未闭合 prompt 前缀 ｜ `2013`:389 字面量不匹配
- 语义 `3xxx`：`3001`:418 并行块只允许 agent ｜ `3002`:437 兄弟重名 ｜ `3003`:464 非法参数名 ｜ `3004`:466 重复参数 ｜ `3005`:507 引用尚未完成的 agent ｜ `3006`:509 未声明 output ｜ `3007`:518 并行读兄弟输出 ｜ `3008`:522 并行内顺序引用 ｜ `3009`:524 并行内未声明 output ｜ `3010`:554 prompt 路径非相对 ｜ `3011`:576 body 误用 `in(...)`

### HWG

- 词法 `1xxx`：`HWG1001` 未闭合注释块（adapter.py:796）
- 语法 `2xxx`：`2001`:583 `(label)` 后缺 `#name#`/`{` ｜ `2002`:590 意外 token ｜ `2003`:599 manual 写法 ｜ `2004`:604 manual 不得绑工具 ｜ `2005`:615 空文件绑定 ｜ `2006`:638 `@graph` 后缺 IO ｜ `2007`:647 `@include` 后缺引号路径 ｜ `2008`:653 未终结 include 路径 ｜ `2009`:672 缺 `->`/`=>` ｜ `2010`:688 缺目标节点 ｜ `2011`:704 未知块 ｜ `2012`:725 未闭合 `(` ｜ `2013`:752 未闭合 `{` ｜ `2014`:779 未闭合 `[` ｜ `2015`:786 未闭合名 ｜ `2016`:789 空名 ｜ `2017`:809 字面量不匹配
- 语义 `3xxx`：`3001`:275 非法 IO 参数名 ｜ `3002`:277 重复 IO 参数 ｜ `3003`:966 重复节点 id ｜ `3004`:971 retry 非法 ｜ `3005`:977 工具节点带 `tools:` ｜ `3006`:982 `tools: []` ｜ `3007`:987 非法工具名/glob ｜ `3008`:993 多个 `(schedule)` ｜ `3009`:998 边起点未声明 ｜ `3010`:1000 边终点未声明 ｜ `3011`:1015 `[-1]` 引用不存在 ｜ `3012`:1017 自引用当前输出 ｜ `3013`:1021 引用未声明节点 ｜ `3014`:1023 引用未声明输出 ｜ `3015`:1025 拓扑不可达的顺序引用 ｜ `3016`:1031 条件语法非法 ｜ `3017`:1041 条件字段不在 `out(...)` ｜ `3018`:1045 `exists()` 节点未声明 ｜ `3019`:1047 `exists()` 输出未声明 ｜ `3020`:1052 自环缺 `maxLoops` ｜ `3021`:1060 无界环 ｜ `3022`:1074 分支边缺 `on:` ｜ `3023`:1086 无起点 ｜ `3024`:1089 多起点 ｜ `3025`:1094 无终点 ｜ `3026`:1162 `->`/`=>` 混用 ｜ `3027`:1167 单条 `=>` ｜ `3028`:1173 `=>` 带 `maxLoops` ｜ `3029`:1177 扇出不汇聚 ｜ `3030`:1188 join 必须 `"all"` ｜ `3031`:1190 `join` 无扇出
- include `4xxx`：`4001`:909 空 include 路径 ｜ `4002`:913 include 环 ｜ `4003`:922 不可读 ｜ `4004`:925 不存在 ｜ `4005`:930 被包含文件解析失败（嵌套子诊断）｜ `4006`:958 未 splice 就校验

默认 severity：全部 `error`；warning 候选 `HWO3006`、`HWO3010`、`HWG4006`（不影响可执行性，可 `--deny` 升级）。

## 6. 三种渲染器

### 6.1 Plain（非 TTY / `--plain`；同时是兼容层）

```
flow.hwg:3:5: error: expected '->' or '=>' after #a# [HWG2009]
```

与 `-fdiagnostics-plain-output` 同构：单行、无色、无 caret、不换行。**`validate()` 继续返回 `list[str]` 时使用的就是这一渲染**，parity golden 与调用方零改动。

### 6.2 Pretty（TTY 默认）

```
hwo: error: agent '#a#' is missing its closing '}' [HWO2010]
  --> /path/flow.hwo:1:1
   |
 1 | #a# {
   | ^~~~ opened here, never closed
   |
   = note: reached end of file at line 3 while looking for '}'
   = help: add '}' at the end of the agent body
```

规则：`-->` locus 行；源码行行号按集合内最大行号右对齐（GCC 同款）；caret 行 `^` 落在语义最小单元起点、`~` 铺满 span（宽度按 `wcwidth`）；fix-it 用 GCC 的"第二行 caret"呈现插入文本。

配色对齐 `GCC_COLORS` 默认值：`error=01;31 warning=01;35 note=01;36 locus=01 quote=01 fixit-insert=32 fixit-delete=31`；由 `HWO_COLORS`/`HWG_COLORS` 环境变量覆盖，空串关闭；尊重 `NO_COLOR`。

### 6.3 JSON（`--json`）

```json
{"ok": false, "diagnostics": [{"code": "HWG2009", "severity": "error", "phase": "parse",
  "message": "…", "locus": {"file": "flow.hwg", "line": 3, "column": 5, "endLine": 3, "endColumn": 7},
  "notes": [], "fixits": []}], "suppressed": 0}
```

`hwo`/`hwg` 工具返回保持 `{"ok", "result"}` 不变，**新增** `"diagnostics"` 字段。

## 7. 文案规范（七条硬规则）

1. **三段式**：`<现象> (<上下文>) ; <did you mean 'X'?>`
2. **位置只在 locus 行**，message 内不得重复 `root#writer#:` 类前缀。
3. **标识符统一 `'…'` 单引号**。
4. **不粘贴跨行片段**：片段单行、≤40 字符、换行显式转义（现状 #3 违反）。
5. **描述期望/缺失/改法**，不描述解析器内部状态：`agent '#a#' is missing its closing '}'`。
6. **未声明优先给拼写建议**，不得报成顺序错误（现状 #4 违反）：`undeclared agent '#reseracher#'; did you mean '#researcher#'?`。顺序类（`HWO3005`/`HWG3015`）只在被引用者**确实已声明**时使用。
7. **EOF 类错误**：caret 指 EOF（对标 GCC `expected declaration … at end of input`），并以 `note` + 关联 locus 回指未闭合的 `{`——比 GCC 多一条信息，主行保持同款简洁。

建议算法：候选集 = 当前作用域实体（HWO：同层 agent 名 / 该 agent 的 `out` 字段；HWG：全部节点 id / `out` 字段；另 `EFFORT_GEARS`、工具名），`difflib.get_close_matches(cutoff≈0.6)`，命中生成 `FixIt(replace)`。`HWO2002` 现有的 "use one of: …" 保留为 `help`。

## 8. 控制项

```
--color=auto|always|never     --plain          --caret/--no-caret
--line-numbers/--no-line-numbers
--show-location=once|every-line   --message-length=N
--error-limit=N (默认 20)     --json
--deny=<code>  --allow=<code>
```

退出码：`0` 成功、`1` 存在 error（warning 不影响）。`/hwo compile`、`/hwg compile` 与工具通道共用渲染器，差异只由开关决定。

多错误恢复：parser 增加 `_error(code, locus, msg)` + `_synchronize()`；HWO 同步点 `#` `(` `//` 空行 EOF，HWG 同步点 `#` `@` `(` `->` `=>` EOF。同一 `(code, locus)` 只报一次，重复降为 note 回指首报（对标 GCC 的 once-only 规则）。校验阶段本就聚合多错，只需换容器。

## 9. 分期实施

**P0 — 产品层旁路（不碰 canonical，零 parity 风险）**
新增 `diagnostics.py`：数据模型 + 3 渲染器 + `SourceMap`（offset→line/col）+ 建议算法 + "抛点指纹 → 编号"映射。
- 解析类（HWO 14 + HWG 18 处）：用现成 `index` + 源文本反推 file:line:col，立即具备 caret。注意：`index` 可能指向 EOF 而非错误起点（实测 `unclosed.hwo` 报 13=len），故 caret 指 EOF、note 回指 `{`（§7 规则 7），不得把 index 当错误起点。
- 校验类（HWO 11 + HWG 37 处）：诚实边界——AST 无 span，P0 只到**文件级** + 命名实体规范化表述 + 拼写建议；行号等 P1。
- 兼容：`msg` 保持旧文本（`tests/test_hwo_view.py:192,201`、`tests/test_hwg_view.py:156` 断言的 `parse error`/`cannot read` 子串原样保留），结构化诊断旁路附加。

**P1 — canonical 侧加法（两语言同步）**
新增 `validate_diagnostics(steps, srcmap) -> list[Diagnostic]`；保留 `validate()` 原签名与文本 → `test_parity.py` 快照不重生成（当前实测：HWO 5/5、HWG 14/14 全绿）。新增 `samples/diagnostics/*.json` 独立快照锁编号与 span。跑 `sync_hwo.sh`/`sync_hwg.sh` 同步 TS 镜像。

**P2 — parser span + 错误恢复 + `--error-limit`**
`HwoParseError`/`HwgParseError` 增加可选 `span`/`code` 字段（`index` 属性保留 → 现有 32 个 raise 点零改动可编译），逐步补 span；加 `_synchronize()`。

**P3 — fix-it 全量 + related + warning 分级**
缺失 `}`/`]`/`//` 插入、多余 token 删除、`did you mean` 全覆盖；`#x# -> … -> #y#` 路径链 related 信息（`hwg_adapter/adapter.py:1025` 雏形升级）；三条 warning 候选 + `--deny/--allow`。

**P4 — UI 与后编辑检查器**
`hwo_ui.py:440` 换结构化渲染；`diagnostics_adapter/registry.json` 给 `.hwo`/`.hwg` 挂 `--plain` 单行 checker（该机制已有 `.py/.sh/.json/.js` 先例，扩展名当前缺失）。

## 10. 验收标准

1. 每条诊断具备 code + file:line:col + span + 单行 message + 可选 fix-it。
2. 位置正确：caret 落语义最小单元；EOF 类指最后一行末列并有 note 回指。
3. 多错误：三个独立错误 3/3 全报（对标实测 gcc）；`--error-limit=2` 出现抑制计数行。
4. 拼写建议：`#reseracher#` → `'#researcher#'`；`notez` → `notes`。
5. 宽度：含中文源码行 caret 对齐正确。
6. 颜色：`--plain` 与 `--color=never` 输出逐字节无 ANSI；`NO_COLOR` 生效。
7. 兼容：`/hwo`、`/hwg`、工具通道 `ok`/`msg` 语义不变；`test_hwo_view`、`test_hwg_view`、`test_hwg_runner` 现有断言全绿；`agent_gateway/{hwo,hwg}/test_parity.py` 快照不重生成。
8. 跨语言：同一文件在 Python 与 TS 下 plain 渲染逐字节相同、JSON 相同。

## 11. 风险与未决项

- **最大范围风险 = TS 镜像同步**（`hwo-core.ts` 602 行 / `hwg-core.ts` 1348 行 + golden 重生成）。对策：P0 只改 Python 产品层且不改 message 文本；文本重写集中在 P1 与 TS 一起做。
- `root#scope#` 前缀无测试锁定（`test_hwg_runner.py:615,644` 只锁 include 文案），可安全规范化。
- 反引号注释块不进 AST（`hwo_adapter/adapter.py:193,351` 只 skip），命中注释区的 caret 无归属——P2 处理。
- `multi_start` 列表按源码顺序（`adapter.py:1081` 遍历 `nodes`），确定性已验证，保留。
- 未决：`--json` 是否入首版；环境变量命名（`HWO_COLORS`/`HWG_COLORS` vs 统一 `LAINTAS_DIAG_COLORS`）；文档语言体例（本文按 `task-runtime-audit-2026-09-17.md` 的中文正文体例）。

## 附录 A — 复核协议（2026-09-20）

程序化断言全部通过（一个 Python 脚本，24 项检查）：

- 点位计数：HWO 14/11、HWG 18/37/11 ✓
- 行号锚点抽查 ✓（3 处多行调用复核为脚本过严：锚点在 raise/append 行、文案在下一行，引用正确）
- 头条缺陷复现：EOF offset（`at 13`=len(src)）✓、未声明 agent 误报为顺序问题 ✓、含换行片段入引号 ✓
- 兼容子串锚点：`tests/test_hwo_view.py:192,201`、`tests/test_hwg_view.py:156`、`tests/test_hwg_runner.py:615,644` ✓
- parity 基线绿：`agent_gateway/hwo` all 5 samples ok、`agent_gateway/hwg` all 14 samples ok ✓
- 带 errors 的 golden = 5 个：`bad_conditions(2) bad_fanout(2) include(5) multi_start(1) unbounded_cycle(1)` ✓
