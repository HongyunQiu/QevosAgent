# SKILL: 构建内置 UI App(交互式图形应用)

适用领域：在 QevosAgent 内部构建**带图形界面 + Agent 能力**的应用——例如流程图/节点图编辑器、
看板、思维导图、数据表、状态机设计器等,凡是"结构化文件 + 图形化交互 + 需要 Agent 智能"的场景。
当用户要求"做一个能可视化交互的内置程序/App/面板",而不只是跑一段脚本时，读本 skill。

> 下文以**流程图/节点图编辑器**(节点 + 连线)作为贯穿示例——它结构通用,把"节点/连线"换成你领域里的
> 对应物即可套用。

> 状态：本 skill 描述**目标契约**。若下列端点/桥尚未落地，以 `doc/interactive-app.md` 的分期为准，
> 先确认 `runtime:web` 分支与文件/事件端点已实现再据此造 App。

> ⚠️ **近期硬约束(必读)**：UI App 目前必须做成**纯独立应用**——只用"面板内确定性"
> (读写项目文件 + 前端逻辑)完成一切基线功能。**"需要 Agent 智能"这一档暂不接入**
> (等子 Agent 落地后再接，见 §5)。**绝不要设计任何"没有 Agent 就完不成"的流程**：
> Agent 不是常驻的，硬指望它会导致"以为要 Agent 介入却没 Agent → 失败"。

---

## ⛔ 先读:别把它当标准 Web 前后台(最重要的护栏)

LLM 极易顺手 scaffold 一个 Express/Flask 后端 + 数据库 + 鉴权——**在这里是错的，禁止**。

- **前端**：是标准 Web 前端(iframe 里的 HTML/JS/CSS)。放手用 canvas / echarts / 图编辑器等任意库。
- **后端**：你**不写**。标准后端的三件事已被替代：
  - 数据存储 → **文件系统**(项目文件夹里的 Markdown 就是"数据库")。
  - CRUD 路由 → **通用文件端点**(root 相对读写) + **事件旁路**。
  - 智能业务逻辑 → **Agent(LLM + 领域 skill)**，你写的是 skill，不是 `if(action)` 服务器代码。

**没有常驻后端进程、没有端口、没有部署。** 类比 Obsidian / VS Code 插件，不是 Web 应用。

**走偏判据**：一旦你开始写 REST 路由 / 建数据库表 / 加鉴权 / 起常驻 server —— 停。
先分清这块逻辑是"确定性(→写文件)"还是"要智能(→写/调 skill)"，而不是"再加个后端接口"。

### 面板 = 视图 + 意图发射器(沙箱几乎不是限制)

面板跑在 sandbox iframe 里，看起来缺一堆权限(下载、WebSerial/WebUSB、原生对话框…)。
**别在前端硬开沙箱去要这些**——因为真正的能力全在 Agent/后端，而它对本地有**无限权限**(shell/python/任意命令)。

> 面板从不需要"直接触达本地"，它只需要**发意图**给一条消息之外的满权限执行者。

- 要落文件(导出报表/图片/CSV)→ `qevos.writeFile` / Agent 落盘，**不用** `allow-downloads`。
- 要连硬件/串口/外设 → Agent 跑原生驱动(如 `pyserial`)——原生是 WebSerial 的**超集**，更强。
- 要跑重计算(大数据处理/仿真/编译/大图布局)→ 交后端原生工具,不要塞进 LLM(见 §4)。
- **唯一该留在前端的**：低延迟、高频、纯页内的交互(拖拽渲染、帧率级绘制)。真要实时流(60Hz 波形、
  实时预览)也**别靠文件轮询**，用一条 WS 直连流管道推给面板。

**信任提醒**：正因为面板能驱动满权限后端，UI App 的 HTML 要按**"可信本地代码"**对待(信任级别≈跑一个脚本)。
将来若做 App 分享/第三方安装需重新评估；任何"同步直调后端"的通道(如未来的 `qevos.invoke`)
**只暴露该 App 声明过的工具，绝不给裸 shell**。

---

## App 三档(共用现有 App 系统：`apps/*.md`)

