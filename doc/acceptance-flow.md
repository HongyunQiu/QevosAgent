# 验收流程设计

这份文档描述 `QevosAgent` 当前的验收机制：agent 如何声明"完成"、系统如何审核、以及不同结果下的处理路径。

---

## 概览

一次 `done` 要穿过三道串联的门。门 1 产出验收 verdict（三态），门 2/3 是记忆沉淀的强制步骤。所有退出路径最终都要落下一个 **run 级终态**（`run_outcome`），它是后续自动续作的判定依据。

```
agent 调用 done
       │
       ▼
验收门 1：_review_completion_report()
       │
       ├── needs_more_work ──► 注入错误提示，继续循环（不产生终态）
       │
       ├── weak_pass ────────► run_outcome = partial / blocked
       │                       保存结果，系统发起 ask_user，等待用户决策
       │                            │
       │                            ├── 用户说"继续" ──► 恢复 loop（清空上一轮验收状态）
       │                            ├── 用户说"完成" ──► 退出，终态保留 partial
       │                            └── 无人应答时，dashboard 可自动代答"完成"，
       │                                然后另起一个新 run 续作（见「自动续作」）
       │
       └── pass ─────────────► 继续往下
                                    │
                                    ▼
                        验收门 2：episodic 记忆（未调 append_episodic → 打回）
                                    │
                                    ▼
                        验收门 3：concept 宏观记忆评估（必经一次，与成败无关）
                                    │
                                    ▼
                              run_outcome = completed，退出

其他退出路径（均绕过上述三门，但都会落终态）：
  迭代预算耗尽 ──► 先开收尾窗口索取缺口 ──► run_outcome = exhausted
  用户 /exit    ──────────────────────────► run_outcome = aborted
  异常          ──────────────────────────► run_outcome = failed
```

> **nostop 模式**几乎旁路整套机制：门 1 只要有 `final_answer` 就 pass（reason=`nostop_human_in_loop`），门 2 由 Python 层自动写入，门 3 整个跳过，`weak_pass` 也不 pause。人在回路即质量门。但 `run_outcome` 仍然照常记录。

---

## 完成报告

验收门 1 的核心输入是**完成报告**（`completion_report`），存放在 `state.meta["completion_report"]`。

### 提交方式：submit_completion_report 工具

agent 在调用 `done` 之前，应先调用 `submit_completion_report` 工具：

```json
{
  "goal_understanding": "对用户任务目标的自然语言描述",
  "completed_work":     ["已完成事项 1", "已完成事项 2"],
  "remaining_gaps":     ["未完成/遗留事项 1"],
  "evidence_type":      "artifact",
  "evidence":           ["runs/20260410-120000/artifacts/output.json"],
  "outcome":            "done",
  "confidence":         "high"
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `goal_understanding` | str | agent 对任务目标的理解。是验收门最先校验的字段，也是判断 agent 是否在做正确事情的唯一语义锚点。 |
| `completed_work` | list[str] | 已完成事项列表，至少填一项（或 `final_answer` 非空）。 |
| `remaining_gaps` | list[str] | 遗留/未完成事项。**会被原样带进 `run_outcome.gaps`，是自动续作唯一的结构化输入。** |
| `evidence_type` | enum | 证据类型，见下表。 |
| `evidence` | list[str] | 证据列表。`evidence_type=artifact` 时填文件路径；其他类型填描述文字。 |
| `outcome` | enum | 完成状态，见下表。**这是驱动三态结果的核心字段。** |
| `confidence` | enum | 完成信心：`low` / `medium` / `high`。当前仅记录，未被任何判定消费。 |

### outcome 枚举

| 值 | 含义 | 验收结果 | run 终态 |
|----|------|----------|----------|
| `done` | 完整完成，无遗留 | `pass` | `completed` |
| `done_partial` | 主体完成，有已知缺口 | `weak_pass` | `partial` |
| `done_blocked` | 外部阻塞，只完成了可做部分 | `weak_pass` | `blocked` |

### evidence_type 枚举

| 值 | 含义 | 额外校验 |
|----|------|----------|
| `artifact` | 文件产物（路径） | 验收门会检查路径是否实际存在 |
| `tool_result` | 工具调用的返回结果 | 无额外校验 |
| `observation` | 观察到的现象或状态 | 无额外校验 |
| `none` | 无具体证据 | 无额外校验（也是非法枚举值的降级目标） |

---

## 验收门 1：完成报告（_review_completion_report）

位于 `agent/core/loop.py`，在每次 `ActionType.DONE` 触发时调用。

### 检查顺序

```
1. 读取 state.meta["completion_report"]
   └── 无结构化报告？→ 尝试旧 ACCEPTANCE 格式兼容（见下）

