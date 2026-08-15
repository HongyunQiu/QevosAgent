# QevosAgent 移动端

Android WebView 壳，连接到局域网（或 ZeroTier）中运行的 QevosAgent Dashboard。

## 快速开始

### 构建 APK

1. 用 **Android Studio** 打开 `mobile/android/` 目录
2. 等待 Gradle 同步完成
3. `Build → Build Bundle(s) / APK(s) → Build APK(s)`
4. APK 在 `app/build/outputs/apk/debug/app-debug.apk`

或命令行（需配置 Android SDK）：
```bash
cd mobile/android
./gradlew assembleDebug      # macOS/Linux
gradlew.bat assembleDebug    # Windows
```

### 使用

1. 将 APK 发送到手机安装（允许安装未知来源）
2. 首次打开会进入**连接设置**页
3. 输入运行 QevosAgent 的主机 IP 地址，端口默认 `8765`
4. 点击"保存并连接"

> ZeroTier 用户：在连接设置中填写 ZeroTier 分配给主机的 IP。

### 需求

- Android 7.0+（API 24+）
- 与主机同一 WiFi，或通过 ZeroTier 互通
- 主机上 QevosAgent 正在运行（Dashboard 服务监听 8765 端口）

## 浏览器执行体

手机可以充当 Agent `web_interact` 工具的执行体——Agent 操作的是**手机上的浏览视图**，
而不是 PC 上的 Chrome 或 Electron 标签页。

### 开启

边把手菜单 → **🌐 浏览器执行体：关** → 点一下切到「已接入」。开关会被记住。

开启后 dashboard 控制台会打印一行 `🌐 浏览器执行体已接入：<机型>`。
菜单里的 **🌐 切到浏览器视图** 可以在看板和浏览视图之间来回切。

### 工作方式

命令走 dashboard 已有的 WebSocket（`ws://host:port/?role=browser-agent`），
手机只出站连接、**不监听任何端口**——所以 ZeroTier / NAT / 移动数据下都能用，
也沿用了服务端已有的 IP 白名单校验。

`/api/browser-action` 的路由优先级：**手机已接入 → Electron → CDP**。
没有手机接入时行为和以前完全一样。每次动作只送到**一个**执行体，不存在同时派发。

**每条返回值都带 `via` 字段**（`mobile:<机型>` / `electron` / `cdp`），失败的动作
也带——出错时「当时是谁在执行」恰恰是最需要知道的。手机掉线后动作会回落到本机
浏览器，`via` 是唯一能看出「换人了」的地方。

**`BROWSER_EXECUTOR_STRICT=1`** 可关掉回落：已接入过的手机若**意外**断开
（掉线 / 被系统冻结），后续动作直接 503 报错而不转给本机浏览器。在手机菜单里
**主动**关闭执行体不算意外，仍然正常回落——那是用户在说「回到默认」。

**同时只有一台设备能当执行体。** 第二台开启会顶掉第一台，被顶掉的那台会弹窗告知
并自动关掉开关——广播给所有手机会让每条命令执行 N 次。

### 与 web_show 的关系：同一个浏览视图

手机上 `web_show` 的面板和 `web_interact` 操作的目标是**同一个浏览 WebView**，
和桌面 Electron 的模型一致（那边两者共用 `gViews` 里的同一个 WebContentsView）。

面板不再走 `window.open`——安卓 WebView 默认 `supportMultipleWindows=false`，
那样会把看板就地导航掉。改为 dashboard 页面通过 `window.QevosNative.openView()`
交给原生层。桥**只接受路径**，origin 由原生侧用已配置的服务器拼出来，页面无法
借它把浏览视图指向任意主机。

沿用 Electron 的 `allowNavigation` 二分：

| | 导航 | 谁设置 |
|---|---|---|
| `web_show` 面板 | **锁定**，点链接不跳走 | 收到 open-view 时 |
| 自动化视图 | 放开，正常跟随 | `new_tab` / `navigate` 时解锁 |