| 档 | runtime | 点击行为 | 状态 |
|---|---|---|---|
| 脚本 App | `shell`/`python`/`powershell` | 跑一次子进程看输出(旧能力，不在本 skill 范围) | ✅ |
| **UI App** | `web` | 开 HTML 面板，读写项目文件夹，纯前端工具即可 | ✅ 当前形态 |
| **Agent-UI App** | `web` + `skill:` | 面板 + 背后领域 skill，结构化事件唤醒 Agent | 🔒 预留，未接入(待子 Agent) |

**当前只做 UI App(纯独立)**。`skill:` 字段可以先写上作预留，但**其事件近期不会被自动处理**——
不要依赖它。心智模型：App(`apps/xxx.md`) = 可复用工具(编辑器)；项目文件夹 = 一份文档。一个 App 开多个项目文件夹。

---

## 1. 写 App 文件(`apps/<id>.md`)

```markdown
---
name: 流程图
icon: 🔀
description: 基于 Markdown 的流程图/节点图设计
runtime: web            # ← 关键：走面板而非脚本
skill: flowchart        # ← 可选：结构化事件交给哪个领域 skill 处理(Agent-UI 档)
entry: panel.html       # ← 可选：面板入口文件；省略则正文即 HTML
enabled: true
---
<!-- 正文即面板 HTML（或让 entry 指向单独文件）。可内联 <script>/<style>，
     可引用自带库；要纯本地就把库 vendor 进项目文件夹，别拉 CDN。 -->
```

- frontmatter 新字段 `skill`/`entry`/`root` 均可选，缺省即退化成"纯前端 UI App"。
- 面板运行在 sandbox iframe(`allow-scripts allow-same-origin allow-forms`)，内部 JS 可直接 `fetch('/api/...')`。

### 需要 npm / 是一整个前端工程(React/Vue/Vite)?

区分**构建期 vs 运行期**:npm 几乎只在**构建期**用来把源码打包成 `dist/`(纯静态，**不含 node_modules**)；**运行期是纯静态、零 npm**。所以:

- **你(Agent)可以自己 `npm install && npm run build`**——在有 node/npm 的机器上，这就是预期路径。装的包只在源码工作区、构建完即弃。
- **平台已支持构建产物(v1)**：把构建输出放到 **`apps-dist/<id>/`**(需有 `index.html`)。此时 `apps/<id>.md`
  只保留 frontmatter(`runtime: web` + name/icon…，正文可空/仅 fallback)；面板会**自动服务 `apps-dist/<id>/index.html`
  + `assets/…`**,并注入 `<base>` 与 `qevos` 桥。源码工程放开发工作区(如 `app-src/<id>/`)。
- **打包用相对 base**(Vite:`base: './'`),资源写成 `./assets/…`;`<base>` 会把它们解析到 `/api/app/<id>/` 下。绝对 `/assets` 仍会 404。
- **产物与源码分离**:`apps-dist/<id>/` 是产物(gitignore、可 ship);源码 / node_modules **绝不 commit、绝不进 `apps/`/`app-data/`**。
- `qevos` 桥与框架无关：打包后的代码直接调 `window.qevos.readFile(...)` 即可(桥已注入进 `index.html`)。
- 运行时真需要常驻 node 服务(SSR/自带 server)→ **不支持**(UI App 无自己的后端)，属未来 sidecar。

**代码管理 / git**（运行时绑 QevosAgent，但源码是独立仓库，两者不冲突）：
- **App 源码 = 一个独立 git 仓库**：放 `app-src/<id>/`（被 QevosAgent 忽略 → 可直接 `git init`，无嵌套/submodule 冲突），或磁盘任意位置。**不要**做成 QevosAgent 的 submodule。
- 部署 = 构建输出到 `apps-dist/<id>/` + 写 `apps/<id>.md`。QevosAgent 只收**产物**，源码/`node_modules` 不入主库。
- 三层各自的版本：① 源码(`app-src/`)→ 自己的仓库；② 产物(`apps-dist/`)→ 忽略、可重生、不 git；③ 数据/文档(项目 root)→ 可选，用户项目也能各自 git。

---

## 2. 面板侧 API：`qevos` 桥(写进你生成的 HTML 里)

`qevos` 桥由平台**自动注入**面板(外部模块 `/qevos-bridge.js`,你**不用手写**,直接调 `window.qevos.*`):

