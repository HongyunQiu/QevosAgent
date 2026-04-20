# JSON 错误处理机制

本文档描述 QevosAgent 在 Agent 主循环中处理 LLM JSON 输出错误的完整机制。

---

## 一、问题背景

Agent 要求 LLM 每轮以固定 JSON 格式输出（含 `thought`、`action`、`tool`、`args` 等字段）。由于模型能力差异、上下文过长、输出截断等原因，实际输出频繁出现各类 JSON 错误，处理这些错误是保障 Agent 稳定运行的关键。

---

## 二、整体架构

错误处理分为四个层次，按触发顺序依次生效：

```
LLM 原始输出
    │
    ▼
【层 1】_extract_json：容错提取
    │  失败
    ▼
【层 2】parse_response：错误分类 + error_type 标注
    │  返回 ActionType.ERROR
    ▼
【层 3】loop.py ERROR 块：即时修复响应
    │  ├── max_tokens 自动扩容（截断类错误）
    │  ├── 连续失败计数 + 过载提示
    │  └── _apply_runtime_patch：生成运行时补丁规则
    ▼
【层 4】runtime_patches：注入 system prompt，持久化到 AGENTS.md
```

---

## 三、层 1 — `_extract_json`：容错提取

**位置**：[`agent/core/llm.py`](../agent/core/llm.py)

在将原始字符串交给 `parse_response` 之前，先尝试五种策略逐级提取有效 JSON：

| 策略 | 说明 |
|------|------|
| 0. 预处理 | 剥离 `<think>...</think>` 推理标签（DeepSeek R1 / Qwen QwQ 风格） |
| 1. 直接解析 | `json.loads(stripped)`，最快路径；若报 newline 错误额外尝试转义换行后重解 |
| 2. ` ```json ` 围栏 | 定位 ` ```json ` 标记，用 `raw_decode` 从花括号起点解析，避免围栏内嵌套干扰 |
| 3. 花括号扫描 | 逐个 `{` 位置尝试 `raw_decode`，优先返回含 `thought`/`action` 的对象 |
| 4. json_repair | 调用第三方库修复畸形 JSON（缺引号、多余逗号等） |
| 5. 兜底 | 返回扫描到的任意 dict，宁可误判也不报失败 |

策略 2 刻意放在策略 3 之前，防止正文中的偶发 `{` 遮蔽真正的 JSON 载体。

---

## 四、层 2 — `parse_response`：错误分类

**位置**：[`agent/core/llm.py:parse_response`](../agent/core/llm.py)

提取成功后还需验证结构；提取失败或结构非法时，对错误进行分类并设置 `Action.error_type`：

| error_type | 触发条件 | 典型表现 |
|---|---|---|
| `prose_no_json` | 输出不含任何 `{` | 模型完全忘记 JSON 协议，输出纯文本 |
| `prose_with_json` | 有 `{` 但无 `thought`/`action` 字段 | 输出了 JSON 片段（如代码示例）但不是 agent 响应 |
| `bare_newline` | 字符串值内含未转义换行 | `"thought": "第一行\n第二行"` |
| `unescaped_backslash` | 字符串内含非法反斜杠转义 | Windows 路径 `C:\Users\foo` 未双写 `\\` |
| `unterminated_string` | 字符串未闭合 | 输出超出 max_tokens 被截断 |
| `split_structure` | thought 字段提前闭合 | `{"thought":"..."}, "action": ...` 结构错位 |
| `single_quote_key` | key 使用单引号 | `{'thought': "..."}` |
| `unknown` | 上述均不匹配 | 其他罕见格式错误 |

每个分支都会生成对应的中文诊断提示（写入 `Action.thought`），在下一轮作为 user 消息告知模型如何修正。

**特殊处理**：若解析成功但 `data` 缺少 `thought`/`action`，检测是否是 `{"role": ..., "content": "..."}` 包装格式，若是则递归解包内层 content。

---

## 五、层 3 — `loop.py` ERROR 块：即时修复

**位置**：[`agent/core/loop.py`](../agent/core/loop.py)，`action.type == ActionType.ERROR` 分支

### 5.1 max_tokens 自动扩容

仅针对 `"JSON 解析失败"` 类错误（通常意味着输出被截断）：

- 每次截断失败将 `max_tokens` 翻倍，上限受环境变量 `LLM_MAX_TOKENS_CAP`（默认 32768）约束
- 最多尝试 `JSON_PARSE_RETRY_MAX`（默认 3）次，避免无限扩容
- 扩容记录写入 `long_term`，供后续 session 参考

### 5.2 连续失败计数与过载提示

维护 `state.meta["_json_fail_streak"]` 记录连续 JSON 错误次数：

