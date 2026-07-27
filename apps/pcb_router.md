---
name: PCB 编辑器
icon: 🔲
description: PCB 布局布线编辑器 — 交互布局(拖动/旋转/换面) + GRID 迷宫自动布线 + 占用网格/飞线分析
runtime: web
sidecar: worker.py
sidecar_linger: 300
enabled: true
---
<!-- 本 App 为多文件形态：面板由 apps-dist/pcb_router/index.html 提供，
     源码在 myQevosApp 仓库 pcb_router/dist/ 下，deploy.py 部署。
     布线内核 route.mjs(Node)随 dist 同包，由 sidecar worker.py 转发调用。
     以下正文仅作 apps-dist 缺失时的降级提示。 -->
<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>PCB 编辑器</title></head>
<body style="font:14px sans-serif;background:#0d1117;color:#c9d1d9;display:flex;align-items:center;justify-content:center;height:100vh">
<div>⚠ 未找到构建产物 apps-dist/pcb_router/ — 请在 myQevosApp 仓库运行 deploy.py</div>
</body></html>