```js
// —— 文件 API（相对 project root；确定性编辑走这里，零 LLM、实时）——
const md   = await qevos.readFile('flow.md');          // string | null（utf8 文本）
await       qevos.writeFile('flow.md', md);            // {ok}
const buf  = await qevos.readBinary('model.stl');      // ArrayBuffer | null（二进制：STL/图片等，勿用 readFile 读）
await       qevos.writeBinary('shot.png', base64);     // {ok} base64 → 原始字节落盘
const view = await qevos.readJSON('.qevos/view.json');  // 解析后的对象 | null
await       qevos.writeJSON('.qevos/view.json', state); // 美化写入
const ok   = await qevos.exists('flow.md');            // boolean
await       qevos.remove('scratch.txt');               // 删除
const files= await qevos.list('.qevos');               // [{path,type,size}] 递归

// —— 结构化事件（🔒 惰性日志：写入 panel_events.jsonl，近期无自动消费方，不保证被处理；
//     可当自身遥测/状态；勿把基线功能建在"会被 Agent 处理"上）——
await qevos.emit('review_flow', { focus: 'approval' });

// —— onPush（server→面板推送，SSE；已可用）——
//   当前生产者：项目文件经 API 被写/删时推 {type:'file-changed',path}（多实例同步/外部编辑刷新）。
//   "Agent 主动回推" 仍 🔒 v2（待子 Agent）。返回取消订阅函数。
const off = qevos.onPush(msg => { if (msg.type==='file-changed') reloadFrom(msg.path); });

// —— 主题（light/dark 自动跟随 dashboard，已可用）——
qevos.theme                                   // 'dark' | 'light'（当前值）
const offT = qevos.onTheme(t => redraw(t));   // 切换时回调；返回取消订阅
```

### 面板配色：必须跟随 light/dark 主题（平台已自动铺路）

桥在每个面板里自动做了三件事，App **不用自己探测主题**：

1. 把 `data-theme`（`'light'`/`'dark'`）写在面板自己的 `<html>` 上，dashboard 切换时实时跟随；
2. 注入 `/qevos-theme.css` —— 一套随主题自动翻转的 CSS 变量（GitHub 系调色板，与 dashboard 一致）：
   `--q-bg/--q-bg2/--q-bg3/--q-bg4`（底色由深到浅）、`--q-border`、`--q-text`、`--q-muted`、
   `--q-blue/green/purple/orange/red/yellow/cyan`（强调色）、`--q-canvas`（大面积画布/3D 视口底色）、
   `--q-mono/--q-sans`（字体栈）。注入先于 App 样式，App 可覆盖；
   该表还带一条元素级默认皮肤：**滚动条**（dashboard 同款细条、随主题翻转）——面板不用自己写，
   想要别的风格就自己写 `::-webkit-scrollbar` 规则覆盖；
3. 暴露 `qevos.theme` / `qevos.onTheme(cb)` 给 CSS 管不到的场景。

**App 侧规则**：
- 样式颜色一律写 `var(--q-*)`，不要硬编码 hex —— 这样零 JS 即自动换肤；
- 半透明浮层、选中态等 `--q-*` 覆盖不到的细节，自定义变量 + `[data-theme="light"]` 覆盖；
- **canvas/WebGL 内的颜色 CSS 管不到**（three.js 场景背景、图编辑器画布网格）→ 初始化时按
  `qevos.theme` 取色（可 `getComputedStyle` 读自己的 CSS 变量），再 `qevos.onTheme(cb)` 里重绘。

**不要**用 `/api/inject` 传结构化数据——那是把消息当"用户聊天文本"，会污染上下文，只用于自然语言。

---

## 3. 项目文件夹约定(磁盘)

项目 = 一个**持久文件夹**(走 cwd 轴，非 run_dir)。内部**一律 root 相对路径**，不把绝对路径当句柄。

```
my-flow/                    ← project root
├─ qevos.project.json       ← marker：{ "type":"flowchart", "app":"flowchart", "entry":"panel.html", "version":1 }
├─ flow.md                  ← 语义真相（节点 + 连线，Agent 写）
├─ subflows/…               ← 多文件工程其余部分（如子流程）
└─ .qevos/
   └─ view.json             ← 视图状态：节点坐标/缩放（面板直写）
```

**分文件 = 免费解决写入冲突**：几何/视图放 `.qevos/view.json`(面板高频直写)，语义放 `flow.md`(Agent 写)，
两边各写各的文件 → 无需锁、无需 patch 协议。拖节点只脏 `view.json`，不碰 MD。

