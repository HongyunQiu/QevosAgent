# 执行图（Execution Graph）设计

这份文档描述 `QevosAgent` 的**执行图**机制：它的定位、数据模型、生命周期、在主循环中的接入点、上下文投影规则、迭代预算策略、收敛检测与升级梯，以及 dashboard 的可视化。

> 相关文档：验收与终态见 [`acceptance-flow.md`](./acceptance-flow.md)，上下文压缩与 handoff 见 `agent/core/compression.py`，双语要求见 [`i18n-guide.md`](./i18n-guide.md)。

---

## 一、它解决什么问题

主循环 [`run()`](../agent/core/loop.py) 是一个扁平的 ReAct 循环：一个 `AgentState`、一条线性 `short_term`、每轮由模型自选下一个动作。控制流完全隐式，没有任何显式的计划结构。

这带来两个具体缺陷：

**1. 压缩之后会失忆。** `_collapse_to_bridge` 把 `short_term` 硬重置为 `[goal, handoff]` 之后，模型对"我试过什么、哪条路走不通"的全部记忆就是一份散文式 handoff。仓库里大半反应式补救机制——循环检测、`_collapse_attractor_context`、advisor 介入、`_loop_warn_counts`——本质上都在治这个失忆导致的重走老路。

**2. 终止条件是个哑计时器。** `max_iterations` 耗尽时并不知道任务进行到哪，可能正切在一次关键操作的半途。`_WRAPUP_PROMPT` 那一整段苦口婆心，就是在给这个愚蠢的终止方式打补丁。

执行图对这两点给出结构化的解：

- 图是**几百 token 的骨架，压缩打不掉**。它是这套运行时里唯一能扛过上下文重置的结构化记忆。
- 图提供了**不依赖迭代数的进展度量**（节点闭合率），使终止条件从"跑够了没"变成"还在推进吗"。

### 它不是什么

- **不是强制阶段。** 图是模型自主启用的**能力**，不是运行时强加的流程。简单任务永远不建图。
- **不是工具约束器。** 节点不绑定工具集，工具仍由模型自主选择。模型的工具遵循能力已经到位，只要方法（节点）定义清楚，选工具不是问题。
- **不是状态机回溯。** 见 §6，回溯是逻辑的，不恢复环境。

### 与 SKILL 的关系

图在某种程度上是 SKILL 的一种化身，区别在于：**SKILL 是先验知识，图未必先有**。二者构成一个闭环：

```
SKILL（先验）──seed──▶ 执行图（本次执行）──蒸馏──▶ SKILL / concept（新先验）
```

右半边目前是断的：run 结束时的沉淀通道 `append_episodic`（散文摘要）和 `save_concept`（宏观认知）都是非结构化的。而一张走完的图是**结构化的方法论**——节点、顺序、出口证据、以及走不通的岔路和理由。

v1 **不实现**蒸馏，但数据模型现在就为它留好形状（`title` / `goal` / `exit` / `abandon_reason` 已经够用）。以后接"复盘时把成功的图提为 SKILL 候选"是加法，不是重构。同理，`plan_create` 接受可选的 `from_skill` 来源标记，为左半边留位，成本是一个字段。

---

## 二、定位：随时可建、随时可弃的能力

模型可以在任务的**任何时刻**决定"这活儿值得用图的方式来做"，未必在开头。这带来三个必须先定义清楚的语义，不定义清楚就会得到一张会说谎的图。

### a. 建图之前的历史 → 自动根节点

模型跑到第 40 轮才建图时，前 40 轮不属于任何节点。若图从空白开始，它就不是"地图"，而是"从半路开始的地图"。

**规则**：`plan_create` 时自动生成根节点 `n0`：

```
title: "前序工作"
status: done
goal:   取自当前 scratchpad / 最近一份 handoff 的摘要
iter_range: [0, 当前迭代]
closed_by: "implicit"
```

零额外 LLM 调用，地图闭合。

