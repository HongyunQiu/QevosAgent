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

## 2026-10-15 | 修正 App-only 启动黑屏

用户实测重启后全黑，确认此前把“只显示内置 APP TAB 内容”误解成了“只显示已动态打开的 App 面板”；而动态 App TAB 状态不会跨 Dashboard 重启持久化，因此启动时没有任何可显示内容。现将 App-only 模式的入口改为原有静态 `Apps` 页面：启动后自动激活并加载 App 列表，只显示 `Apps` 和从中打开的 Web App TAB；关闭最后一个动态 App 后返回 `Apps` 页面，不再出现空白黑屏。同步更新 `.env.example` 的语义说明，页面脚本语法与差异检查通过。

## 2026-10-15 | 精简 App-only 启动界面

按专用启动器场景进一步移除 App-only 模式中的顶部 App 标签栏和 Apps 工具栏，使页面打开后直接呈现应用图标网格。普通 Dashboard 模式的标签与 Apps 管理工具栏保持不变。

## 2026-10-16 | 优化左右看板开关的顶栏样式

参考 QNOTES 的轻量顶栏控件风格，移除左右看板开关默认的背景、边框和胶囊外形，使其直接融入顶栏。悬停时仅提升图标颜色，按下瞬间才显示浅层主题背景；保留键盘 `focus-visible` 轮廓以保证可访问性，横屏与竖屏沿用同一反馈。
## 2026-10-31 | 记忆系统整体改造——目录索引+全量正文分离+自动迁移

**背景**：2026-10-31 实验把 memory_macro.md 从 419K 全量正文换成 20K 目录索引版（降95% system prompt 占用），但发现新任务产生的记忆（save_concept）仍写入根目录 memory_macro.md，导致正文混入索引文件（已发生一次：体检章节混入 L236-244）。用户要求做整体改动+增加自动迁移功能（已有客户下载新版本运行时自动检测并迁移）。

**改动（commit 76ec9cb，4 files, +154/-1）**：
1. **run_goal.py**：新增 `_auto_migrate_memory()` 函数 + 启动检测（阈值 100K）。检测 memory_macro.md >100K 且 memory/memory_macro_full.md 不存在时，自动备份全量正文到 memory/ 子目录并解析 ## 标题+行号生成目录索引。幂等（已迁移跳过），失败不阻断启动（try/except）。
2. **standard.py**：`tool_save_concept` 增加目录索引检测——若 path 指向的文件前500字符含「记忆目录（按需加载索引）」标记，自动重定向到 memory/memory_macro_full.md。防止 LLM 误将新章节写入索引。
3. **loop.py**：验收门反馈消息中 save_concept 路径从 concept_path 改为 os.path.join(dirname, 'memory', 'memory_macro_full.md')，引导 LLM 写入全量正文。
4. **AGENTS.md**：末尾新增「记忆管理约定」章节，文档化文件布局、写入规则、自动迁移逻辑、save_concept 重定向、按需加载方法。

**测试**：
- 迁移函数：200 章节/45.2K → 6.2K 索引 + 45.2K 全量，幂等性验证通过
- save_concept 重定向：索引文件→重定向到全量正文，非索引文件→直接写入，均正确
- 语法检查：run_goal.py / standard.py / loop.py 全部 py_compile 通过

**清理**：memory_macro.md 中混入的体检章节正文（L236-244，11行）已删除（该章节已存在于 memory/memory_macro_full.md）。

**遗留**：
- llm.py:1184 注入机制未改（仍全量注入 concept_memory 字符串）——当前索引版 ~20K 足够小，不需要改；若索引增长超过 100K 可考虑加预算/截断
- memory/ 子目录未纳入 git（.gitignore 或本地数据），客户首次运行需自动迁移生成
- 未做端到端实际运行验证（需重启 agent 才能验证完整流程）
## 2026-11-14 | 看板『当前任务』置顶块样式统一为普通分组观感

承接上一任务(runs/20261114-171538)给任务看板加『当前任务』置顶块后,用户反馈其字体/风格/左边尖三角与普通分组不一致,要求统一。改 dashboard/public/index.html 两处:①CSS .runs-pinned-head 对齐 .run-group-head(padding 左8/gap6/color muted),新增 .runs-pinned-head .caret(9px muted width10)与 .runs-pinned-name;②renderRunsList 里 topHtml 的标题从硬拼『▶ 当前任务』(▶随标题变11px蓝色)改为 <span class=caret>▾</span><span class=runs-pinned-name>当前任务</span>,三角字符▶→▾、结构/字号/颜色与普通分组完全一致。保留 .runs-pinned .run-item 淡蓝背景作『当前』标识。node --check 内联脚本通过。serveStatic 实时 readFileSync(public/index.html) 无构建产物,刷新即生效。手机执行体因看板IP白名单无法跨设备截图,改由用户刷新看板确认观感,用户回复『可以了』。遗留:三角为静态▾(置顶块无折叠交互),用户未要求折叠。
## 2026-11-14 | 发版 v0.4.1

