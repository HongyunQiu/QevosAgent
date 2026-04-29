# QevosAgent 版本规范

## 版本号格式

采用标准三段式语义化版本：

```
v<major>.<minor>.<patch>
示例：v1.2.3
```

所有版本号统一使用同一套命名空间，`desktop/package.json` 与 `update-manifest.json` 始终保持一致。

---

## 三段含义与交付渠道

| 段 | 含义 | 触发条件 | 交付方式 |
|----|------|----------|----------|
| **major** | 重大版本 | Electron 升级、架构重构、破坏性改动 | 重新打包，发布新安装包 |
| **minor** | 功能版本 | main.js / Python 依赖 / dashboard 核心逻辑有改动 | 重新打包，发布新安装包 |
| **patch** | 内容版本 | 仅改动 `update-manifest.json` 列出的内容文件 | 增量更新，用户无需重装 |

**判断规则**：`major.minor` 相同 → patch 更新，走增量通道；`major` 或 `minor` 变化 → 需要下载新安装包。

---

## 发版操作步骤

### Patch 更新（内容文件变更）

1. 修改目标内容文件（如 `AGENTS.md`、`SKILLS/*.md`、`run_goal.py` 等）
2. 修改 `update-manifest.json`，将 `version` 的 patch 位 +1
3. **不需要修改** `desktop/package.json`
4. 推送到 `main` 分支，用户下次启动时自动检测并静默更新

```json
// update-manifest.json
{
  "version": "v1.2.4",   // 仅 patch +1
  "files": [ ... ]
}
```

### Minor / Major 更新（需要新安装包）

1. 修改相应代码（main.js、Python 依赖等）
2. **同步修改**以下两处版本号，保持一致：
   - `desktop/package.json` → `"version"`
   - `update-manifest.json` → `"version"`
3. 推送 tag（格式 `v*.*.*`），CI 自动打包并发布 GitHub Release
4. 用户启动时检测到 major/minor 差异，tabbar 显示橙色提示，点击跳转 GitHub Releases 下载

```json
// desktop/package.json
{ "version": "1.3.0" }

// update-manifest.json
{ "version": "v1.3.0", "files": [ ... ] }
```

---

## 可增量更新的文件范围

`update-manifest.json` 的 `files` 列表决定哪些文件走增量通道。所列文件必须同时满足：
1. 已通过 `desktop/scripts/setup_python.js` 的 `APP_COPY_MAP` 拷贝进 `vendor/app/`
2. 属于纯内容文件，变更不需要重新安装 Python 依赖或修改 Electron 主进程

当前包含：

```
AGENTS.md                       # Agent 运行指令
ADVISOR.md                      # Advisor 角色指令
SKILLS/coding.md                # 编程技能
SKILLS/data_analysis.md         # 数据分析技能
SKILLS/web_research.md          # 网络研究技能
SKILLS/tscircuit.md             # tscircuit 技能
run_goal.py                     # Agent 主入口
agent/core/llm.py               # LLM 接口
agent/core/loop.py              # 执行主循环
agent/core/compression.py       # 上下文压缩
agent/core/advisor.py           # Advisor 逻辑
agent/core/executor.py          # 工具执行器
agent/core/async_manager.py     # 异步管理
agent/core/types_def.py         # 类型定义
agent/tools/standard.py         # 标准工具集
agent/runtime/persistence.py    # 运行时持久化
agent/runtime/user_interrupt.py # 用户中断处理
dashboard/server.js             # Dashboard Node.js 服务端
dashboard/public/index.html     # Dashboard 主界面前端
dashboard/public/view.html      # Dashboard 子视图前端
```

**不应加入列表的文件**：`desktop/main.js`、`desktop/preload.js`（Electron 主进程）、`requirements.txt`（pip 依赖变更需重新打包）、`dashboard/node_modules/`（运行时无法 npm install）、`dashboard/package.json`（同上）、`.env`（用户配置）。

---

## 版本初始化机制

用户首次安装后，增量更新系统会以安装包内嵌的 `app.getVersion()` 作为基准版本写入 `vendor/app/.content_version`，避免将安装包已包含的文件重复下载。

```
首次启动流程：
  .content_version 不存在
  → 读取 app.getVersion()（来自 desktop/package.json）
  → 写入 .content_version
  → 与 update-manifest.json version 比对
  → 相同则无需更新
```

---

## 用户界面行为

| 情形 | Tabbar 显示 | 点击行为 |
|------|-------------|----------|
| 无更新 | 按钮隐藏 | — |
| Patch 更新可用 | 绿色 `↑ v1.2.4` | 原地下载并静默替换文件，完成后提示重启 |
| Minor/Major 更新可用 | 橙色 `↑ v1.3.0` | 打开浏览器跳转至 GitHub Releases |
| 下载中 | 蓝色 `↓ xx%` | 不可点击 |
| 下载完成 | 绿色 `✓ 重启应用` | 调用 `app.relaunch()` 重启 |

---

## 版本号与文件对应关系

```
desktop/package.json  "version"   ← electron-builder 打包时嵌入，决定安装包版本
update-manifest.json  "version"   ← 增量更新系统参考，patch 更新时独立推进
vendor/app/.content_version       ← 运行时记录当前已应用的内容版本（自动维护，勿手动修改）
```
