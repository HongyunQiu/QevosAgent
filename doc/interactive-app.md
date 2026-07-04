# 交互式 App(UI App)最小落地稿

> 目标:让 QevosAgent 内部能承载「带图形化交互 + Agent 能力」的应用(流程图/节点图编辑器、
> 看板、思维导图、状态机等),**复用现有 App 系统,不另起炉灶**。下文以**流程图/节点图编辑器**
> (节点 + 连线)作贯穿示例——结构通用,换成其他领域同理。
> 原则:最小 delta、少约束、缺省即退化成普通 App。
>
> **本文档面向维护者**(记设计动机与平台内部改动)。**Agent 侧的操作契约**(怎么造一个 UI App、
> `qevos` 桥 API、护栏)见 [`SKILLS/ui_app.md`](../SKILLS/ui_app.md) —— 那份自包含,是 Agent 会 `read_skill` 读到的。

---

## 0. 一句话结论

现有 App = `apps/*.md`(frontmatter + 脚本正文)→ 点卡片跑一次脚本看输出。
交互式 App **不是新系统**,而是给 App 加**第三档 runtime `web`**:点击不再 spawn 脚本,
而是打开一块能跑自定义 HTML/JS 的**面板**,面板通过一层薄桥读写**项目文件夹**、
并用**结构化事件**唤醒背后的 skill。

运行面、实时推送、文件读写、唤醒通道**都已存在**(见 §2),真正新增的只有
**一条结构化事件旁路**和**把文件端点从 run 级放大到项目 root 级**。

---

## 0.5 心智模型:别把 UI App 当成标准 Web 前后台

**最容易踩的误区**:以为 UI App = "用 QevosAgent 的 node 服务托管一套自己的 Web 前后端"。
前端对,后端错——而且后端错的这一半正是本模型的价值所在。

- **前端 = 标准 Web 前端(是)**:iframe 里的 HTML/JS/CSS,由 node 服务静态托管,走 HTTP/WS。
  原有前端技能、任意图形库(canvas / echarts / 图编辑器)全部适用,没有黑魔法。
- **后端 = 你不写(不是标准后端)**:标准后端负责的三件事被三个**你不用写**的替身顶替:

  | 标准后端负责 | 这里由谁顶替 | 你写吗 |
  |---|---|---|
  | 数据存储 / DB / ORM | **文件系统**——项目文件夹里的 Markdown 就是数据库 | ❌ 直接读写文件 |
  | CRUD 路由 / 控制器 | **一个通用文件端点**(root 相对读写)+ 事件旁路 | ❌ 复用通用端点 |
  | 智能业务逻辑(逻辑校验 / 语义生成…) | **Agent(LLM + skill/工具)** | ⚠️ 写的是 *skill*,不是 `if(action)` 服务器代码 |

**没有你自己的常驻后端进程**:没有端口、没有 server 生命周期、没有后端部署——你只是往 `apps/` 丢文件。
node 服务托管的是**通用基座**(渲染面 + 文件 API + 事件总线 + WS 推送 + Agent 桥),不是你写的那台服务器。

**更贴切的类比:插件 + LLM 协处理器,而非 Web 应用。** 像 Obsidian / VS Code 插件——
宿主给一块 webview、一套文件 API、一条事件总线,你带前端 + 声明式逻辑;唯一新增的宿主服务是"Agent"。

**为什么这个区分 = 不重复造轮子**:你省掉的正是后端那一整摊(状态层 / 业务路由 / 鉴权 / 部署,
尤其是 Agent 集成)。价值不在"它也能当 Web 前后台",而在"它让你不用写后端"。

**走偏判据**:若发现自己在给 App **写 REST 路由 + 建数据库表 + 搞鉴权 + 起常驻服务**,
说明正把它做成独立 Web 应用、偏离了杠杆。该停下来问这块逻辑是"确定性(→写文件)"还是
"要智能(→写 skill)",而不是"再加个后端接口"。

---

## 1. App 三档(共用同一套文件格式 / 卡片网格 / CRUD / 编辑器)

| 档 | runtime | 点击行为 | 状态载体 | 现状 |
|---|---|---|---|---|
| 脚本 App | `shell`/`python`/`powershell` | 跑一次子进程,看输出 | 无 | ✅ 已有 |
| **UI App** | `web` | 开 HTML 面板,读写项目文件夹(纯前端工具即可) | 项目文件夹 | 🆕 |
| **Agent-UI App** | `web` + `skill:` | 面板 + 背后挂 skill,结构化事件唤醒 Agent | 项目文件夹 | 🆕 |

后两档复用同一个 `apps/*.md`,只是 `runtime: web`、正文放 HTML(或 `entry` 指向入口文件)。

### 心智模型:App = 工具(编辑器),项目文件夹 = 文档

- **App(`apps/flowchart.md`)** = 可复用工具,住 `APPS_DIR`。像"流程图编辑器"这个程序。
- **项目文件夹(`my-flow/`)** = 一份文档。同一个 App 可打开多个项目文件夹。

