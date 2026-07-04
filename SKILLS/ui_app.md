# SKILL: 构建内置 UI App(交互式图形应用)

适用领域：在 QevosAgent 内部构建**带图形界面 + Agent 能力**的应用——例如流程图/节点图编辑器、
看板、思维导图、数据表、状态机设计器等,凡是"结构化文件 + 图形化交互 + 需要 Agent 智能"的场景。
当用户要求"做一个能可视化交互的内置程序/App/面板",而不只是跑一段脚本时，读本 skill。

> 下文以**流程图/节点图编辑器**(节点 + 连线)作为贯穿示例——它结构通用,把"节点/连线"换成你领域里的
> 对应物即可套用。

> 状态：本 skill 描述**目标契约**。若下列端点/桥尚未落地，以 `doc/interactive-app.md` 的分期为准，
> 先确认 `runtime:web` 分支与文件/事件端点已实现再据此造 App。

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

| 档 | runtime | 点击行为 |
|---|---|---|
| 脚本 App | `shell`/`python`/`powershell` | 跑一次子进程看输出(旧能力，不在本 skill 范围) |
| **UI App** | `web` | 开 HTML 面板，读写项目文件夹，纯前端工具即可 |
| **Agent-UI App** | `web` + `skill:` | 面板 + 背后领域 skill，结构化事件唤醒 Agent |

**心智模型**：App(`apps/xxx.md`) = 可复用工具(编辑器)；项目文件夹 = 一份文档。一个 App 开多个项目文件夹。

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

---

## 2. 面板侧 API：`qevos` 桥(写进你生成的 HTML 里)

面板 HTML 里用下面这套与宿主/Agent 通信。**这是你要嵌进去的客户端接口，照抄**：

```js
// 读写项目文件夹（相对 project root；确定性编辑走这里，零 LLM、实时）
const md = await qevos.readFile('flow.md');
await qevos.writeFile('.qevos/view.json', JSON.stringify(state));

// 发结构化事件唤醒 Agent（需要"智能"的操作走这里）
qevos.emit('review_flow', { focus: 'approval' });

// 接收 Agent 回推（Agent 改完文件后让面板重渲染 / 高亮等）
qevos.onPush(msg => { /* 重新读文件并重绘 */ });
```

若桥未加载，等价的原始端点(退化用)：
`GET/POST` 项目文件端点(root 相对) · `POST /api/panel-event`(追加事件) · WS 推送(`qevos.onPush` 底层)。
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

---

## 4. 编辑分级路由(决定卡不卡、贵不贵)

| 操作类型 | 走哪 | 例 |
|---|---|---|
| 客户端确定性 | 面板 `qevos.writeFile` 直写 | 拖坐标、连线、删除、轻计算 |
| **服务端确定性重计算** | 后端原生工具，**不过 LLM**(未来 `qevos.invoke`；现阶段用脚本 App 兜底) | 大图自动布局、批量校验、渲染高清导出、跑外部 CLI |
| 需要智能 | `qevos.emit` → Agent + 领域 skill → 改 MD | "这个流程有没有逻辑漏洞"、根据描述生成节点、语义优化建议 |
| 自然语言 | 现有 inject 通道 | "把审批步骤拆成两步" |

**两条铁律**：
1. 不要让每次拖拽都过一次 LLM——客户端确定性编辑必须本地直写文件。
2. **确定性重计算(布局/校验/导出)不是"智能"**,别塞给 Agent/LLM——它该是被调用的**工具**,不是 LLM 推理。

---

## 5. Agent 侧运行时(处理事件)

- 事件从 `qevos.emit` 落到 `panel_events.jsonl`；用 `panel_poll` 工具读取(工具自带说明)。
- Agent-UI 档：App 的 `skill:` 字段指明领域 skill(如 `flowchart`)——`read_skill` 载入它来跑领域逻辑
  (解析结构 / 校验 / 生成节点)，改 `flow.md`。
- 改完后让面板刷新：`web_show` 回推新内容 / 触发 `qevos.onPush`。

---

## 端到端配方(流程图样例)

1. 造 App：写 `apps/flowchart.md`(`runtime: web`, `skill: flowchart`, 正文/entry 是图编辑器 HTML)。
2. 面板加载后 `qevos.readFile('flow.md')` → 渲染成图(节点 + 连线)。
3. 用户拖节点/连线 → `qevos.writeFile('.qevos/view.json' 或 'flow.md')`(确定性、实时)。
4. 用户点「检查逻辑」→ `qevos.emit('review_flow', …)` → Agent 被唤醒、`panel_poll` 读到、
   载入 `flowchart` skill 分析、改 `flow.md`。
5. Agent → `web_show` 回推 / `qevos.onPush` 重渲染，高亮有问题的节点/路径。

新增代码几乎为零：**前端 HTML + 文件约定 + 领域 skill**，其余复用平台基座。

---

## 检查清单(造 App 前自检)

- [ ] 我在写**前端 + 文件读写 + skill**，而不是一个后端 server？
- [ ] 确定性交互是否**本地直写文件**、没过 LLM？
- [ ] 几何/视图与语义是否**分文件**(`.qevos/view.json` vs `flow.md`)？
- [ ] 结构化事件走 `qevos.emit`/`panel-event`，**没塞进 `/api/inject`**？
- [ ] 文件路径都是 **root 相对**、没有绝对路径句柄？
- [ ] 要纯本地：库是否 vendor 进项目、**没拉 CDN**？

> 设计动机与平台内部改动见 `doc/interactive-app.md`（维护者向）。本 skill 是 Agent 侧操作契约，自包含。
