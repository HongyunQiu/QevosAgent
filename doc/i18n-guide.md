# 双语开发规范：新增代码如何处理 i18n

本文面向后续维护和新功能开发，说明在哪些场景下需要处理双语，以及如何操作，避免漏掉中文硬编码。

---

## 一、判断是否需要 i18n

不是所有字符串都需要双语化。按输出目标分三类：

| 类型 | 是否 i18n | 说明 |
|------|-----------|------|
| **LLM facing**（写入 `short_term`、系统提示、工具错误返回值） | **必须** | LLM 必须和用户语言一致，否则中英文用户体验割裂 |
| **终端输出**（`print`、日志、进度提示） | **应该** | 影响命令行用户的可读性 |
| **Dashboard 前端字符串** | **应该** | 面向看板用户 |
| 代码内注释、变量名 | **不需要** | 内部开发语言，不影响用户 |
| 纯英文 API 键名、路径、协议常量 | **不需要** | 这些本来就是语言无关的 |
| 日志文件中的结构化标记（如 `[TOOL ERROR]`） | 已有 OR 逻辑兼容，**不需要重复** | 见"marker 协议"部分 |

**快速判断**：这条字符串会出现在用户视野里（终端、浏览器、LLM 输出）吗？是 → 需要 i18n。

---

## 二、Python 后端：添加新字符串

### 步骤

**① 在 `agent/i18n.py` 同时添加 zh 和 en 两条**

选择合适的前缀（见下方前缀规范），在 `_STRINGS["zh"]` 和 `_STRINGS["en"]` 中各加一条，保持 key 完全一致：

```python
# agent/i18n.py

"zh": {
    ...
    "rg.my_new_string": "这是新字符串，参数: {param}",
    ...
},
"en": {
    ...
    "rg.my_new_string": "This is the new string, param: {param}",
    ...
},
```

**② 在代码里用 `t()` 调用，不要写字面量**

```python
# 正确
from agent.i18n import t
print(t("rg.my_new_string", param=value))

# 错误 — 直接硬编码中文
print(f"这是新字符串，参数: {value}")
```

**③ 特殊情况：`run_goal.py` 中延迟 import**

`run_goal.py` 是独立入口，`.env` 由 `ensure_env_defaults()` 在 `main()` 顶部加载。`t` 必须在该调用之后 import，否则 `QEVOS_LANG` 从 `.env` 读取失败：

```python
def main():
    ensure_env_defaults()
    from agent.i18n import t   # ← 必须在这里，不能放文件顶部
    ...
```

### 前缀规范

| 前缀 | 适用文件/场景 |
|------|--------------|
| `loop.*` | `loop.py` 终端进度输出 |
| `interrupt.*` | `user_interrupt.py` 干预命令 |
| `status.*` / `log.*` | `/status`、`/log` 命令显示 |
| `marker.*` | 写入 `short_term` 的协议标记（LLM 可见） |
| `compress.*` | `compression.py` 压缩提示 |
| `note.*` | 草稿本自动笔记迷你 LLM |
| `advisor.*` | `advisor.py` 高级指导员 |
| `sys.*` | `llm.py` 系统提示各节 |
| `err.*` | `llm.py` JSON 错误反馈 |
| `parse.*` | `llm.py` 内联解析错误 |
| `rg.*` | `run_goal.py` 相关 |
| `exec.*` | `executor.py` 工具执行错误 |
| `ui.*` | 新增 Python 生成的 UI 文本（如有） |

如果新功能属于全新模块，可以新增一个前缀（如 `scheduler.*`），只要在 zh/en 两个表里都加上即可。

---

## 三、前端 Dashboard：添加新字符串

### 步骤

**① 在 `dashboard/public/ui_i18n.js` 同时添加 zh 和 en 两条**

```js
const STRINGS = {
  zh: {
    ...
    'myfeature.label': '新功能标签',
    'myfeature.count': '{n} 个项目',
  },
  en: {
    ...
    'myfeature.label': 'New feature label',
    'myfeature.count': '{n} item(s)',
  },
};
```

**② 在 HTML/JS 里用 `uiT()` 调用**

```js
// 普通字符串
element.textContent = uiT('myfeature.label');

// 带参数插值
countEl.textContent = uiT('myfeature.count', { n: items.length });

// 模板字面量中嵌入
el.innerHTML = `<span>${uiT('myfeature.label')}</span>`;
```

