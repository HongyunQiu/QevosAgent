# 工作日志

## 2026-10-12 | 忽略本机未跟踪产物

将 `apps/mcad_intercheck.md` 和本机外部工作区链接 `biophotonics-workstation/` 加入仓库忽略规则，避免它们干扰后续 QevosAgent 安卓版局域网实例发现功能的开发。未修改或删除这两个路径中的任何内容。

## 2026-10-12 | 安卓版局域网实例自动发现

为 Dashboard 增加 `_qevosagent._tcp.local` DNS-SD/mDNS 服务发布，广播实际监听端口、实例昵称、版本、API 版本和不泄露本机路径的稳定实例标识；仅在 Dashboard 对非回环地址监听时启用，并支持用 `QEVOS_MDNS=0` 关闭。安卓版设置页使用系统 `NsdManager` 扫描服务，针对旧版 Android 串行解析多个响应，通过 MulticastLock 改善旧设备兼容性；发现到的每个 IPv4 地址作为独立连接入口展示和保存，不按实例合并，以便用户明确选择物理局域网、热点或 VPN 地址。保留原有手动 IP、配置导入导出和连接前 `/api/version` 探测流程。补充了 `.env.example` 与移动端说明。

验证结果：Android `assembleDebug` 在现有 Temurin 17 下构建成功；本机 mDNS 发布与浏览回环测试成功；Dashboard 全部现有 Node 测试通过。`npm audit` 仍报告项目已有的 `ws` 高危以及 `node-cron`/`uuid` 中危问题，本阶段未进行无关依赖升级。移动端 VPN（尤其 ZeroTier）是否转发 mDNS 仍取决于系统和 VPN 实现，手动地址继续作为兜底；本阶段尚未进行多台真机/热点环境测试。

## 2026-10-12 | 过滤不可达的 mDNS 网卡地址

真机验证发现同一个服务的 DNS-SD 主机记录同时包含 Wi-Fi、ZeroTier、VMware VMnet1 和 VMnet8 四个 IPv4 地址；这不是四个实例，而是 `bonjour-service` 为本机所有活动网卡生成的地址记录。安卓版现在并发请求每个候选地址的 `/api/version`，仅当 HTTP 200 且响应包含 QevosAgent `version` 字段时才展示，并用接口返回的最新实例昵称覆盖广播缓存。由此保留可达的 Wi-Fi 与 ZeroTier 独立入口，同时隐藏手机不可达的 VMware、WSL、Docker 或过期地址。扫描七秒无可用结果时显示明确的 Wi-Fi/VPN 手动地址提示，不再无限显示“正在扫描”。Android Debug APK 已重新构建并覆盖安装到 Samsung SM-F9710；构建及安装成功，等待用户在实际设置页确认最终地址列表。

## 2026-10-12 | 修正安卓重新连接按钮可读性

真机反馈连接失败页的“重新连接”按钮在当前 DayNight/Material 主题下呈现黑色文字，和深色按钮背景对比不足。为该按钮显式设置与设置页主要操作按钮一致的 `colorPrimary` 背景和白色文字，避免系统主题变化覆盖默认前景色。

真机截图复核后确认用户实际指的是服务器设置页的“重新扫描”按钮；该普通 Button 同样被三星 DayNight 主题渲染为深色背景配黑色文字。已为“重新扫描”补上与底部“添加/保存/加载”一致的 `colorPrimary` 背景和白色文字。连接失败页“重新连接”的显式配色仍保留，防止同类问题。

## 2026-10-12 | 补齐桌面安装包的 mDNS 依赖

检查发现发布 CI 只在 `desktop/` 执行 npm 安装，而新增的 `bonjour-service` 此前只声明在 `dashboard/package.json`，干净环境打出的 Electron 安装包会缺少该模块。现已将其加入桌面运行依赖及锁文件，使 GitHub Actions 的既有 `desktop/npm install` 自动纳入安装包；Windows 本地打包脚本也在 setup/build 前执行 `npm install`。同时让 `setup_python.js` 跳过源码树中的任意 `node_modules`，避免开发机碰巧存在的 `dashboard/node_modules` 被复制进 `vendor/app`，保证本地与 CI 产物一致且不重复打包依赖。

