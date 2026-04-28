# Dashboard RUNS 列表摘要功能

## 背景

RUNS 列表原本只显示格式化的时间戳（如 `04/29 14:23:01`），难以快速区分各次任务的内容。

## 方案设计

### 摘要存储位置

将摘要作为 `summary` 字段写入每个 run 的 `status.json`。该文件在 Dashboard 轮询时本来就会被读取，无需额外 I/O。

草稿本（scratchpad）不适合存摘要，原因：
- 它是 agent 运行时工作记忆，内容由 agent 频繁修改和裁剪
- Dashboard 需要额外解析才能提取摘要
- 语义不对——草稿本是思考过程，不是任务标签

### 两阶段摘要

| 阶段 | 内容来源 | 说明 |
|------|---------|------|
| 任务启动时 | `goal` 首句，最长 40 字 | 立即可用，告诉用户"任务要做什么" |
| 任务完成时 | `final_answer` 首句，最长 40 字 | 覆盖启动摘要，告诉用户"任务做了什么" |

### 历史任务兼容

旧的 run 的 `status.json` 没有 `summary` 字段，服务端读到 `undefined` 后存为空字符串，前端自动 fallback 显示时间戳，不需要任何迁移操作。

## 实现细节

### `agent/runtime/persistence.py`

新增辅助函数 `_make_summary(text, max_len=40)`：按中英文句末符号（`。？！\n.?!`）截取第一句；超长则截断并补省略号。

`_status_payload` 增加 `summary` 字段，支持 `summary_override` 参数供 `finish()` 传入完成摘要。

```
启动时：summary = _make_summary(goal)
完成时：summary = _make_summary(final_answer)  # 经 summary_override 覆盖
```

### `dashboard/server.js`

- `state` 增加 `runSummaries: {}` 字典（`runId → summary`）
- runs 列表变化时，对未缓存的 run 读 `status.json` 填充摘要
- active run 的 `status.json` 更新时同步刷新摘要（完成后即时生效）

### `dashboard/public/index.html`

- `.run-id` 加 `flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap`，防止长摘要撑破布局
- `renderRunsList` 优先显示 `runSummaries[r]`，无则 fallback 到 `fmtRunId(r)`
- hover tooltip 显示"时间戳 — 摘要"，保留时间参考

## 涉及文件

- `agent/runtime/persistence.py`
- `dashboard/server.js`
- `dashboard/public/index.html`
