# 代理设置对 UI App / 子进程的影响 —— 问题记录

> 状态：**已修复（2026-07-19，方案 ①+②）**。`childEnv()` 在所有 spawn 点
> （agent / 脚本 App / 终端）给子进程 env 合并回环 NO_PROXY；`POST /api/env`
> 保存代理时 `.env` 配套写入 NO_PROXY。§6 验证清单已全过（带毒代理第二实例 +
> 裸 urllib 探针实测）。本文余下部分保留为问题分析与回归依据。
> 2026-07-19 由 mechanical_design App 的真实故障引出（见 §2 事故记录）。

## 1. 问题一句话

设置面板的 HTTP 代理写进 `.env` 后由**服务进程注入给所有子进程**，但"本地地址自动跳过"
这个承诺只在 `agent/core/llm.py` 一个消费点实现了——其余子进程（脚本 App、UI App 工具链、
Agent 跑的任意 Python 脚本）继承了 `HTTP_PROXY` 却**没有配套的 `NO_PROXY`**，
它们对 `127.0.0.1` 的回环请求会被送去代理。更糟的是代理常回 200，造成**静默假成功**。

## 2. 事故记录（首个实锤案例）

- 现象：mechanical_design 面板加"打孔"特征后模型不更新；`regen_status.json` 全绿、
  `via:"pushed"`，但 `app-data/.../model.qmesh` 纹丝不动。诊断耗时高——所有日志都说成功。
- 链路：面板 → `POST /api/app/mcad_regen/run` → 服务 spawn python（继承 `HTTP_PROXY=http://172.24.217.58:7897`，无 `NO_PROXY`）
  → 脚本 `urllib` POST `http://127.0.0.1:8765/api/app-file/...` → **请求进了代理** → 代理回 200
  → 脚本判定推送成功 → 文件未落盘 → 面板等 `file-changed` 推送落空。
- 止血（myQevosApp commit acd3391，仅该 App）：`ProxyHandler({})` 显式绕过代理 +
  响应校验 + 推后就地验证文件内容，任一失败退回直接落盘。
- 关键教训：**代理劫持的典型形态是"假成功"而非报错**，凡是"声称成功但没效果"的本机
  HTTP 链路，第一个排查代理。

## 3. 注入与消费的全链路盘点

**注入源**（一处）：
- 设置面板"HTTP 代理"（[index.html:2045](../dashboard/public/index.html)）→
  `POST /api/env` 把 `HTTP_PROXY`/`HTTPS_PROXY` 写入 `.env` 并同步 live `process.env`
- `.env` 只管理这两个键，**不写 `NO_PROXY`**。即使用户 shell 里有 NO_PROXY，
  服务进程若非从该 shell 启动（桌面端/服务化）就没有它 → 子进程拿到"有毒不带解药"的环境

**消费面（继承 `process.env` 的所有 spawn）**：

| 消费者 | 途径 | 是否受害 |
|---|---|---|
| LLM 客户端 | [llm.py:240](../agent/core/llm.py) 显式信任+回环/RFC1918/.local/NO_PROXY 豁免 | ✅ 已加固（唯一） |
| 脚本 App（runAppScript） | python/powershell/shell 子进程 | ❌ 裸奔（本次事故） |
| Agent 跑的任意脚本/工具 | run_goal 子进程链 | ❌ 裸奔 |
| 面板前端 fetch | 浏览器同源请求，不看环境变量 | ✅ 天然免疫 |
| node 服务自身出网 | undici/fetch 默认不读 HTTP_PROXY | ✅ 基本免疫 |

受害模式集中在：**子进程里用 python urllib/requests、curl 等"遵守代理环境变量"的客户端
回连本机 dashboard 端点**——这恰是 UI App 确定性重计算（编辑分级第②档）的标准姿势，
以后每个走"脚本回推文件/事件"的 App 都会踩。

## 4. 为什么这是架构问题（而非个别 bug）

- 承诺与实现错位：UI 文案承诺"本地自动跳过"，但豁免逻辑写在**一个消费点**（llm.py）里，
  而环境注入发生在**源头**（server spawn）。每新增一个子进程消费者都要重新记得这件事
  ——本次 mcad_regen 就是忘了的第 N 个。
- 消费点加固不可扩展：修 App 一个个修（mcad skill 已写入"回环请求必须绕过代理"约定），
  但约定靠记忆，架构靠缺省安全。

## 5. 候选修复（待决策，按推荐排序）

1. **源头修（推荐）：spawn 时补 NO_PROXY**。服务构造子进程 env 处（runAppScript、
   agent spawn、终端 spawn）合并 `NO_PROXY += localhost,127.0.0.1,::1`（保留用户已有条目）。
   一处修改，所有现在/未来的子进程免疫；语义与 llm.py 的豁免一致。约 5 行。
2. **落盘修：`.env` 写代理时配套写 NO_PROXY**（若无）。覆盖"用户手改 .env / 重启加载"路径，
   与 ① 互补，用户在 .env 里可见可改。约 5 行。
3. 消费端约定（已做，兜底）：ui_app / mcad skill 明确"本机回环请求一律显式绕过代理
   （python: `ProxyHandler({})`）+ 声称成功后验证副作用"。对不受我们控制的第三方工具无效，
   仅作纵深防御。
4. 不做的：给每个 App 发豁免工具函数（重复造轮子）；砍掉代理设置（Agent 出网确实需要）。

> 修复 ①② 属 Core 改动，按惯例做成缺省行为即可（不涉 PRO 扩展点冲突）；
> 落地时记得同步设置面板 hint 文案与本文件状态行。

## 6. 验证清单（2026-07-19 实测）

- [x] 带毒环境直连：第二实例 `HTTP_PROXY=http://127.0.0.1:9`（死端口）+ `NO_PROXY=example.com`
  （故意不含回环）→ 脚本 App 内裸 urllib POST 127.0.0.1 成功、文件落盘；
  子进程实测 `NO_PROXY=example.com,localhost,127.0.0.1,::1`（用户条目保留、回环合并）
- [x] `POST /api/env` 保存代理 → `.env` 配套写入 `NO_PROXY=localhost,127.0.0.1,::1`，
  其余键完好（本机真实 .env 正是无 NO_PROXY 的中毒配置，已顺带治愈）
- [x] mechanical_design 加特征自动更新（App 端豁免保留作纵深防御，不再是唯一防线）
- [x] LLM 出网不受影响：llm.py 自读环境变量做豁免判断，本修复只增回环条目，
  外网域名代理行为不变（代码审视确认，无行为交集）

> 注意：修复对**已在跑的服务进程**不生效（env 在启动时定格），需重启实例。
