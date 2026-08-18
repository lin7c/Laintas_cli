# blindpick

当前模型（incumbent）vs 挑战者模型（challenger）的对垒扩展：两个子 agent 在
各自独立的 git worktree 里完成同一任务，跑完你盲判，只把胜者一侧的改动
应用回工作区，裁决累计成一张评分表。

## 用法

```
/blindpick                       全屏同台（TTY）—— 开局 / 实况 / 盲判 / 评分都在里面
/blindpick challenger            打开 /model 同款模型选择器挑挑战者
/blindpick run <任务>             开一局（后台运行，立即返回）
/blindpick show [id]             查看待判对局的两份 diff（标注模型名）
/blindpick pick a|b|tie|bad [id] 应用某一侧 / 平局 / 都不行
/blindpick discard [id]          丢弃待判对局（记录保留）
/blindpick delete <id>           删除某一局：记录 + worktree + 分支
/blindpick prune [N|--all]       按留存策略清理旧对局
/blindpick status                文本状态（非 TTY 下 /blindpick 也走这个）
/blindpick ratings               累计 Elo 评分 + 位置偏差自检
/blindpick export [路径]          把裁决过的对局导出成 DPO 偏好对
/blindpick reset                 清空对局记录（运行中禁止）
```

有多局待判时，不带 id 的 `show/pick/discard` 会列出对局要你指定，而不是替你
挑最老的一局。

两种模式：

- **同台模式**（无参数 + TTY）：全屏对垒界面（`ui.py`）。左右两栏是两个
  竞争者，**同时执行、实时输出** —— 谁在读什么文件、跑什么命令、说了什么，
  两边并排看着。跑完两栏自动切成**各自的回复（markdown 渲染）+ 改动规模**，
  按 `a/b/t/x` 当场裁决，**裁决之后才揭晓谁是谁**。界面里不需要再敲任何
  命令。按 `d` 切到左右并排的 diff。`n` 直接在界面里输入任务开下一局，不用
  退出去；沙箱授权和两个竞争者跑出来的授权请求都落在界面底部的授权条上
  （`y`/`n`）。`[` `]` 翻看历史对局，`v` 看累计评分，`D` 按两次删除本局。

  三种视图按对局状态自动选：跑的时候看**实况**，跑完看**回复**，`d` 看
  **改动**。只读任务（"看一下这个项目"）没有 diff，回复就是全部成果 ——
  所以它是完成后的默认视图。
- **直接模式**（带子命令）：和普通命令一样直接执行，diff 用 CLI 自己的
  文件 diff 视图渲染，直接标注模型名，不做盲选。

界面的实况来自 `agent_ui_events.hub` —— `/agents` 用的同一个逐 agent 事件
索引；对局 worker 把两个子 agent ingest 进去，UI 只读不执行。

## 一局的生命周期

0. A/B 在**开局时**就抽签定死并落盘：实况要在两边还在跑的时候就打上标签，
   不可能等到看 diff 时才决定顺序。子 agent 的名字也按 A/B 生成，否则事件
   流里的 agent 名就把配对泄露了。
1. 扩展自建两个 worktree（`worktree_manager.create_isolated_worktree`，
   自动复制未提交 WIP），并先把复制来的 WIP 固化为一个 baseline commit。
2. 用 `spawn_subagent(state_overrides={"cwd": worktree, "_model_override": …})`
   派生两个子 agent —— overrides 在子线程启动前写入，模型 pin 无竞态；
   cwd 已设则 spawn 跳过自建 worktree，worktree 归扩展所有。两侧都 pin
   provider（只 pin 模型 id 会让同一个 id 落到别的 provider 上）。
3. 后台线程等两个子 agent 结束（一局共用一个 45 分钟上限，不是每侧一个），
   各自 `git commit` 一个 result commit。子 agent 的工作 = `baseline..result`
   恰好一个 commit，与 WIP 彻底分离；`.laintas/` 运行时状态全程排除。
4. 裁决：`git diff baseline result | git apply` 应用到主树 —— patch 只含
   子 agent 的改动，不会重复应用 WIP；主树同一行被改过则干净失败并保留
   分支，绝不半应用。