**多项目 root（v1 ②，已实现）**：一个 App = 编辑器，项目文件夹 = 文档,可在**磁盘任意位置**(含 QevosAgent 之外)。
- 默认(无 root)→ 读写 `app-data/<id>/`(该 App 的默认文档)。
- 打开任意文件夹:文件夹放 `qevos.project.json`(至少 `{"app":"<id>"}`)→ 前端 `openProject('<绝对路径>')`
  经 `/api/app-project` 解析出 App → 以该文件夹为 root 打开面板。
- 面板侧**透明**:`qevos.*` 全部自动带上 root(桥从 `window.__QEVOS__.root` 读),`qevos.root` 可查;
  你写 App **不用关心 root**,永远用相对路径调 `qevos.readFile/writeFile`。

---

## 4. 编辑分级路由(决定卡不卡、贵不贵)

| 操作类型 | 走哪 | 状态 | 例 |
|---|---|---|---|
| 客户端确定性 | 面板 `qevos.writeFile` 直写 | ✅ 当前 | 拖坐标、连线、删除、轻计算 |
| **服务端确定性重计算** | 后端原生工具，**不过 LLM**(未来 `qevos.invoke`；现阶段用脚本 App 兜底) | ◻ 部分(脚本 App) | 大图自动布局、批量校验、渲染高清导出、跑外部 CLI |
| 需要智能 | 召唤一次性 Agent run 处理 → 改 MD | 🔒 预留，未接入(待子 Agent) | "这个流程有没有逻辑漏洞"、根据描述生成节点 |
| 自然语言 | 现有 inject 通道 | ◻ 仅当用户显式对主 Agent 说 | "把审批步骤拆成两步" |

**铁律**：
1. 不要让每次拖拽都过一次 LLM——客户端确定性编辑必须本地直写文件。
2. **确定性重计算(布局/校验/导出)不是"智能"**,别塞给 Agent/LLM——它该是被调用的**工具**。
3. **近期只用第一档(+脚本 App 兜第二档)完成一切基线功能**。第三/四档暂不接入,**App 不得依赖它们**。

---

## 5. Agent 侧运行时 —— 🔒 预留，未接入(待子 Agent)

**当前不要接 Agent。** 原因:没有子 Agent 隔离时,面板召唤 Agent 会和用户正在跑的**主 Agent 抢
运行时**(争 run 槽、混上下文、注入错对象)。故"智能档"只保留基座、不接入,等子 Agent(独立 run /
上下文 / 生命周期)完成后再接。这是接入的**前置条件**,见 [[sub-agent]]。

已存在的**惰性预留缝**(能写能读,但近期无自动消费方):
- `qevos.emit` → `panel_events.jsonl`;`panel_poll` 工具可**被动**读取。
- `panel_poll` **只在用户显式要求主 Agent 去看面板时才用,勿主动轮询**——主动 poll = 扰动主 Agent。

**接入后(未来)**会是:用户显式触发 → 召唤一次性 Agent run(seeded 项目 + 事件)→ 读事件、改 `flow.md`
→ 回推面板 → run 结束;App 与该 run 无绑定。现在**不实现**这条路径。

---

## 端到端配方(流程图样例，当前=纯独立)

1. 造 App：写 `apps/flowchart.md`(`runtime: web`, 正文/entry 是图编辑器 HTML)。
2. 面板加载后 `qevos.readFile('flow.md')` → 渲染成图(节点 + 连线)。
3. 用户拖节点/连线 → `qevos.writeFile('.qevos/view.json' 或 'flow.md')`(确定性、实时)。
4. 结构化/重计算类操作 → 面板内直算或脚本 App 兜底(不过 Agent)。
5. (🔒 未接入)"用 AI 检查/生成"这类智能功能,等子 Agent 后再加;当前不要在 App 里放依赖 Agent 的按钮。

新增代码几乎为零：**前端 HTML + 文件约定**,其余复用平台基座。

---

## 6. 自测 UI App(构建后必做,别自己摸索——照此做)

造完/改完 UI App **必须自动验证一遍**。**不要**临时发明测试办法:用下面这套现成组合。
这是**构建期的 Agent→App 自动化**(你在开发时给自己写测试),与"App 纯独立、不驱动 Agent"**不冲突**——
方向和时机都不同,App 运行时仍不依赖你。

