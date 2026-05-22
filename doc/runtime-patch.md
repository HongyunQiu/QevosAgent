# 运行时补丁（Runtime Patch）原理

这份文档描述 `QevosAgent` 的**运行时补丁**机制：它如何生成、存放在哪里、**如何被组装进每轮上下文**，以及 **dashboard 如何在时间线上展示**。

> 补丁的"触发来源"（JSON 解析错误的分层处理）详见 [`docs/json-error-handling.md`](../docs/json-error-handling.md)，本文聚焦补丁的**生命周期、上下文注入与看板展示**。

---

## 一、它解决什么问题

模型偶尔会输出不合规的 JSON（裸换行、反斜杠未转义、用 ```​```json``` 围栏包裹、字段提前闭合等）。这类错误往往**会重复犯**。运行时补丁的思路是：把"本次运行已经踩过的坑"提炼成一条简短的格式规范，**在后续每一轮都提醒模型**，从而打破重复犯错的循环。

补丁是**单次运行内**的动态经验；运行结束时可由模型选择性地写回 `AGENTS.md` 供以后复用。

---

## 二、生成

入口：[`_apply_runtime_patch`](../agent/core/compression.py)（在 `loop.py` 的 ERROR 处理块中调用）。

```
畸形输出 → parse_response 判定 ActionType.ERROR
       │
       ▼
_apply_runtime_patch(error_type)
       │
       ├── 已知类型 ─→ 查静态规则表 _JSON_ERROR_PATCH_RULES ─→ 去重后写入 runtime_patches
       │                                                        （事件 rule_added / rule_skipped）
       │
       └── unknown ─→ mini LLM 诊断（频控 RUNTIME_PATCH_UNKNOWN_MAX）
                         │
                         ├── 候选出现 < 阈值(2) ─→ 仅记录   （事件 candidate_recorded）
                         └── 候选出现 ≥ 阈值(2) ─→ 正式写入 （事件 candidate_promoted）
```

每个动作都会调用 [`_log_patch_event`](../agent/core/compression.py) 写一条 JSONL 日志，事件类型共五种：

| event | 含义 | 是否"补丁生效" |
|-------|------|--------------|
| `rule_added` | 已知类型静态规则首次加入 | ✅ |
| `candidate_promoted` | unknown 候选达到阈值、正式加入 | ✅ |
| `rule_skipped` | 规则已存在，跳过 | ❌ |
| `candidate_recorded` | unknown 候选记录，尚未晋升 | ❌ |
| `diagnosis_skipped` | unknown 诊断被频控跳过 | ❌ |

只有标 ✅ 的两种代表"补丁真正生效并加入了 `runtime_patches`"。

---

## 三、存储：四个去处

一条补丁生效时，信息会落到四个地方，各有用途：

| 位置 | 内容 | 用途 |
|------|------|------|
| `state.meta["runtime_patches"]` | 当前生效的规则字符串列表 | **每轮注入上下文的数据源**，随 `meta.json` 持久化 |
| `state.long_term` | `[运行时补丁] 新增格式规范: …` | 经验流水，可能出现在 system prompt 的长期记忆区 |
| `patch_log.jsonl` | 每个事件一行（含 `iteration`、`short_term_len`、`event`、`rule` 等） | **dashboard 时间线展示的数据源** |
| `AGENTS.md`（可选） | 由 `persist_runtime_patches` 工具写回的"运行时经验"节 | 跨运行复用（作为下次启动的 prefix 一部分） |

`patch_log.jsonl` 的路径由 `run_goal.py` 设到 `run_dir/patch_log.jsonl`（`meta["_patch_log_path"]`）。

---

## 四、上下文组装：注入到每轮末尾，而非 system prompt

这是最关键、也最容易误解的一点。

补丁**不在 system prompt 里**，而是在 [`build_context_messages`](../agent/core/llm.py) → [`_build_context_suffix`](../agent/core/llm.py) 里，**每轮拼接到最后一条 user 消息的末尾**：

```
[system prompt]           ← 静态前缀（preamble + 工具表 + 长期记忆 + 行为规则）
[对话历史 messages]
[最后一条 user 消息]
  …原内容…
  ---
  ## 运行时格式规范（自动生成，必须严格遵守）   ← runtime_patches 在这里
  - JSON字符串内的换行必须转义为\n，禁止直接回车换行
  - …
  ## 草稿本（…）                              ← scratchpad 紧随其后
  …