2. goal_understanding 是否非空？
   └── 否 → needs_more_work (missing_completion_report)

3. completed_work 是否非空，或 final_answer 是否有内容？
   └── 否 → needs_more_work (missing_completed_work)

4. evidence_type == "artifact"？
   └── 是 → 逐一检查 evidence 中的路径是否存在
       └── 有缺失 → needs_more_work (artifact_missing)，列出具体路径

5. outcome in {done_partial, done_blocked}？
   ├── 报告提交后又有新环境观察（watcher/后台 job 注入）？
   │   └── 是 → needs_more_work (stale_completion_report)，最多打回 2 次
   └── 否 → weak_pass

6. 以上全部通过 → pass
```

### 三种 verdict

**`needs_more_work`** — 继续循环补救。**这不是终态**，它 `continue` 回主循环，不产生 `run_outcome`。

| reason | 提示内容 |
|--------|----------|
| `missing_completion_report` | 提示调用 `submit_completion_report` 或追加 ACCEPTANCE 块 |
| `missing_completed_work` | 提示补充 `completed_work` 或提供 `final_answer` |
| `artifact_missing` | 列出缺失的文件路径，提示 `write_file` 后重试 |
| `stale_completion_report` | 报告提交后环境有了新观察，要求用最新数据重新提交 |

`stale_completion_report` 存在的原因：`weak_pass` 会把报告原文拼进 ask_user 展示给用户，若照搬提交时的旧快照，用户会看到与最新进展自相矛盾的数字。打回次数上限 2（`_stale_report_rejections`），防止 watcher 每迭代注入导致"刚提交又过期"的死循环。

**`weak_pass`** — 保存结果，系统发起 ask_user

`final_answer` 被保存并落盘，`run_outcome` 记为 `partial` / `blocked`，然后系统根据完成报告自动生成问题：

```
[主体工作完成，有已知遗留]

已完成:
  - 事项 A
  - 事项 B

遗留/阻塞:
  - 事项 C（API 不可达）

是否在此基础上继续推进？如果是，请告诉我下一步的重点；如果不需要，直接回复「完成」即可。
```

状态被标记为 `paused`，循环暂停，控制权交回调用方（`run_goal.py`）。

**`pass`** — 继续走门 2、门 3，最终 `run_outcome = completed` 后退出。

---

## 验收门 2 / 门 3：记忆沉淀

| 门 | 触发条件 | 行为 |
|----|----------|------|
| 门 2：episodic | `_episodic_appended` 未置位 | 打回，要求调用 `append_episodic` |
| 门 3：concept | `_concept_evaluated` 未置位 | 打回一次，要求评估是否 `save_concept`；无论是否保存都只走一次 |

这两门的打回**不产生 verdict**，只往 `state.meta["acceptance_failures"]` 追加 `{"reason": "missing_episodic"}` 之类的记录。

门 3 有一条收尾捷径：打回时把门 1 的结论暂存进 `_pending_final`，若 agent 接下来直接调用 `save_concept` 成功，则跳过"再 done 一次"的完整迭代，直接复用暂存结论收尾（`save_concept` 会改动 system prompt 前缀，那一次迭代的缓存失效代价最高）。两条路径共用 `_finalize_run`，行为一致。

---

## run 级终态（run_outcome）

`status` 和 `run_outcome` 是**正交**的两个维度，必须都读：

- **`status`** — 进程生命周期，dashboard 消费：`running` / `paused` / `done` / `failed`
- **`run_outcome`** — 任务完成质量，自动续作消费

只看 `status` 无法区分"完整完成"和"部分完成后用户放行"——两者都落 `done`。

| run_outcome | 触发 | resumable |
|-------------|------|-----------|
| `completed` | 验收 pass | ❌ |
| `partial` | `done_partial` → weak_pass | ✅ |
| `blocked` | `done_blocked` → weak_pass | ✅ |
| `exhausted` | 迭代预算耗尽 | ✅ |
| `aborted` | 用户 `/exit`、暂停中退出 | ❌ |
| `failed` | 异常中断 | ❌ |

落盘位置：

- `state.meta["run_outcome"]` — 完整记录
- `status.json` — `run_outcome`（字符串）、`resumable`（布尔）、`run_outcome_detail`（完整记录）
- `execution_summary.md` — 人读视图，含 `## Remaining Gaps` 章节

