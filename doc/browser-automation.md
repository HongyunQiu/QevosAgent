# 浏览器自动化（web_interact）

Agent 可通过 `web_interact` 工具在已打开的浏览器视图中执行自动化操作，包括页面导航、JavaScript 执行、截图、元素点击与表单填写等。

系统支持两种运行模式，使用同一套工具接口，调用方式完全相同：

| 模式 | 适用场景 | 依赖 |
|------|---------|------|
| **Electron 模式** | 桌面应用（默认） | 无需额外配置 |
| **CDP 模式** | 命令行 / 纯浏览器 | 浏览器需以 `--remote-debugging-port=9222` 启动 |

---

## 架构

### Electron 模式

```
Agent
  └─ tool_web_interact()  →  POST /api/browser-action
       └─ dashboard/server.js  →  serverEvents.emit('browser-action')
            └─ desktop/main.js  →  WebContentsView.webContents API
```

`desktop/main.js` 通过 `gViews` Map 管理所有标签页（key 为 `'view-' + display_id`），接收事件后直接调用 Electron 的 `webContents` 方法执行操作，结果通过回调同步返回。

视图分为两种类型，行为不同：

| 视图类型 | 创建方式 | 页内导航 | 链接点击 |
|---------|---------|---------|---------|
| **内容视图** | `web_show` | 禁止（锁定 dashboard URL） | 打开系统浏览器 |
| **自动化视图** | `web_interact new_tab` | 允许 | 同页跳转；`target=_blank` 打开系统浏览器 |

### CDP 模式（非 Electron）

```
Agent
  └─ tool_web_interact()  →  POST /api/browser-action
       └─ dashboard/server.js  →  cdpBrowserAction()
            └─ CDP WebSocket  →  Chrome / Edge
```

`server.js` 通过 HTTP 连接本机 `localhost:9222`（或 `CDP_PORT` 环境变量指定的端口）获取标签列表，再对每个标签的 WebSocket 调试地址发送 Chrome DevTools Protocol 命令。

`cdpTargets` Map（`display_id → targetId`）记录每个 display_id 对应的标签，确保多次操作作用于同一页面。

---

## 使用前提

### Electron 模式

无需配置，只需先通过 `web_show` 创建对应 `display_id` 的视图，或直接调用 `new_tab` 创建新视图。

### CDP 模式

以调试端口启动 Chrome 或 Edge：

```bash
# Windows
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222

# macOS
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222

# Linux
google-chrome --remote-debugging-port=9222
```

如需使用非默认端口，启动 server.js 时设置环境变量：

```bash
CDP_PORT=9333 node dashboard/server.js
```

未开启调试端口时调用工具，会收到如下错误，Agent 可据此提示用户：

```
无法连接到浏览器 CDP（端口 9222）。请以 --remote-debugging-port=9222 启动 Chrome/Edge 后重试。
```

---

## `web_interact` 工具

### 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `action` | string | ✅ | 操作类型，见下表 |
| `display_id` | string | — | 目标视图 ID，对应 `web_show` 的 `display_id`，默认 `"default"` |
| `payload` | object | — | 操作参数，不同 action 所需字段不同 |

### 支持的操作

#### `new_tab` — 打开新标签页

在 Electron 中创建新的 `WebContentsView` 标签页，或在 CDP 模式中通过 `/json/new` 创建新标签。

```python
web_interact(action="new_tab", display_id="browser1", payload={
    "url": "https://example.com",   # 可选，默认 about:blank
    "title": "示例页面"              # 可选，标签页标题
})
```

返回：`{ "ok": true }`

---

#### `navigate` — 导航到 URL

跳转到指定 URL 并等待页面加载完成（最多等待 15 秒）。

```python
web_interact(action="navigate", display_id="browser1", payload={
    "url": "https://example.com/page"
})
```

返回：`{ "ok": true }` 或 `{ "ok": true, "note": "load timeout" }`（超时但继续执行）

---

#### `eval` — 执行 JavaScript

在页面上下文中执行任意 JavaScript 表达式，返回执行结果。支持 Promise（自动 await）。

```python
# 读取页面标题
web_interact(action="eval", display_id="browser1", payload={
    "code": "document.title"
})
# 返回：{ "ok": true, "result": "页面标题" }

# 等待异步操作
web_interact(action="eval", display_id="browser1", payload={
    "code": "fetch('/api/data').then(r => r.json())"
})
```