### b. 图能结束、能被放弃 → `graph.status`

一张过期的图若继续每轮往尾部注入结构，比没有图更糟。

| status | 含义 | 投影行为 |
|--------|------|---------|
| `active` | 当前正在遵循 | 完整投影（见 §5） |
| `completed` | 所有节点达终态且模型声明完成 | 收缩为一行 |
| `abandoned` | 模型认定此图不适用 | 收缩为一行 + 放弃理由 |

非 `active` 时投影收缩，但 `graph.json` **全量保留**——这是最终那张地图的组成部分。

### c. 一个 run 可有多张图，串行

"任何时刻可建 + 可弃"逻辑上就允许第二张。`graph.json` 存成图的**列表**，**同时只有一张 `active`**。最终产物是一串地图，比单图更诚实地反映模型的认知转折。

**嵌套（节点内含子图）明确排除在 v1 之外**——那是 subagent 的形状，subagent 未落地之前做嵌套只会得到一个没人执行的第二层。

---

## 三、数据模型

单一真相源：`runs/<run_id>/graph.json`。agent 看到的投影与 dashboard 渲染的图形**必须来自同一份数据**，绝不允许两套表示各写各的。

内存中挂在 `state.meta["_graph"]` **单一子树**下（不散落到 meta 顶层，否则 `_RESUME_RESET_KEYS` 那份名单会更脆）。

```jsonc
{
  "version": 1,
  "budget_granted": 87,          // 本 run 因图累计授予的迭代数（只记录，不设限）
  "graphs": [
    {
      "gid": "g1",
      "status": "active",         // active | completed | abandoned
      "title": "PCB 布线器 M2 差分对支持",
      "created_iter": 40,
      "closed_iter": null,
      "closed_reason": "",
      "from_skill": null,         // 为 SKILL→图 播种预留
      "cursor": "n5",             // 当前所在节点
      "nodes": { /* 见下 */ },
      "edges": [ /* 见下 */ ]
    }
  ]
}
```

### 节点

```jsonc
{
  "id": "n3",
  "title": "接入差分中心线求解器",     // ≤20 字，dashboard 节点标签
  "goal": "本节点要达成什么（一到两句）",
  "status": "done",                  // planned | active | done | abandoned | blocked
  "parent": "n2",
  "exit": {
    "evidence_type": "artifact",     // artifact | tool_result | observation | none
    "expect": ["runs/20260802-.../diff_solver.py"]
  },
  "budget": 8,                       // 模型自估迭代数
  "granted": true,                   // 预算是否已发放（每节点只发一次）
  "iter_range": [12, 31],            // 实际消耗 [进入, 关闭]
  "seg": null,                       // 若节点边界封段，记段号；默认 null
  "isolate": false,                  // 是否在进入本节点时封段（见 §5）
  "outcome": { "summary": "", "gaps": [] },
  "side_effects": [],                // 环境残留台账（见 §6）
  "abandon_reason": "",
  "closed_by": "evidence_verified"   // evidence_verified | self_certified | implicit
}
```

`exit.evidence_type` 直接复用验收门那套四分类（见 `_parse_acceptance_evidence`），不另起炉灶。

### 边

```jsonc
{ "from": "n2", "to": "n3", "kind": "then", "cond": "" }
```

| kind | 含义 | dashboard 线型 |
|------|------|---------------|
| `then` | 顺序推进 | 实线 |
| `alt` | 同一分叉点的平行备选 | 虚线 |
| `fallback` | 前驱失败时的退路 | 点线 |

---

## 四、图的读写：审慎决策付费，记账免费

图操作分两类，成本刻意区别对待。

### 显式工具（各花 1 次迭代）

| 工具 | 用途 |
|------|------|
| `plan_create(title, nodes, edges, reason)` | 建图。可含多个节点 |
| `plan_revise(ops, reason)` | 结构性批量修改（重排、批量增删、改 exit 契约） |
| `plan_abandon(reason)` | 放弃当前图，回到自由模式 |

