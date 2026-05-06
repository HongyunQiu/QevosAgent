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

## 项目说明

| 文件 | 说明 |
|------|------|
| `MainActivity.kt` | 主界面，全屏 WebView |
| `SettingsActivity.kt` | IP/端口配置，存入 SharedPreferences |
| `activity_main.xml` | 主布局：WebView + 错误界面 |
| `activity_settings.xml` | 设置布局 |

## 后续计划

- iOS 版本（WKWebView 壳，流程类似）
- 推送通知（Agent 完成任务时通知手机）
