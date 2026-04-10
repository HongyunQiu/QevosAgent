# 验收流程设计

这份文档描述 `simpleAgent` 当前的验收机制：agent 如何声明"完成"、系统如何审核、以及不同结果下的处理路径。

---

## 概览

验收发生在 agent 调用 `done` 动作的那一刻。系统不会立即退出，而是先经过一个验收门（`_review_completion_report`），根据审核结果决定继续、暂停还是退出。

```
agent 调用 done
       │
       ▼
_review_completion_report()
       │
       ├── needs_more_work ──► 注入错误提示，继续循环
       │
       ├── weak_pass ────────► 保存结果，系统发起 ask_user，等待用户决策
       │                            │
       │                            ├── 用户说"继续" ──► 恢复 loop，带完整上下文推进
       │                            └── 用户说"完成" ──► 正式退出
       │
       └── pass ─────────────► 直接退出
```

---

## 完成报告

验收门的核心输入是**完成报告**（`completion_report`），存放在 `state.meta["completion_report"]`。

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
| `remaining_gaps` | list[str] | 遗留/未完成事项。`outcome` 为 `done` 时可为空。 |
| `evidence_type` | enum | 证据类型，见下表。 |
| `evidence` | list[str] | 证据列表。`evidence_type=artifact` 时填文件路径；其他类型填描述文字。 |
| `outcome` | enum | 完成状态，见下表。**这是驱动三态结果的核心字段。** |
| `confidence` | enum | 完成信心：`low` / `medium` / `high`。当前用于记录，未来可用于差异化处理。 |

### outcome 枚举

| 值 | 含义 | 验收结果 |
|----|------|----------|
| `done` | 完整完成，无遗留 | `pass`，直接退出 |
| `done_partial` | 主体完成，有已知缺口 | `weak_pass`，暂停询问用户 |
| `done_blocked` | 外部阻塞，只完成了可做部分 | `weak_pass`，暂停询问用户 |

### evidence_type 枚举

| 值 | 含义 | 额外校验 |
|----|------|----------|
| `artifact` | 文件产物（路径） | 验收门会检查路径是否实际存在 |
| `tool_result` | 工具调用的返回结果 | 无额外校验 |
| `observation` | 观察到的现象或状态 | 无额外校验 |
| `none` | 无具体证据 | 无额外校验 |

---

## 验收门逻辑（_review_completion_report）

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
   └── 是 → weak_pass

6. 以上全部通过 → pass
```

### 三种 verdict

**`needs_more_work`** — 继续循环补救

系统将错误原因追加到 `short_term`，agent 继续执行。错误信息按原因定制：

| reason | 提示内容 |
|--------|----------|
| `missing_completion_report` | 提示调用 `submit_completion_report` 或追加 ACCEPTANCE 块 |
| `missing_completed_work` | 提示补充 `completed_work` 或提供 `final_answer` |
| `artifact_missing` | 列出缺失的文件路径，提示 `write_file` 后重试 |

**`weak_pass`** — 保存结果，系统发起 ask_user

`final_answer` 被保存到 `state.meta["final_answer"]` 并落盘，然后系统根据完成报告自动生成问题：

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

**`pass`** — 直接退出

`final_answer` 被保存，循环 `break`，返回 `AgentState`。

---

## 向后兼容：旧 ACCEPTANCE 格式

如果 agent 没有调用 `submit_completion_report`，验收门会检查草稿本中是否有 `ACCEPTANCE` 关键字，并将其转换为结构化报告：

```
# 草稿本中的旧格式（仍然有效）
ACCEPTANCE
criteria: 完成了 X 功能
evidence_type: artifact
evidence: runs/20260410-120000/artifacts/output.json
verdict: PASS
```

转换规则：
- `goal_understanding` ← `state.goal`（原始任务描述）
- `completed_work` ← `final_answer` 首行（若有）
- `remaining_gaps` ← 空列表
- `evidence_type` / `evidence` ← 从 ACCEPTANCE 块解析
- `outcome` ← 固定为 `done`（旧格式不支持三态，统一视为完整完成）
- `confidence` ← 固定为 `medium`

**这意味着旧格式只能走 `pass` 路径，无法触发 `weak_pass` 的用户询问环节。** 要使用三态结果和延续推进功能，需改用 `submit_completion_report`。

---

## 延续工作：ask_user 与多轮推进

`weak_pass` 触发的暂停与 agent 主动调用 `ask_user` 工具走的是同一套机制：

1. `state.meta["paused"] = True`
2. `state.meta["awaiting_input"] = <问题文本>`
3. 循环 `break`，状态落盘为 `paused`
4. 调用方收到 `AgentState`，读取 `awaiting_input`，向用户展示
5. 用户输入追加到 `state.short_term`
6. 以**原始 goal** 重新调用 `agent.run(goal, state=state)`

由于 `state.short_term` 和 `state.long_term` 完整保留，重新启动的 agent 拥有完整上下文：它知道已完成了什么、遗留了什么、用户的新指令是什么，可以直接从当前基础上推进，而不必重头开始。

### 用户回复处理

调用方（`run_goal.py`）收到 paused 状态后的处理逻辑：

```
用户回复"完成" / 空回复  →  直接结束，state.meta["final_answer"] 已存在
用户给出新指令         →  追加到 short_term → agent.run(goal, state=state)
                            agent 带完整历史继续执行，completion_report 会在下轮 done 时重置
```

---

## 状态记录

验收结果写入 `state.meta["completion_review"]`，格式：

```json
{
  "status":  "pass | weak_pass | needs_more_work",
  "reason":  "completion_report_sufficient | partial_completion | blocked_completion | artifact_missing | ...",
  "report":  { ...normalized completion_report... },
  "missing": ["仅 artifact_missing 时存在，列出缺失路径"]
}
```

每次 `needs_more_work` 的失败记录追加到 `state.meta["acceptance_failures"]`，可用于事后分析或调试。

---

## 涉及的代码位置

| 内容 | 文件 | 位置 |
|------|------|------|
| `_normalize_completion_report` | `agent/core/loop.py` | `_parse_acceptance_evidence` 之后 |
| `_completion_report_from_legacy_acceptance` | `agent/core/loop.py` | 同上 |
| `_review_completion_report` | `agent/core/loop.py` | 同上 |
| DONE 处理块（验收门调用点） | `agent/core/loop.py` | `ActionType.DONE` 分支 |
| `tool_submit_completion_report` | `agent/tools/standard.py` | 异步工具节之前 |
| `submit_completion_report` 工具注册 | `agent/tools/standard.py` | `get_standard_tools()` |
| ask_user 暂停机制（通用） | `agent/core/loop.py` | `action.tool == "ask_user"` 分支 |