**为什么要付这次迭代**：「决定用图的方式来做这件事」是一个审慎决策，它应该在时间线上留下明确的、可观察的落点，而不是藏在某次读文件的 JSON 字段里。这也正是它作为"能力"而非"记账"的体现。

### Inline `graph_op`（零迭代）

在正常工具调用的**同一个响应 JSON** 里顺带推进。先例：inline 模式的 `scratchpad_note`（见 `Action.scratchpad_note` 与 `_apply_inline_scratchpad_note`），模型在同一响应中输出，运行时直接应用，零额外调用。

给 `Action` 加可选字段 `graph_op`：

```jsonc
{ "op": "enter",   "node": "n3" }
{ "op": "exit",    "node": "n3", "summary": "…", "side_effects": ["…"], "gaps": [] }
{ "op": "extend",  "after": "n3", "node": { /* 单个节点 */ }, "kind": "then" }
{ "op": "abandon", "node": "n3", "reason": "…" }
{ "op": "fork",    "from": "n2",  "node": { /* 单个节点 */ } }
```

**`extend` / `fork` 每次只允许追加一个节点。** 多节点必须走 `plan_create` / `plan_revise` 付费。理由很实在：inline 字段跟着工具调用一起输出，塞一整棵树会撑爆单次输出上限，正好撞上仓库里那个反复出现的"args 过长 → 截断 → JSON 解析失败"死循环（`_json_fail_streak` 那一整套补救就是为它写的）。

### 局部前向规划

`extend` 的设计意图是**只往前规划一到三个节点**，而不是一次画完全图。这既符合真实认知过程（远处的节点本来就看不清），也让图的增长永远是**追加式**的——尾部投影内容变化，但 system prompt 前缀和 `short_term` 前缀都不动，KV 缓存不受影响。

### exit 的实证校验

`exit` 操作触发一次**纯 Python、零 LLM 调用**的校验：

```
evidence_type == "artifact"
    → 复用 _extract_claimed_artifact_paths 解析 expect
    → 逐个检查文件是否真实存在
    ├── 全部存在 → status=done, closed_by="evidence_verified"
    └── 有缺失   → exit 被拒，节点保持 active，注入缺失清单反馈

evidence_type ∈ {tool_result, observation, none}
    → 接受，status=done, closed_by="self_certified"
    → 计入「不可实证闭合」统计（见 §8）
```

**为什么不强制 artifact**：很多真实工作确实没有落盘产物，硬性要求会逼模型写垃圾文件来交差。这个反模式在别处见得太多。代价是自证闭合无法实证，处理方式见 §8——只记录、只作为软信号，不阻断。

---

## 五、上下文投影

### 注入位置

图的文本投影通过 `_build_context_suffix` 注入到**最后一条 user 消息末尾**，与 `scratchpad` / `runtime_patches` 同一通道。system prompt 前缀完全不动。

顺序（越靠后离生成点越近、遵守度越好）：

```
runtime_patches → scratchpad → 【执行图】 → thought_rigor
```

图排在 scratchpad 之后：scratchpad 是明细笔记，图是驱动下一步动作的骨架，应当更靠近生成点。`thought_rigor` 保持最后一位不变。

### 节点边界默认不封段

**这是本设计里最容易踩错的一条。**

如果每个节点边界都调 `_seal_segment_and_handoff` + `_collapse_to_bridge`，`short_term` 会被硬重置，即**彻底的 KV 缓存清零**。10 个节点的计划 = 10 次全量上下文重建，比规划本身贵一个量级。

规则：

- 节点是**逻辑边界，不是上下文边界**。默认 `isolate: false`，进入新节点只是尾部换一段简报。
- 仅当模型显式声明 `isolate: true`（"下一节点不需要看到本节点的过程细节"）才封段，节点记下 `seg` 号。
- **反向收益**：当上下文压力本来就要触发压缩时（`_maybe_compress_for_context`），把封段点**对齐到最近的节点边界**。今天压缩是撞到阈值就地开炸，可能切在一次工具调用的半途；对齐之后 handoff 天然是完整语义单元。这是加图之后压缩质量的净提升。

