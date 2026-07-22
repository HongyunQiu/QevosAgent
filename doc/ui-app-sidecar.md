# 受管 Sidecar —— 基本概念（开发者向）

> 状态：✅ 已实现（2026-07-20，commit 8a5e06c）。
> 本文讲**概念与心智模型**，帮助开发者理解"它是什么、为什么长这样、什么时候用"。
> 平台内部实现细节见 [interactive-app.md §7.7](interactive-app.md)；
> App 作者的操作契约（worker skeleton、规则清单）见 [SKILLS/ui_app.md §4.5](../SKILLS/ui_app.md)。

---

## 1. 一句话定义

**Sidecar = 挂在 UI App 旁边、由平台全权管理生命周期的常驻 python 进程**，
用来持有那些"必须跨调用存活"的东西——DLL/SDK 句柄、硬件连接、重初始化的计算内核。
面板通过 `qevos.call(method, params)` 调它，像调一个远程函数库。

名字借自 Kubernetes 的 sidecar 容器：主体（面板）旁边跟着一个辅助进程，
同生共死（近似），主体不用关心它怎么被拉起和回收。

---

## 2. 它解决什么问题（为什么必须存在）

UI App 的既有模式是"面板 + 伴生脚本 App"：面板 `POST /api/app/<id>/run` 传参数，
平台 spawn 一个子进程跑完就退。这对**一次性计算**（导出、校验、重网格化）足够了。

但它在原理上解决不了一类需求：**句柄要活着**。

典型例子 qhyccd.dll 相机：`OpenQHYCCD` 返回的句柄背后是 SDK 在**该进程内存里**维护的
USB 会话、固件状态、制冷控制线程。进程退出，句柄即灭，无法序列化到磁盘再复活。
如果每次调用都 open→操作→close，不仅慢，还会重置制冷斜坡、打断曝光序列。
CAD 内核（初始化几秒、模型状态在内存）同理。

**结论：持久化运行的库 ⇒ 持久化运行的进程。** 这不是语言或框架的局限，是操作系统的边界。
换 C++、换 node addon 都绕不开——所以问题只剩一个：**这个常驻进程的生命周期归谁管？**

## 3. 为什么是"受管"，而不是让 App 自带服务端

三种形态的对比（这是本设计最重要的一次取舍）：

| 形态 | 结局 |
|---|---|
| App 自带 HTTP server（标准前后端） | 占端口、端口冲突、面板关了没人杀（孤儿进程锁着相机）、本机任意页面可打该端口（无鉴权）、每个作者重写一遍 server 骨架 |
| node 服务进程内加载 DLL（native addon/ffi） | 相机/CAD SDK 是崩溃大户，一次 segfault 带走整个 dashboard 和所有面板 |
| **受管 sidecar（采用）** | 独立进程（崩溃隔离），无端口（stdio），平台负责启动/回收/重启（无孤儿），作者只写 handler 函数 |

受管 sidecar 本质上就是形态一的"服务端"，但**端口、生命周期、鉴权、传输全部由平台代管**。
[interactive-app.md §0.5](interactive-app.md) 的承诺"你不写后端"由此保持成立，只是精确化为：
**你不写 server 骨架，只写一组无状态入口的 handler 函数**（状态活在进程里，但你不管理进程）。

---

## 4. 全景图

```
浏览器沙箱（面板，天然无 OS 能力）
   │
   │ ① 控制通道（小件）: qevos.call('start_exposure', {ms:5000})
   ▼
node 服务（不碰 DLL，只做转发 + 看门）
   │  POST /api/app/:id/call
   │  stdin  一行 {id, method, params, root}      ┐
   │  stdout 一行 {id, result | error}            ├ JSON-lines RPC
   │  stdout 无 id 行 {event, data} → SSE 推面板  ┘
   ▼
worker.py（常驻 OS 进程，全 OS 能力）
   ├─ ctypes.CDLL("qhyccd.dll")  ← 句柄活在这里，open 一次
   │
   │ ② 数据通道（大件）: 帧/模型直接落盘 base 目录
   ▼
app-data/<id>/ 或项目 root ── file-changed(SSE) ──► 面板 readBinary()
```

两条通道的分工是硬规则：**RPC 只走小 JSON；帧、模型等大件一律走文件**
（worker 落盘 → 既有 `file-changed` 推送 → 面板 `readBinary`）。
超大 stdout 行会被平台视为违规、直接掐掉进程。

## 5. 三个角色各自负责什么

| 角色 | 负责 | 不负责 |
|---|---|---|
| **面板**（前端 JS） | 发起 `qevos.call`；订阅 `onPush` 收 `sidecar-event`/`sidecar-exit`；收事件后从文件通道取大件 | worker 何时启动/是否活着（首次 call 自动拉起，崩溃有事件通知） |
| **平台**（server.js） | 懒启动、linger 空闲回收、崩溃清理+通知+下次调用重启、服务退出统一杀；请求↔响应按 id 关联；超时；路径穿越防护 | 业务逻辑（一行都不懂，只转发 JSON） |
| **worker**（python） | 一次性初始化（load DLL / open 设备）；dispatch handler；长操作丢线程、先回 ack、完成推事件 | 进程自身的启停、重启、多实例去重（平台保证同 key 只有一个） |

