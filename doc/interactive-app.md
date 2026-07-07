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
| **UI App** | `web` | 开 HTML 面板,读写项目文件夹(纯前端工具即可) | 项目文件夹 | ✅ v0 当前形态 |
| **Agent-UI App** | `web` + `skill:` | 面板 + 背后挂 skill,(未来)结构化事件召唤 Agent | 项目文件夹 | 🔒 预留,未接入(待子 Agent,见 §5.1) |

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

### D3. 文件端点 root 参数化(多项目 root)—— ✅ 已实现(v1 ②)
- `resolveAppBase(id, root)`([server.js](../dashboard/server.js)):有绝对 `root` → 那个文件夹(磁盘任意处);否则 `app-data/<id>/`。
- `/api/app-file`、`/api/app-files`、`/api/app-stream`、`/api/panel-event` 全部接受 `root`(query 或 body),**限制在 root 内**(path-traversal 防护);SSE/push 按 base 目录键,同项目多实例同步。
- 面板:`/api/app/:id/panel?root=<abs>` → 注入 `window.__QEVOS__.root`;桥把 root 透传到每次调用,暴露 `qevos.root`。
- **打开项目流**:`GET /api/app-project?root=<abs>` 读文件夹的 `qevos.project.json` marker → 得到 `app` → 前端 `openProject()`/`openAppPanel(app, root)`(同 app 不同 root = 不同页签)。
- `panel_poll(app, root?)` 也认 root。**"编辑器 vs 文档"就此兑现:一个 App 打开磁盘任意位置的多份文档。**

### D4. 结构化事件旁路(唯一必须的新原语)
- `POST /api/panel-event`:追加 `{ts,event,data,display_id}` 到 `run_dir/panel_events.jsonl`
  (独立 append 端点;`/api/run-file` 是覆盖写,故单列)。约 15 行。
- Agent 侧薄工具 `panel_poll(display_id?)` 读这个 jsonl(或复用现有 file-read)。约 20 行。
- **作用**:事件走旁路、保持结构化、**不污染聊天上下文**(区别于 `/api/inject` 把消息当用户文本)。

### D5. `qevos` 桥 —— ✅ 已实现(v1,模块化 SDK)
不再是内联字符串:抽成**服务的模块** [`dashboard/public/qevos-bridge.js`](../dashboard/public/qevos-bridge.js),
面板由 server 注入 `<script>window.__QEVOS__={app}</script><script src="/qevos-bridge.js">`,模块自配置。

- **文件 API**:`readFile/writeFile/readJSON/writeJSON/exists/remove/list`(root-scoped 到 `app-data/<id>/`)。
- **`emit`**:结构化事件(惰性日志),不变。
- **`onPush(cb)`**:server→面板 **SSE** 通道(`GET /api/app-stream/:id`),返回退订函数。
  v1 生产者:文件经 API 写/删时推 `{type:'file-changed',path}`(多实例同步/外部编辑刷新);Agent 主动回推仍 v2。
- 新增后端:`GET /api/app-stream/:id`(SSE)、`GET /api/app-files/:id`(列目录)、`DELETE /api/app-file/:id/*`;
  `POST /api/app-file` 写成功后 `pushToPanel()` 通知开着的面板。

仍是**可选**:App 想直接 fetch 也行,但桥已默认注入、直接用即可。

---

## 5. 消息协议:松散 JSON,不钉 schema

事件就是自由 `{ event, data }`。对面是 LLM,能理解松散结构;**把协议钉死才是过度约束**。

| 方向 | 载体 | 状态 | 示例 |
|---|---|---|---|
| 面板 → Agent | `panel_events.jsonl`(惰性日志) | 🔒 写得进,近期无自动消费方 | `{"event":"review_flow","data":{"focus":"approval"}}` |
| Agent → 面板 | `web_show` 回推 / `qevos.onPush` | 🔒 `onPush` 当前为桩 | 重渲染、高亮问题节点/路径 |
| 用户自然语言 → Agent | 现有 `/api/inject` | ◻ 仅用户显式 | "把审批步骤拆成两步" |

