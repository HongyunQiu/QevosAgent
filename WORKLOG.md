# 工作日志

## 2026-10-12 | 忽略本机未跟踪产物

将 `apps/mcad_intercheck.md` 和本机外部工作区链接 `biophotonics-workstation/` 加入仓库忽略规则，避免它们干扰后续 QevosAgent 安卓版局域网实例发现功能的开发。未修改或删除这两个路径中的任何内容。

## 2026-10-12 | 安卓版局域网实例自动发现

为 Dashboard 增加 `_qevosagent._tcp.local` DNS-SD/mDNS 服务发布，广播实际监听端口、实例昵称、版本、API 版本和不泄露本机路径的稳定实例标识；仅在 Dashboard 对非回环地址监听时启用，并支持用 `QEVOS_MDNS=0` 关闭。安卓版设置页使用系统 `NsdManager` 扫描服务，针对旧版 Android 串行解析多个响应，通过 MulticastLock 改善旧设备兼容性；发现到的每个 IPv4 地址作为独立连接入口展示和保存，不按实例合并，以便用户明确选择物理局域网、热点或 VPN 地址。保留原有手动 IP、配置导入导出和连接前 `/api/version` 探测流程。补充了 `.env.example` 与移动端说明。

验证结果：Android `assembleDebug` 在现有 Temurin 17 下构建成功；本机 mDNS 发布与浏览回环测试成功；Dashboard 全部现有 Node 测试通过。`npm audit` 仍报告项目已有的 `ws` 高危以及 `node-cron`/`uuid` 中危问题，本阶段未进行无关依赖升级。移动端 VPN（尤其 ZeroTier）是否转发 mDNS 仍取决于系统和 VPN 实现，手动地址继续作为兜底；本阶段尚未进行多台真机/热点环境测试。

## 2026-10-12 | 过滤不可达的 mDNS 网卡地址

真机验证发现同一个服务的 DNS-SD 主机记录同时包含 Wi-Fi、ZeroTier、VMware VMnet1 和 VMnet8 四个 IPv4 地址；这不是四个实例，而是 `bonjour-service` 为本机所有活动网卡生成的地址记录。安卓版现在并发请求每个候选地址的 `/api/version`，仅当 HTTP 200 且响应包含 QevosAgent `version` 字段时才展示，并用接口返回的最新实例昵称覆盖广播缓存。由此保留可达的 Wi-Fi 与 ZeroTier 独立入口，同时隐藏手机不可达的 VMware、WSL、Docker 或过期地址。扫描七秒无可用结果时显示明确的 Wi-Fi/VPN 手动地址提示，不再无限显示“正在扫描”。Android Debug APK 已重新构建并覆盖安装到 Samsung SM-F9710；构建及安装成功，等待用户在实际设置页确认最终地址列表。