**③ 静态 HTML 属性（title、placeholder 等）交给 JS 设置**

不要在 HTML 标签里直接写中文属性值，改在 JS 初始化时赋值：

```html
<!-- 错误 -->
<button title="这是提示">...</button>

<!-- 正确：HTML 留空，JS 初始化时设置 -->
<button id="myBtn">...</button>
```

```js
// 在页面初始化代码里
document.getElementById('myBtn').title = uiT('myfeature.btn_title');
```

**④ `window.QEVOS_LANG` 由 `server.js` 自动注入，无需手动处理**

`serveStatic()` 会在每个 HTML 响应的 `<head>` 注入：
```html
<script>window.QEVOS_LANG="zh";</script>
```
`ui_i18n.js` 读取此值初始化 `uiT()`，新页面或新功能页面只需确保 `<head>` 中包含 `<script src="/ui_i18n.js"></script>` 即可。

---

## 四、marker 协议：写入 short_term 的标记

写入 `short_term` 的协议标记（LLM 读到的内容）统一用 `t("marker.*")` 生成，消费侧（`persistence.py`、`server.js`）已有 OR 逻辑同时匹配中英文，兼容旧日志。

**新增 marker 时的操作：**

1. 在 `agent/i18n.py` 的 `marker.*` 节添加 zh/en 两条
2. 生产侧（通常是 `loop.py` 或其他写入 `short_term` 的地方）用 `t("marker.xxx")` 生成
3. 如果消费侧（`persistence.py` 或 `server.js`）有字符串匹配逻辑，在原有 tuple/正则中追加英文变体

```python
# persistence.py 示例：原有
_MY_MARKERS = ("中文标记",)
# 追加英文
_MY_MARKERS = ("中文标记", "English marker")
```

```js
// server.js 示例
text.match(/中文标记|English marker/)
```

---

## 五、常见遗漏点 checklist

在提交代码前，快速过一遍：

- [ ] 新增的 `print()` / `logging.*` 里有中文字面量吗？→ 改用 `t()`
- [ ] 新增的工具返回值 `ToolResult(error=...)` 里有中文吗？→ 改用 `t("exec.*")`
- [ ] 新增的注入 `short_term` 的消息里有中文字面量吗？→ 改用 `t("marker.*")`
- [ ] 新增了系统提示的某个节？→ 在 `sys.*` 下加 zh/en
- [ ] 新增了 Dashboard 前端组件？→ `ui_i18n.js` + `uiT()` 调用
- [ ] 新增了 HTML 静态属性（title、placeholder、alt）？→ JS 初始化赋值
- [ ] `agent/i18n.py` 里 zh 和 en 两个表的 key 数量一致？（数量不一致说明遗漏了英文翻译）

### 快速检查命令

找出代码中可能遗漏的中文字符串（排除注释和已知的 i18n.py）：

```bash
# 找 Python 文件中未经 t() 包裹的中文字面量（不含 i18n.py 自身）
grep -rn --include="*.py" "[^\#].*[一-鿿]" agent/ run_goal.py \
  | grep -v "i18n.py" | grep -v "^.*#"

# 找 JS 文件中的中文字面量（排除 ui_i18n.js 自身）
grep -rn "[一-鿿]" dashboard/public/ \
  | grep -v "ui_i18n.js"
```

> Windows PowerShell 下 `\u` 范围写法可能不同，可改用 `findstr /n /r "[^\x00-\x7F]"` 粗过滤后人工复核。

---

## 六、暂未处理的部分（已知例外）

以下内容暂不做 i18n，如未来有需求再处理：

| 文件 | 原因 |
|------|------|
| `agent/tools/standard.py` 工具描述字符串 | 量大，且工具描述本身就是传给 LLM 的提示，改动影响面广，需专项处理 |
| `AGENTS.md` / `ADVISOR.md` | 用户可手动编辑，维护两份语言文件容易混乱；建议用户自行决定语言 |

如果未来要处理 `AGENTS.md` 的双语，参考方案：用 `AGENTS.en.md` / `AGENTS.zh.md` 后缀文件，加载时按 `LANG` 选择，找不到对应后缀则 fallback 到 `AGENTS.md`。