### 自适应折叠

投影是**每轮持续成本**，长跑里一张几十节点的网会把尾部撑爆。因此：**地图全量落盘，投影按距离折叠。**

| 内容 | 渲染粒度 |
|------|---------|
| 当前节点 | 完整：goal / exit 契约 / 预算余量 / 已用迭代 |
| 祖先链 | 每节点一行摘要 |
| 当前分叉点的兄弟分支（含已废弃） | 每个一行 + 废弃理由 |
| 前方 `planned` 节点 | 每个一行 |
| 远处的废弃子树 | 折叠成一行："n7 分支（3 节点）已废弃：<理由>" |

软上限约 600 token（`GRAPH_PROJECTION_BUDGET` 可覆盖）。超限时优先折叠距离当前节点最远的废弃子树。

**废弃理由那一行永远保留**——这正是防重走老路的关键，也是图相对签名式循环检测的核心优势。

### system prompt 侧

只加一小段"何时值得建图"的说明（工具清单本来就在 system prompt 里，加 `plan_*` 三个工具是常规工具注册）。这部分一次 run 内不变，吃满 KV 缓存，不构成成本。

---

## 六、逻辑回溯与环境残留台账

**不做状态回溯。** 环境无法 100% 重建——文件写了、包装了、远端目录建了，这些都回不去。强行做状态恢复只会得到一个骗人的"已回滚"。

但**方法学上的回溯保留**：可以回到之前的分叉节点，走另外一条路。

### 必须配套：`side_effects`

"不恢复状态"这件事本身必须被显式记录，否则重入分叉点是**有害**的。

场景：n3 走 A 路，写了半截 `config.yaml`、在远端建了目录、装了个包。判定此路不通，回 n2 改走 B 路。此时 A 路的过程可能已被压缩掉，**但环境里 A 路的残骸还在**。B 路会撞上一个它以为干净、实际不干净的世界。

因此每个节点记 `side_effects`——不是全量审计，是**模型自己申报**的"我改动了世界的哪些地方"，在 `exit` / `abandon` 时随 `graph_op` 一起提交。

废弃分支并重入分叉点时，注入的简报必须带上残留：

```
[重入 n2] 分支 n3（A 路）已废弃：<abandon_reason>

该分支对环境的遗留改动：
  - 写入 config.yaml（不完整）
  - 远端 172.24.217.39 创建 /opt/foo/
  - pip 安装了 xxx

选择新分支前，先确认这些残留是否需要清理、或可以复用。
```

这是"不做状态回溯"的诚实代价，也是它能成立的前提。

### 图是只追加的

废弃分支**永不删除**，作为证据留在图上。最终产物因此是一张**网状轨迹**——今天的时间线是单一路线，图是它的增强版：不仅记录走过什么，还记录**考虑过但没走**、以及**走了但退回来**的路径和理由。

---

## 七、迭代预算：无上限，但全量记录

### 决策

**图激活期间不设迭代上限。** 用户关注结果而非过程；多跑一些迭代换到好效果是可接受的，而受限于迭代导致任务超迭代草草结束，反而是不希望看到的局面。

### 发放规则

- 节点**首次 `enter`** 时发放，额度 = 该节点自报的 `budget`
- 每个 node id **只发一次**（`granted` 标记），`plan_revise` 改动不重新发放
- 画了但从未进入的节点**一分钱不发**
- 走现成通道：`_add_iterations` / `max_iterations` 扩展逻辑已经为 `/+N` 建好

**为什么按进入发放而不是按规划发放**：按规划发放会让模型能靠画图凭空铸造预算——多画几个节点 = 多拿迭代，而画节点几乎零成本。这不是"模型想作弊"，而是快没预算时画图恰好是它眼前最像"推进"的动作。按进入发放关闭这个漏洞，同时额度来自模型自己的估算，`估 8 / 实用 40` 这个比值本身就是极好的可观察性信号。