发布 v0.4.1（末位+1，上一版 v0.4.0）。本次包含 v0.4.0 之后 13 个提交，主题：①Dashboard 侧面板/布局改进（ac48ec2/70888ac/d6d5456/4879211/e05641c）；②内置 App-only 模式（3d7b1f1/1abdda6/4957a06）；③记忆系统整体改造——目录索引+全量正文分离+自动迁移（76ec9cb）；④panel_control 新增 listControls 枚举面板控件（a537c2d）；⑤看板『当前任务』置顶块样式统一（fec08ee）。

发版机制沿用 e6294a4：改 desktop/package.json version + desktop/package-lock.json 两处 version + 本 WORKLOG，commit 'release: v0.4.1'，打 tag v0.4.1 并 push 触发 .github/workflows/release.yml（on push tags v*.*.*，三平台 Electron + Android 打包并创建 GitHub Release）。

重要提醒（网站版本号）：qevos.ai 首页版本号由服务器 cron 脚本 scripts/sync_releases.sh 每天 04:00 从 GitHub releases/latest 拉取生成 /api/latest.json，前端 fetch 显示。上次 v0.4.0 网站仍显示 0.3.x，根因是 v0.4.0 的 GitHub Release 未成功生成（workflow 未跑成功），导致 cron 拉到的 latest 仍是旧 tag。故本次发版后须确认 GitHub Releases 中确实出现 v0.4.1，否则网站不会自动更新。

## 2026-11-15 | feat(dashboard): 桥加 qevos.goApps() + 外壳监听，支持全屏 App 一键回看板

背景：ChatCosmos 跑在 App-only 全屏模式（body.app-only），CSS 隐藏了 topbar/sidebar/tab 条，用户进全屏 App 后无任何可见入口回 Apps 图标看板（『关不掉、回不去』根因）。探讨照片管理时确认：图片管理器要做独立 App（方案B），必须有『从全屏 App 回看板』的把手。

改动（两文件，共9行）：
- dashboard/public/qevos-bridge.js：window.qevos 加 goApps()，向 parent postMessage {type:'qv-go-apps',app}。
- dashboard/public/index.html：window message 监听加 qv-go-apps 分支 → setCenterTab('apps')。

设计要点：goApps 只切走不销毁当前 App 的 iframe（setCenterTab 只 toggle active class，切走 pane 仅暂停音视频不 remove），对话/任务/播放位置原样保留；看板再点本 App 时 openAppPanel 检测 tab 已存在会复用不新建。goApps 方向是『回 home』，进 App 仍靠点图标。

部署：bridge 属 QevosAgent=平台层，可纳入 ChatCosmos 更新包 platform 段（dashboard/public 相对根目录、不在 PLATFORM_PROTECT），随普通 OTA 下发 + chatcosmos-platform-switch 自动重启 QevosAgent，不必重打整机镜像。node --check 通过。配套 ChatCosmos 侧改动见 ChatCosmos 仓库 WORKLOG。
## 2026-11-18 | 修复 Google OpenAI 兼容接口 400（logprobs/chat_template_kwargs 被 Protobuf 校验拒）

**症状**：连 Google（generativelanguage.googleapis.com）OpenAI 兼容接口，第 0 轮调用即报 400 INVALID_ARGUMENT：`Unknown name "logprobs": Cannot find field` + `Unknown name "chat_template_kwargs": Cannot find field`。

**根因**：Google 端点不被 `_detect_official_openai_endpoint` 识别为 official（只认 api.openai.com/openai.com），故 `_call_api` 走非 official 分支：① 主调用请求 `logprobs`（顶层 kwargs）；② 塞 `chat_template_kwargs` 进 `extra_body`（SDK 序列化后合并到请求体顶层）。Google 走严格 Protobuf JSON 校验，未定义顶层字段一律 `Cannot find field` 拒绝。框架本已有降级机制（`_suppressed_params` + `_wrap_create_method` 的 `wrapped_send` 拦截器从 HTTP body 顶层删 key + `_logprobs_enabled` 缓存降级），但 `_try_create_with_param_strip` 的 `is_param_error` 判据只匹配 `unsupported/unknown parameter`，正则只认 `unknown parameter: xxx`，都不认 Google 的 `Unknown name "x": Cannot find field` + `INVALID_ARGUMENT` 措辞 → 容错未触发，400 直接抛给上层。