如同 Word vs .docx。用已有的「打开文件夹」页签([index.html:1501](../dashboard/public/index.html) `promptAddDirTab`)
选一个项目 root,再用某 UI App 打开它;或 App frontmatter 给默认 `root`。

---

## 2. 已经现成的(最小化的前提)

| 需要的能力 | 现状 | 位置 |
|---|---|---|
| 跑自定义 JS/CSS 的运行级面板 | `web_show(content_type:html)` 已把完整 HTML 塞进 sandbox iframe(`allow-scripts allow-same-origin allow-forms` + `srcdoc`) | [view.html:629-640](../dashboard/public/view.html) |
| 实时推送 Agent→UI | WebSocket | [view.html:449](../dashboard/public/view.html) |
| UI→Agent 唤醒 | `POST /api/inject` 写 cmd 文件,Agent 读取被唤醒 | [server.js:1717](../dashboard/server.js) |
| 面板读写文件(确定性编辑不过 LLM) | `GET/POST /api/run-file/:runId/*` | [server.js:2353](../dashboard/server.js) |
| App 文件格式 / CRUD / 网格 / 编辑器 / runtime 分发 | 全套 | [server.js:1226](../dashboard/server.js) `parseAppFile` / [1267](../dashboard/server.js) `runAppScript` / [index.html:3877](../dashboard/public/index.html) `renderAppsGrid` |

sandbox iframe 带 `allow-same-origin`,内部 JS 可直接 `fetch('/api/...')`。

---

## 3. 磁盘约定:项目 = 持久文件夹,内部相对路径寻址

不要用「带绝对路径的文件名」当句柄。用**文件夹 root**,内部一律 **root 相对路径**。
root 走 **cwd 轴(持久)**,不走 run_dir(临时)。`display_id`(哪个面板)与
`project_root`(编辑哪个文件夹)是两条正交轴,一个面板编辑一个 root。

```
my-flow/                    ← project root(持久,cwd 轴)
├─ qevos.project.json       ← marker:type / 所属 app / 入口 / 版本
├─ flow.md                  ← 语义真相(节点 + 连线,Agent 写)
├─ subflows/…               ← 多文件工程其余部分(如子流程)
└─ .qevos/
   └─ view.json             ← 视图状态:节点坐标 / 缩放(面板直写)
```

两个红利:

1. **marker 让项目可"打开"且自描述**:指向文件夹 → 读 `qevos.project.json` → 知道拉起哪个 App。
2. **`.qevos/view.json` 免费化解写入归属**:几何/视图(面板高频直写、确定性、不过 LLM)与
   语义 `flow.md`(Agent 写)**分文件**。拖节点只脏 `view.json`,不碰 MD → **无需锁、无需 patch 协议**。

`qevos.project.json` 建议字段(全部可选,缺省退化):

```json
{ "type": "flowchart", "app": "flowchart", "entry": "panel.html", "version": 1 }
```

---

## 4. 最小 delta 清单(逐项:改哪、复用什么、约几行)

### D1. runtime 增加 `web` 分支
- `APP_RUNTIMES` 加 `'web'`([server.js:1224](../dashboard/server.js))。
- `POST /api/app/:id/run`([server.js:2142](../dashboard/server.js)):当 `meta.runtime === 'web'` 时
  **不**调 `runAppScript`,改为返回面板打开信息(display_id / project_root / entry),
  由前端打开面板。约 15 行。
- 编辑器 runtime `<select>`([index.html:3992](../dashboard/public/index.html))多一项 `web`;
  `parseAppFile` 已支持 `key in meta` 扩展,认 `skill` / `entry` / `root` 只需把它们加进
  `meta` 缺省表([server.js:1228](../dashboard/server.js))。约 10 行。

### D2. 面板托管为中心页签(与终端/Apps 平级)
- index.html 加一个 `center-pane` + 页签,内放 `<iframe src="/view/...">`,
  **照终端 iframe 那套抄**([index.html:3777](../dashboard/public/index.html))。view.html 已把渲染 / WS / tab 做好,复用。
- Electron 下也可继续走 `web_show → /api/open-view → WebContentsView`([server.js:2635](../dashboard/server.js) /
  [main.js:170](../desktop/main.js));页签托管是浏览器模式与"平级 UX"所需。纯前端拼装,不动后端。约 30 行。

### D3. 文件端点从 run 级放大到 root 级
- 现 `/api/run-file/:runId/*` 只认 run_dir。新增(或泛化)一个接受 `{root, relpath}` 的项目文件端点,
  **限制在 root 内**(path-traversal 防护)。约 20 行。
- **v0 想更省**:先令 `project_root = 固定工作目录 / cwd`,`marker` 与 `.qevos/` 都是普通文件、
  无需后端支持;等有"多项目切换"再把 root 参数化。