### 记录

| 位置 | 内容 |
|------|------|
| `graph.budget_granted` | 本 run 累计授予数 |
| 节点 `budget` vs `iter_range` | 单点自估/实用比 |
| dashboard 顶部条 | `原始 30 + 图授予 87 = 117` |

### 与其他模式的关系

仓库里已经有一个无限迭代模式（`nostop`，`_max_iterations = "∞"`）。但它的安全故事是**人在回路**，所以它把三道验收门全跳了：门 1 只要有 `final_answer` 就 pass、门 2 由 Python 自动写 episodic、门 3 整个跳过。

图模式**不能照抄**，因为图模式可能无人值守：

| 模式 | 迭代上限 | 三道验收门 | 安全网 |
|------|---------|-----------|--------|
| 普通 | `max_iterations` | 保留 | 预算耗尽（钝） |
| nostop | ∞ | **跳过** | 人在回路 |
| **图激活** | **∞** | **保留** | **收敛检测 → 升级梯 → pause** |

连带项：图激活期间，剩余迭代警告（`_iter_warn_injected`）与收尾窗口（`_WRAPUP_BUDGET`）行为上与 nostop 一致（不触发）。

---

## 八、收敛检测与升级梯

去掉预算上限后，安全网整个换了位置。**替代守卫就是图本身。**

### 为什么现有防呆不够

现有机制全是**签名式**的——同工具同参数重复（`_call_sig_history`、`_loop_warn_counts`）、逐字相同输出（`_identical_err_streak`）、连续格式错误（`_fmt_err_streak`）。它们抓不住最贵的那类失败：**每轮都在调不同工具、写不同文件、看着像在推进，但四十轮下来一个节点都没关掉。**

### 图提供的指标

在每轮开头计算（与 `_poll_watchers` 同位置），全部是 `graph.json` 上的算术，**零 LLM 调用**：

| 指标 | 定义 | 捕捉什么 |
|------|------|---------|
| `stall_iters` | 距上次任一节点 `→done` 的迭代数 | productive-looking 非收敛 |
| `node_revisits` | 同一节点被 `enter` 的次数 | 结构性循环（每次工具都不同，签名检测完全看不见） |
| `open_fanout` | `planned+active` 节点数 / `done` 节点数 | 只分叉不闭合，一棵只长叶子的树 |
| `unverified_streak` | 连续 `closed_by == "self_certified"` 的闭合数 | 靠自证关节点伪造进展 |

### 升级梯：接进已有梯子，不新建机制

现有梯子已经很完整（`_loop_advisor_pending` → `_collapse_attractor_context` → advisor → `ask_user` pause）。**只加第二个触发源，梯子本身不动**，通过 `_graph_stall_pending` 走同一段代码，仅 `trigger_reason` 不同。

| 层 | 触发阈值（环境变量可覆盖） | 动作 |
|----|--------------------------|------|
| L1 软提示 | `stall_iters ≥ 20` 或 `node_revisits ≥ 3` | 注入"本节点已 25 轮无出口证据，你自估 8 轮，考虑换路或拆分" |
| L2 advisor | `stall_iters ≥ 40` 或 `node_revisits ≥ 5` 或 `open_fanout ≥ 5` | advisor 介入，**并带图上下文** |
| L3 求助用户 | L2 之后再 stall 20 轮 | `ask_user` 暂停 |

`unverified_streak ≥ 5` 作为 L1 的附加软触发，**只提示不阻断**。

### advisor 第一次能看到结构

今天 `_build_advisor_context` 喂给 advisor 的是进展日志 + handoff + 最近执行片段，**全是散文**。加入图之后新增一节 `## 执行图`（折叠投影同款渲染），advisor 才第一次能看到**结构性**停滞——"你在 n5 和 n7 之间来回三次了"，而不是靠读一堆文字猜。