### 5.1 Agent 耦合:近期"保留能力、不接入"

**决策**:UI App 近期做成**纯独立应用**,不接 Agent。原因:尚无子 Agent 隔离时,面板召唤 Agent
会和用户正在跑的**主 Agent 抢运行时**(争 run 槽 / 混上下文 / 注入错对象)。故:

- `emit` / `panel_events.jsonl` / `panel_poll` 作为**惰性预留缝**保留(能写能被动读),但**不建自动消费 / 召唤路径**。
- `panel_poll` 仅供主 Agent 在**用户显式要求**时被动读取,**不主动轮询**(主动 poll = 扰动主 Agent)。
- 接入的**前置条件是子 Agent**(独立 run / 上下文 / 生命周期);完成后再在这条已存在的缝上接"召唤"路径,零返工。

**由此,测出的"App 以为要 Agent 却没 Agent 而失败"从构造上消除**:App 不得依赖 Agent,基线功能必须零 Agent 可完成。

---

## 6. 编辑分级路由(决定卡不卡、贵不贵)

1. **客户端确定性**(拖坐标、连线、删除)→ 面板 `qevos.writeFile('.qevos/view.json' / 'flow.md')`
   → **零 LLM、低延迟**。【✅ 当前】
2. **服务端确定性重计算**(大图布局、批量校验、高清导出)→ 后端原生工具,**不过 LLM**(未来 `qevos.invoke`,现用脚本 App 兜底)。【◻ 部分】
3. **需要智能**(逻辑校验、语义生成)→ 召唤一次性 Agent run 处理 → 改 MD。【🔒 预留,未接入,待子 Agent — 见 §5.1】
4. **自然语言** → 现有 inject 通道。【◻ 仅用户显式对主 Agent】

> 关键:确定性重计算不是"智能",别塞给 LLM——它是被调用的工具。见 [SKILLS/ui_app.md](../SKILLS/ui_app.md) §4。
> **近期只用第①档(+脚本 App 兜②)完成一切基线功能;③④暂不接入,App 不得依赖。**

无此分级,每拖一根线都过一次 Opus,又慢又烧钱。

---

## 7. 端到端时序(流程图样例)

1. 点 `flowchart` App 卡 → 选/带项目 root(`my-flow/`)→ D1 返回面板信息 → D2 打开面板页签。
2. 面板加载 `panel.html`,`qevos.readFile('flow.md')` 渲染成图(节点 + 连线)。
3. 用户拖节点/连线 → `qevos.writeFile('.qevos/view.json' 或 'flow.md')`(确定性、实时)。
4. 结构化/重计算类 → 面板内直算或脚本 App 兜底(不过 Agent)。
5. 🔒(未接入,待子 Agent)"用 AI 检查/生成":未来经召唤一次性 run 处理并回推面板;当前不做、App 不放依赖 Agent 的按钮。

全程新增代码:**一个 append 端点 + 一个 poll 工具 + 一个中心页签 + 一个 root 文件端点 + 30 行桥**;其余全是复用。

---

## 7.5 构建型 UI App(需要 npm 的前端工程)

内联 HTML 只够轻量档;当 App 本身是**一整个前端工程**(React/Vue/Vite + node_modules + 构建步骤)时:

### 核心区分:构建期 vs 运行期
- 90% 的"需要 npm"是**构建期**的:`npm run build` 把源码打包成 `dist/`(纯静态,**不含 node_modules**)。
- **运行期是纯静态、零 npm**。所以问题的真身是"如何存放/服务构建产物 `dist/`",不是"运行时跑 npm"。

### 模型扩展:App 源码工程 ≠ App 运行产物
> `runtime: web` 的面板内容,可以是**内联 HTML**(轻量),也可以指向一个**构建好的静态目录 `dist/`**(工程)。
> 平台只服务静态产物,永不碰源码工程。