**控制面板 → 用 `panel_control`(默认,跨模式无标志)**，不要用 web_interact/CDP 碰自己的面板：
- 面板是我们自己的代码(桥在里面),`panel_control` 经桥的 SSE 通道下发指令,**Electron 与普通浏览器一致、无需调试启动浏览器**。
- action：`click`/`fill`/`value`/`getText`/`getHtml`/`exists`/`count`/`waitFor`/`eval`。
- **前提:面板已打开**(有 SSE 连接)。未打开会报错——先让面板开着(桌面版可用 web_interact 开 WebContentsView;浏览器模式请开发者先开着该面板)。
- `web_interact`/CDP **只留给外部非-UI-App 页面**,或需要**像素截图 / 无头渲染上下文**时(桥给不了图)。

**断言 → 优先文件态**(你的文件工具读 `app-data/<id>/` 或 root)+ `panel_poll` 读事件;DOM 用 `panel_control` 的 `getText/getHtml/eval`。

**配方**：
1. 确保面板打开且连着(桌面版 `web_interact new_tab` 指向 `…/api/app/<id>/panel`；浏览器模式用现开的面板)。
2. 驱动:`panel_control(app="<id>", action="click", selector="#save")`、`panel_control(app, "fill", selector, value)`、`panel_control(app, "eval", code="…")`。
3. **断言(按稳健度排序)**：
   | 优先级 | 通道 | 怎么做 |
   |---|---|---|
   | 1（最稳） | 文件态 | 读 `app-data/<id>/flow.md`、`.qevos/view.json`,校验内容 |
   | 2 | 事件 | `panel_poll('<id>')` 校验发出的事件 |
   | 3 | DOM | `panel_control(... action="getText"/"getHtml"/"eval")` 读 DOM |
   | 4 | 看一眼面板图 | `panel_control(app, "screenshot")` —— DOM→PNG(内建 html2canvas),跨模式无标志,直接注入视觉。**是重绘非抓屏**：布局/文字够用,精细样式可能偏差；跨域资源会失败；canvas 应用则像素级完美 |
   | 5 | 像素级留证（仅 Electron） | `web_interact screenshot`(WebContentsView `capturePage`，完美但仅 Electron) |

   **优先文件态断言**——确定性、无渲染时序、比扒 DOM 稳得多(文件即状态的红利);要"看一眼"用第 4 档,要像素级才用第 5 档。

**可选:埋语义探针**(想要比扒 DOM 更干净的状态断言时才做):在面板里暴露
`window.qevosTest = { getState(){ return … } }`,然后 `panel_control(app, "eval", code="JSON.stringify(qevosTest.getState())")` 取用。opt-in。

**方向说明**:`panel_control` 是 **Agent→App**(操控/自测),不制造运行时 App→Agent 依赖,与"App 纯独立"不冲突。它也是 v2"Agent 副驾操控面板"的传输层。

---

## 检查清单(造 App 前自检)

- [ ] **App 在零 Agent 下能开、能做完基线功能**？(近期硬约束，绝不能依赖 Agent)
- [ ] 我在写**前端 + 文件读写**，而不是一个后端 server？
- [ ] 确定性交互是否**本地直写文件**、没过 LLM？
- [ ] 几何/视图与语义是否**分文件**(`.qevos/view.json` vs `flow.md`)？
- [ ] 结构化事件走 `qevos.emit`/`panel-event`，**没塞进 `/api/inject`**？
- [ ] 文件路径都是 **root 相对**、没有绝对路径句柄？
- [ ] 配色是否走 `var(--q-*)` / `[data-theme="light"]`、canvas 类接了 `qevos.onTheme`？(§2，勿硬编码深色)
- [ ] 要纯本地：库是否 vendor 进项目 / 或走构建产物 `dist/`、**没拉 CDN**？
- [ ] 工程型 App：只注册了 `dist/`、**node_modules 没入库/没进 apps 或 app-data**？相对 base?
- [ ] **造完已用 §6 那套(`web_interact` 驱动 + 文件态断言)自测过**？

> 设计动机与平台内部改动见 `doc/interactive-app.md`（维护者向）。本 skill 是 Agent 侧操作契约，自包含。