返回：`{ "ok": true, "result": <JS返回值> }`

---

#### `get_html` — 获取页面 HTML

读取当前页面的完整 HTML 内容。

```python
web_interact(action="get_html", display_id="browser1", payload={})
```

返回：`{ "ok": true, "html": "<!DOCTYPE html>..." }`

---

#### `screenshot` — 截图

截取当前页面截图，返回 base64 编码的 PNG 图片。

```python
web_interact(action="screenshot", display_id="browser1", payload={})
# 返回：{ "ok": true, "data": "<base64 PNG>" }
```

可将返回的 `data` 传给 `web_show`（`content_type: "image"`）展示给用户。

---

#### `click` — 点击元素

通过 CSS 选择器定位元素并触发点击事件。

```python
web_interact(action="click", display_id="browser1", payload={
    "selector": "#submit-btn"
})

# 示例：点击第一个链接
web_interact(action="click", display_id="browser1", payload={
    "selector": "a[href]"
})
```

返回：`{ "ok": true }`（元素不存在时静默忽略）

---

#### `fill` — 填写输入框

通过 CSS 选择器定位输入框，设置值并触发 `input` / `change` 事件（兼容 React、Vue 等框架的响应式更新）。

```python
web_interact(action="fill", display_id="browser1", payload={
    "selector": "input[name='username']",
    "value": "hello"
})
```

返回：`{ "ok": true }`（元素不存在时静默忽略）

---

#### `key_type` — 原生文字输入

向已聚焦元素注入文字，**绕过 React / Vue 的 JS 事件拦截**，适用于 `contenteditable` 富文本框（如 Twitter 发推框、飞书文档等）。

```python
web_interact(action="key_type", display_id="browser1", payload={"text": "你好世界"})
```

返回：`{ "ok": true }`

---

#### `key_press` — 按下特殊键

```python
web_interact(action="key_press", display_id="browser1", payload={"key": "Enter"})
```

支持的键名：`Enter` / `Tab` / `Escape` / `Backspace` / `Delete` / `ArrowUp` / `ArrowDown` / `ArrowLeft` / `ArrowRight` / `Home` / `End` / `PageUp` / `PageDown` / `Space`

---

#### `key_combo` — 组合键

发送带修饰键的快捷键。

```python
# Ctrl+A 全选
web_interact(action="key_combo", display_id="browser1", payload={"key": "A", "modifiers": ["ctrl"]})

# Ctrl+Enter 发送（常见于聊天/发帖场景）
web_interact(action="key_combo", display_id="browser1", payload={"key": "Enter", "modifiers": ["ctrl"]})

# Ctrl+Shift+Z 重做
web_interact(action="key_combo", display_id="browser1", payload={"key": "Z", "modifiers": ["ctrl", "shift"]})
```

支持的修饰键：`ctrl` / `shift` / `alt` / `meta`（macOS Command）

---

#### `mouse_move` — 移动鼠标

```python
web_interact(action="mouse_move", display_id="browser1", payload={"x": 400, "y": 300})
```

---

#### `mouse_click` — 坐标点击

```python
# 单击
web_interact(action="mouse_click", display_id="browser1", payload={"x": 400, "y": 300})

# 双击
web_interact(action="mouse_click", display_id="browser1", payload={"x": 400, "y": 300, "count": 2})

# 右键
web_interact(action="mouse_click", display_id="browser1", payload={"x": 400, "y": 300, "button": "right"})
```

---

#### `mouse_down` / `mouse_up` — 分离的按下与抬起

用于**长按**：在 `mouse_down` 和 `mouse_up` 之间可插入等待或其他操作。

```python
# 长按（按住 1 秒）
web_interact(action="mouse_down", display_id="browser1", payload={"x": 400, "y": 300})
web_interact(action="eval",       display_id="browser1", payload={"code": "await new Promise(r => setTimeout(r, 1000))"})
web_interact(action="mouse_up",   display_id="browser1", payload={"x": 400, "y": 300})
```

---

#### `drag` — 拖拽

从起点平滑移动到终点，中间插值 `steps` 步（默认 10），适用于拖拽排序、滑块控件、画布绘制。

```python
web_interact(action="drag", display_id="browser1", payload={
    "x1": 200, "y1": 300,   # 起点
    "x2": 500, "y2": 300,   # 终点
    "steps": 20,             # 插值步数，越多越平滑
})
```

