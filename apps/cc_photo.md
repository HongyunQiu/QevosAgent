---
name: 资源图片管理器
icon: 🖼️
description: 天文照片统一管理——把计划批次(captures/)与零散照片(singles/)合并成一条时间轴，支持三层选中、金字塔看图(直方图拉伸)、传U盘、打包下载FITS
runtime: web
enabled: true
---
<!-- 面板由 apps-dist/cc_photo/index.html 提供；源码在 ChatCosmos 仓库 photo_app/ 下，
     用 sidecar/tools/deploy_app.py --app photo 部署。本 App 不带自己的 worker，
     复用 ChatCosmos worker 的 photo_timeline / usb_detect / usb_transfer / pack_download / tiler_ingest 方法
     （随 chatcosmos 照常三处部署）。以下正文仅作 apps-dist 缺失时的降级提示。 -->
<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8"><title>资源图片管理器</title></head>
<body style="font-family:sans-serif;padding:40px;background:#0f1117;color:#e5e7eb">
<h2>🖼️ 资源图片管理器</h2>
<p>面板文件缺失，请在 ChatCosmos 仓库运行 <code>python3 sidecar/tools/deploy_app.py --app photo --target &lt;QevosAgent&gt;</code> 部署。</p>
</body></html>
