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
没有手机接入时行为和以前完全一样。

**同时只有一台设备能当执行体。** 第二台开启会顶掉第一台，被顶掉的那台会弹窗告知
并自动关掉开关——广播给所有手机会让每条命令执行 N 次。

### 与桌面端的行为差异

| | 说明 |
|---|---|
| `mouse_move` | 触摸屏没有「移动而不按下」，**不产生 hover**。只画坐标标记，结果里带 `note` 说明。需要悬停请用 `eval` 派发 `mouseover` |
| `button` 参数 | 无效，触摸没有左右键之分 |
| `key_type` | 走 `execCommand('insertText')`，会触发真实 input 事件（React 受控组件可用）；需要先有聚焦元素 |
| `scroll` | 滚动坐标处最内层可滚动元素，无惯性；`deltaX/deltaY` 仍是 CSS 像素，与桌面端一致 |
| 坐标 | 一律用**最近一次 screenshot 的像素坐标**，手机侧自动换算，不要自己缩放 |
| 截图 | 长边超过 1600px 会等比缩小；硬件层内容（WebGL、`<video>`）可能截出空白 |

### 已知限制

- **不做前台服务。** app 退到后台时通道会断。这是有意的：后台的 Activity 本来就
  无法可靠截图和接收触摸，保住 socket 只会得到一个连着的死执行体。
- 浏览视图在后台（用户正看着看板）时的截图行为**需要真机验证**——WebView 在
  INVISIBLE 状态下 Chromium 是否仍然产出软件绘制帧，各 ROM 可能不一致。
  `new_tab` / `navigate` 会自动切到浏览视图，所以常规流程不受影响。
- 网页会渲染成**移动版布局**，选择器和坐标与桌面版不同。

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