- **源码工程**(package.json / node_modules / vite.config / src)住在开发工作区(如 `app-src/<id>/`,**gitignore、不打包、不入库**)。
- 注册进 App 的是构建产物,放 **`apps-dist/<id>/`**(gitignore)。
- **已实现(v1)**:`APPS_DIST_DIR`([server.js](../dashboard/server.js));`/api/app/:id/panel` 若发现 `apps-dist/<id>/index.html` 则服务它(否则回退内联正文),并注入 `<base href="/api/app/<id>/">` + `qevos` 桥;`GET /api/app/:id/*` 静态服务 `apps-dist/<id>/` 资源(MIME + 路径穿越防护)。
- **打包 base 必须相对**(`vite build` 配 `base: './'`),资源 `./assets/…` 经 `<base>` 解析到 `/api/app/:id/`;绝对 `/assets` 仍 404。

### 构建在哪儿跑 / Agent 能否自己装
- **有工具链的机器(开发机 / 授权阶段)**:Agent 有 shell,**自己 `npm install && npm run build` 就是预期路径**。装的包在源码工作区、构建完即弃、**不 ship**。
- **打包后的用户机**:默认**没有** node/npm(Electron 内含 node 但不暴露为 `node`/`npm` 命令;当前只 bundle 了 Python)。所以默认策略是 **App 预构建、只 ship `dist/`**,用户机零 npm。
- 若要让 Agent **在任何机器**上都能构建,需给它一个到处都在的 node 工具链,两条实现路(见下)。

### 让 node 工具链到处可用(可选,若要一等支持)
1. **`ELECTRON_RUN_AS_NODE`**:Electron 二进制本就内含完整 node,置 `ELECTRON_RUN_AS_NODE=1` 后 `process.execPath` 即纯 node;再 bundle `npm-cli.js` 即得 npm。零额外体积,但 node 版本被 Electron 锁死、**原生模块(node-gyp)编译易碎**——只适合纯静态前端,当轻量兜底。
2. **bundle 独立 node(推荐,和现有 Python 对称)**:新增 `setup_node.js` 拉官方 node 到 `vendor/node`(镜像 `setup_python.js`)→ electron-builder `extraResources`/asarUnpack(二进制**必须在 asar 外**)→ [main.js `EMBEDDED_PYTHON`](../desktop/main.js) 同构解析出 `EMBEDDED_NODE`,并把 `vendor/node` **prepend 进 spawn 子进程的 PATH**,Agent `npm` 即到处可用。代价:每平台 +40–70MB。省体积可改**首次用时懒下载**到 `userData/vendor/node`(构建期在线、运行期本地)。

### 纯本地约束的调和
`npm install` 拉 registry = **构建期在线**(在开发/授权机上),**运行期纯静态=纯本地**——与产品本身"npm+CI 构建、发布纯本地包"一致。**铁律:node/node_modules/工具链永不入库、永不 ship,只有 `dist/` 进产物。**

### 例外:运行期真需要 node 服务(SSR / 自带 server)
按设计**不支持**(UI App 无自己的后端 server,§0.5)。落到未来的**受管 sidecar**逃生梯,少数、需显式声明。绝大多数前端工程 SPA/静态化即可,无需 SSR。

### 代码管理 / git(源码独立仓库)
运行时绑 QevosAgent(面板靠 `qevos` 桥 + 宿主),但**源码可独立 git**,两者不冲突。因为 `app-src/`、
`apps-dist/`、`app-data/` 都被 QevosAgent `.gitignore` → 在其中 `git init` 是**独立仓库,无嵌套/submodule 冲突**。

- **App 源码工程 = 一个独立仓库**:放 `app-src/<id>/`(方便)或磁盘任意位置(最干净,因 QevosAgent 只需产物)。**不做 submodule**(避免与开源/PRO 主仓库耦合)。
- **产物 = 部署**:build → `apps-dist/<id>/` + 写 `apps/<id>.md`;主库只收产物,源码/`node_modules` 不入库。
- 三层 VCS:① `app-src/`→ 自有仓库;② `apps-dist/`→ 忽略、可重生、不 git;③ 数据/文档(项目 root)→ v1 ② 后位置自由,用户项目也可各自 git。

---

## 8. 明确不做(避免过度约束)

