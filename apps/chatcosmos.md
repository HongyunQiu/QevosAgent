---
name: ChatCosmos
icon: 🔭
description: 对话式天文摄影助手——ChatGPT 风格对话框 + 调试按钮，一键完成扫描/连接/校准/拍摄/叠加全流程
runtime: web
sidecar: assets/worker.py
sidecar_linger: 300
enabled: true
---
<!-- 本 App 已拆为多文件形态：面板由 apps-dist/chatcosmos/index.html 提供，
     源码在 ChatCosmos 仓库 app/ 下（2026-10-14 从 myQevosApp 迁来），用 sidecar/tools/deploy_app.py 部署。
     以下正文仅作 apps-dist 缺失时的降级提示。 -->
<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8"><title>ChatCosmos</title></head>
<body style="font-family:sans-serif;padding:40px;background:#0f1117;color:#e5e7eb">
<h2>🔭 ChatCosmos</h2>
<p>对话式天文摄影助手。面板文件缺失，请在 ChatCosmos 仓库运行 <code>python3 sidecar/tools/deploy_app.py --target &lt;QevosAgent&gt;</code> 部署。</p>
<p>源码：<code>ChatCosmos/app/</code></p>
</body></html>