### D4. 结构化事件旁路(唯一必须的新原语)
- `POST /api/panel-event`:追加 `{ts,event,data,display_id}` 到 `run_dir/panel_events.jsonl`
  (独立 append 端点;`/api/run-file` 是覆盖写,故单列)。约 15 行。
- Agent 侧薄工具 `panel_poll(display_id?)` 读这个 jsonl(或复用现有 file-read)。约 20 行。
- **作用**:事件走旁路、保持结构化、**不污染聊天上下文**(区别于 `/api/inject` 把消息当用户文本)。

### D5. `qevos` 桥(可选薄糖,不是框架)
注入面板 ~30 行,省掉每个 App 重写 fetch:

```js
qevos.emit(event, data)      // → POST /api/panel-event
qevos.readFile(relpath)      // → GET  项目文件端点
qevos.writeFile(relpath, s)  // → POST 项目文件端点
qevos.onPush(cb)             // ← web_show / WS 推送
```

故意做成**可选薄工具**而非必须遵守的协议——App 想直接 fetch 也行。

---

## 5. 消息协议:松散 JSON,不钉 schema

事件就是自由 `{ event, data }`。对面是 LLM,能理解松散结构;**把协议钉死才是过度约束**。

| 方向 | 载体 | 示例 |
|---|---|---|
| 面板 → Agent | `panel_events.jsonl` | `{"event":"review_flow","data":{"focus":"approval"}}` |
| Agent → 面板 | `web_show` 回推 / `qevos.onPush` | 重渲染、高亮问题节点/路径 |
| 用户自然语言 → Agent | 现有 `/api/inject` | "把审批步骤拆成两步" |

---

## 6. 编辑分级路由(决定卡不卡、贵不贵)

1. **客户端确定性**(拖坐标、连线、删除)→ 面板 `qevos.writeFile('.qevos/view.json' / 'flow.md')`
   → **零 LLM、低延迟**。
2. **服务端确定性重计算**(大图布局、批量校验、高清导出)→ 后端原生工具,**不过 LLM**(未来 `qevos.invoke`,现用脚本 App 兜底)。
3. **需要智能**(逻辑校验、语义生成)→ `qevos.emit(...)` → 写事件 → Agent `panel_poll` → skill 处理 → 改 MD。
4. **自然语言** → 现有 inject 通道。

> 关键:确定性重计算不是"智能",别塞给 LLM——它是被调用的工具。见 [SKILLS/ui_app.md](../SKILLS/ui_app.md) §4。

无此分级,每拖一根线都过一次 Opus,又慢又烧钱。

---

## 7. 端到端时序(流程图样例)

1. 点 `flowchart` App 卡 → 选/带项目 root(`my-flow/`)→ D1 返回面板信息 → D2 打开面板页签。
2. 面板加载 `panel.html`,`qevos.readFile('flow.md')` 渲染成图(节点 + 连线)。
3. 用户拖节点/连线 → `qevos.writeFile('.qevos/view.json' 或 'flow.md')`(确定性、实时)。
4. 用户点「检查逻辑」→ `qevos.emit('review_flow',{...})` → `panel_events.jsonl` → Agent 被唤醒、
   `panel_poll` 读到、用 skill 分析、改 `flow.md`。
5. Agent 改完 → `web_show` 回推 / `qevos.onPush` 重渲染,高亮问题节点/路径。

全程新增代码:**一个 append 端点 + 一个 poll 工具 + 一个中心页签 + 一个 root 文件端点 + 30 行桥**;其余全是复用。

---

## 8. 明确不做(避免过度约束)

- ❌ 不建独立于 App 的第二套"UI App 管理系统"——复用 `apps/` 这套。
- ❌ frontmatter 不搞复杂 schema,新字段(`skill`/`entry`/`root`)全可选,缺省退化成普通 UI App。
- ❌ 不建写入归属锁 / patch 协议——靠 §3「几何与语义分文件」天然免冲突。
- ❌ 不建 project 级存储子系统——项目就是磁盘上一个文件夹,天然跨会话持久。
- ❌ 不强制 Agent——UI App 可纯前端(第二档),挂 skill 只是第三档可选升级。
- ❌ 不发明新实时协议——WS 推 + Agent 改完 `web_show` 回推即闭环。

---

## 9. 分期建议

- **v0(打通闭环)**:D1 + D2 + D4;`project_root` 先固定为某工作目录;桥先内联少量 fetch。
  产出:一个能开面板、能双向结构化通信的最小 UI App。
- **v1(工程化)**:D3(root 参数化多项目)+ D5(完整 `qevos` 桥)+ marker/`.qevos` 约定;
  以 flowchart App 为第一个完整样例,之后 UI App 照此模板复制。

> 纪律:凡改到 Core 的地方,优先做成**缺省 no-op 的扩展点**(对齐
> [doc/pro-extension-points.md](pro-extension-points.md)),让 PRO 叠加不冲突。