- ❌ 不建独立于 App 的第二套"UI App 管理系统"——复用 `apps/` 这套。
- ❌ frontmatter 不搞复杂 schema,新字段(`skill`/`entry`/`root`)全可选,缺省退化成普通 UI App。
- ❌ 不建写入归属锁 / patch 协议——靠 §3「几何与语义分文件」天然免冲突。
- ❌ 不建 project 级存储子系统——项目就是磁盘上一个文件夹,天然跨会话持久。
- ❌ 不强制 Agent——**近期 UI App 一律纯独立**;挂 skill 的"智能档"是**预留、未接入**(待子 Agent)。
- ❌ 近期不建"召唤 Agent / 自动消费事件"路径——避免和主 Agent 抢运行时(见 §5.1)。

---

## 7.6 自测 UI App(Agent 构建期自动化)

Agent 造完 UI App 要能自测,也能按用户要求操控其正打开的面板。

- **控制/读取 → `panel_control`(默认,✅ 已实现)**:面板是我们自己的代码(桥在里面),
  `panel_control` 经桥的 **SSE 通道**下发指令、面板执行后 POST 回结果——**Electron 与普通浏览器一致,
  无需 CDP/调试启动浏览器**。action:`click/fill/value/getText/getHtml/exists/count/waitFor/eval`。
  端点:`POST /api/panel-control`(推 `{type:'__ctl',id,…}` 并等结果)+ `POST /api/panel-control-result`(面板回传);
  桥 init 即开 SSE 以保证可达。前提:面板已打开(有 SSE 连接)。
- **面板截图 → `panel_control(app,"screenshot")`(✅ 已实现)**:桥内 **DOM→PNG**(懒加载 vendored html2canvas,
  `public/vendor/html2canvas.min.js`),返回 base64、`inject=True` 直接注入视觉。跨模式无标志。
  **是重绘非抓屏**(浏览器禁止页面像素级截自己):布局够用、精细样式可能偏差、跨域资源会 taint 失败;
  纯 `<canvas>` 应用走 `toDataURL` 捷径则像素级完美。
- **`web_interact`/CDP 降级为兜底**:只用于**外部非-UI-App 页面**,或需要**像素级留证**(Electron `capturePage`)。
  —— 这解决了"CDP 需调试浏览器,普通浏览器用户用不了"的短板。
- **断言** → **优先文件态**(读 `app-data/<id>/` 或 root)+ `panel_poll`(事件)+ `panel_control` 读 DOM 兜底。
- **方向说明**:`panel_control` 是 **Agent→App**(操控/自测),不制造运行时 App→Agent 依赖(与 §5.1 不冲突),
  同时是 v2"Agent 副驾操控面板"的传输层。
- 可选探针 `window.qevosTest`;Agent 侧配方见 [SKILLS/ui_app.md](../SKILLS/ui_app.md) §6。

> 局限:`panel_control` 需要一个**活着的面板**(有 SSE)。无头冷启动(纯 CLI 部署、无任何面板打开)
> 仍需渲染器:Electron 用 `web_interact`(WebContentsView,无标志);纯浏览器/CLI 则要求先开着面板,或退回文件态/端点级断言。

---

## 9. 分期建议

- **v0(打通闭环)**:D1 + D2 + D4;`project_root` 先固定为某工作目录;桥先内联少量 fetch。
  产出:一个能开面板、能双向结构化通信的最小 UI App。
- **v1(工程化)✅ 完成**:D5(完整 `qevos` 桥)+ 构建型 App(dist,§7.5)+ D3(root 参数化多项目 + marker「打开项目」);
  以 flowchart App 为第一个完整样例,之后 UI App 照此模板复制。**全程纯独立**,不接 Agent。
- **v2(接 Agent,待子 Agent 落地)**:在已保留的 `emit`/`panel_events`/`panel_poll` 缝上,
  接"用户显式触发 → 召唤一次性子 Agent run → 处理 → 回推面板"路径;`onPush` 实装。前置=子 Agent。

> 纪律:凡改到 Core 的地方,优先做成**缺省 no-op 的扩展点**(对齐
> [doc/pro-extension-points.md](pro-extension-points.md)),让 PRO 叠加不冲突。