## 6. 生命周期（平台视角）

```
        首次 qevos.call()
   （无）────────────────► 运行中 ◄─┐
                             │      │ 每次 call 重置 linger 计时
     linger 到期 且 无面板打开│      │ 崩溃 → 拒掉 pending
                             ▼      │       + 推 sidecar-exit(code, stderr尾部)
                           回收 ────┘ 下次 call 自动重启（状态清零！）
```

- **懒启动**：声明了 `sidecar:` 不等于进程存在；第一次 `call` 才 spawn。
- **linger（缺省 300s，可配）**：空闲到期时，若**还有面板开着**（SSE 判活）则续命——
  防止"用户切个页签，制冷了半天的相机被重置"。无面板才回收。
- **崩溃即重启，但状态即清零**：worker 重启后 DLL 要重新 load、设备要重新 open。
  面板收到 `sidecar-exit` 后应重新走初始化流程（或下次 call 时 worker 自己在启动段重建）。
- **同 key 单实例**：`sidecar_scope: app`（缺省）= 全 App 一个进程，天然解决硬件独占
  （两个面板同时开也不会重复 OpenCamera）；`scope: root` = 每个项目文件夹一个进程（CAD 内核类）。

## 7. 什么时候用（判据决策表）

| 场景 | 走哪 |
|---|---|
| 一次性计算，冷启动可接受（导出/校验/重网格化） | 伴生脚本 App（`/run` + `QEVOS_RUN_ARGS`） |
| **句柄/连接/内核状态需要跨调用存活**（相机、CAD 内核、重 import） | **sidecar** |
| 可移植纯算法、帧级低延迟（几何运算、格式解析） | 编 WASM 直接进面板，连 RPC 都不用 |
| 需要智能（语义生成/校验） | 🔒 Agent 档（待子 Agent，App 不得依赖） |

**一句话判据：句柄需要跨调用存活 → sidecar；否则脚本 App。**
拿不准就先用脚本 App——从脚本 App 升级到 sidecar 只是把函数搬进 worker.py + 面板改调 `qevos.call`，
方向反过来（拆 sidecar）同样便宜，不存在锁定。

**明确不做**（踩线即架构走偏）：
- ❌ 面板任意 exec / 桥暴露"跑任意命令"——任何面板 XSS 都会升级成本机 RCE；
- ❌ node 进程内 native addon 加载 DLL——崩溃传染；
- ❌ App 自带 HTTP/WS server——见 §3 表第一行；
- ❌ 用 RPC 返回帧/大 buffer——大件走文件通道。

---

## 8. 与代码的映射（维护者速查）

| 概念 | 代码位置 |
|---|---|
| 声明 | `apps/<id>.md` frontmatter：`sidecar` / `sidecar_scope` / `sidecar_linger`（[server.js](../dashboard/server.js) `parseAppFile` 缺省表） |
| worker 文件 | `apps-dist/<id>/worker.py`（限定在该目录内，穿越被拒） |
| 进程注册表 | server.js `sidecars` Map + `sidecarEnsure/sidecarCall/sidecarStop/sidecarArmLinger` |
| HTTP 入口 | `POST /api/app/:id/call`（`$status`/`$stop` 为平台保留方法，不进 worker） |
| 面板 SDK | [qevos-bridge.js](../dashboard/public/qevos-bridge.js) `qevos.call(method, params, {timeout}?)`（缺省 30s，上限 600s） |
| 事件下发 | worker 无 id stdout 行 → `pushToPanel` → 面板 `onPush` 收 `{type:'sidecar-event',…}` |
| 环境 | `QEVOS_SIDECAR=1`、`QEVOS_APP_ID`、`QEVOS_ROOT`；cwd=`apps-dist/<id>/`；回环 NO_PROXY 已注入（[ui-app-proxy.md](ui-app-proxy.md)） |

## 9. 调试与常见坑

- `qevos.call('$status')` 查 `{running, pid, uptimeMs}`；`qevos.call('$stop')` 杀进程——
  **改完 worker 代码必须 `$stop` 一次**，否则跑的还是旧进程（懒启动只在无进程时 spawn）。
- **stdout 是协议通道**：worker 里随手 `print()` 调试会被当协议噪音忽略（幸运）或撞坏 JSON 行（不幸）。
  日志一律 `sys.stderr`——平台保留 stderr 尾部，崩溃时随 `sidecar-exit` 带给面板。
- **长操作必须丢线程**：主循环是单线程逐行处理，handler 里同步等曝光 30 秒，
  期间所有 call（包括状态查询）都排队到超时。模式：起线程 → 立即回 `{started:true}` → 完成后 `emit` 事件。
- Windows 下注意 `flush=True`（skeleton 已带；平台侧也设了 `PYTHONUNBUFFERED=1` 兜底）。
- 崩溃排查顺序：面板收到的 `sidecar-exit.stderr` 尾部 → dashboard 控制台的
  `▶ Sidecar started / ✖ Sidecar exited` 系统消息 → 手动在 `apps-dist/<id>/` 下直接跑
  `python worker.py` 敲一行 `{"id":"t","method":"ping","params":{}}` 复现。