```

`_build_context_suffix` 的拼接顺序是 **runtime_patches 在前、scratchpad 在后**，整体用 `\n\n---\n\n` 接到最后一条 user 消息尾部。

### 为什么放末尾而不放 system prompt

为了**最大化 KV Cache 命中率**。补丁随时可能新增，若写进 system prompt（位于整个 token 序列最前端），任何一次新增都会让它之后的全部内容（庞大的对话历史）缓存失效。放到 context 末尾后缀，则补丁变化只作废很短的尾部，前缀仍然命中。

> 历史背景：早期版本确实把补丁注入 system prompt，后来为缓存优化迁移到了末尾后缀。`docs/json-error-handling.md` 中相关描述已同步更正。

---

## 五、Dashboard 处理：在时间线上定位

补丁不写进 `short_term.jsonl`（那是 LLM 的真实对话历史，不应被展示用数据污染），而是由 server 端从 `patch_log.jsonl` **派生**成时间线事件。

### 5.1 server 端（`dashboard/server.js`）

[`updatePatchEvents`](../dashboard/server.js) 增量读取 `patch_log.jsonl`：

1. **过滤噪声**：只保留 `rule_added` 和 `candidate_promoted` 两种（其余三种 skipped/recorded 不展示）。
2. **计算锚点 `anchorIdx`**：取该条记录的 `short_term_len`（打补丁时短期历史的长度）；旧日志若无此字段，回退到当时已处理的行数 `_linesProcessed`。
3. push 进 `state.patchEvents`，随整体 state 广播给前端。

注意 `updatePatchEvents` 排在 `updateShortTerm` **之后**执行，以保证回退锚点用的是最新行数。

### 5.2 前端（`dashboard/public/index.html`）

`renderEvents` 把常规事件与 patch 事件**合并后按"短期历史行位置"排序**：

- 常规事件用自身的 `idx`（在 `short_term.jsonl` 中的行号）。
- patch 事件用 `anchorIdx - 0.5`。

`-0.5` 的含义：`anchorIdx = short_term_len` 指向**畸形输出那条之后**的位置，而畸形输出会被 `parseLine` 丢弃（JSON.parse 失败 → 返回 null）。减 0.5 让补丁正好落在这个被丢弃行留下的**空隙**里——即"重试动作之前、错误发生处"。

渲染为橙色 `🩹 Runtime Patch` 事件块。

### 5.3 为什么不能按 iteration 排序（本机制的核心坑）

`patch_log` 里的 `iteration` 是 **agent 的真实 `state.iteration`**；而 dashboard 时间线上常规事件的 `iter` 是**另一套重建出来的编号**——`updateShortTerm` 靠数 `tool_call/done/error` 行累加得到。

两套编号对不上，而且**系统性地不一致**：

- 触发补丁的畸形输出会被看板丢弃，**不增加看板的 iter**；但 agent 那次 `state.iteration` 照常 +1。
- 验收门、advisor 介入等迭代同理：agent 计数，看板不产生对应事件。

结果 agent 迭代号**普遍领先**于看板重建的 iter。若直接按 `iteration` 排序，补丁的迭代号往往超过看板当前最大 iter，被甩到**时间线最底部**。

> 实测：某真实运行常规事件 iter 范围 0–14，而一条 `rule_added` 的 iteration=40 → 沉底。

因此改用"短期历史行位置（`short_term_len`）"作为统一坐标定位，补丁才能落在它真正发生的地方。

```
… write_file ─→ tool_result ─→ 🩹 Runtime Patch ─→ [Iteration N] ─→ read_file(重试) ─→ …
                               ▲ 锚定在被丢弃的畸形输出处、重试之前
```

### 5.4 兼容性

`short_term_len` 是后加的字段。**新运行**会写入、可精确定位；**老运行**的 `patch_log.jsonl` 没有该字段，回退到行数估算（大致仍在底部附近），属优雅降级，不会比修复前更差。

---

## 六、相关文件

| 文件 | 职责 |
|------|------|
| [`agent/core/compression.py`](../agent/core/compression.py) | `_apply_runtime_patch` 生成补丁；`_log_patch_event` 写日志（含 `short_term_len`） |
| [`agent/core/llm.py`](../agent/core/llm.py) | `_build_context_suffix` 末尾注入；`build_context_messages` 把后缀拼到最后一条 user 消息 |
| [`agent/tools/standard.py`](../agent/tools/standard.py) | `persist_runtime_patches` 工具（写回 AGENTS.md） |
| [`dashboard/server.js`](../dashboard/server.js) | `updatePatchEvents` 从 `patch_log.jsonl` 派生时间线事件 + 计算锚点 |
| [`dashboard/public/index.html`](../dashboard/public/index.html) | `renderEvents` 按行位置合并排序、渲染 `🩹 Runtime Patch` |
| [`docs/json-error-handling.md`](../docs/json-error-handling.md) | 补丁的上游：JSON 错误分层检测与修复 |