验证结果：桌面单元测试 7/7 通过；`npm run setup` 与 `electron-builder --win --dir` 成功；解包产物包含根级 `node_modules/bonjour-service/dist/index.js` 和 `vendor/app/dashboard/mdns.js`，从后者执行 Node 模块解析能正确定位前者，同时产物中不存在重复的 `vendor/app/dashboard/node_modules`。

进一步复核 GitHub Release 工作流：Windows、macOS、Linux 共享同一个 `desktop/npm install → npm run setup → electron-builder` 流程，因此桌面依赖声明会覆盖三个平台；Android 为独立 Gradle 任务，不受该 Node 依赖影响。为防止未来新增 Dashboard 运行依赖时 Electron Builder 未执行服务端代码、仍然产出缺模块的坏包，在三平台 setup 后增加了 `require('./vendor/app/dashboard/mdns')` 显式验证，模块解析失败会在上传 Release 产物前终止对应平台构建。

## 2026-10-12 | 发布 v0.4.0

本阶段增加了跨平台 mDNS/DNS-SD 实例发布、Android 自动发现与可达性验证，并为桌面安装包新增 `bonjour-service` 运行依赖。依据 `doc/versioning.md` 中“新增运行依赖必须发布 minor/major 完整安装包”的规则，将版本从 0.3.45 提升到 0.4.0，而非仅提升 patch。同步更新桌面 package 与 lock 基准版本；推送 `v0.4.0` 标签后由 GitHub Actions 构建 Windows、macOS、Linux 和 Android 发布产物。

## 2026-10-14 | 左右看板全布局开关

将顶部原本只在手机竖屏显示的左右看板按钮扩展为所有布局常驻入口。桌面和横屏宽布局下，两个按钮现在可独立收起或打开左右网格栏，并兼容大字号列宽及中间看板全屏模式；窄屏与竖屏仍保留原有互斥抽屉、遮罩点击关闭行为。按钮状态、提示文字和 `aria-expanded` 会随实际可见状态同步，切换响应式断点时保留桌面折叠偏好且不会让抽屉失效。Dashboard 现有 Node 测试通过，页面内联脚本语法检查通过。

## 2026-10-14 | 修复看板与设置按钮加载期失效

修复左右看板全布局开关中错误地向 `getAttribute()` 返回值赋值的问题。该表达式虽然能通过脚本解析，但在页面初始化调用 `syncPanelToggleButtons()` 时会抛出运行时异常，中断后续初始化，使设置按钮以及竖屏左右抽屉都表现为无响应。现改为分别设置 DOM 属性和 `aria-label`。真实浏览器验证设置弹窗正常打开；430×900 竖屏下左右抽屉分别可打开、互斥关闭且遮罩状态正确，控制台无错误。

## 2026-10-14 | 移除中间看板全屏按钮

在左右看板已经支持独立收起和打开后，移除中间标签栏中功能重复的“全屏”按钮，并删除对应的 `pane-zen` 样式、状态切换函数、响应式清理监听及 Escape 退出分支。任务输出卡片自身的全屏查看功能保持不变；左右栏同时收起仍可获得完整中央工作区。页面内联脚本语法检查及 `git diff --check` 均通过。

## 2026-10-14 | 统一左右看板按钮样式

将顶部左右看板开关的圆角、内边距、字号和行高调整为与主题及设置图标按钮一致的胶囊式样式。移除展开状态下持续显示的蓝色边框，改为仅用普通前景色表达展开状态；深色和浅色主题都使用统一的标准边框，避免两个看板按钮在顶部栏中过度突出。

## 2026-10-15 | 增加内置 App 专用界面模式

新增启动时环境变量 `DASHBOARD_APP_ONLY=1`，用于将 Dashboard 切换为内置 Web App 专用显示模式，不在设置 UI 中暴露可修改入口。服务端在返回 HTML 时注入只读启动标志；前端启用后隐藏顶部栏、左右栏、命令栏及所有非 App TAB，仅让动态打开的内置 App iframe 占据窗口，存在多个 App 时保留 App TAB 条用于切换。修正首次 App 状态同步会切回 Events 的旧行为，并在关闭当前 App 时自动切到剩余 App；没有已打开 App 时保持空白等待。大字号、横屏和竖屏布局均由专用高优先级规则覆盖，默认未设置环境变量时原界面完全不变。已补充 `.env.example` 说明；服务端与页面脚本语法、`git diff --check` 通过。现有长耗时 Node 测试在当前 30 秒命令窗口内未完整执行完，未将其计为通过。