记录结构：

```json
{
  "outcome":   "partial",
  "reason":    "partial_completion",
  "resumable": true,
  "gaps":      ["格式 C 的解析未实现"],
  "iteration": 27,
  "at":        "2026-07-26T08:31:00+00:00",
  "error":     "仅 failed 时存在"
}
```

两条约定：

1. **先写者胜。** `_set_run_outcome` 不覆盖已有值——先到达的路径最贴近真实结束原因，后来的兜底不得篡改。
2. **消费方读 `resumable`，不要自己判断枚举。** 否则将来加新终态时每个消费点都得跟着改。

---

## 迭代耗尽与收尾窗口

预算耗尽若直接 `break`，agent 从来没有机会交代"做到哪了、还差什么"，`exhausted` 就只是一个光秃秃的布尔，续作只能从零重建上下文。

因此耗尽时先开一次**收尾窗口**：

1. 额外给 `_WRAPUP_BUDGET`（当前 2）次迭代
2. 置 `_wrapup_window`，**只放行收尾类工具**：`submit_completion_report` / `append_episodic` / `save_concept` / `scratchpad_*`；其余工具返回错误提示而不真正执行
3. 注入强指令，要求 agent 立刻提交完成报告，并明确告知 `remaining_gaps` 是唯一会传递给后续任务的信息
4. 窗口只开一次（`_wrapup_window_used`），预算再耗尽即强制退出

软提示挡不住 agent 拿最后的预算继续干新活，而预算一旦烧完就再没有机会拿到 gaps——所以这里用的是硬拦截。

---

## 向后兼容：旧 ACCEPTANCE 格式

如果 agent 没有调用 `submit_completion_report`，验收门会检查草稿本中是否有 `ACCEPTANCE` 关键字，并将其转换为结构化报告：

```
ACCEPTANCE
criteria: 完成了 X 功能
evidence_type: artifact
evidence: runs/20260410-120000/artifacts/output.json
verdict: PASS
```

转换规则：

- `goal_understanding` ← `state.goal`（原始任务描述）
- `completed_work` ← `final_answer` 首行（若有），兜底 `["已生成最终回答"]`
- `remaining_gaps` ← 空列表
- `evidence_type` / `evidence` ← 从 ACCEPTANCE 块解析（默认 `artifact`）
- `outcome` ← 固定为 `done`
- `confidence` ← 固定为 `medium`

**旧格式只能走 `pass` 路径，无法触发 `weak_pass`，也永远不会产出可续作的终态。** 要使用三态结果和延续推进功能，必须改用 `submit_completion_report`。

---

## 延续工作：ask_user 与多轮推进

`weak_pass` 触发的暂停与 agent 主动调用 `ask_user` 走的是同一套机制：