- 超过 `retry_max` 次后，在错误反馈中追加强提示，指示模型改变策略（拆步写文件）
- 成功解析后自动清零

### 5.3 运行时补丁生成

调用 `_apply_runtime_patch(raw, action, state, llm)`（见层 4），在此轮产生补丁规则。

### 5.4 错误反馈注入 short_term

将诊断信息 + 修复建议作为 `user` 消息追加到对话历史，模型在下一轮看到具体的错误原因和修正示例。

---

## 六、层 4 — 运行时补丁系统

**位置**：[`agent/core/compression.py:_apply_runtime_patch`](../agent/core/compression.py)

### 6.1 设计目标

将错误的修复规则从「仅对当前轮注入」升级为「注入到每轮的 system prompt」，使规则在整个运行期间持续生效，而不只修复单次错误。

### 6.2 已知类型：静态规则映射

```python
_JSON_ERROR_PATCH_RULES = {
    "bare_newline":        "JSON字符串内的换行必须转义为\\n，禁止直接回车换行",
    "unescaped_backslash": "Windows路径的反斜杠\\必须写成\\\\，或改用正斜杠/",
    "unterminated_string": "超长内容先用write_file写入文件，args/final_answer只引用路径，避免截断",
    "split_structure":     "thought/action/tool/args必须全部在同一个顶层{}内，thought不能单独成对象",
    "single_quote_key":    "JSON的key必须用双引号\"\"，不能用单引号''",
}
```

命中后直接去重追加到 `state.meta["runtime_patches"]`，无 LLM 调用，确定性快速。

### 6.3 未知类型：mini LLM 诊断

`error_type == "unknown"` 时触发，设计了两个保护机制：

**频控**：每次运行最多诊断 `RUNTIME_PATCH_UNKNOWN_MAX`（默认 2）次，防止密集错误时滥用调用。

**候选晋升**：诊断结果先进入 `meta["_patch_candidates"]`，同一规则出现 ≥ 2 次才升级为正式 patch，过滤偶发误判。

mini LLM 调用使用 `complete_text`（无 JSON 格式要求，max_tokens=60），输入仅为错误信息摘要和原始输出片段，延迟极低。

### 6.4 注入 system prompt

[`build_system_prompt`](../agent/core/llm.py) 每轮重建时，若 `runtime_patches` 非空，在 system prompt 中插入独立节：

```
## 运行时格式规范（自动生成，必须严格遵守）
- JSON字符串内的换行必须转义为\n，禁止直接回车换行
- ...
```

位置在工具列表之后、草稿本之前，确保每轮都可见。

### 6.5 持久化工具

模型可在任务结束前调用 `persist_runtime_patches` 工具，将本次运行有效的规则写回 AGENTS.md 的 `## 运行时经验（自动生成）` 节，供后续运行加载使用（作为启动时 prefix 的一部分）。写入时自动替换已有节，不会重复追加。

---

## 七、数据流总结

```
错误发生
  │
  ├─ error_type 已知 ──→ 静态规则 ──→ runtime_patches（当轮生效）
  │                                         │
  └─ error_type=unknown                     │  每轮 build_system_prompt
       │                                    ▼
       ├─ 频控通过 ──→ mini LLM ──→ _patch_candidates
       │                               │ 出现≥2次
       │                               ▼
       │                         runtime_patches（当轮生效）
       └─ 频控拒绝（跳过）
                                         │
                                         ▼ 任务结束
                              persist_runtime_patches
                                         │
                                         ▼
                                   AGENTS.md（持久化）
```

---

## 八、相关环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `JSON_PARSE_RETRY_MAX` | `3` | JSON 截断类错误最多扩容重试次数 |
| `LLM_MAX_TOKENS_CAP` | `32768` | max_tokens 扩容上限 |
| `RUNTIME_PATCH_UNKNOWN_MAX` | `2` | 每次运行 unknown 类型 mini LLM 诊断上限 |

---

## 九、涉及文件

| 文件 | 职责 |
|------|------|
| [`agent/core/types.py`](../agent/core/types.py) | `Action.error_type` 字段定义 |
| [`agent/core/llm.py`](../agent/core/llm.py) | `_extract_json`（容错提取）、`parse_response`（分类标注）、`build_system_prompt`（注入 patches） |
| [`agent/core/compression.py`](../agent/core/compression.py) | `_apply_runtime_patch`（静态映射 + mini LLM 候选晋升） |
| [`agent/core/loop.py`](../agent/core/loop.py) | ERROR 块协调逻辑：扩容、计数、补丁调用、反馈注入 |
| [`agent/tools/standard.py`](../agent/tools/standard.py) | `persist_runtime_patches` 工具（写回 AGENTS.md） |