锁定态下点外链的处理**与桌面端不同**：Electron 交给系统浏览器只是切个窗口，
安卓上启动另一个 app 会让 QevosAgent 退到后台**被系统冻结、执行体断线**。所以
本机正担任执行体时直接拦截并 toast 提示；不担任执行体时才交给系统浏览器。

**`display_id` 只有一个槽位。** 手机不像桌面能开 N 个视图，任何 action 都作用于
当前这一个。请求的 `display_id` 与当前显示的不一致时，返回值的 `note` 会明说
（不报错，但不会让 agent 以为操作的是另一个面板）。`new_tab` 是唯一会重新认领
槽位身份的 action。

### 与桌面端的行为差异

| | 说明 |
|---|---|
| `mouse_move` | 触摸屏没有「移动而不按下」，**不产生 hover**。只画坐标标记，结果里带 `note` 说明。需要悬停请用 `eval` 派发 `mouseover` |
| `button` 参数 | 无效，触摸没有左右键之分 |
| `key_type` | 走 `execCommand('insertText')`，会触发真实 input 事件（React 受控组件可用）；需要先有聚焦元素 |
| `scroll` | 滚动坐标处最内层可滚动元素，无惯性；`deltaX/deltaY` 仍是 CSS 像素，与桌面端一致 |
| 坐标 | 一律用**最近一次 screenshot 的像素坐标**，手机侧自动换算，不要自己缩放 |
| 截图 | 走 `PixelCopy` 抓窗口合成面，会自动把浏览视图切到前台；长边超过 1600px 等比缩小 |

### 真机验证结论（Galaxy Z Fold SM-F9710 / Android 15）

全部 action 已在真机跑通：点击命中精确到 CSS 像素（目标 238,180 → 页面实收
237,180）、橙色标记与实际落点重合、`你好 hello 123` 中文输入成功、内层可滚动
元素滚动命中、`get_html` 正常。

踩到的三个坑，都会影响使用方式：

1. **手机必须解锁。** 锁屏遮住 Activity 时 WebView 不渲染，截图是**纯白**的
   （触摸和 `eval` 仍然工作，所以只有截图会出错——很容易误判成代码 bug）。
2. **灭屏会断线。** 三星 Freecess 在屏幕 doze 后冻结进程，socket 直接被
   `Software caused connection abort`，且进程冻住后连重连都发不出去。长时间
   自动化期间需要让屏幕常亮（开发调试可用 `adb shell svc power stayon true`）。
3. **`key_type` 之后软键盘会顶掉视口。** `adjustResize` 让 WebView 变矮，之前
   截图算出的坐标全部失效。输入之后要么 `eval` 里 `blur()`，要么重新截图再定位。

### 已知限制

- **不做前台服务。** app 退到后台时通道会断。这是有意的：后台的 Activity 本来就
  无法可靠截图和接收触摸，保住 socket 只会得到一个连着的死执行体。
- 网页会渲染成**移动版布局**，选择器和坐标与桌面版不同。
- 视口宽度按 `document.documentElement.clientWidth` 换算，不是
  `window.innerWidth`——后者是视觉视口，实测两者差 5%（499 vs 475），
  用错会让标记和 `elementFromPoint` 在长页面底部偏出目标元素。

## 项目说明

| 文件 | 说明 |
|------|------|
| `MainActivity.kt` | 主界面，看板 WebView + 浏览 WebView 切换 |
| `BrowserAgent.kt` | 浏览器执行体：WS 通道 + 各 action 的原生实现 |
| `SettingsActivity.kt` | IP/端口配置，存入 SharedPreferences |
| `activity_main.xml` | 主布局：看板 WebView + 浏览 WebView + 错误界面 |
| `activity_settings.xml` | 设置布局 |

## 后续计划

- iOS 版本（WKWebView 壳，流程类似）
- 推送通知（Agent 完成任务时通知手机）