1. `state.meta["paused"] = True`
2. `state.meta["awaiting_input"] = <问题文本>`
3. 循环 `break`，状态落盘为 `paused`
4. 调用方收到 `AgentState`，读取 `awaiting_input`，向用户展示
5. 用户输入追加到 `state.short_term`
6. 以**原始 goal** 重新调用 `agent.run(goal, state=state)`

由于 `state.short_term` 和 `state.long_term` 完整保留，重新启动的 agent 拥有完整上下文。

### 续跑 = 重新验收

`run()` 的恢复分支会清空 `_RESUME_RESET_KEYS` 中的所有键：

```
completion_report      completion_review       run_outcome
_episodic_appended     _concept_evaluated      _pending_final
_obs_since_report      _stale_report_rejections
_wrapup_window         _wrapup_window_used
_iter_warn_injected    timeout
```

不清会出三类问题：

- **旧报告残留** → agent 本轮若未重新提交，门 1 读到上一轮报告再判一次 `weak_pass`，把刚解决掉的遗留项**原样再问用户一遍**（问题文本一字不差）
- **门 2/3 标记残留** → 续跑做的工作没有任何记忆沉淀
- **收尾窗口标记残留** → 续跑一开始就把工具全禁掉

`acceptance_failures` 不清——那是跨轮累积的诊断记录。

### 用户回复处理

```
用户回复"完成"/done/finish/ok/好/不用了  →  直接结束，终态保留 partial
用户给出新指令                          →  追加到 short_term → agent.run(goal, state=state)
```

---

## 自动续作（weak_pass → 新 run）

弱通过意味着"主体做完了，且 agent 自己知道差在哪"。默认它停下问人；开启自动续作后，
dashboard 会替人答一次，并**另起一个新 run** 去啃剩下的缺口。

**整套机制只存在于 `dashboard/server.js`，Python 侧一行未改。** 它借的是本文已经描述过的
两个既有行为：`_set_run_outcome` 在 pause 之前就把 `partial`/`blocked` 写进了 `status.json`，
而 weak_pass 的问题文本本来就告诉用户"回复「完成」即可结束"——`run_goal.py` 对这个词的处理
是干净退出（走 `finish()`，正常写出 `execution_summary.md`）。dashboard 只是替人说了这句话。

```
poll 看到 status=paused + run_outcome∈{partial,blocked} + 进程仍存活
     └─ 写 runs/<parent>/web_cmd.txt = "/inject 完成"      ← 父 run 按既有路径收尾
agent 进程退出
     └─ 读 meta.json 的 _user_goal，合成续作 goal，launchAgent()
新 run 目录出现
     └─ 在 runs/.followup.jsonl 里记 parent→child，代数由此累计
```

### 为什么是新 run 而不是原地续跑

原地续跑（`agent.run(goal, state=state)`）保留全部 `short_term`——而弱通过卡住的往往正是
那条推理路径，继承它等于继承僵局。另有一个硬故障：续跑不重置 `state.iteration`，若本次
weak_pass 来自"预算耗尽→收尾窗口→done_partial"，续跑一进主循环就再次 `iteration >= max`，
而 `_wrapup_window_used` 又被 `_RESUME_RESET_KEYS` 清掉 → 重开收尾窗口 → 两次迭代内再交
一份报告 → 又一次 weak_pass，无限循环。

新 run 拿到的是干净上下文和完整预算，代价是要自己把上一轮的成果读回来——而这恰好是
磁盘上现成的：`execution_summary.md`（结论 + Remaining Gaps）与压缩机制留下的
`handoff_*.md` 都已经是蒸馏过的形态。

### 续作 goal

原始 user goal 打头（run 列表的摘要取自它，被前言顶掉就看不出这个 run 在干什么），随后是
一段固定前言：指名读 `execution_summary.md` / `handoff_*.md`，**明令不要读
`short_term.jsonl`**（70 KB–1 MB，且正是要卸掉的那份思维定式），要求动手前先调用一次
`think` 回答三问（真正的卡点是什么／不沿用旧方案还有哪条路／打算走哪条），最后授权换路。