### L3 是一个好终点

`ask_user` pause 会 `_checkpoint_state(status="paused")`：全部状态落盘、图完整、未完成节点天然就是 gaps、续作可直接重建。

所以"不设上限"不等于"跑到天荒地老"，等于**跑到收敛、或跑到图判定不收敛为止，然后带着完整解释停下来等人**。这比 `exhausted` 那个丢失半截思路的哑超时好得多。

---

## 九、与验收、终态、自动续作的衔接

### 未完成节点 = 迄今最好的结构化 gaps

今天 `run_outcome.gaps` 来自 `remaining_gaps` 的自由文本，`_WRAPUP_PROMPT` 里那一大段"不要写成'还需进一步完善'这类无法执行的笼统说法"，本质上是在用 prompt 硬求模型产出结构。

有图之后，run 结束时 `active` 图上所有 `planned` / `blocked` 节点**天然就是 gaps**——带标题、带目标、带出口契约、带父节点、带兄弟分支的废弃理由。零额外成本产出，比任何自由文本都强。

**规则**：`_set_run_outcome` 写入终态时，自动把未完成节点合并进 `gaps`：

```jsonc
{
  "node": "n6",
  "title": "端口有序扇出",
  "goal": "…",
  "exit": { "evidence_type": "artifact", "expect": ["…"] },
  "parent": "n5"
}
```

### 补一个缺失的终态

非收敛导致的 pause 今天**不落 `run_outcome`**——退出处的 `else` 分支只覆盖 `exhausted` 和 `user_stopped`，`ask_user` pause 直接 `break`。自动续作层因此看不见它。

**规则**：图 stall 导致的 L3 暂停写入 `run_outcome = blocked`，`reason = "graph_stall"`，`resumable = true`。

### done 时图未走完

若 `done` 时 `active` 图仍有 `planned` 节点：**注入一次软提示**（"图中 n5/n6 未完成，确认要结束吗"），**不硬阻断**——模型可能合理地认定剩余节点已无必要。但这些节点仍然照常进入 `gaps`。

### 续作时的连续性

- **`_graph` 绝不能进 `_RESUME_RESET_KEYS`。** 那份名单清的是"上一轮的验收结论"（残留会让门失效），图恰恰相反，是必须跨段延续的载体。这个坑不写下来一定会踩。
- 续作时从 `graph.json` 重建图，已 `done` 节点保持 `done`，`cursor` 落在第一个未完成节点。
- `graph` 是纯数据，不持有 live 引用，因此**不需要**加进 `_NON_SERIALIZABLE_KEYS`。

---

## 十、Dashboard 可视化

图对**用户**和对 **agent** 同等重要，但二者消费的是同一份数据的两种渲染：agent 读折叠文本投影，用户看 SVG。

### 后端

| 端点 | 内容 |
|------|------|
| `GET /api/run/:runId/graph` | `graph.json` 原文 |

WS 广播：`graph.json` 变化时推 `{ type: 'graph', runId }`，前端拉取（与现有 run 文件的推-拉模式同构，见 `broadcast()` 系列）。

### 渲染

受**纯本地约束**，不引入任何外部图库（无 CDN、无 npm 图布局包）。手写分层 SVG：

- **布局**：按 `parent` 深度分层，同层横排。计划图是浅的（深度个位数），Sugiyama 简化版足够，不需要力导向。
- **节点配色**

  | status | 样式 |
  |--------|------|
  | `done` | 绿色实心 |
  | `active` | 蓝色 + 脉冲 |
  | `planned` | 灰色虚线框 |
  | `abandoned` | 灰色虚线 + 删除线 |
  | `blocked` | 橙色 |

- **边线型**：`then` 实线 / `alt` 虚线 / `fallback` 点线
- **hover**：`goal` + `exit` 契约 + `iter_range` + `abandon_reason` + `side_effects`
- **点击节点 → 时间线过滤到该节点的 `iter_range`**