**方案（方法B，比无脑删字段更稳健）**：未采用「全局删字段」（那会破坏本地 vLLM/Qwen3 用户的 enable_thinking 控制和信心指数 logprobs 熵）。改为扩展 `_try_create_with_param_strip` 的错误识别：① `is_param_error` 增加 `unknown name`/`cannot find field`/`invalid_argument` 判据；② 字段提取改用 `re.findall(r'Unknown name "(\w+)"')` 一次抓全部被拒字段（Google 一次 400 同时报两个）。命中后 `logprobs`→`_logprobs_enabled=False`+顶层 pop；`chat_template_kwargs`→进 `_suppressed_params`，由 `wrapped_send` 拦截器在 HTTP 层从 body 顶层删除。本地 vLLM 永不返回此类错误，零副作用。

**验证**：py_compile 语法 OK；用真实 Google 错误字符串走判据+正则，`is_param_error=True`，`bad_list=['logprobs','chat_template_kwargs']` 全部捕获。

**改动文件**：agent/core/llm.py（`_try_create_with_param_strip`，约 L621 起）。备份 runs/20261118-003809/artifacts/llm.py.bak。

**遗留**：未实机连 Google 端到端跑（本机无 Google key/网络受限）；逻辑已逐环节坐实，下次连 Google 首次 400 后应自动降级成功。


## 2026-11-18 | 加固 llm.py 容错：识别 create() 的 TypeError 非法顶层参数，避免无限重试刷屏

承接 20261118-003809（修了 Google OpenAI 兼容 400 INVALID_ARGUMENT）。排查 run 20261118-010006 为何仍报错：实际报错是 Python TypeError `Completions.create() got an unexpected keyword argument 'chat_template_kwargs'`，从 short_term 第5行起无限刷屏到第575行——**不是** 003809 修的那个 HTTP 400。

根因两条独立叠加：(1) 进程代码陈旧——旧 commit 91ba99b 把 chat_template_kwargs 当**顶层**参数传 `kwargs["chat_template_kwargs"]={...}`，新版代码已改塞 `extra_body`（llm.py:786/794，正确写法），但 010006 进程（01:00 启动）加载的是旧字节码没重载。实测复现：顶层传→TypeError；extra_body→OK。(2) 容错不覆盖 TypeError——f7594c9 的 is_param_error 判据要求 `'400' in es`，Python TypeError 串里没有 400，于是 re-raise，外层退避用相同 kwargs 无限重试。

加固（用户授权，llm.py 属受保护文件）：在 `_try_create_with_param_strip` 的 response_format 降级块之后、is_param_error 之前，新增 TypeError 识别分支——匹配 `unexpected keyword argument 'X'`，把 X 从顶层 kwargs 弹出、并从 extra_body 移除、加入 _suppressed_params（HTTP 拦截器兜底），再重试一次。这样陈旧进程/旧 SDK 也不会无限刷。

验证：py_compile 通过；端到端 harness（create 抛一次 TypeError 后第二次成功）通过，确认剥离后恢复且 suppressed 记录正确。已知缺口：未在真实 Google/vLLM 端点实测；对**已运行的旧进程无效**（补丁只对新启动/重载进程生效，需重启 agent/dashboard 才加载新代码）。分析报告 runs/20261118-010319/artifacts/why_010006_still_errors.md。

## 2026-11-18 | 修复 chat_template_kwargs 三层降级死循环（Google OpenAI 兼容端点）

承接 run 20261118-011324（全程 Connection error）与 003809/010006（已修 Google 400 + TypeError，commit f7594c9/e19630c）。用户反馈『修了 chat_template_kwargs 还是不行』，实证定位到之前修复引入了更隐蔽的次生 bug，最终表现成 Connection error / TypeError 反复刷屏。

三层根因（均用脚本实证，非猜测）：
1. wrapped_send（HTTP 拦截器）删 body 顶层被拒字段后重建 httpx.Request，但 dict(request.headers) 残留【旧】body 的 content-length，而删字段后 new_body 变短（实测 129→86），header 与实际 body 长度不符 → httpx 发送协议错乱 → 被包装成 'Connection error'。同时 httpx 0.28+ 的 Request() 默认 extensions={}，重建时漏传原 extensions 也会发送失败。
2. _call_api 的 nullify 块 `for _p in _suppressed: kwargs[_p]=None` 把 extra_body 型参数（chat_template_kwargs/thinking/continue_final_message/add_generation_prompt）当【顶层】参数传给 create() → create() 无同名顶层参数 → TypeError 'unexpected keyword argument chat_template_kwargs'。降级救一次，但下一轮 _call_api 又重新设 None → 每轮先 TypeError。
3. _call_api 的 extra_body 注入处无条件塞 chat_template_kwargs/thinking，即使已 suppressed 也照塞 → 又触发 400。