think 是写在开局任务里的要求，不是工具门控。中途注入的软提示会被跳过（收尾窗口就是因此
改用硬拦截），但开局任务的遵循度是另一回事——那等同于"这个 agent 是否执行任务要求"。

### 边界与约束

| 项 | 行为 |
|----|------|
| 开关 | `AUTO_FOLLOWUP=0` 关闭；`AUTO_FOLLOWUP_MAX_GEN` 控制代数，默认 1 |
| 代数用尽 | 记一条 `skipped`，**保持暂停**交回人工——即本特性之前的原有行为 |
| nostop | 天然排除：nostop 下 weak_pass 根本不 pause |
| 普通 ask_user | 不触发：判据是 `paused` + `run_outcome` 同时成立，而 `run_outcome` 在续跑时会被 `_RESUME_RESET_KEYS` 清掉，不存在陈旧残留 |
| 死掉的 run | 不触发：要求进程仍存活，否则重启 dashboard 会把磁盘上最新的那个旧 paused run 拉起来 |
| 继承 | skills（含中途 `read_skill` 拉进来的）、`--agents-profile`、`--advisor-profile` |
| 账本 | `runs/.followup.jsonl`，`GET /api/followup-history` 可读 |

账本记的是 nudged / launched / linked / skipped / dropped。**自动续作是否真的有用是个经验
问题**——续作那一代最终是 `completed` 还是又一次 weak_pass，顺着 `linked` 的 `child` 去读它的
`run_outcome` 就能统计。没有数据之前不要给它加更多机制。

---

## 状态记录

验收结果写入 `state.meta["completion_review"]`：

```json
{
  "status":  "pass | weak_pass | needs_more_work",
  "reason":  "completion_report_sufficient | partial_completion | blocked_completion | artifact_missing | stale_completion_report | ...",
  "report":  { ...normalized completion_report... },
  "missing": ["仅 artifact_missing 时存在，列出缺失路径"]
}
```

> 已知不一致：`missing_completion_report` 与 `missing_completed_work` 两个分支直接 return，**不写 `completion_review`**，该字段会停留在上一轮的旧值。

每次验收失败追加到 `state.meta["acceptance_failures"]`，可用于事后分析。

---

## 涉及的代码位置

| 内容 | 文件 | 位置 |
|------|------|------|
| `_normalize_completion_report` | `agent/core/loop.py` | `_parse_acceptance_evidence` 之后 |
| `_completion_report_from_legacy_acceptance` | `agent/core/loop.py` | 同上 |
| `_review_completion_report` | `agent/core/loop.py` | 同上 |
| `_set_run_outcome` / `RUN_OUTCOME_*` / `_RESUME_RESET_KEYS` | `agent/core/loop.py` | `_review_completion_report` 之后 |
| `_wrapup_blocks` / `_WRAPUP_*` | `agent/core/loop.py` | 同上 |
| `_finalize_run` | `agent/core/loop.py` | 同上 |
| 三道门（DONE 处理块） | `agent/core/loop.py` | `ActionType.DONE` 分支 |
| 收尾窗口开启 | `agent/core/loop.py` | 主循环 `iteration >= max_iterations` 处 |
| 耗尽/中止终态兜底 | `agent/core/loop.py` | `run()` 的 `try/except/else` 尾部 |
| 续跑重置 | `agent/core/loop.py` | `run()` 的 `else`（恢复）分支 |
| `tool_submit_completion_report` | `agent/tools/standard.py` | 异步工具节之前 |
| run 终态落盘 | `agent/runtime/persistence.py` | `_status_payload` / `_write_execution_summary` |
| 终态兜底 + nostop 键清理 | `run_goal.py` | `finally` 块 / `_NOSTOP_RESET_KEYS` |
| 自动续作（侦测/代答/拉起/账本） | `dashboard/server.js` | `followup*` 一节 |
| 回归测试 | `test/tests_acceptance_gate.py`、`dashboard/followup.test.js` | — |