最后这条是整个可视化的价值支点：图给结构，现有时间线给细节，二者用迭代区间对齐。否则图只是个好看的进度条。

### 顶部状态条

```
图 g1 (active) · 节点 4/9 已闭合 · 图授予迭代 87（原始 30 → 总计 117）· stall 12 轮
```

---

## 十一、v1 边界

明确**不做**：

| 项 | 原因 |
|----|------|
| 并行分支执行 | subagent 相关工作未完成；`AgentState` 非线程安全，`_interrupt_handler` / `_async_manager` / persistence 全部假定单一所有者；dashboard 的 `agentProc` 是单例 |
| 嵌套子图 | subagent 的形状，现在做只会得到没人执行的第二层 |
| 状态回溯 / 环境恢复 | 环境无法 100% 重建，改用 `side_effects` 台账（§6） |
| 节点绑定工具集 | 工具由模型自主选择；与"模型自主"取向冲突，收益不明 |
| 图 → SKILL 蒸馏 | 范围过大；但数据模型已留形状（§1） |

真并行将来应当骑在 `agent/team/api.py` 的独立进程扇出上，而不是在进程内开线程。

---

## 十二、实施批次

| 批 | 内容 | 主要触点 |
|----|------|---------|
| 1 | 数据模型、`graph.json` 持久化、`plan_create` / `plan_revise` / `plan_abandon` 工具、inline `graph_op`、折叠投影 | 新增 `agent/core/graph.py`；`types_def.Action` 加 `graph_op`；`llm.parse_response` / `_build_context_suffix`；`persistence.save_graph`；`tools/standard.py` |
| 2 | `exit` 实证校验、预算发放、`side_effects` 台账与重入简报 | `graph.py`；loop 的 `graph_op` 应用点；复用 `_extract_claimed_artifact_paths` |
| 3 | 收敛检测、升级梯接入、`run_outcome` / gaps 衔接、`blocked/graph_stall` 终态 | loop 每轮开头；复用现有升级梯代码块；`_set_run_outcome`；`advisor._build_advisor_context` |
| 4 | dashboard 端点、SVG 视图、时间线联动、顶部状态条 | `dashboard/server.js`；`dashboard/public/index.html` |

批 1–2 可独立验证（图能建、能推进、能落盘、投影正确）；批 3 依赖批 2 的 `closed_by` 统计；批 4 纯前端，可与 3 并行。

### 主循环接入点

共 5 处，均在 `agent/core/loop.py`：

1. **每轮开头**（`_poll_watchers` 附近）：收敛指标计算 + L1/L2/L3 触发
2. **上下文构建**：`build_context_messages` 传入图投影
3. **TOOL_CALL 分支**：`execute` 之后应用 inline `graph_op`（与 `_apply_inline_scratchpad_note` 同位置）
4. **压缩触发点**：封段对齐到最近节点边界
5. **DONE 分支 / 退出处**：未完成节点并入 `gaps`；stall pause 落终态

---

## 十三、注意事项清单

- **`_graph` 不进 `_RESUME_RESET_KEYS`**（§9），这是最容易踩的坑
- 图状态**只挂 `state.meta["_graph"]` 单一子树**，不散落到 meta 顶层
- 节点边界**默认不封段**（§5），否则每次节点切换 = 全量 KV 缓存清零
- inline `extend` / `fork` **每次仅一个节点**，防单次输出截断
- 投影排在 `scratchpad` 之后、`thought_rigor` 之前
- **i18n**：所有注入 `short_term` 的文本（节点简报、重入残留提示、L1 软提示、exit 拒绝反馈）与 dashboard 字符串必须走 `t()`，建议统一 `graph.` 前缀；`agent/i18n.py` 的 zh / en 两份同步添加
- 中文模板里的字面大括号必须转义（曾因此崩过 `t()` 的 format）
- `graph.json` 写入走 `_write_text_atomic` / `_write_json_atomic`，与其他 run 文件一致
