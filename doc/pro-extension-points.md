# PRO 扩展点（开源 Core 预留的稳定契约）

> 目的：让闭源的 **QevosAgentPro**（下游）以"只新增文件、绝不就地改 Core 文件"的方式
> 叠加特色功能，从而 `git merge upstream/main`（开源 Core → PRO）永远不冲突。

## 模型

- **上游 / upstream**：开源 `QevosAgent`，所有通用 AGENT 功能在此开发。
- **下游 / downstream**：闭源 `QevosAgentPro`，定期 `git merge upstream/main` 同步上游，
  自己只维护"特色功能"。
- **唯一纪律**：PRO 不编辑 Core 的任何文件。特色功能一律以**新文件**形式存在，
  通过下列扩展点挂入。一旦发现非改 Core 不可，说明 Core 缺扩展点——把扩展点加到
  **开源版**（默认 no-op），而不是在 PRO 里改 Core。

所有扩展点在对应文件缺席时都是 **no-op**：开源版构建的行为与"从未有过这些缝"完全一致。

---

## ① 鉴权 provider — `dashboard/auth-provider.js`

`dashboard/server.js` 启动时若发现此文件则加载。可选导出（均可 async）：

| 导出 | 签名 | 作用 |
|---|---|---|
| `handle`      | `(req, res) -> truthy?` | 鉴权**之前**先给 provider 处理自有端点（登录页、`/api/login`）。返回 truthy 表示已完全处理本请求。 |
| `checkHttp`   | `(req) -> {ok:true} \| {ok:false, status?, headers?, body?}` | HTTP 请求鉴权门。`ok:false` 时 Core 用给定 `status/headers/body` 直接响应（默认 401）。 |
| `checkUpgrade`| `(req) -> boolean` | WebSocket 升级鉴权门（默认状态流 + `/ws/term` 都走它）。返回 false → 401 拒绝。 |

注意：Core 自带的回环放行 + `DASHBOARD_ALLOW/DENY` IP 名单（`isIpAllowed`）仍是**第一道门**，
始终先执行；provider 是叠加在其之上的第二道门。

## ② 路由插件 — `dashboard/routes-pro.js`

在 Core 的 `if (req.url === ...)` 分发链**之前**调用。导出：

| 导出 | 签名 | 作用 |
|---|---|---|
| `handle` | `(req, res, ctx) -> truthy?` | 处理了就返回 truthy，Core 不再继续分发。`ctx = { json, readBody }` 便捷helper。 |

PRO 把 `/api/login`、`/api/users` 等专属路由写在这里，不碰 Core 的分发链。

## ③ 前端覆盖层 — `dashboard/public/pro/pro.js`

若该文件存在，Core 在每个 HTML 页面 `</body>` 前注入 `<script src="/pro/pro.js" defer>`。
文件本身由常规静态处理器从 `public/pro/` 下提供。`pro.js` 自行挂载 UI（登录页、账号控件等），
不需要 Core 在 `index.html` 里预留 DOM。整个 `public/pro/` 目录都是 PRO 私有，
随便加 css/js/资源。

## ④ run 生命周期钩子 — `agent/pro/hooks.py`

`agent/runtime/persistence.py` 在 run 开始/结束时调用（缺席即 no-op，异常被吞，
绝不影响 run 本身）。可选导出：

| 导出 | 签名 | 时机 |
|---|---|---|
| `on_run_start`  | `(run_dir: str, state) -> None` | run 开始落地后 |
| `on_run_finish` | `(run_dir: str, state, outcome: str, error: str\|None) -> None` | run 结束、复盘文件写完后 |

管理系统的**实时**上报客户端可订阅这里。若只要准实时，PRO 也可以完全不用此钩子，
改成独立 sidecar 轮询 `runs/` 目录——那样 Core 连 ④ 都不需要。

## ⑤ 工具自动发现 — `agent/pro/tools.py`

`agent/tools/standard.py::get_standard_tools()` 会尝试合并 PRO 工具。导出：

| 导出 | 签名 | 作用 |
|---|---|---|
| `get_pro_tools` | `() -> dict[str, ToolSpec]` | 返回 PRO 专属工具表，并入标准工具集。 |

PRO 专属工具放 `agent/pro/`，不改 `standard.py`。

---

## PRO 仓库目标形态

```
QevosAgentPro/
  (开源 Core 全部文件 —— 由 git merge upstream/main 同步，PRO 从不手改)
  dashboard/auth-provider.js     ← ①
  dashboard/routes-pro.js        ← ②
  dashboard/public/pro/          ← ③（登录页、账号 UI、资源）
  agent/pro/hooks.py             ← ④（管理系统实时上报）
  agent/pro/tools.py             ← ⑤（PRO 专属工具）
  agent/pro/__init__.py
  desktop/package.pro.json       ← 打包 overlay（产品名/appId/版本，构建时合并）
  desktop/main.pro.js            ← PRO 壳入口（require Core main + 拉起上报 sidecar）
```

PRO 持有的全是**新增文件**，与 Core 文件零行重叠 → `git merge upstream/main` 永不冲突。

## 同步流程（PRO 端）

```bash
# 一次性
git remote add upstream <开源 QevosAgent 仓库地址>

# 每次同步上游
git fetch upstream
git merge upstream/main      # 因为只新增文件，正常情况零冲突
```

若某次 merge 真的冲突，几乎一定是因为 PRO 违反了"不改 Core 文件"的纪律——
把那处改动重构成扩展点（加到开源版，默认 no-op），冲突即根除。