5. 应用后胜者 worktree+分支删除，落选 worktree 删除、分支保留待查
   （`git branch -D laintas/...` 随时可清）。
6. 裁决写进 `store.py` 的账本：一条 match（成对，未判）+ 一条 vote（判词），
   两个只追加的文件，vote 永远不能改写它判的那条 match。

## 评分、偏差与导出

- `ratings`：Bradley-Terry（按 Elo 刻度显示，K=16）。局数少的时候数字会
  抖，别把噪声读成进步。
- 位置偏差：每条 vote 记下当时先显示的是哪一侧。先显示的一侧胜率明显偏离
  50%，说明在测布局而不是测质量 —— 只报警不修正。
- `export`：判过的对局导出成 DPO 偏好对，带 `chosen_run_id`/`rejected_run_id`
  可以接回网关训练账本，把"整局输赢"摊成每一步的正负样本。

数据默认落在 `<cwd>/.laintas/blindpick/`；`~/.laintas/blindpick.json` 里配
`{"data_dir": "..."}` 可以把全机器的裁决汇到一处。

## 为什么这样设计（旧版的教训）

- v1 在 `spawn_subagents_parallel` 返回**之后**才设 `_model_override`，而子
  线程在返回前就已启动 —— pin 可能落空且静默；v2 用 `state_overrides`
  原子写入。
- v1 把 WIP 和子 agent 改动混进同一个 commit 再整段 apply 回仍带着同一份
  WIP 的父树 —— 只要有未提交改动就必炸；v2 的 baseline commit 把两者切开。
- v1 在命令线程里阻塞等待最长 45 分钟；v2 全程后台线程，`status` 查进度。
- CLI 被强杀留下的 running 幽灵对局和孤儿 worktree：下次启动自动把
  interrupted 局标失败，24h 后 GC 回收 worktree。
- v2.1/2.2 的工作台是"列表 + 详情"：跑的时候只有一行进度，跑完才拿两段
  diff 出来比。这测的是 diff 的可读性，不是两个模型怎么做事的；而且开局要
  先退出全屏去弹审批框，回来时那句结果又落在一个提前 return 的分支后面 ——
  于是按 r 输入任务"没有任何反应"。v3 把中心换成两栏实况，审批进界面，开局
  不再离开屏幕。
- v2.0/2.1 写了 `store.py` 却从来没人调用它 —— 每一局的结论都随着那行
  控制台输出一起消失，跑一局两倍的钱只买到一次性的印象。v2.2 把 match/vote
  接上，评分和导出才真的存在。

## 留存：这个扩展不留垃圾

一局会产生：一条记录、两个 worktree、两个分支。没有上限就是三份持续增长的
垃圾，所以留存是**自动**的，不需要你记得去打扫：

- 启动时清掉超出窗口的**已结束**对局 —— 记录、worktree、分支一起。默认保留
  最近 20 局 / 14 天，在 `blindpick_state.json` 里改 `keep_rounds` /
  `keep_days`，`0` 表示不限。
- 进行中和**待裁决**的对局永远不清：那是你花了钱还没看的东西。
- 没有任何记录指向的 `laintas/blindpick-*` 分支（被强杀的 CLI、旧版本留下
  的），超过 24 小时后一并删除。只认这个前缀，只删没人引用的，正被 worktree
  占用的不动。
- 手动：`delete <id>` 删一局，`prune` 立刻按策略清，`prune --all` 只留
  进行中和待裁决的。

删除的顺序是固定的：**先回收资源，最后删记录**。反过来做，记录一没，分支就
再也没人找得到了 —— 那正是旧版本在仓库里堆下孤儿分支的原因。

裁决账本（`.laintas/blindpick/`）不在留存范围内：它只追加、体积小，而且它
就是这一切的产出。

## 状态与数据

- `<cwd>/.laintas/blindpick_state.json`：挑战者设置
- `<cwd>/.laintas/blindpick_rounds.jsonl`：对局流水（running → pending →
  applied/discarded/tie/both_bad/failed）
- `<cwd>/.laintas/blindpick/matches.jsonl` + `votes.jsonl`：裁决账本
- 一局 = 两次完整 agent 运行，约两倍成本。这是度量的诚实价格。