---

#### `scroll` — 滚动

```python
# 向下滚动 500px
web_interact(action="scroll", display_id="browser1", payload={"x": 400, "y": 300, "deltaY": 500})

# 水平滚动
web_interact(action="scroll", display_id="browser1", payload={"x": 400, "y": 300, "deltaX": 300})
```

---

## 典型工作流

### 打开页面并提取数据

```python
# 1. 打开新标签页
web_interact(action="new_tab", display_id="scraper", payload={"title": "数据采集"})

# 2. 导航到目标页面
web_interact(action="navigate", display_id="scraper", payload={"url": "https://example.com"})

# 3. 提取数据
result = web_interact(action="eval", display_id="scraper", payload={
    "code": "Array.from(document.querySelectorAll('.item')).map(el => el.textContent)"
})

# 4. 截图存档
screenshot = web_interact(action="screenshot", display_id="scraper", payload={})
web_show(content=screenshot["data"], content_type="image", display_id="result", title="采集结果")
```

### 自动化表单提交（普通 input）

```python
web_interact(action="new_tab",   display_id="form", payload={"url": "https://example.com/login"})
web_interact(action="fill",      display_id="form", payload={"selector": "#username", "value": "user"})
web_interact(action="fill",      display_id="form", payload={"selector": "#password", "value": "pass"})
web_interact(action="click",     display_id="form", payload={"selector": "button[type=submit]"})
```

### 在 React contenteditable 中发帖（如 Twitter）

```python
# 1. 截图确认坐标
shot = web_interact(action="screenshot", display_id="tw", payload={})
# 2. 点击发推框聚焦（坐标根据截图确定）
web_interact(action="mouse_click", display_id="tw", payload={"x": 600, "y": 200})
# 3. 原生键盘输入（绕过 React 拦截）
web_interact(action="key_type",    display_id="tw", payload={"text": "Hello from QevosAgent!"})
# 4. 截图确认内容
web_interact(action="screenshot",  display_id="tw", payload={})
# 5. 点击发布按钮
web_interact(action="click",       display_id="tw", payload={"selector": "[data-testid='tweetButton']"})
```

### 拖拽排序

```python
web_interact(action="drag", display_id="board", payload={
    "x1": 200, "y1": 150,   # 拖拽源位置
    "x2": 200, "y2": 400,   # 目标位置
    "steps": 20,
})
```

### 长按弹出上下文菜单

```python
web_interact(action="mouse_down", display_id="app", payload={"x": 350, "y": 250})
web_interact(action="eval",       display_id="app", payload={"code": "await new Promise(r=>setTimeout(r,800))"})
web_interact(action="mouse_up",   display_id="app", payload={"x": 350, "y": 250})
web_interact(action="screenshot", display_id="app", payload={})  # 确认菜单已出现
```

---

## 实现文件

| 文件 | 职责 |
|------|------|
| `agent/tools/standard.py` | `tool_web_interact()` 函数及 `ToolSpec` 注册 |
| `dashboard/server.js` | `/api/browser-action` 路由；CDP 工具函数（`cdpHttp` / `cdpSend` / `cdpNavigate` / `cdpBrowserAction`）；`cdpTargets` Map |
| `desktop/main.js` | `serverEvents.on('browser-action', ...)` 处理器；`openElectronView()` 的 `allowNavigation` 参数 |

### 关键常量

| 常量 | 默认值 | 说明 |
|------|--------|------|
| `CDP_PORT` | `9222` | CDP 调试端口，可通过环境变量覆盖 |
| CDP 超时 | 10 秒（单条命令）/ 15 秒（导航） | 硬编码于 `cdpSend` / `cdpNavigate` |

---

## 限制与注意事项

- **跨域限制**：CDP 模式受浏览器同源策略约束；Electron 模式中 `contextIsolation: true`，`eval` 只能操作页面自身的 DOM 与脚本。
- **并发**：CDP 每个标签同一时刻只能有一个调试 WebSocket 连接，`cdpSend` 采用一次性连接模式，避免冲突。
- **元素不存在**：`click` / `fill` 使用可选链（`?.`）静默忽略，若需判断是否成功请用 `eval` 检查元素存在性。
- **动态内容**：`navigate` 等待 `loadEventFired` 事件，对于 SPA（单页应用）异步渲染的内容，可在 `navigate` 后用 `eval` 轮询检查目标元素。