三处修复（agent/core/llm.py）：
1. wrapped_send 重建 Request 前 _headers.pop('content-length', None) 让 httpx 按 new_body 重算，并传 extensions=request.extensions。
2. nullify 块新增 _EXTRA_BODY_KEYS 集合，跳过 extra_body 型参数，只对真正的顶层参数设 None。
3. extra_body 注入改为守卫式：已被 _suppressed 拒绝的 key 不再注入。

验证：verify_real2.py 用真实 OpenAIBackend 连续三次 _call_api(thinking_budget=1000 强制注入 chat_template_kwargs) 全部成功返回，无 TypeError/Connection error/400，_suppressed 最终为空（Google 端点直接接受干净请求）。Google 端点实测：带 chat_template_kwargs 报 400 Unknown name，plain 调用 OK。

遗留：用户需重启 QevosAgent 进程加载新代码（长驻进程不重载字节码=白改，反复踩过的坑）。

## 2026-11-18 | 修复折叠屏旋转后竖屏主界面只占左侧窄列

连接三星折叠屏真机保留现场后确认：Android Activity 与 WebView 尺寸均正常（WebView 1848×2307），没有 FATAL/ANR；异常来自 Dashboard CSS。横屏桌面布局中点过右侧面板折叠后，`.app.right-panel-collapsed` 会在旋转进入竖屏时继续保留。该选择器的优先级高于移动端 `.app` 单列规则，使竖屏仍计算为 `230px 1fr 0` 三列，topbar/events/cmdbar 只占第一列；230 CSS px × 2.625 DPR ≈ 604 物理像素，与现场截图完全吻合。

修复方式：将桌面端左右面板折叠的列宽与隐藏规则整体限制在 `@media (min-width: 769px) and (orientation: landscape)` 内。移动端断点（窄屏或任意竖屏）因此不再受到残留折叠 class 影响，同时桌面横屏折叠行为保持不变。

验证：使用独立 Edge DevTools 会话加载本机 Dashboard，覆盖 4 种视口（704×879 折叠屏竖屏、900×1200 宽竖屏、700×400 窄横屏、1200×800 桌面横屏）× 4 种折叠状态（无/左/右/双侧）× 2 种字号模式（普通/大字），32/32 组合通过；移动端 topbar/events/cmdbar 均占满单列，桌面横屏三列与折叠列宽均符合预期。手机当前连接的 Dashboard 来源是远端 ZeroTier 主机 `172.24.217.34:8765`，不是本机工作树；源代码修复需部署到该主机后，手机端刷新页面才会永久生效。为不中断正在运行的 Agent，又通过 WebView DevTools 向当前页面注入了等价的临时媒体规则：保留 `right-panel-collapsed` 时计算列宽从 `230px 474px 0` 恢复为单列 `704px`，topbar/events/cmdbar 均为 704px，Agent 计时持续运行且页面未刷新。

## 2026-11-18 | 完善 OpenAI 兼容后端多阶段参数降级且保持正常连接零变化

审查 f7594c9/e19630c/be3e30c 三段 LLM 连接修复后，用离线仿真确认三个缺口：参数剥离只重试一次，服务端逐项拒绝 `response_format → logprobs → chat_template_kwargs` 时第二个 400 会逃逸；400 只从异常文本判断，异常对象已有 `status_code=400` 但文本不含数字时无法降级；`_suppressed_params` 未记录字段位于 create kwargs、extra_body 还是 SDK 序列化请求体，未知 extra 字段会在后续调用被错误投到顶层并反复触发 TypeError。

修复集中在错误降级路径：`_try_create_with_param_strip` 改为最多剥离 8 个字段、每轮必须实际取得进展的有界循环，优先读取 `status_code/response.status_code`；新增 `_suppressed_create_params` 与 `_suppressed_extra_body_params`，与原 `_suppressed_params`（仅 SDK 注入的序列化 body 字段）分层缓存；`_call_api` 按来源跳过字段，并补齐 `add_generation_prompt` 的独立守卫。未发生参数拒绝时三个集合均为空，仍只调用一次 `create()`，原请求字典不变。

新增 `test/tests_llm_param_fallback.py` 9 项离线回归：正常兼容后端单次调用及完整请求字典、官方 OpenAI 请求形状、连续结构化 400、TypeError 后继续 400、无进展立即终止、普通异常原对象原样抛出、extra_body 不泄漏顶层、HTTP content-length 重算与 extensions 保留。`py_compile` 通过；新测试 9/9；全仓 `python -m unittest discover -s test -p 'tests_*.py'` 共 289/289 通过。未调用真实付费模型端点；此前正常连接行为由精确请求字典断言覆盖。
