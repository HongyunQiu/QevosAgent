---
name: 电路图编辑器
icon: 📐
description: MarkdownSchematic MD编辑器 — 引脚/实例网络编辑 + 全局网络索引 + ERC检查 + 拓扑可视化
runtime: web
skill: schematic_editor
enabled: true
---
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>电路图编辑器</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { height: 100%; overflow: hidden; }
  body { font: 13px -apple-system, 'Segoe UI', Roboto, sans-serif; background: #0d1117; color: #c9d1d9; display: flex; flex-direction: column; }

  /* ── 滚动条随暗色主题 ── */
  * { scrollbar-width: thin; scrollbar-color: #30363d transparent; }   /* Firefox */
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: #30363d; border-radius: 5px; border: 2px solid #0d1117; }
  ::-webkit-scrollbar-thumb:hover { background: #484f58; }
  ::-webkit-scrollbar-corner { background: transparent; }
  #sidebar ::-webkit-scrollbar-thumb, #right-panel ::-webkit-scrollbar-thumb { border-color: #161b22; }

  /* ── 顶部工具栏 ── */
  #toolbar { display: flex; align-items: center; gap: 10px; padding: 8px 14px; border-bottom: 1px solid #21262d; background: #161b22; flex-shrink: 0; height: 44px; }
  #toolbar .title { font-weight: 700; font-size: 15px; color: #58a6ff; margin-right: 8px; white-space: nowrap; }
  #toolbar select { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; border-radius: 6px; padding: 5px 10px; font-size: 13px; max-width: 340px; }
  #toolbar select:hover { border-color: #58a6ff; }
  #toolbar .info { margin-left: auto; color: #6e7681; font-size: 12px; white-space: nowrap; }
  #status { margin-left: 12px; color: #3fb950; font-size: 12px; min-width: 110px; text-align: right; white-space: nowrap; }
  #status.error { color: #f85149; }

  /* ── 主体三栏布局 ── */
  #main { display: flex; flex: 1; overflow: hidden; }

  /* ── 左侧器件列表 ── */
  #sidebar { width: 250px; border-right: 1px solid #21262d; background: #161b22; overflow-y: auto; flex-shrink: 0; display: flex; flex-direction: column; }
  #sidebar h3 { font-size: 12px; color: #6e7681; padding: 10px 12px 6px; text-transform: uppercase; letter-spacing: 0.5px; flex-shrink: 0; }
  #file-tree { flex: 1; overflow-y: auto; }
  .dir-label { padding: 6px 12px 3px; color: #6e7681; font-size: 11px; font-weight: 600; }
  .file-item { padding: 4px 12px 4px 18px; cursor: pointer; border-left: 3px solid transparent; font-size: 12.5px; display: flex; align-items: baseline; gap: 6px; }
  .file-item:hover { background: #1c2129; border-left-color: #30363d; }
  .file-item.active { background: #1c2129; border-left-color: #58a6ff; }
  .file-item.active .fi-name { color: #58a6ff; font-weight: 600; }
  .file-item .fi-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .file-item .fi-meta { color: #6e7681; font-size: 10.5px; flex-shrink: 0; }
  #proj-stats { flex-shrink: 0; border-top: 1px solid #21262d; padding: 8px 12px; font-size: 11px; color: #6e7681; line-height: 1.7; }
  #proj-stats b { color: #c9d1d9; }

  /* ── 中间明细区 ── */
  #detail-panel { flex: 1; overflow: auto; padding: 12px 16px; min-width: 300px; }
  #detail-panel h2 { font-size: 16px; color: #58a6ff; margin-bottom: 2px; }
  #detail-panel .subtitle { color: #6e7681; font-size: 12px; margin-bottom: 10px; }
  #detail-panel h4 { font-size: 12px; color: #6e7681; margin: 14px 0 6px; text-transform: uppercase; letter-spacing: 0.5px; }
  #detail-panel h4 .grp { color: #d29922; text-transform: none; }
  .section-block { margin-bottom: 26px; }
  table.sch { border-collapse: collapse; width: 100%; font-size: 13px; }
  table.sch th { background: #161b22; color: #6e7681; font-weight: 600; padding: 6px 10px; text-align: left; border-bottom: 2px solid #30363d; position: sticky; top: 0; z-index: 1; white-space: nowrap; }
  table.sch td { padding: 4px 10px; border-bottom: 1px solid #21262d; }
  table.sch tr:hover td { background: #161b2244; }
  table.sch tr.net-highlight td { background: #1f6feb22; }
  table.sch tr.flash td { animation: flashrow 1.6s; }
  @keyframes flashrow { 0% { background: #f0c67444; } 100% { background: transparent; } }
  td.net-cell { color: #79c0ff; cursor: text; min-width: 80px; }
  td.net-cell:focus { outline: 1px solid #58a6ff; background: #0d1117; border-radius: 3px; }
  td.net-cell.unconnected { color: #6e7681; font-style: italic; }
  td.net-cell.placeholder { color: #d29922; font-style: italic; }
  td.net-cell.modified { color: #3fb950; }
  td.rownum { color: #6e7681; font-size: 11px; width: 36px; }
  td.pnum { color: #c9d1d9; width: 52px; }
  td.pname { color: #adbac7; }
  td.pnote { color: #6e7681; font-size: 12px; }
  td.iref { color: #f0c674; font-weight: 600; white-space: nowrap; }
  td.ival { color: #adbac7; white-space: nowrap; }
  td.ifp { color: #6e7681; font-size: 11px; max-width: 170px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .type-badge { display: inline-block; font-size: 10px; padding: 1px 6px; border-radius: 8px; font-weight: 600; }
  .type-active { background: #1f6feb22; color: #58a6ff; }
  .type-passive { background: #23863622; color: #3fb950; }
  .type-power { background: #d2992222; color: #d29922; }
  .type-ground { background: #6e768122; color: #6e7681; }
  .type-input { background: #8957e522; color: #8957e5; }
  .type-output { background: #f8514922; color: #f85149; }
  .type-bidirectional { background: #f0c67422; color: #f0c674; }
  .inst-filter { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; border-radius: 5px; padding: 3px 9px; font-size: 12px; width: 200px; margin-left: 10px; }
  .inst-filter::placeholder { color: #6e7681; }

  /* ── 右侧标签页面板 ── */
  #right-panel { width: 320px; border-left: 1px solid #21262d; background: #161b22; flex-shrink: 0; display: flex; flex-direction: column; }
  #right-expand { position: fixed; right: 0; top: 50%; transform: translateY(-50%); z-index: 90; background: #161b22; border: 1px solid #30363d; border-right: none; border-radius: 8px 0 0 8px; padding: 14px 5px; cursor: pointer; color: #58a6ff; font-size: 12px; display: none; writing-mode: vertical-lr; letter-spacing: 2px; }
  #right-expand:hover { background: #1c2129; }
  #right-tabs { display: flex; border-bottom: 1px solid #21262d; flex-shrink: 0; }
  #right-tabs .tab-collapse { flex: 0 0 26px; text-align: center; padding: 8px 0; color: #6e7681; cursor: pointer; font-size: 12px; border-bottom: 2px solid transparent; }
  #right-tabs .tab-collapse:hover { color: #58a6ff; }
  #right-tabs .tab { flex: 1; text-align: center; padding: 8px 4px; font-size: 12px; color: #6e7681; cursor: pointer; border-bottom: 2px solid transparent; }
  #right-tabs .tab:hover { color: #c9d1d9; }
  #right-tabs .tab.active { color: #58a6ff; border-bottom-color: #58a6ff; font-weight: 600; }
  #right-tabs .tab .cnt { font-size: 10px; }
  #right-tabs .tab .cnt.bad { color: #f85149; }
  .tab-body { flex: 1; overflow-y: auto; display: none; }
  .tab-body.active { display: flex; flex-direction: column; overflow-y: hidden; }
  .tab-search { padding: 8px 10px; flex-shrink: 0; }
  .tab-search input { width: 100%; background: #21262d; color: #c9d1d9; border: 1px solid #30363d; border-radius: 5px; padding: 4px 9px; font-size: 12px; }
  .tab-search input::placeholder { color: #6e7681; }
  .tab-list { flex: 1; overflow-y: auto; }
  .net-item { padding: 4px 12px; font-size: 12px; cursor: pointer; display: flex; align-items: center; gap: 6px; }
  .net-item:hover { background: #1c2129; }
  .net-item.active { background: #1f6feb22; }
  .net-item .nname { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #79c0ff; }
  .net-item .count { color: #6e7681; font-size: 11px; flex-shrink: 0; }
  .net-item .scope-badge { color: #6e7681; font-size: 10px; background: #21262d; border-radius: 6px; padding: 0 5px; flex-shrink: 0; }
  .net-dot-indicator { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .erc-item { padding: 6px 12px; font-size: 12px; cursor: pointer; border-bottom: 1px solid #21262d55; }
  .erc-item:hover { background: #1c2129; }
  .erc-item .sev { font-size: 10px; font-weight: 700; border-radius: 6px; padding: 0 6px; margin-right: 6px; }
  .erc-item .sev.error { background: #f8514922; color: #f85149; }
  .erc-item .sev.warn { background: #d2992222; color: #d29922; }
  .erc-item .loc { color: #6e7681; font-size: 11px; margin-top: 2px; }
  .empty-hint { color: #6e7681; font-size: 12px; text-align: center; padding: 24px 12px; }

  /* ── 拓扑 ── */
  #topo-wrap { flex: 1; overflow: auto; }
  #topo-svg { display: block; }
  .net-line { fill: none; stroke-width: 1.5; opacity: 0.55; cursor: pointer; }
  .net-line.highlight { stroke-width: 3; opacity: 1; }
  .net-dot { stroke-width: 1.5; cursor: pointer; }
  .net-label { font-size: 10px; fill: #6e7681; cursor: pointer; }
  .net-label.highlight { fill: #f0c674; font-weight: 700; }
  .pin-label { font-size: 10px; fill: #adbac7; }
  .pin-label.highlight { fill: #f0c674; font-weight: 700; }

  /* ── 空状态 ── */
  .empty-state { display: flex; align-items: center; justify-content: center; height: 100%; color: #6e7681; font-size: 14px; text-align: center; padding: 40px; }
  .empty-state .icon { font-size: 40px; margin-bottom: 12px; }

  /* ── 按钮 ── */
  .btn { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; border-radius: 6px; padding: 5px 12px; cursor: pointer; font-size: 12px; white-space: nowrap; }
  .btn:hover { border-color: #58a6ff; }

  /* ── 网络详情悬浮面板 ── */
  #net-popup { display: none; position: fixed; z-index: 200; background: #161b22; border: 1px solid #30363d; border-radius: 10px; box-shadow: 0 8px 32px #00000088; width: 460px; max-height: 76vh; overflow: hidden; flex-direction: column; }
  #net-popup-header { padding: 10px 14px; border-bottom: 1px solid #30363d; display: flex; justify-content: space-between; align-items: center; flex-shrink: 0; }
  #net-popup-title { color: #58a6ff; font-size: 14px; word-break: break-all; }
  #net-popup-body { padding: 10px 12px; overflow-y: auto; }
  .np-file { color: #6e7681; font-size: 11px; margin: 8px 0 2px; }
  .np-entry { display: flex; gap: 8px; padding: 3px 6px; font-size: 12px; cursor: pointer; border-radius: 5px; align-items: baseline; }
  .np-entry:hover { background: #1c2129; }
  .np-entry .ref { color: #f0c674; font-weight: 600; min-width: 44px; }
  .np-entry .pin { color: #c9d1d9; min-width: 56px; }
  .np-entry .extra { color: #6e7681; font-size: 11px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  /* ── 悬停网络连线图浮窗 ── */
  #hover-topo { display: none; position: fixed; z-index: 300; background: #161b22f5; border: 1px solid #30363d; border-radius: 10px; box-shadow: 0 8px 32px #000000aa; padding: 8px 10px 4px; pointer-events: none; }
  #hover-topo .ht-hint { color: #6e7681; font-size: 10px; text-align: right; padding: 2px 2px 3px; }

  /* ── 邻域原理图 overlay ── */
  #schematic-overlay { display: none; position: fixed; top: 44px; left: 0; right: 0; bottom: 0; background: #0d1117; z-index: 150; flex-direction: column; }
  #sch-toolbar { display: flex; align-items: center; gap: 10px; padding: 7px 14px; border-bottom: 1px solid #21262d; background: #161b22; flex-shrink: 0; }
  #sch-toolbar .sch-title { color: #58a6ff; font-weight: 700; font-size: 14px; }
  #sch-toolbar .sch-sub { color: #6e7681; font-size: 11px; }
  #sch-canvas-wrap { flex: 1; overflow: hidden; cursor: grab; position: relative; }
  #sch-canvas-wrap.panning { cursor: grabbing; }
  #sch-svg { width: 100%; height: 100%; display: block; }
  #sch-hint { position: absolute; right: 10px; bottom: 8px; color: #6e7681; font-size: 10px; pointer-events: none; }
  .sch-btn-mini { display: inline-block; margin-left: 6px; padding: 0 6px; font-size: 11px; color: #6e7681; border: 1px solid #30363d; border-radius: 5px; cursor: pointer; vertical-align: 2px; }
  .sch-btn-mini:hover { color: #58a6ff; border-color: #58a6ff; }
  .sch-wire { fill: none; stroke-width: 1.4; }
  .sch-neigh { cursor: pointer; }
  .sch-neigh:hover .sch-neigh-box { stroke: #58a6ff; stroke-width: 1.6; }
  .sch-netflag { cursor: pointer; }
  .sch-netflag:hover text { font-weight: 700; }
</style>
</head>
<body>

<!-- ── 工具栏 ── -->
<div id="toolbar">
  <span class="title">📐 电路图编辑器</span>
  <select id="project-select" title="选择项目目录">
    <option value="">📂 选择项目...</option>
  </select>
  <button id="open-dir-btn" class="btn" title="打开磁盘上任意目录">📂 打开目录</button>
  <select id="file-select" disabled title="选择MD文件">
    <option value="">📄 选择文件...</option>
  </select>
  <button id="reload-btn" class="btn" title="重新加载项目">⟳</button>
  <span class="info" id="file-info"></span>
  <span id="status">就绪</span>
</div>

<!-- 目录选择面板 -->
<div id="dir-picker" style="display:none;position:fixed;top:44px;left:0;right:0;bottom:0;background:#0d1117ee;z-index:100;padding:20px;overflow-y:auto;">
  <div style="max-width:700px;margin:0 auto;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
      <h2 style="color:#58a6ff;font-size:16px;">📂 选择项目目录</h2>
      <button id="dir-picker-close" class="btn" style="font-size:18px;">✕</button>
    </div>
    <div id="dir-breadcrumb" style="display:flex;gap:4px;align-items:center;margin-bottom:12px;flex-wrap:wrap;font-size:12px;color:#6e7681;"></div>
    <div style="display:flex;gap:8px;margin-bottom:16px;">
      <input id="dir-input" style="flex:1;background:#21262d;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:8px 12px;font-size:13px;" placeholder="或输入完整路径..." />
      <button id="dir-go-btn" class="btn">打开</button>
    </div>
    <div id="dir-list"></div>
    <p style="color:#6e7681;font-size:11px;margin-top:12px;">单击目录进入，「打开此目录」确认选择</p>
  </div>
</div>

<!-- ── 主体 ── -->
<div id="main">
  <!-- 左：器件/文件列表 -->
  <div id="sidebar">
    <h3>📁 器件列表</h3>
    <div id="file-tree"><div class="empty-hint">选择项目后显示</div></div>
    <div id="proj-stats" style="display:none"></div>
  </div>

  <!-- 中：器件明细 -->
  <div id="detail-panel">
    <div class="empty-state" id="detail-empty">
      <div><div class="icon">📐</div>选择左侧器件查看引脚 / 实例表格<br><br>
      <span style="font-size:12px;color:#6e7681">网络名可直接编辑，修改后自动保存到 MD 文件</span></div>
    </div>
    <div id="detail-content" style="display:none"></div>
  </div>

  <!-- 右：标签页 -->
  <div id="right-panel">
    <div id="right-tabs">
      <div class="tab active" data-tab="nets">网络</div>
      <div class="tab" data-tab="global">全局</div>
      <div class="tab" data-tab="erc">检查 <span class="cnt" id="erc-cnt"></span></div>
      <div class="tab" data-tab="topo">拓扑</div>
      <div class="tab-collapse" id="right-collapse" title="收起面板">⇥</div>
    </div>
    <div class="tab-body active" id="tab-nets">
      <div class="tab-list" id="net-list"><div class="empty-hint">选择器件后显示本文件网络</div></div>
    </div>
    <div class="tab-body" id="tab-global">
      <div class="tab-search"><input id="global-search" placeholder="搜索全局网络..." /></div>
      <div class="tab-list" id="global-list"><div class="empty-hint">加载项目后显示</div></div>
    </div>
    <div class="tab-body" id="tab-erc">
      <div class="tab-list" id="erc-list"><div class="empty-hint">加载项目后显示</div></div>
    </div>
    <div class="tab-body" id="tab-topo">
      <div class="tab-search"><input id="topo-filter" placeholder="过滤网络名..." /></div>
      <div id="topo-wrap"><div class="empty-hint" id="topo-empty">选择器件后显示拓扑</div><svg id="topo-svg" xmlns="http://www.w3.org/2000/svg"></svg></div>
    </div>
  </div>
</div>

<!-- 右侧面板收起后的展开把手 -->
<div id="right-expand" title="展开面板">◀ 面板</div>

<!-- 邻域原理图 overlay -->
<div id="schematic-overlay">
  <div id="sch-toolbar">
    <button id="sch-back" class="btn" title="返回上一个器件" disabled>← 返回</button>
    <span class="sch-title" id="sch-title"></span>
    <span class="sch-sub" id="sch-sub"></span>
    <span style="margin-left:auto"></span>
    <button id="sch-fit" class="btn" title="适配视图">⛶ 适配</button>
    <button id="sch-close" class="btn" title="关闭 (Esc)">✕</button>
  </div>
  <div id="sch-canvas-wrap">
    <svg id="sch-svg" xmlns="http://www.w3.org/2000/svg"></svg>
    <div id="sch-hint">拖拽平移 · 滚轮缩放 · 点击邻居器件漫游 · 已忽略 GND/电源走线（以符号表示）</div>
  </div>
</div>

<!-- 悬停网络连线图浮窗 -->
<div id="hover-topo"></div>

<!-- 网络详情悬浮面板 -->
<div id="net-popup">
  <div id="net-popup-header">
    <h3 id="net-popup-title"></h3>
    <button id="net-popup-close" class="btn" style="font-size:16px;padding:2px 8px;">✕</button>
  </div>
  <div id="net-popup-body"></div>
</div>

<script>
// ═══════════════════════════════════════════════════════════
//  MarkdownSchematic 编辑器 v2 — 纯前端实现
//  规范: MarkdownSchematic specs/circuit_spec.md v1.8
//  · 单实例器件(### pins / pins/分组 / 实体/pins) 与 类+实例(### instances) 双格式
//  · 全项目网络索引(按目录作用域) · ERC-lite · 拓扑可视化
// ═══════════════════════════════════════════════════════════

const APP_ID = 'schematic_editor';
const $ = id => document.getElementById(id);
const statusEl = $('status');
const projectSelect = $('project-select');
const fileSelect = $('file-select');
const fileInfo = $('file-info');
const fileTree = $('file-tree');
const projStats = $('proj-stats');
const detailEmpty = $('detail-empty');
const detailContent = $('detail-content');
const netListEl = $('net-list');
const globalListEl = $('global-list');
const globalSearch = $('global-search');
const ercListEl = $('erc-list');
const ercCnt = $('erc-cnt');
const topoSvg = $('topo-svg');
const topoEmpty = $('topo-empty');
const topoFilter = $('topo-filter');
const netPopup = $('net-popup');
const netPopupTitle = $('net-popup-title');
const netPopupBody = $('net-popup-body');

// ── 状态 ──
let customRoot = null;          // 「打开目录」指定的绝对 root；null = app-data 默认
let currentProject = null;      // 默认 root 下的子目录（'.' = 全部）
let currentFile = null;         // 当前打开的 md 相对路径
let fileList = [];              // [{path}] 项目内全部 md
let docs = {};                  // path -> 解析后的文档对象
let netIndex = {};              // scope+' '+net -> [{file,ref,pin,pinName,type,value}]
let refMeta = {};               // scope+'::'+ref -> {pinCount,value} 用于悬停图符号大小
let refLoc = {};                // scope+'::'+ref -> {file,kind:'device'|'instance'} 原理图漫游定位
let scopes = new Set();         // 项目内出现过的目录作用域
let ercIssues = [];
let selectedNet = null;         // 当前高亮网络（原始名，不含scope）
let selfWriteAt = 0;
let browsingDir = null;
const saveTimers = {};          // file -> timer

function setStatus(msg, isError) {
  statusEl.textContent = msg;
  statusEl.className = isError ? 'error' : '';
}
function esc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function scopeOf(path) {
  const i = path.lastIndexOf('/');
  return i < 0 ? '' : path.slice(0, i);
}
function netKey(scope, net) { return scope + ' ' + net; }
function isPlaceholder(net) { return /\$\{[^}]*\}/.test(net); }
function isRealNet(net) {
  return !!net && net !== 'NC' && net !== 'N.C' && net !== 'N.C.' &&
         !net.startsWith('unconnected') && !isPlaceholder(net);
}
// 拓扑视图里忽略的网络（连接数巨大、无观看价值）
function isIgnoredNet(net) { return net === 'GND'; }

// ── 网络颜色（哈希取色）──
const COLORS = [
  '#58a6ff','#3fb950','#f0c674','#f85149','#8957e5',
  '#79c0ff','#56d364','#e3b341','#ff7b72','#bc8cff',
  '#a5d6ff','#7ee787','#f0883e','#ffa198','#d2a8ff'
];
function getNetColor(net) {
  let h = 0;
  for (let i = 0; i < net.length; i++) h = ((h << 5) - h + net.charCodeAt(i)) | 0;
  return COLORS[Math.abs(h) % COLORS.length];
}

// ═══════════════════════════════════════════════════════════
//  文件 I/O（统一走带 root 的路径；修复：保存也带 root）
// ═══════════════════════════════════════════════════════════

function encRel(rel) { return String(rel).split('/').map(encodeURIComponent).join('/'); }

async function readFileRel(rel) {
  if (customRoot) {
    const r = await fetch(`/api/app-file/${APP_ID}/${encRel(rel)}?root=${encodeURIComponent(customRoot)}`);
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    return d.content;
  }
  return qevos.readFile(rel);
}
async function writeFileRel(rel, content) {
  if (customRoot) {
    const r = await fetch(`/api/app-file/${APP_ID}/${encRel(rel)}?root=${encodeURIComponent(customRoot)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content })
    });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    return d;
  }
  return qevos.writeFile(rel, content);
}
async function listFiles() {
  const params = new URLSearchParams();
  if (customRoot) { params.set('root', customRoot); params.set('dir', '.'); }
  else params.set('dir', currentProject || '.');
  const r = await fetch(`/api/app-files/${APP_ID}?${params}`);
  const d = await r.json();
  if (d.error) throw new Error(d.error);
  return d.files || [];
}

// ═══════════════════════════════════════════════════════════
//  MD 解析器（规范 v1.8）
//  文档 = { title, lines, sections[] }
//  section = { kind:'device'|'class'|'interface'|'impl'|'other'|'',
//              ref, name, meta{}, pinTables[], instances|null }
//  pinTable = { key, entity, group, cols, pins[] }
//  pin = { lineIdx, cells[], colMap, rowNo, pin, name, type, net, note }
//  instances = { columns[], colOf{}, pinCols[{pin,col}], rows[] }
//  instRow = { lineIdx, cells[], ref, value, footprint, near }
// ═══════════════════════════════════════════════════════════

function splitRow(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|')) s = s.slice(0, -1);
  return s.split('|').map(c => c.trim());
}
function isSeparatorRow(line) { return /^\|[-\s|:]+\|?\s*$/.test(line); }

function mapPinCols(cells) {
  const m = { rowNo: -1, pin: -1, name: -1, type: -1, net: -1, note: -1, side: -1, part: -1, group: -1, count: cells.length };
  cells.forEach((c, i) => {
    if (c === '行号' || /^Row/i.test(c)) m.rowNo = i;
    else if (c === '引脚号' || /^Pin/i.test(c)) m.pin = i;
    else if (c === '名称' || /^Name/i.test(c)) m.name = i;
    else if (c === '类型' || /^Type/i.test(c)) m.type = i;
    else if (c === '网络' || c === '网络名' || /^Net/i.test(c)) m.net = i;
    else if (c === '说明' || /^Note|^Desc/i.test(c)) m.note = i;
    else if (c === '方位') m.side = i;
    else if (c === 'part' || c === 'Part' || c === '单元') m.part = i;
    else if (c === '功能组' || c === '功能聚类') m.group = i;
  });
  if (m.pin < 0 || m.net < 0) {
    // 表头不规范 → 按列数位置推断
    if (cells.length >= 6) { m.rowNo = 0; m.pin = 1; m.name = 2; m.type = 3; m.net = 4; m.note = 5; }
    else if (cells.length === 5) { m.pin = 0; m.name = 1; m.type = 2; m.net = 3; m.note = 4; }
    else return null;
  }
  return m;
}

function parseDoc(text) {
  const lines = text.split('\n');
  const doc = { title: '', lines, sections: [] };
  let cur = { kind: '', ref: '', name: '', meta: {}, pinTables: [], instances: null, headingIdx: -1 };
  doc.sections.push(cur);
  let table = null;   // {mode:'pins',pt} | {mode:'inst'}

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    if (/^# (?!#)/.test(line)) { doc.title = line.slice(2).trim(); table = null; continue; }

    if (/^## (?!#)/.test(line)) {
      const h = line.slice(3).trim();
      cur = { kind: 'other', ref: '', name: h, meta: {}, pinTables: [], instances: null, headingIdx: i };
      let m;
      if ((m = h.match(/^器件:\s*(\S+)\s*(.*)$/))) { cur.kind = 'device'; cur.ref = m[1]; cur.name = m[2] || m[1]; }
      else if ((m = h.match(/^类:\s*(.+)$/))) { cur.kind = 'class'; cur.name = m[1]; }
      else if (h === 'interface') { cur.kind = 'interface'; }
      else if ((m = h.match(/^implementation\/(.+)$/))) { cur.kind = 'impl'; cur.name = m[1]; }
      doc.sections.push(cur);
      table = null;
      continue;
    }

    if (/^### /.test(line)) {
      const key = line.slice(4).trim();
      if (key === 'instances') {
        cur.instances = { columns: [], colOf: {}, pinCols: [], rows: [], headerLineIdx: -1 };
        table = { mode: 'inst' };
      } else if (key === 'pins' || key.startsWith('pins/') || key.endsWith('/pins') || key.includes('/pins/')) {
        const parts = key.split('/');
        let entity = null, group = null;
        if (parts.length === 2) { if (parts[0] === 'pins') group = parts[1]; else entity = parts[0]; }
        else if (parts.length >= 3) { entity = parts[0]; group = parts[2]; }
        const pt = { key, entity, group, cols: null, pins: [], headerLineIdx: -1 };
        cur.pinTables.push(pt);
        table = { mode: 'pins', pt };
      } else {
        table = null;
      }
      continue;
    }

    if (line.startsWith('|')) {
      if (isSeparatorRow(line)) continue;
      const cells = splitRow(line);
      if (table && table.mode === 'pins') {
        const pt = table.pt;
        if (pt.headerLineIdx < 0) { pt.cols = mapPinCols(cells); pt.headerLineIdx = i; continue; }
        if (!pt.cols) continue;
        const c = pt.cols;
        const get = j => (j >= 0 && j < cells.length) ? cells[j] : '';
        const pin = { lineIdx: i, cells, colMap: c, rowNo: get(c.rowNo), pin: get(c.pin), name: get(c.name), type: get(c.type), net: get(c.net), note: get(c.note),
                      side: get(c.side), part: get(c.part), group: get(c.group) };
        if (pin.pin !== '' || pin.name !== '' || pin.net !== '') pt.pins.push(pin);
      } else if (table && table.mode === 'inst' && cur.instances) {
        const it = cur.instances;
        if (it.headerLineIdx < 0) {
          it.columns = cells; it.headerLineIdx = i;
          cells.forEach((c, ci) => {
            const m = c.match(/^引脚(.+?)网络$/);
            if (m) it.pinCols.push({ pin: m[1], col: ci });
            else it.colOf[c] = ci;
          });
          continue;
        }
        const co = it.colOf;
        const get = name => (co[name] != null && co[name] < cells.length) ? cells[co[name]] : '';
        const row = { lineIdx: i, cells, ref: get('编号'), value: get('值'), footprint: get('封装'), near: get('靠近') };
        if (row.ref !== '') it.rows.push(row);
      }
      continue;
    }

    // 非表格行：结束当前表格；提取 "- 键: 值" 元信息
    table = null;
    const mm = line.match(/^- ([^:：]+)[:：]\s*(.*)$/);
    if (mm) cur.meta[mm[1].trim()] = mm[2].trim();
  }

  // 首个隐式 section 若无内容则丢弃；有内容但无名则用 # 标题兜底
  const s0 = doc.sections[0];
  if (!s0.pinTables.length && !s0.instances && doc.sections.length > 1) doc.sections.shift();
  else if (!s0.name) s0.name = doc.title;
  return doc;
}

// section 的全部引脚（拍平分组；v1.8 兼容：分组/实体标题映射到 功能组/part）
function sectionPins(sec) {
  const out = [];
  sec.pinTables.forEach(pt => pt.pins.forEach(p => {
    if (!p.group && pt.group) p.group = pt.group;
    if (!p.part && pt.entity) p.part = pt.entity;
    out.push(p);
  }));
  return out;
}
// section 显示名
function sectionLabel(sec, doc) {
  if (sec.kind === 'device') return (sec.ref ? sec.ref + ' ' : '') + sec.name;
  if (sec.kind === 'class') return sec.name;
  if (sec.kind === 'interface') return 'interface';
  if (sec.kind === 'impl') return 'implementation/' + sec.name;
  return sec.name || doc.title || '';
}

// ═══════════════════════════════════════════════════════════
//  全项目网络索引 + ERC-lite
//  规范：网络名按目录作用域隔离 → 索引 key = scope + net
// ═══════════════════════════════════════════════════════════

function rebuildIndex() {
  netIndex = {}; refMeta = {}; refLoc = {}; scopes = new Set(); ercIssues = [];
  const refSeen = {};   // scope::ref -> [file]

  for (const [file, doc] of Object.entries(docs)) {
    const scope = scopeOf(file);
    scopes.add(scope);

    for (const sec of doc.sections) {
      const pins = sectionPins(sec);

      if (sec.instances && sec.instances.rows.length) {
        // ── 类 + 实例 ──
        const pinByNo = {};
        pins.forEach(p => { pinByNo[p.pin] = p; });
        // 引脚列数与类引脚数一致性
        const declared = parseInt(sec.meta['引脚数'] || '', 10);
        const nCols = sec.instances.pinCols.length;
        if (pins.length && nCols !== pins.length) {
          ercIssues.push({ sev: 'warn', type: 'pin-mismatch', file, msg: `「${sectionLabel(sec, doc)}」instances 引脚列 ${nCols} 个 ≠ pins 表 ${pins.length} 行` });
        } else if (!pins.length && declared && nCols !== declared) {
          ercIssues.push({ sev: 'warn', type: 'pin-mismatch', file, msg: `「${sectionLabel(sec, doc)}」instances 引脚列 ${nCols} 个 ≠ 声明引脚数 ${declared}` });
        }
        for (const row of sec.instances.rows) {
          const rk = scope + '::' + row.ref;
          (refSeen[rk] = refSeen[rk] || []).push(file);
          refMeta[rk] = { pinCount: nCols, value: row.value };
          refLoc[rk] = { file, kind: 'instance' };
          for (const pc of sec.instances.pinCols) {
            const net = pc.col < row.cells.length ? row.cells[pc.col] : '';
            if (isPlaceholder(net)) {
              ercIssues.push({ sev: 'error', type: 'unresolved-var', file, ref: row.ref, net, msg: `${row.ref} 引脚${pc.pin} 网络仍是占位符 ${net}` });
              continue;
            }
            if (!isRealNet(net)) continue;
            const k = netKey(scope, net);
            (netIndex[k] = netIndex[k] || []).push({
              file, ref: row.ref, pin: pc.pin,
              pinName: (pinByNo[pc.pin] || {}).name || '', type: (pinByNo[pc.pin] || {}).type || '',
              value: row.value
            });
          }
        }
      } else if (pins.length) {
        // ── 单实例器件 / 模块接口 ──
        const ref = sec.ref || doc.title || file.replace(/\.md$/, '');
        refMeta[scope + '::' + ref] = { pinCount: pins.length, value: sec.name };
        refLoc[scope + '::' + ref] = { file, kind: 'device' };
        if (sec.kind === 'device' && sec.ref) {
          const rk = scope + '::' + sec.ref;
          (refSeen[rk] = refSeen[rk] || []).push(file);
        }
        for (const p of pins) {
          if (isPlaceholder(p.net)) {
            // 类模板里的 ${netN} 是规范预期；单实例器件里出现才是问题
            if (sec.kind === 'device') {
              ercIssues.push({ sev: 'error', type: 'unresolved-var', file, ref, net: p.net, msg: `${ref} 引脚${p.pin} 网络仍是占位符 ${p.net}` });
            }
            continue;
          }
          if (!isRealNet(p.net)) continue;
          const k = netKey(scope, p.net);
          (netIndex[k] = netIndex[k] || []).push({ file, ref, pin: p.pin, pinName: p.name, type: p.type, value: '' });
        }
      }
    }
  }

  // 编号重复
  for (const [rk, files] of Object.entries(refSeen)) {
    if (files.length > 1) {
      const ref = rk.split('::')[1];
      ercIssues.push({ sev: 'error', type: 'dup-ref', file: files[0], ref, msg: `编号 ${ref} 重复出现 ${files.length} 次（${[...new Set(files)].join(', ')}）` });
    }
  }
  // 单脚网络（作用域内只挂 1 个引脚）
  for (const [k, entries] of Object.entries(netIndex)) {
    if (entries.length === 1) {
      const net = k.slice(k.indexOf(' ') + 1);
      const e = entries[0];
      ercIssues.push({ sev: 'warn', type: 'single-pin', file: e.file, net, ref: e.ref, msg: `网络 ${net} 只连接 1 个引脚（${e.ref}.${e.pin}）` });
    }
  }
  ercIssues.sort((a, b) => (a.sev === b.sev ? 0 : a.sev === 'error' ? -1 : 1));
}

// 当前文件出现的网络 → [{net, count, global}]
function currentFileNets() {
  if (!currentFile || !docs[currentFile]) return [];
  const doc = docs[currentFile];
  const scope = scopeOf(currentFile);
  const counts = {};
  for (const sec of doc.sections) {
    if (sec.instances && sec.instances.rows.length) {
      for (const row of sec.instances.rows)
        for (const pc of sec.instances.pinCols) {
          const net = pc.col < row.cells.length ? row.cells[pc.col] : '';
          if (isRealNet(net)) counts[net] = (counts[net] || 0) + 1;
        }
    } else {
      for (const p of sectionPins(sec)) if (isRealNet(p.net)) counts[p.net] = (counts[p.net] || 0) + 1;
    }
  }
  return Object.entries(counts)
    .map(([net, c]) => ({ net, count: c, global: (netIndex[netKey(scope, net)] || []).length }))
    .sort((a, b) => b.count - a.count || a.net.localeCompare(b.net));
}

// ═══════════════════════════════════════════════════════════
//  项目加载
// ═══════════════════════════════════════════════════════════

async function loadProject() {
  setStatus('加载文件列表...');
  fileSelect.innerHTML = '<option value="">📄 选择文件...</option>';
  fileSelect.disabled = true;
  docs = {}; currentFile = null; selectedNet = null;
  detailEmpty.style.display = 'flex'; detailContent.style.display = 'none';

  let all;
  try { all = await listFiles(); }
  catch (e) { setStatus('加载失败: ' + e.message, true); return; }

  fileList = all.filter(f => f.type === 'file' && f.path.endsWith('.md') && !f.path.split('/').some(p => p.startsWith('.')));
  fileList.sort((a, b) => a.path.localeCompare(b.path));

  if (!fileList.length) {
    fileTree.innerHTML = '<div class="empty-hint">未找到MD文件</div>';
    setStatus('无MD文件');
    return;
  }

  // 文件下拉
  const dirGroups = {};
  fileList.forEach(f => {
    const d = scopeOf(f.path);
    (dirGroups[d] = dirGroups[d] || []).push(f);
  });
  for (const dir of Object.keys(dirGroups).sort()) {
    let parent = fileSelect;
    if (dir) { parent = document.createElement('optgroup'); parent.label = dir + '/'; fileSelect.appendChild(parent); }
    dirGroups[dir].forEach(f => {
      const opt = document.createElement('option');
      opt.value = f.path;
      opt.textContent = dir ? f.path.slice(dir.length + 1) : f.path;
      parent.appendChild(opt);
    });
  }
  fileSelect.disabled = false;

  // 全量解析（并发池）
  setStatus(`解析 0/${fileList.length} ...`);
  let done = 0;
  const queue = fileList.map(f => f.path);
  async function worker() {
    while (queue.length) {
      const p = queue.shift();
      try {
        const text = await readFileRel(p);
        if (typeof text === 'string') docs[p] = parseDoc(text);
      } catch (e) { console.warn('parse failed', p, e); }
      done++;
      if (done % 8 === 0) setStatus(`解析 ${done}/${fileList.length} ...`);
    }
  }
  await Promise.all(Array.from({ length: 6 }, worker));

  rebuildIndex();
  renderFileTree();
  renderProjStats();
  renderGlobalList();
  renderErcList();
  setStatus(`✓ 已加载 ${Object.keys(docs).length} 个文件`);

  // 自动打开第一个文件
  if (fileList.length) selectFile(fileList[0].path);
}

// ═══════════════════════════════════════════════════════════
//  左侧器件列表 / 项目统计
// ═══════════════════════════════════════════════════════════

function fileSummary(path) {
  const doc = docs[path];
  if (!doc) return { label: path.split('/').pop(), meta: '' };
  for (const sec of doc.sections) {
    if (sec.instances && sec.instances.rows.length)
      return { label: sec.name || doc.title, meta: '×' + sec.instances.rows.length };
    if (sec.kind === 'device')
      return { label: (sec.ref ? sec.ref + ' ' : '') + sec.name, meta: sectionPins(sec).length + 'p' };
  }
  const pins = doc.sections.reduce((n, s) => n + sectionPins(s).length, 0);
  return { label: doc.title || path.split('/').pop(), meta: pins ? pins + 'p' : '' };
}

function renderFileTree() {
  fileTree.innerHTML = '';
  const dirGroups = {};
  fileList.forEach(f => {
    const d = scopeOf(f.path);
    (dirGroups[d] = dirGroups[d] || []).push(f);
  });
  for (const dir of Object.keys(dirGroups).sort()) {
    if (dir) {
      const dl = document.createElement('div');
      dl.className = 'dir-label';
      dl.textContent = '📂 ' + dir + '/';
      fileTree.appendChild(dl);
    }
    dirGroups[dir].forEach(f => {
      const s = fileSummary(f.path);
      const item = document.createElement('div');
      item.className = 'file-item' + (f.path === currentFile ? ' active' : '');
      item.dataset.path = f.path;
      item.title = f.path;
      item.innerHTML = `<span class="fi-name">${esc(s.label)}</span><span class="fi-meta">${esc(s.meta)}</span>`;
      item.addEventListener('click', () => selectFile(f.path));
      fileTree.appendChild(item);
    });
  }
}

function renderProjStats() {
  let comps = 0, insts = 0, pins = 0;
  for (const doc of Object.values(docs)) {
    for (const sec of doc.sections) {
      if (sec.instances && sec.instances.rows.length) {
        insts += sec.instances.rows.length;
        pins += sec.instances.rows.length * sec.instances.pinCols.length;
      } else if (sec.kind === 'device' || sectionPins(sec).length) {
        comps++;
        pins += sectionPins(sec).length;
      }
    }
  }
  const nets = Object.keys(netIndex).length;
  projStats.style.display = 'block';
  projStats.innerHTML =
    `器件 <b>${comps}</b> · 实例 <b>${insts}</b><br>` +
    `网络 <b>${nets}</b> · 引脚 <b>${pins}</b>` +
    (ercIssues.length ? `<br>检查 <b style="color:${ercIssues.some(i => i.sev === 'error') ? '#f85149' : '#d29922'}">${ercIssues.length} 项</b>` : '');
}

// ═══════════════════════════════════════════════════════════
//  文件选择与明细渲染
// ═══════════════════════════════════════════════════════════

async function selectFile(filePath, focus) {
  currentFile = filePath;
  fileSelect.value = filePath;
  selectedNet = null;
  document.querySelectorAll('.file-item').forEach(i => i.classList.toggle('active', i.dataset.path === filePath));

  if (!docs[filePath]) {
    // 兜底：单文件重新读取
    try {
      const text = await readFileRel(filePath);
      if (typeof text === 'string') { docs[filePath] = parseDoc(text); rebuildIndex(); }
    } catch (e) { setStatus('加载失败: ' + e.message, true); return; }
  }
  const doc = docs[filePath];
  if (!doc) { setStatus('文件为空或不存在', true); return; }

  const totalPins = doc.sections.reduce((n, s) => n + sectionPins(s).length, 0);
  const totalInsts = doc.sections.reduce((n, s) => n + (s.instances ? s.instances.rows.length : 0), 0);
  fileInfo.textContent = totalInsts ? `${totalInsts} 实例` : `${totalPins} 引脚`;

  renderDetail(doc);
  renderNetList();
  renderTopology();
  setStatus('✓ ' + filePath.split('/').pop());

  if (focus) applyFocus(focus);
}

function renderDetail(doc) {
  detailEmpty.style.display = 'none';
  detailContent.style.display = 'block';
  detailContent.innerHTML = '';

  doc.sections.forEach((sec, si) => {
    if (!sec.pinTables.length && !sec.instances) return;
    const block = document.createElement('div');
    block.className = 'section-block';

    const h2 = document.createElement('h2');
    h2.textContent = sectionLabel(sec, doc);
    if (!sec.instances && sectionPins(sec).length) {
      const sb = document.createElement('span');
      sb.className = 'sch-btn-mini';
      sb.textContent = '◫ 原理图';
      sb.title = '查看该器件的邻域原理图';
      sb.addEventListener('click', () => {
        const scope = scopeOf(currentFile);
        const ref = sec.ref || doc.title || currentFile.replace(/\.md$/, '');
        openSchematic({ kind: 'device', scope, ref, label: sectionLabel(sec, doc), file: currentFile, sec }, false);
      });
      h2.appendChild(sb);
    }
    block.appendChild(h2);

    const subtitleBits = [];
    if (sec.kind === 'class') subtitleBits.push('类（多实例）');
    if (sec.kind === 'device') subtitleBits.push('器件（单实例）');
    if (sec.kind === 'interface') subtitleBits.push('模块接口');
    if (sec.kind === 'impl') subtitleBits.push('内部实现');
    if (sec.meta['类型']) subtitleBits.push('类型 ' + sec.meta['类型']);
    if (sec.meta['封装']) subtitleBits.push(sec.meta['封装']);
    const pinsN = sectionPins(sec).length;
    if (pinsN) subtitleBits.push(pinsN + ' 引脚');
    if (sec.instances) subtitleBits.push(sec.instances.rows.length + ' 实例');
    if (sec.meta['描述']) subtitleBits.push(sec.meta['描述']);
    const sub = document.createElement('div');
    sub.className = 'subtitle';
    sub.textContent = subtitleBits.join(' · ');
    block.appendChild(sub);

    // ── 引脚表（模板 or 单实例）──
    sec.pinTables.forEach(pt => {
      if (!pt.pins.length) return;
      const h4 = document.createElement('h4');
      h4.innerHTML = (sec.instances ? '引脚模板' : '引脚') +
        (pt.group ? ` <span class="grp">/ ${esc(pt.group)}</span>` : '') +
        (pt.entity ? ` <span class="grp">[${esc(pt.entity)}]</span>` : '');
      block.appendChild(h4);
      block.appendChild(buildPinTable(pt, sec));
    });

    // ── 实例表 ──
    if (sec.instances && sec.instances.rows.length) {
      const h4 = document.createElement('h4');
      h4.textContent = `实例 (${sec.instances.rows.length})`;
      const filter = document.createElement('input');
      filter.className = 'inst-filter';
      filter.placeholder = '过滤 编号/值/网络...';
      h4.appendChild(filter);
      block.appendChild(h4);
      const tbl = buildInstTable(sec, si);
      block.appendChild(tbl);
      filter.addEventListener('input', () => {
        const q = filter.value.trim().toLowerCase();
        tbl.querySelectorAll('tbody tr').forEach(tr => {
          tr.style.display = (!q || tr.textContent.toLowerCase().includes(q)) ? '' : 'none';
        });
      });
    }

    detailContent.appendChild(block);
  });
}

function typeBadge(t) {
  if (!t) return '<span style="color:#6e7681">-</span>';
  const cls = ['active','passive','power','ground','input','output','bidirectional'].includes(t) ? 'type-' + t : 'type-passive';
  return `<span class="type-badge ${cls}">${esc(t)}</span>`;
}

function netCellClass(net) {
  if (isPlaceholder(net)) return 'net-cell placeholder';
  if (!isRealNet(net)) return 'net-cell unconnected';
  return 'net-cell';
}
function netCellHtml(net) {
  if (net === '') return '<em>未连接</em>';
  return esc(net);
}

// 引脚表（网络列可编辑；方位/part/功能组列有值才显示）
function buildPinTable(pt, sec) {
  const table = document.createElement('table');
  table.className = 'sch';
  const showRowNo = pt.cols && pt.cols.rowNo >= 0;
  const showSide = pt.pins.some(p => p.side);
  const showPart = pt.pins.some(p => p.part);
  const showGroup = pt.pins.some(p => p.group);
  table.innerHTML = `<thead><tr>${showRowNo ? '<th>行号</th>' : ''}<th>引脚号</th><th>名称</th><th>类型</th>` +
    (showSide ? '<th>方位</th>' : '') + (showPart ? '<th>part</th>' : '') + (showGroup ? '<th>功能组</th>' : '') +
    `<th>网络</th><th>说明</th></tr></thead>`;
  const tbody = document.createElement('tbody');

  pt.pins.forEach(pin => {
    const tr = document.createElement('tr');
    tr.dataset.net = pin.net;
    tr.innerHTML =
      (showRowNo ? `<td class="rownum">${esc(pin.rowNo)}</td>` : '') +
      `<td class="pnum">${esc(pin.pin)}</td>` +
      `<td class="pname">${esc(pin.name)}</td>` +
      `<td>${typeBadge(pin.type)}</td>` +
      (showSide ? `<td style="color:#adbac7">${esc(pin.side)}</td>` : '') +
      (showPart ? `<td style="color:#f0c674">${esc(pin.part)}</td>` : '') +
      (showGroup ? `<td style="color:#d29922;font-size:12px">${esc(pin.group)}</td>` : '') +
      `<td class="${netCellClass(pin.net)}" contenteditable="true" spellcheck="false">${netCellHtml(pin.net)}</td>` +
      `<td class="pnote">${esc(pin.note)}</td>`;
    const netTd = tr.querySelector('.net-cell');
    bindNetCell(netTd, () => pin.net, newNet => {
      pin.net = newNet;
      pin.cells[pin.colMap.net] = newNet;
      commitLineEdit(currentFile, pin.lineIdx, pin.cells);
      tr.dataset.net = newNet;
    });
    tr.addEventListener('click', e => {
      if (e.target === netTd) return;
      if (isRealNet(pin.net)) highlightNet(pin.net);
    });
    // 悬停 → 该引脚网络的连线图
    tr.addEventListener('mouseenter', e => {
      const doc = docs[currentFile];
      const centerRef = sec.ref || (doc && doc.title) || '';
      showHoverTopo(() => buildHoverGraph(centerRef, sectionLabel(sec, doc || {}), [{ pin: pin.pin, name: pin.name, net: pin.net }]), e);
    });
    tr.addEventListener('mouseleave', hideHoverTopoSoon);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  return table;
}

// 实例表（每个引脚网络列可编辑）
function buildInstTable(sec, si) {
  const it = sec.instances;
  const pinByNo = {};
  sectionPins(sec).forEach(p => { pinByNo[p.pin] = p; });

  const table = document.createElement('table');
  table.className = 'sch';
  let head = '<th>编号</th><th>值</th><th>封装</th>';
  it.pinCols.forEach(pc => {
    const nm = (pinByNo[pc.pin] || {}).name;
    head += `<th>引脚${esc(pc.pin)}${nm ? ` <span style="color:#adbac7">${esc(nm)}</span>` : ''}</th>`;
  });
  if (it.colOf['靠近'] != null) head += '<th>靠近</th>';
  table.innerHTML = `<thead><tr>${head}</tr></thead>`;
  const tbody = document.createElement('tbody');

  it.rows.forEach(row => {
    const tr = document.createElement('tr');
    tr.dataset.ref = row.ref;
    let html = `<td class="iref">${esc(row.ref)}<span class="sch-btn-mini sch-open" title="邻域原理图">◫</span></td><td class="ival">${esc(row.value)}</td><td class="ifp" title="${esc(row.footprint)}">${esc(row.footprint)}</td>`;
    it.pinCols.forEach(pc => {
      const net = pc.col < row.cells.length ? row.cells[pc.col] : '';
      html += `<td class="${netCellClass(net)}" data-col="${pc.col}" contenteditable="true" spellcheck="false">${netCellHtml(net)}</td>`;
    });
    if (it.colOf['靠近'] != null) html += `<td class="pnote">${esc(row.near)}</td>`;
    tr.innerHTML = html;

    tr.querySelectorAll('.net-cell').forEach(td => {
      const col = parseInt(td.dataset.col, 10);
      bindNetCell(td, () => (col < row.cells.length ? row.cells[col] : ''), newNet => {
        while (row.cells.length <= col) row.cells.push('');
        row.cells[col] = newNet;
        commitLineEdit(currentFile, row.lineIdx, row.cells);
      });
    });
    tr.querySelector('.sch-open').addEventListener('click', e => {
      e.stopPropagation();
      const scope = scopeOf(currentFile);
      openSchematic({ kind: 'instance', scope, ref: row.ref, label: row.ref + (row.value ? ' ' + row.value : ''), file: currentFile, sec, row }, false);
    });
    tr.addEventListener('click', e => {
      if (e.target.classList && e.target.classList.contains('net-cell')) return;  // 网络格自己处理
      if (e.target.classList && e.target.classList.contains('sch-open')) return;
      renderInstanceTopology(sec, row);
      switchTab('topo');
    });
    // 悬停 → 该实例全部引脚的连线图
    tr.addEventListener('mouseenter', e => {
      showHoverTopo(() => {
        const pins2 = it.pinCols.map(pc => ({
          pin: pc.pin, name: (pinByNo[pc.pin] || {}).name || '',
          net: pc.col < row.cells.length ? row.cells[pc.col] : ''
        }));
        return buildHoverGraph(row.ref, row.ref + (row.value ? ' ' + row.value : ''), pins2);
      }, e);
    });
    tr.addEventListener('mouseleave', hideHoverTopoSoon);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  return table;
}

// 网络单元格通用编辑绑定
function bindNetCell(td, getNet, onChange) {
  td.addEventListener('focus', () => { td.textContent = getNet(); });
  td.addEventListener('blur', () => {
    const newNet = td.textContent.trim();
    const old = getNet();
    if (newNet !== old) {
      onChange(newNet);
      td.classList.add('modified');
    }
    const now = getNet();
    td.classList.remove('placeholder', 'unconnected');
    const cls = netCellClass(now).replace('net-cell', '').trim();
    if (cls) td.classList.add(cls);
    td.innerHTML = netCellHtml(now);
  });
  td.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); td.blur(); }
    if (e.key === 'Escape') { td.textContent = getNet(); td.blur(); }
  });
  td.addEventListener('click', e => {
    if (document.activeElement === td) return;
    const net = getNet();
    if (isRealNet(net)) { e.stopPropagation(); showNetPopup(net, e); }
  });
}

// ═══════════════════════════════════════════════════════════
//  保存（精确行替换 → 防抖写回；修复：带 root）
// ═══════════════════════════════════════════════════════════

function commitLineEdit(file, lineIdx, cells) {
  const doc = docs[file];
  if (!doc) return;
  doc.lines[lineIdx] = '| ' + cells.join(' | ') + ' |';
  scheduleSave(file);
  rebuildIndex();
  renderNetList();
  renderGlobalList();
  renderErcList();
  renderProjStats();
}

function scheduleSave(file) {
  clearTimeout(saveTimers[file]);
  saveTimers[file] = setTimeout(async () => {
    try {
      const doc = docs[file];
      if (!doc) return;
      selfWriteAt = Date.now();
      await writeFileRel(file, doc.lines.join('\n'));
      setStatus('✓ 已保存 ' + file.split('/').pop());
    } catch (e) {
      setStatus('保存失败: ' + e.message, true);
    }
  }, 500);
}

// ═══════════════════════════════════════════════════════════
//  右侧标签页
// ═══════════════════════════════════════════════════════════

function switchTab(name) {
  document.querySelectorAll('#right-tabs .tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-body').forEach(b => b.classList.toggle('active', b.id === 'tab-' + name));
  // 切标签时如面板被收起则自动展开
  const rp = $('right-panel');
  if (rp.style.display === 'none') { rp.style.display = 'flex'; $('right-expand').style.display = 'none'; }
}
document.querySelectorAll('#right-tabs .tab').forEach(t => {
  t.addEventListener('click', () => switchTab(t.dataset.tab));
});

// ── 右侧面板收起 / 展开 ──
$('right-collapse').addEventListener('click', () => {
  $('right-panel').style.display = 'none';
  $('right-expand').style.display = 'block';
});
$('right-expand').addEventListener('click', () => {
  $('right-panel').style.display = 'flex';
  $('right-expand').style.display = 'none';
});

// ── 网络（当前文件）──
function renderNetList() {
  const nets = currentFileNets();
  netListEl.innerHTML = '';
  if (!nets.length) { netListEl.innerHTML = '<div class="empty-hint">本文件无已连接网络</div>'; return; }
  nets.forEach(({ net, count, global }) => {
    const item = document.createElement('div');
    item.className = 'net-item';
    item.dataset.net = net;
    item.innerHTML = `<span class="net-dot-indicator" style="background:${getNetColor(net)}"></span>` +
      `<span class="nname">${esc(net)}</span>` +
      `<span class="count">${count}${global > count ? ' / 全局' + global : ''}</span>`;
    item.addEventListener('click', e => showNetPopup(net, e));
    item.addEventListener('mouseenter', () => highlightNet(net, true));
    item.addEventListener('mouseleave', () => { if (selectedNet !== net) clearHighlight(); });
    netListEl.appendChild(item);
  });
}

// ── 全局网络 ──
function renderGlobalList() {
  const q = (globalSearch.value || '').trim().toLowerCase();
  const multiScope = scopes.size > 1;
  const rows = Object.entries(netIndex)
    .map(([k, entries]) => {
      const sp = k.indexOf(' ');
      return { scope: k.slice(0, sp), net: k.slice(sp + 1), entries };
    })
    .filter(r => !q || r.net.toLowerCase().includes(q))
    .sort((a, b) => b.entries.length - a.entries.length || a.net.localeCompare(b.net));

  globalListEl.innerHTML = '';
  if (!rows.length) { globalListEl.innerHTML = '<div class="empty-hint">无匹配网络</div>'; return; }
  const frag = document.createDocumentFragment();
  rows.slice(0, 500).forEach(r => {
    const item = document.createElement('div');
    item.className = 'net-item';
    item.dataset.net = r.net;
    item.innerHTML = `<span class="net-dot-indicator" style="background:${getNetColor(r.net)}"></span>` +
      `<span class="nname">${esc(r.net)}</span>` +
      (multiScope && r.scope ? `<span class="scope-badge">${esc(r.scope)}</span>` : '') +
      `<span class="count">${r.entries.length}pin</span>`;
    item.addEventListener('click', e => showNetPopup(r.net, e, r.scope));
    frag.appendChild(item);
  });
  globalListEl.appendChild(frag);
  if (rows.length > 500) {
    const more = document.createElement('div');
    more.className = 'empty-hint';
    more.textContent = `... 共 ${rows.length} 个网络，输入关键字过滤`;
    globalListEl.appendChild(more);
  }
}
globalSearch.addEventListener('input', renderGlobalList);

// ── ERC 检查 ──
function renderErcList() {
  const errs = ercIssues.filter(i => i.sev === 'error').length;
  ercCnt.textContent = ercIssues.length ? `(${ercIssues.length})` : '';
  ercCnt.className = 'cnt' + (errs ? ' bad' : '');

  ercListEl.innerHTML = '';
  if (!ercIssues.length) { ercListEl.innerHTML = '<div class="empty-hint">✓ 未发现问题</div>'; return; }
  const frag = document.createDocumentFragment();
  ercIssues.slice(0, 400).forEach(issue => {
    const item = document.createElement('div');
    item.className = 'erc-item';
    item.innerHTML = `<span class="sev ${issue.sev}">${issue.sev === 'error' ? '错误' : '警告'}</span>${esc(issue.msg)}` +
      `<div class="loc">${esc(issue.file)}</div>`;
    item.addEventListener('click', () => {
      selectFile(issue.file, { net: issue.net, ref: issue.ref });
    });
    frag.appendChild(item);
  });
  ercListEl.appendChild(frag);
  if (ercIssues.length > 400) {
    const more = document.createElement('div');
    more.className = 'empty-hint';
    more.textContent = `... 共 ${ercIssues.length} 项`;
    ercListEl.appendChild(more);
  }
}

// ═══════════════════════════════════════════════════════════
//  拓扑可视化（右侧标签页）
// ═══════════════════════════════════════════════════════════

const SVGNS = 'http://www.w3.org/2000/svg';
function svgEl(tag, attrs) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  return el;
}

// 当前文件的拓扑：单实例器件 → 引脚-网络轨道图
function renderTopology() {
  topoSvg.innerHTML = '';
  const doc = currentFile && docs[currentFile];
  if (!doc) { topoEmpty.style.display = 'block'; topoEmpty.textContent = '选择器件后显示拓扑'; return; }

  const sec = doc.sections.find(s => !s.instances && sectionPins(s).length) ||
              doc.sections.find(s => sectionPins(s).length || s.instances);
  if (!sec) { topoEmpty.style.display = 'block'; topoEmpty.textContent = '本文件无引脚数据'; return; }
  if (sec.instances) {
    topoEmpty.style.display = 'block';
    topoEmpty.textContent = '类文件：点击实例表中某一行（非网络格）查看该实例的连接拓扑';
    return;
  }
  const scope = scopeOf(currentFile);
  const pins = sectionPins(sec).map(p => ({
    pin: p.pin, name: p.name, net: p.net,
    globalCount: (netIndex[netKey(scope, p.net)] || []).length
  }));
  drawPinNetTopology(pins, sectionLabel(sec, doc), true);
}

// 实例拓扑：某个实例的引脚 → 网络（含全局引脚数）
function renderInstanceTopology(sec, row) {
  topoSvg.innerHTML = '';
  const scope = scopeOf(currentFile);
  const pinByNo = {};
  sectionPins(sec).forEach(p => { pinByNo[p.pin] = p; });
  const pins = sec.instances.pinCols.map(pc => {
    const net = pc.col < row.cells.length ? row.cells[pc.col] : '';
    return { pin: pc.pin, name: (pinByNo[pc.pin] || {}).name || '', net,
             globalCount: (netIndex[netKey(scope, net)] || []).length };
  });
  drawPinNetTopology(pins, row.ref + (row.value ? ' ' + row.value : ''), true);
}

function drawPinNetTopology(pins, title, showGlobal) {
  topoEmpty.style.display = 'none';
  const filterText = (topoFilter.value || '').trim().toLowerCase();
  const W = 318;
  const rowH = 24, top = 34;
  const H = top + pins.length * rowH + 16;
  topoSvg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  topoSvg.setAttribute('width', W);
  topoSvg.setAttribute('height', H);

  const t = svgEl('text', { x: 8, y: 18 });
  t.textContent = title;
  t.style.fill = '#58a6ff'; t.style.fontSize = '11px'; t.style.fontWeight = '700';
  topoSvg.appendChild(t);

  // 网络分组（本视图内 ≥2 脚才画轨道）
  const netGroups = {};
  pins.forEach((p, i) => {
    if (!isRealNet(p.net)) return;
    (netGroups[p.net] = netGroups[p.net] || []).push(i);
  });
  const railNets = Object.keys(netGroups).filter(n =>
    !isIgnoredNet(n) && netGroups[n].length >= 2 && (!filterText || n.toLowerCase().includes(filterText)));

  const pinX = 96, railX0 = 116, railStep = 13, labelX = 120;
  const posY = i => top + i * rowH + 8;

  // 引脚标签
  pins.forEach((p, i) => {
    const label = svgEl('text', { x: 4, y: posY(i) + 3, class: 'pin-label', 'data-net': p.net });
    let txt = String(p.pin || '');
    if (p.name) txt += ' ' + p.name;
    if (txt.length > 14) txt = txt.slice(0, 13) + '…';
    label.textContent = txt;
    topoSvg.appendChild(label);
  });

  // 网络轨道（每个网络一条独立竖轨，避免重叠）
  railNets.forEach((net, ni) => {
    const color = getNetColor(net);
    const idxs = netGroups[net];
    const railX = railX0 + (ni % 14) * railStep;
    const ys = idxs.map(posY);
    const minY = Math.min(...ys), maxY = Math.max(...ys);

    let d = '';
    idxs.forEach(i => { d += `M${pinX},${posY(i)} L${railX},${posY(i)} `; });
    d += `M${railX},${minY} L${railX},${maxY}`;
    const path = svgEl('path', { d, stroke: color, class: 'net-line', 'data-net': net });
    path.addEventListener('click', () => highlightNet(net));
    topoSvg.appendChild(path);

    idxs.forEach(i => {
      const c = svgEl('circle', { cx: pinX, cy: posY(i), r: 3.5, fill: color, stroke: '#0d1117', class: 'net-dot', 'data-net': net });
      c.addEventListener('click', () => highlightNet(net));
      topoSvg.appendChild(c);
    });

    const lb = svgEl('text', { x: railX + 4, y: minY - 3, class: 'net-label', 'data-net': net });
    lb.textContent = net.length > 20 ? net.slice(0, 18) + '…' : net;
    lb.addEventListener('click', () => highlightNet(net));
    topoSvg.appendChild(lb);
  });

  // 未上轨的引脚：显示网络名（灰色文字），带全局连接数
  pins.forEach((p, i) => {
    if (!p.net || railNets.includes(p.net)) return;
    if (filterText && !(p.net.toLowerCase().includes(filterText))) return;
    const color = isRealNet(p.net) ? getNetColor(p.net) : '#30363d';
    const c = svgEl('circle', { cx: pinX, cy: posY(i), r: 3, fill: color, stroke: '#0d1117', class: 'net-dot', 'data-net': p.net });
    topoSvg.appendChild(c);
    const lb = svgEl('text', { x: labelX, y: posY(i) + 3, class: 'net-label', 'data-net': p.net });
    let txt = p.net.length > 22 ? p.net.slice(0, 20) + '…' : p.net;
    if (showGlobal && p.globalCount > 1) txt += `  (+${p.globalCount - 1})`;
    lb.textContent = txt;
    if (isRealNet(p.net)) {
      lb.style.cursor = 'pointer';
      lb.addEventListener('click', e => showNetPopup(p.net, e));
    }
    topoSvg.appendChild(lb);
  });
}
topoFilter.addEventListener('input', renderTopology);

// ═══════════════════════════════════════════════════════════
//  高亮
// ═══════════════════════════════════════════════════════════

function highlightNet(net, transient) {
  if (!transient) selectedNet = net;
  document.querySelectorAll('#detail-content tr[data-net]').forEach(tr => {
    tr.classList.toggle('net-highlight', tr.dataset.net === net);
  });
  document.querySelectorAll('#detail-content td.net-cell').forEach(td => {
    td.style.background = (td.textContent.trim() === net) ? '#1f6feb33' : '';
  });
  document.querySelectorAll('#topo-svg .net-line, #topo-svg .net-label, #topo-svg .pin-label').forEach(el => {
    el.classList.toggle('highlight', el.getAttribute('data-net') === net);
  });
  document.querySelectorAll('#topo-svg .net-dot').forEach(dot => {
    dot.setAttribute('r', dot.getAttribute('data-net') === net ? 6 : 3.5);
  });
  document.querySelectorAll('.net-item').forEach(item => {
    item.classList.toggle('active', item.dataset.net === net);
  });
}
function clearHighlight() {
  selectedNet = null;
  document.querySelectorAll('#detail-content tr.net-highlight').forEach(tr => tr.classList.remove('net-highlight'));
  document.querySelectorAll('#detail-content td.net-cell').forEach(td => { td.style.background = ''; });
  document.querySelectorAll('#topo-svg .highlight').forEach(el => el.classList.remove('highlight'));
  document.querySelectorAll('#topo-svg .net-dot').forEach(dot => dot.setAttribute('r', 3.5));
  document.querySelectorAll('.net-item.active').forEach(i => i.classList.remove('active'));
}

// 跳转后聚焦：高亮网络 / 闪烁实例行
function applyFocus(focus) {
  if (focus.net) {
    highlightNet(focus.net);
    const row = document.querySelector(`#detail-content tr[data-net="${CSS.escape(focus.net)}"]`) ||
                Array.from(document.querySelectorAll('#detail-content td.net-cell')).find(td => td.textContent.trim() === focus.net);
    if (row) (row.closest ? (row.closest('tr') || row) : row).scrollIntoView({ block: 'center' });
  }
  if (focus.ref) {
    const tr = document.querySelector(`#detail-content tr[data-ref="${CSS.escape(focus.ref)}"]`);
    if (tr) { tr.scrollIntoView({ block: 'center' }); tr.classList.add('flash'); setTimeout(() => tr.classList.remove('flash'), 1700); }
  }
}

// ═══════════════════════════════════════════════════════════
//  悬停网络连线图浮窗
//  行 hover → 中心器件 → 网络 → 对端器件 的连线图（忽略 GND）
// ═══════════════════════════════════════════════════════════

const hoverTopo = $('hover-topo');
let htShowTimer = null, htHideTimer = null;

function isSmallRef(scope, ref) {
  const m = refMeta[scope + '::' + ref];
  if (m && m.pinCount) return m.pinCount <= 4;
  return /^(R|C|L|D|FB|FU|TP|F|Y|X)\d/i.test(ref);
}
function isEditingCell() {
  const a = document.activeElement;
  return a && a.classList && a.classList.contains('net-cell');
}

// 收集一行的图数据：centerRef 的若干引脚 → 各自网络 → 网络上其它引脚
function buildHoverGraph(centerRef, centerLabel, pins) {
  const scope = scopeOf(currentFile || '');
  const nets = [];
  for (const p of pins) {
    if (!isRealNet(p.net) || isIgnoredNet(p.net)) continue;
    let g = nets.find(n => n.net === p.net);
    if (!g) {
      const all = netIndex[netKey(scope, p.net)] || [];
      g = { net: p.net, color: getNetColor(p.net), pins: [], others: [], total: all.length };
      // 对端：去掉中心器件自己的引脚
      g.others = all.filter(e => e.ref !== centerRef);
      nets.push(g);
    }
    g.pins.push(p);
  }
  return { centerRef, centerLabel, scope, nets };
}

function renderHoverGraph(data) {
  const MAXF = 10;                       // 每个网络最多画的对端数
  const rowH = 20, netLabelH = 15, bandGap = 8, topPad = 26;
  const bands = data.nets.map(g => {
    const shown = g.others.slice(0, MAXF);
    const extra = g.others.length - shown.length;
    const rows = Math.max(1, shown.length + (extra > 0 ? 1 : 0));
    return { g, shown, extra, h: netLabelH + rows * rowH + 4 };
  });
  const H = topPad + bands.reduce((s, b) => s + b.h + bandGap, 0) + 2;
  const W = 470;
  const cx = 10, cw = 116;              // 中心器件框
  const hubX = 196, nodeX = 244;        // 网络汇点 / 对端列
  const centerSmall = isSmallRef(data.scope, data.centerRef);
  const cH = centerSmall ? 22 : 38;
  const cyCenter = topPad + (H - topPad) / 2 - 4;

  let s = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" style="display:block">`;
  // 标题
  s += `<text x="8" y="15" fill="#58a6ff" font-size="12" font-weight="700">${esc(data.centerLabel)}</text>`;

  // 先画边（在节点下层）
  let y = topPad;
  const hubYs = [];
  bands.forEach((b, bi) => {
    const rows = Math.max(1, b.shown.length + (b.extra > 0 ? 1 : 0));
    const hubY = y + netLabelH + (rows * rowH) / 2 - rowH / 2 + 10;
    hubYs.push(hubY);
    // 中心 → 汇点
    const srcY = cyCenter - (bands.length - 1) * 6 + bi * 12;
    s += `<path d="M${cx + cw},${srcY} C${cx + cw + 34},${srcY} ${hubX - 34},${hubY} ${hubX - 5},${hubY}" fill="none" stroke="${b.g.color}" stroke-width="1.6" opacity="0.8"/>`;
    // 引脚号标在边起点
    const pinTxt = b.g.pins.map(p => p.pin).join(',');
    s += `<text x="${cx + cw + 4}" y="${srcY - 4}" fill="#adbac7" font-size="9">脚${esc(pinTxt)}</text>`;
    // 汇点 → 各对端
    b.shown.forEach((e2, j) => {
      const ny = y + netLabelH + j * rowH + 10;
      s += `<path d="M${hubX + 5},${hubY} C${hubX + 26},${hubY} ${nodeX - 22},${ny} ${nodeX},${ny}" fill="none" stroke="${b.g.color}" stroke-width="1.1" opacity="0.55"/>`;
    });
    if (b.extra > 0) {
      const ny = y + netLabelH + b.shown.length * rowH + 10;
      s += `<path d="M${hubX + 5},${hubY} C${hubX + 26},${hubY} ${nodeX - 22},${ny} ${nodeX},${ny}" fill="none" stroke="${b.g.color}" stroke-width="1.1" opacity="0.3" stroke-dasharray="3 3"/>`;
    }
    y += b.h + bandGap;
  });

  // 中心器件节点
  s += `<rect x="${cx}" y="${cyCenter - cH / 2}" width="${cw}" height="${cH}" rx="5" fill="#1f6feb22" stroke="#58a6ff" stroke-width="1.3"/>`;
  if (centerSmall) {
    s += `<text x="${cx + cw / 2}" y="${cyCenter + 4}" fill="#f0c674" font-size="11" font-weight="700" text-anchor="middle">${esc(data.centerRef)}</text>`;
  } else {
    s += `<text x="${cx + cw / 2}" y="${cyCenter - 3}" fill="#f0c674" font-size="12" font-weight="700" text-anchor="middle">${esc(data.centerRef)}</text>`;
    let nm = data.centerLabel.replace(data.centerRef, '').trim();
    if (nm.length > 16) nm = nm.slice(0, 15) + '…';
    if (nm) s += `<text x="${cx + cw / 2}" y="${cyCenter + 12}" fill="#6e7681" font-size="9" text-anchor="middle">${esc(nm)}</text>`;
  }

  // 网络汇点 + 对端节点
  y = topPad;
  bands.forEach((b, bi) => {
    const g = b.g;
    const hubY = hubYs[bi];
    // 网络名标签
    let netTxt = g.net.length > 26 ? g.net.slice(0, 24) + '…' : g.net;
    s += `<text x="${hubX}" y="${y + 9}" fill="${g.color}" font-size="10" font-weight="600" text-anchor="middle">${esc(netTxt)} · ${g.total}脚</text>`;
    s += `<circle cx="${hubX}" cy="${hubY}" r="4" fill="${g.color}" stroke="#0d1117" stroke-width="1.5"/>`;
    // 对端节点
    b.shown.forEach((e2, j) => {
      const ny = y + netLabelH + j * rowH + 10;
      const small = isSmallRef(data.scope, e2.ref);
      const meta = refMeta[data.scope + '::' + e2.ref] || {};
      if (small) {
        const label = `${e2.ref}.${e2.pin}`;
        const w = Math.max(34, label.length * 5.6 + 10);
        s += `<rect x="${nodeX}" y="${ny - 7.5}" width="${w}" height="15" rx="7" fill="#21262d" stroke="#30363d"/>`;
        s += `<text x="${nodeX + w / 2}" y="${ny + 3}" fill="#adbac7" font-size="9" text-anchor="middle">${esc(label)}</text>`;
        let vv = e2.value || meta.value || '';
        if (vv.length > 12) vv = vv.slice(0, 11) + '…';
        if (vv) s += `<text x="${nodeX + w + 5}" y="${ny + 3}" fill="#6e7681" font-size="9">${esc(vv)}</text>`;
      } else {
        let label = `${e2.ref}.${e2.pin}`;
        if (e2.pinName) label += ' ' + e2.pinName;
        if (label.length > 20) label = label.slice(0, 19) + '…';
        const w = Math.max(60, label.length * 6.4 + 12);
        s += `<rect x="${nodeX}" y="${ny - 9}" width="${w}" height="18" rx="4" fill="#1c2129" stroke="#58a6ff66" stroke-width="1"/>`;
        s += `<text x="${nodeX + 6}" y="${ny + 3.5}" fill="#c9d1d9" font-size="10">${esc(label)}</text>`;
        let vv = e2.value || (refMeta[data.scope + '::' + e2.ref] || {}).value || '';
        if (vv && vv !== e2.ref) {
          if (vv.length > 14) vv = vv.slice(0, 13) + '…';
          s += `<text x="${nodeX + w + 5}" y="${ny + 3.5}" fill="#6e7681" font-size="9">${esc(vv)}</text>`;
        }
      }
    });
    if (b.extra > 0) {
      const ny = y + netLabelH + b.shown.length * rowH + 10;
      s += `<text x="${nodeX}" y="${ny + 3}" fill="#6e7681" font-size="10" font-style="italic">… 还有 ${b.extra} 个引脚</text>`;
    }
    y += b.h + bandGap;
  });

  s += '</svg>';
  hoverTopo.innerHTML = s + '<div class="ht-hint">已忽略 GND / 未连接</div>';
}

function showHoverTopo(build, evt) {
  clearTimeout(htShowTimer); clearTimeout(htHideTimer);
  if (isEditingCell()) return;
  htShowTimer = setTimeout(() => {
    const data = build();
    if (!data.nets.length) { hideHoverTopo(); return; }
    renderHoverGraph(data);
    hoverTopo.style.display = 'block';
    const rect = hoverTopo.getBoundingClientRect();
    let x = evt.clientX + 18, y2 = evt.clientY + 16;
    if (x + rect.width > window.innerWidth - 8) x = Math.max(8, evt.clientX - rect.width - 14);
    if (y2 + rect.height > window.innerHeight - 8) y2 = Math.max(50, window.innerHeight - rect.height - 8);
    hoverTopo.style.left = x + 'px';
    hoverTopo.style.top = y2 + 'px';
  }, 200);
}
function hideHoverTopo() {
  clearTimeout(htShowTimer);
  hoverTopo.style.display = 'none';
}
function hideHoverTopoSoon() {
  clearTimeout(htShowTimer);
  clearTimeout(htHideTimer);
  htHideTimer = setTimeout(hideHoverTopo, 120);
}
// 滚动/点击时收起，避免位置漂移或压住弹窗
document.getElementById('detail-panel').addEventListener('scroll', hideHoverTopo);
document.addEventListener('mousedown', hideHoverTopo);

// ═══════════════════════════════════════════════════════════
//  邻域原理图（全屏 overlay）
//  中心矩形符号（方位/功能组驱动）· 电源地符号化 · 少脚网直连/多脚网标签旗 · 点邻居漫游
// ═══════════════════════════════════════════════════════════

const schOverlay = $('schematic-overlay');
const schSvg = $('sch-svg');
const schWrap = $('sch-canvas-wrap');
let schHistory = [];          // 漫游历史（返回用）
let schCurrent = null;        // {scope, ref}
let schViewBox = null;        // {x,y,w,h}

const SCH = { pitch: 26, stub: 26, maxDirect: 5 };

// 电源网络判定（取层次名最后一段）：'gnd' / 'pwr' / null
function powerKind(net) {
  if (!isRealNet(net)) return null;
  const seg = net.split('/').pop();
  if (net === 'GND' || /^GND/i.test(seg) || /^VSS/i.test(seg)) return 'gnd';
  if (/^\+/.test(seg) || /^(VCC|VDD|VBUS|VSYS|VBAT|VEE)/i.test(seg)) return 'pwr';
  return null;
}
function defaultSideOf(type) {
  if (type === 'input') return 'L';
  if (type === 'output') return 'R';
  if (type === 'power') return 'T';
  if (type === 'ground') return 'B';
  return 'R';
}

// ref → 中心对象（漫游定位）
function resolveCenter(scope, ref) {
  const loc = refLoc[scope + '::' + ref];
  if (!loc || !docs[loc.file]) return null;
  const doc = docs[loc.file];
  if (loc.kind === 'device') {
    const sec = doc.sections.find(s => s.kind === 'device' && s.ref === ref) ||
                doc.sections.find(s => !s.instances && sectionPins(s).length);
    if (!sec) return null;
    return { kind: 'device', scope, ref, label: sectionLabel(sec, doc), file: loc.file, sec };
  }
  for (const sec of doc.sections) {
    if (!sec.instances) continue;
    const row = sec.instances.rows.find(r => r.ref === ref);
    if (row) return { kind: 'instance', scope, ref, label: ref + (row.value ? ' ' + row.value : ''), file: loc.file, sec, row };
  }
  return null;
}

// 中心引脚列表（实例继承类模板的 名称/类型/方位/功能组；方位=隐 不画）
function schCenterPins(center) {
  if (center.kind === 'device') {
    return sectionPins(center.sec).filter(p => p.side !== '隐')
      .map(p => ({ pin: p.pin, name: p.name, type: p.type, side: p.side, group: p.group, net: p.net }));
  }
  const tpl = {};
  sectionPins(center.sec).forEach(p => { tpl[p.pin] = p; });
  return center.sec.instances.pinCols.map(pc => {
    const t = tpl[pc.pin] || {};
    return { pin: pc.pin, name: t.name || '', type: t.type || '', side: t.side || '', group: t.group || '',
             net: pc.col < center.row.cells.length ? center.row.cells[pc.col] : '' };
  }).filter(p => p.side !== '隐');
}

function openSchematic(center, pushHist) {
  if (!center) return;
  if (pushHist && schCurrent) schHistory.push(schCurrent);
  schCurrent = { scope: center.scope, ref: center.ref };
  $('sch-back').disabled = !schHistory.length;
  schOverlay.style.display = 'flex';
  schRender(center);
}
function openSchematicByRef(scope, ref, pushHist) {
  const c = resolveCenter(scope, ref);
  if (!c) { setStatus('未找到器件 ' + ref, true); return; }
  openSchematic(c, pushHist);
}

// —— 渲染 ——
function schRender(center) {
  const scope = center.scope;
  const pins = schCenterPins(center);
  $('sch-title').textContent = '◫ ' + center.label;
  $('sch-sub').textContent = `${center.file} · ${pins.length} 引脚 · 邻域 1 跳`;

  // 1) 分边（方位列优先；无方位按类型；小器件左右均分）
  const sideMap = { '左': 'L', '右': 'R', '上': 'T', '下': 'B' };
  const noSides = pins.every(p => !p.side);
  const sides = { L: [], R: [], T: [], B: [] };
  // 无方位标注时：电源/地归上下，其余信号脚按序左右均分（类型有 in/out 的仍按类型）
  const hasIO = pins.some(p => p.type === 'input' || p.type === 'output');
  const signalPins = pins.filter(p => !powerKind(p.net) && p.type !== 'power' && p.type !== 'ground');
  const halfAt = Math.ceil(signalPins.length / 2);
  let sigIdx = 0;
  pins.forEach(p => {
    let s = sideMap[p.side];
    if (!s) {
      const pk = powerKind(p.net);
      if (pk === 'gnd') s = 'B';
      else if (pk === 'pwr') s = 'T';
      else if (p.type === 'power') s = 'T';
      else if (p.type === 'ground') s = 'B';
      else if (noSides && !hasIO) { s = sigIdx < halfAt ? 'L' : 'R'; sigIdx++; }
      else s = defaultSideOf(p.type);
    }
    sides[s].push(p);
  });

  // 2) 边内按功能组聚拢（组首次出现序稳定）
  function grouped(arr) {
    const order = [], buckets = {};
    arr.forEach(p => { const g = p.group || ''; if (!(g in buckets)) { buckets[g] = []; order.push(g); } buckets[g].push(p); });
    return order.map(g => ({ group: g, pins: buckets[g] }));
  }
  // 槽位展开：[{kind:'pin',p,off} | {kind:'glabel',text,off}]，返回总槽数
  function placeSlots(gs) {
    const items = []; let off = 0;
    gs.forEach((g, gi) => {
      if (gi > 0) off += 0.5;
      if (g.group) { items.push({ kind: 'glabel', text: g.group, off }); off += 0.6; }  // 组标占独立槽位，不压引脚名
      g.pins.forEach(p => { items.push({ kind: 'pin', p, off }); off += 1; });
    });
    return { items, total: Math.max(off, 1) };
  }
  const L = placeSlots(grouped(sides.L)), R = placeSlots(grouped(sides.R));
  const T = placeSlots(grouped(sides.T)), B = placeSlots(grouped(sides.B));

  // 3) 中心框几何（框内标注 = 名称，缺省回退引脚号）
  const small = pins.length <= 4 && noSides;
  const inLabel = p => p.name || p.pin || '';
  const nameW = Math.max(0, ...pins.map(p => inLabel(p).length)) * 6.5;
  const boxW = Math.max(small ? 80 : 150, nameW * 2 + 50, Math.max(T.total, B.total) * SCH.pitch + 30, center.ref.length * 9 + 20);
  const boxH = Math.max(L.total, R.total) * SCH.pitch + (small ? 20 : 36);
  const yPin = off => 20 + off * SCH.pitch;                    // L/R 槽位 → y
  const xPin = (slots, off) => boxW / 2 - (slots.total * SCH.pitch) / 2 + off * SCH.pitch + SCH.pitch / 2;  // T/B 槽位 → x

  // 4) 网络分档
  const netInfo = {};
  pins.forEach(p => {
    if (!isRealNet(p.net)) { p.mode = 'nc'; return; }
    const pk = powerKind(p.net);
    if (pk) { p.mode = pk; return; }
    if (!netInfo[p.net]) {
      const all = netIndex[netKey(scope, p.net)] || [];
      const others = all.filter(e => e.ref !== center.ref);
      netInfo[p.net] = { others, total: all.length, color: getNetColor(p.net),
                         mode: (others.length > 0 && others.length <= SCH.maxDirect) ? 'wire' : 'label' };
    }
    p.mode = netInfo[p.net].mode;
  });

  let s = '';
  const t = (x, y, txt, fill, size, anchor, extra) =>
    `<text x="${x}" y="${y}" fill="${fill}" font-size="${size}" text-anchor="${anchor || 'start'}" ${extra || ''}>${esc(txt)}</text>`;

  // 5) 中心框 + 引脚名/号/组标
  s += `<rect x="0" y="0" width="${boxW}" height="${boxH}" rx="6" fill="#161b22" stroke="#58a6ff" stroke-width="1.6"/>`;
  s += t(boxW / 2, -26, center.kind === 'instance' ? (center.label.replace(center.ref, '').trim()) : center.label.replace(center.ref, '').trim(), '#6e7681', 10, 'middle');
  s += t(boxW / 2, -10, center.ref, '#f0c674', 13, 'middle', 'font-weight="700"');
  // 左右边：引脚名在框内、文字垂直中心对齐管脚、以框边线为基准左/右对齐；
  // 名称缺省时框内画引脚号（此时线桩外不再重复画号）
  [[L, 'L'], [R, 'R']].forEach(([S2, sd]) => {
    S2.items.forEach(it => {
      const y = yPin(it.off);
      if (it.kind === 'glabel') { s += t(sd === 'L' ? 6 : boxW - 6, y + 6, it.text, '#d29922', 8, sd === 'L' ? 'start' : 'end', 'font-style="italic"'); return; }
      const p = it.p;
      s += `<line x1="${sd === 'L' ? -SCH.stub : boxW}" y1="${y}" x2="${sd === 'L' ? 0 : boxW + SCH.stub}" y2="${y}" stroke="#8b949e" stroke-width="1.4"/>`;
      const nm = inLabel(p);
      if (p.name) s += t(sd === 'L' ? -5 : boxW + 5, y - 4, p.pin, '#6e7681', 8, sd === 'L' ? 'end' : 'start');
      if (nm && !small) s += t(sd === 'L' ? 6 : boxW - 6, y, nm, '#adbac7', 9.5, sd === 'L' ? 'start' : 'end', 'dominant-baseline="central"');
    });
  });
  // 上下边：引脚名竖排在框内（旋转90°），起笔贴框边线，水平中心对齐管脚
  [[T, 'T'], [B, 'B']].forEach(([S2, sd]) => {
    S2.items.forEach(it => {
      if (it.kind !== 'pin') return;
      const x = xPin(S2, it.off), p = it.p;
      s += `<line x1="${x}" y1="${sd === 'T' ? -SCH.stub : boxH}" x2="${x}" y2="${sd === 'T' ? 0 : boxH + SCH.stub}" stroke="#8b949e" stroke-width="1.4"/>`;
      const nm = inLabel(p);
      if (p.name) s += t(x + 3, sd === 'T' ? -4 : boxH + 10, p.pin, '#6e7681', 8);
      if (nm && !small) {
        const nmT = nm.length > 14 ? nm.slice(0, 13) + '…' : nm;
        if (sd === 'T')
          s += `<text transform="translate(${x},6) rotate(90)" fill="#adbac7" font-size="9.5" text-anchor="start" dominant-baseline="central">${esc(nmT)}</text>`;
        else
          s += `<text transform="translate(${x},${boxH - 6}) rotate(90)" fill="#adbac7" font-size="9.5" text-anchor="end" dominant-baseline="central">${esc(nmT)}</text>`;
      }
    });
  });

  // 6) 端点绘制：电源/地符号、NC、标签旗、直连邻居
  const wireNetsBySide = { L: [], R: [] };   // [{net, pins:[{y}]}]
  function endpointAt(p, sd, x, y) {
    if (p.mode === 'gnd') {
      if (sd === 'B' || sd === 'T') {
        const dir = sd === 'B' ? 1 : -1, gy = sd === 'B' ? boxH + SCH.stub : -SCH.stub;
        s += `<line x1="${x - 8}" y1="${gy}" x2="${x + 8}" y2="${gy}" stroke="#8b949e" stroke-width="1.6"/>` +
             `<line x1="${x - 5}" y1="${gy + 4 * dir}" x2="${x + 5}" y2="${gy + 4 * dir}" stroke="#8b949e" stroke-width="1.4"/>` +
             `<line x1="${x - 2}" y1="${gy + 8 * dir}" x2="${x + 2}" y2="${gy + 8 * dir}" stroke="#8b949e" stroke-width="1.2"/>`;
        if (p.net !== 'GND') s += t(x + 12, gy + 8 * dir, p.net.split('/').pop(), '#6e7681', 8);
      } else {
        s += `<g class="sch-netflag" data-net="${esc(p.net)}">` + t(x + (sd === 'L' ? -4 : 4), y + 3.5, '⏚ ' + (p.net === 'GND' ? '' : p.net), '#8b949e', 9, sd === 'L' ? 'end' : 'start') + '</g>';
      }
      return;
    }
    if (p.mode === 'pwr') {
      if (sd === 'T' || sd === 'B') {
        const py2 = sd === 'T' ? -SCH.stub : boxH + SCH.stub, dir = sd === 'T' ? -1 : 1;
        s += `<line x1="${x - 8}" y1="${py2}" x2="${x + 8}" y2="${py2}" stroke="#d29922" stroke-width="1.8"/>`;
        // 相邻电源标签交错两档高度，避免互相重叠
        const stag = (p._tbIdx % 2) * 12;
        s += `<g class="sch-netflag" data-net="${esc(p.net)}">` + t(x, py2 + dir * (10 + stag) + (dir < 0 ? 0 : 4), p.net.split('/').pop(), '#d29922', 9, 'middle', 'font-weight="600"') + '</g>';
      } else {
        s += `<g class="sch-netflag" data-net="${esc(p.net)}">` + t(x + (sd === 'L' ? -4 : 4), y + 3.5, p.net.split('/').pop(), '#d29922', 9, sd === 'L' ? 'end' : 'start', 'font-weight="600"') + '</g>';
      }
      return;
    }
    if (p.mode === 'nc') {
      s += t(x + (sd === 'L' ? -4 : sd === 'R' ? 4 : 3), y + 3.5, '✕', '#484f58', 8, sd === 'L' ? 'end' : 'start');
      return;
    }
    if (p.mode === 'label') {
      const info = netInfo[p.net];
      const nm = p.net.length > 30 ? p.net.slice(0, 28) + '…' : p.net;
      const txt = `${nm}  (${info.total}脚)`;
      s += `<g class="sch-netflag" data-net="${esc(p.net)}">` +
        `<circle cx="${x}" cy="${y}" r="2.5" fill="${info.color}"/>` +
        t(x + (sd === 'L' ? -6 : 6), y + 3.5, txt, info.color, 9, sd === 'L' ? 'end' : 'start') + '</g>';
      return;
    }
    if (p.mode === 'wire' && (sd === 'L' || sd === 'R')) {
      let ent = wireNetsBySide[sd].find(w => w.net === p.net);
      if (!ent) { ent = { net: p.net, ys: [] }; wireNetsBySide[sd].push(ent); }
      ent.ys.push(y);
    } else if (p.mode === 'wire') {
      // 上下边的直连网退化为标签旗
      const info = netInfo[p.net];
      s += `<g class="sch-netflag" data-net="${esc(p.net)}">` + t(x + 3, (sd === 'T' ? -SCH.stub - 4 : boxH + SCH.stub + 10), p.net, info.color, 9) + '</g>';
    }
  }
  L.items.forEach(it => { if (it.kind === 'pin') endpointAt(it.p, 'L', -SCH.stub, yPin(it.off)); });
  R.items.forEach(it => { if (it.kind === 'pin') endpointAt(it.p, 'R', boxW + SCH.stub, yPin(it.off)); });
  let tbIdx = 0;
  T.items.forEach(it => { if (it.kind === 'pin') { it.p._tbIdx = tbIdx++; endpointAt(it.p, 'T', xPin(T, it.off), 0); } });
  tbIdx = 0;
  B.items.forEach(it => { if (it.kind === 'pin') { it.p._tbIdx = tbIdx++; endpointAt(it.p, 'B', xPin(B, it.off), 0); } });

  // 7) 直连邻居：每侧每网一条竖干线，邻居盒按引脚 y 就近堆叠
  const NB_W = 120, NB_H = 32;
  ['L', 'R'].forEach(sd => {
    const occ = [];
    wireNetsBySide[sd].forEach((w, wi) => {
      const info = netInfo[w.net];
      const dir = sd === 'L' ? -1 : 1;
      const x0 = sd === 'L' ? -SCH.stub : boxW + SCH.stub;
      const tx = x0 + dir * (18 + (wi % 10) * 13);
      const nbEdge = x0 + dir * 160;
      const byRef = {};
      info.others.forEach(e => { (byRef[e.ref] = byRef[e.ref] || []).push(e); });
      const refs = Object.keys(byRef);
      // 干线范围 = 引脚 y 与邻居 y 的跨度
      const nys = [];
      refs.forEach((r2, j) => {
        let want = w.ys[Math.min(j, w.ys.length - 1)] + (j - (refs.length - 1) / 2) * (NB_H + 8);
        let ny = want;
        while (occ.some(o => Math.abs(o - ny) < NB_H + 6)) ny += NB_H + 8;
        occ.push(ny); nys.push(ny);
      });
      const allY = w.ys.concat(nys);
      s += `<path class="sch-wire" stroke="${info.color}" d="M${tx},${Math.min(...allY)} V${Math.max(...allY)}"/>`;
      w.ys.forEach(y => { s += `<path class="sch-wire" stroke="${info.color}" d="M${x0},${y} H${tx}"/>`; });
      const junctions = allY.length > 2;
      refs.forEach((r2, j) => {
        const ny = nys[j], entries = byRef[r2];
        s += `<path class="sch-wire" stroke="${info.color}" d="M${tx},${ny} H${nbEdge}"/>`;
        if (junctions && j < refs.length - 1) s += `<circle cx="${tx}" cy="${ny}" r="2.6" fill="${info.color}"/>`;
        const bx2 = sd === 'L' ? nbEdge - NB_W : nbEdge;
        const meta = refMeta[scope + '::' + r2] || {};
        const smallN = isSmallRef(scope, r2);
        let vv = String(entries[0].value || meta.value || '').slice(0, 18);
        if (vv === r2) vv = '';   // value 与编号相同不重复显示
        s += `<g class="sch-neigh" data-ref="${esc(r2)}">` +
          `<rect class="sch-neigh-box" x="${bx2}" y="${ny - NB_H / 2}" width="${NB_W}" height="${NB_H}" rx="${smallN ? 12 : 4}" fill="#1c2129" stroke="#30363d" stroke-width="1.2"/>` +
          t(bx2 + NB_W / 2, ny + (vv ? -2 : 4), r2, '#f0c674', 11, 'middle', 'font-weight="700"') +
          (vv ? t(bx2 + NB_W / 2, ny + 10, vv, '#6e7681', 8, 'middle') : '') +
          t(sd === 'L' ? nbEdge + 3 : nbEdge - 3, ny - 5, '脚' + entries.map(e => e.pin).join(','), '#8b949e', 8, sd === 'L' ? 'start' : 'end') +
          '</g>';
      });
      // 网名标在干线顶端（相邻干线标签交错三档高度，长名截断）
      let nm = w.net.length > 28 ? w.net.slice(0, 26) + '…' : w.net;
      s += `<g class="sch-netflag" data-net="${esc(w.net)}">` + t(tx + 3 * dir, Math.min(...allY) - 5 - (wi % 3) * 9, nm, info.color, 8.5, sd === 'L' ? 'end' : 'start') + '</g>';
    });
  });

  schSvg.innerHTML = s;
  schFit();
}

// —— 视图控制 ——
function schApplyView() {
  if (schViewBox) schSvg.setAttribute('viewBox', `${schViewBox.x} ${schViewBox.y} ${schViewBox.w} ${schViewBox.h}`);
}
function schFit() {
  try {
    const bb = schSvg.getBBox();
    const m = 60;
    const vw = schWrap.clientWidth || 800, vh = schWrap.clientHeight || 600;
    let w = bb.width + m * 2, h = bb.height + m * 2;
    const ar = vw / vh;
    if (w / h < ar) w = h * ar; else h = w / ar;
    schViewBox = { x: bb.x + bb.width / 2 - w / 2, y: bb.y + bb.height / 2 - h / 2, w, h };
    schApplyView();
  } catch (e) { /* 空图 */ }
}
$('sch-fit').addEventListener('click', schFit);
$('sch-close').addEventListener('click', () => { schOverlay.style.display = 'none'; schHistory = []; schCurrent = null; });
$('sch-back').addEventListener('click', () => {
  const prev = schHistory.pop();
  if (prev) { schCurrent = null; openSchematicByRef(prev.scope, prev.ref, false); schCurrent = prev; }
  $('sch-back').disabled = !schHistory.length;
});

// 平移 / 缩放
let schPanning = null;
schWrap.addEventListener('mousedown', e => {
  if (e.target.closest('.sch-neigh') || e.target.closest('.sch-netflag')) return;
  schPanning = { x: e.clientX, y: e.clientY, vb: { ...schViewBox } };
  schWrap.classList.add('panning');
});
window.addEventListener('mousemove', e => {
  if (!schPanning || !schViewBox) return;
  const r = schSvg.getBoundingClientRect();
  schViewBox.x = schPanning.vb.x - (e.clientX - schPanning.x) * schViewBox.w / r.width;
  schViewBox.y = schPanning.vb.y - (e.clientY - schPanning.y) * schViewBox.h / r.height;
  schApplyView();
});
window.addEventListener('mouseup', () => { schPanning = null; schWrap.classList.remove('panning'); });
schWrap.addEventListener('wheel', e => {
  if (!schViewBox) return;
  e.preventDefault();
  const r = schSvg.getBoundingClientRect();
  const px = schViewBox.x + (e.clientX - r.left) / r.width * schViewBox.w;
  const py = schViewBox.y + (e.clientY - r.top) / r.height * schViewBox.h;
  const f = e.deltaY > 0 ? 1.15 : 1 / 1.15;
  schViewBox = { x: px - (px - schViewBox.x) * f, y: py - (py - schViewBox.y) * f, w: schViewBox.w * f, h: schViewBox.h * f };
  schApplyView();
}, { passive: false });

// 画布内点击：邻居漫游 / 网络弹窗
schSvg.addEventListener('click', e => {
  const nb = e.target.closest('.sch-neigh');
  if (nb) { openSchematicByRef(schCurrent.scope, nb.getAttribute('data-ref'), true); return; }
  const fl = e.target.closest('.sch-netflag');
  if (fl) showNetPopup(fl.getAttribute('data-net'), e, schCurrent.scope);
});

// ═══════════════════════════════════════════════════════════
//  网络详情悬浮面板（全局视角，可跳转）
// ═══════════════════════════════════════════════════════════

function showNetPopup(net, event, scopeOverride) {
  const scope = scopeOverride != null ? scopeOverride : scopeOf(currentFile || '');
  const entries = netIndex[netKey(scope, net)] || [];
  netPopupTitle.textContent = net;

  let html = `<div style="color:#6e7681;font-size:11px;margin-bottom:4px;">` +
    `${entries.length} 个引脚连接` + (scope ? `（作用域 ${esc(scope)}/）` : '') + `</div>`;

  const byFile = {};
  entries.forEach(e => { (byFile[e.file] = byFile[e.file] || []).push(e); });
  for (const [file, list] of Object.entries(byFile)) {
    html += `<div class="np-file">📄 ${esc(file)}</div>`;
    list.forEach(e => {
      html += `<div class="np-entry" data-file="${esc(file)}" data-ref="${esc(e.ref)}">` +
        `<span class="ref">${esc(e.ref)}</span>` +
        `<span class="pin">脚${esc(e.pin)}${e.pinName ? ' ' + esc(e.pinName) : ''}</span>` +
        `<span class="extra">${esc(e.value || '')}${e.type ? ' · ' + esc(e.type) : ''}</span></div>`;
    });
  }
  if (!entries.length) html += '<div class="empty-hint">该网络在当前作用域没有已索引的连接</div>';

  netPopupBody.innerHTML = html;
  netPopupBody.querySelectorAll('.np-entry').forEach(el => {
    el.addEventListener('click', () => {
      netPopup.style.display = 'none';
      const file = el.dataset.file;
      if (file === currentFile) applyFocus({ net, ref: el.dataset.ref });
      else selectFile(file, { net, ref: el.dataset.ref });
    });
  });

  netPopup.style.display = 'flex';
  const x = event ? event.clientX : window.innerWidth / 2;
  const y = event ? event.clientY : window.innerHeight / 2;
  netPopup.style.left = Math.max(8, Math.min(x, window.innerWidth - 480)) + 'px';
  netPopup.style.top = Math.max(50, Math.min(y, window.innerHeight - 420)) + 'px';
  highlightNet(net);
}

$('net-popup-close').addEventListener('click', () => { netPopup.style.display = 'none'; });
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (netPopup.style.display === 'flex') { netPopup.style.display = 'none'; return; }
    if (schOverlay.style.display === 'flex') { schOverlay.style.display = 'none'; schHistory = []; schCurrent = null; return; }
    $('dir-picker').style.display = 'none';
  }
});
document.addEventListener('click', e => {
  if (netPopup.style.display === 'flex' && !netPopup.contains(e.target) &&
      !e.target.closest('.net-item') && !(e.target.classList && e.target.classList.contains('net-cell'))) {
    netPopup.style.display = 'none';
  }
});

// ═══════════════════════════════════════════════════════════
//  目录选择面板（打开磁盘任意目录）
// ═══════════════════════════════════════════════════════════

const dirPicker = $('dir-picker');
const dirInput = $('dir-input');
const dirList = $('dir-list');
const dirBreadcrumb = $('dir-breadcrumb');

function normDir(p) { return String(p).replace(/\\/g, '/'); }

$('open-dir-btn').addEventListener('click', () => {
  dirPicker.style.display = 'block';
  if (!browsingDir) {
    const root = (window.__QEVOS__ && window.__QEVOS__.root) || (window.qevos && qevos.root) || '';
    browsingDir = root ? normDir(root) : '/';
  }
  dirInput.value = browsingDir;
  loadDirList(browsingDir);
});
$('dir-picker-close').addEventListener('click', () => { dirPicker.style.display = 'none'; });
$('dir-go-btn').addEventListener('click', async () => {
  const dirPath = dirInput.value.trim();
  if (!dirPath) return;
  dirPicker.style.display = 'none';
  await openDirectory(dirPath);
});
dirInput.addEventListener('keydown', e => { if (e.key === 'Enter') $('dir-go-btn').click(); });

async function loadDirList(dir) {
  dir = normDir(dir);
  browsingDir = dir;
  dirInput.value = dir;
  updateBreadcrumb(dir);
  dirList.innerHTML = '<p style="color:#6e7681;font-size:12px;">加载中...</p>';
  try {
    const resp = await fetch(`/api/app-files/${APP_ID}?root=${encodeURIComponent(dir)}&dir=.`);
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    const files = data.files || [];
    const topDirs = files.filter(f => f.type === 'dir' && !f.path.includes('/') && !f.path.startsWith('.'));
    const mdCount = files.filter(f => f.type === 'file' && f.path.endsWith('.md')).length;

    dirList.innerHTML = '';

    // 「打开此目录」
    const openHere = document.createElement('div');
    openHere.style.cssText = 'padding:8px 12px;cursor:pointer;border:1px solid #1f6feb;border-radius:6px;margin-bottom:10px;background:#1f6feb22;';
    openHere.innerHTML = `<span style="color:#58a6ff;font-weight:600">✓ 打开此目录</span> <span style="color:#6e7681;font-size:11px">（含 ${mdCount} 个 .md）</span>`;
    openHere.addEventListener('click', async () => { dirPicker.style.display = 'none'; await openDirectory(dir); });
    dirList.appendChild(openHere);

    // 上级目录
    const parent = dir.replace(/\/+$/, '').split('/').slice(0, -1).join('/');
    if (parent && parent !== dir) {
      const up = document.createElement('div');
      up.style.cssText = 'padding:6px 12px;cursor:pointer;border:1px solid #30363d;border-radius:6px;margin-bottom:4px;color:#6e7681;';
      up.textContent = '⬆ ..';
      up.addEventListener('click', () => loadDirList(parent || '/'));
      dirList.appendChild(up);
    }

    topDirs.forEach(d => {
      const item = document.createElement('div');
      item.style.cssText = 'padding:8px 12px;cursor:pointer;border:1px solid #30363d;border-radius:6px;margin-bottom:4px;';
      item.onmouseover = () => item.style.borderColor = '#58a6ff';
      item.onmouseout = () => item.style.borderColor = '#30363d';
      item.innerHTML = `<span style="color:#58a6ff">📂</span> <span style="color:#c9d1d9">${esc(d.path)}</span>`;
      item.addEventListener('click', () => loadDirList(dir.replace(/\/+$/, '') + '/' + d.path));
      dirList.appendChild(item);
    });
    if (!topDirs.length) {
      const p = document.createElement('p');
      p.style.cssText = 'color:#6e7681;font-size:12px;padding:4px 0;';
      p.textContent = '无子目录';
      dirList.appendChild(p);
    }
  } catch (e) {
    dirList.innerHTML = `<p style="color:#f85149;font-size:12px;">加载失败: ${esc(e.message)}</p>`;
  }
}

function updateBreadcrumb(dir) {
  const parts = dir.split('/').filter(Boolean);
  dirBreadcrumb.innerHTML = '';
  const rootLink = document.createElement('span');
  rootLink.textContent = '/';
  rootLink.style.cssText = 'cursor:pointer;color:#58a6ff;';
  rootLink.addEventListener('click', () => loadDirList('/'));
  dirBreadcrumb.appendChild(rootLink);
  let path = '';
  parts.forEach(part => {
    path += '/' + part;
    const sep = document.createElement('span');
    sep.textContent = '/';
    sep.style.color = '#6e7681';
    dirBreadcrumb.appendChild(sep);
    const link = document.createElement('span');
    link.textContent = part;
    link.style.cssText = 'cursor:pointer;color:#58a6ff;';
    const p = path;
    link.addEventListener('click', () => loadDirList(p));
    dirBreadcrumb.appendChild(link);
  });
}

async function openDirectory(dirPath) {
  try {
    setStatus('正在打开目录...');
    const resp = await fetch(`/api/app-project?root=${encodeURIComponent(dirPath)}`);
    const data = await resp.json();
    if (data.error) { setStatus('目录无效: ' + data.error, true); return; }
    customRoot = data.root;
    currentProject = '.';
    projectSelect.innerHTML = '';
    const opt = document.createElement('option');
    opt.value = '.';
    opt.textContent = '📂 ' + data.root;
    projectSelect.appendChild(opt);
    await loadProject();
  } catch (e) {
    setStatus('打开目录失败: ' + e.message, true);
  }
}

// ═══════════════════════════════════════════════════════════
//  事件绑定
// ═══════════════════════════════════════════════════════════

projectSelect.addEventListener('change', async () => {
  const val = projectSelect.value;
  if (!val) return;
  customRoot = null;
  currentProject = val;
  await loadProject();
});

fileSelect.addEventListener('change', () => {
  if (fileSelect.value) selectFile(fileSelect.value);
});

$('reload-btn').addEventListener('click', () => loadProject());

// 外部变更 → 重载该文件（多面板/Agent 写入同步）
qevos.onPush(async msg => {
  if (msg.type !== 'file-changed' || !msg.path || !msg.path.endsWith('.md')) return;
  if (Date.now() - selfWriteAt < 1200) return;   // 自己刚写的，跳过
  if (!docs[msg.path] && !fileList.some(f => f.path === msg.path)) return;
  try {
    setStatus('检测到外部修改，重载 ' + msg.path);
    if (msg.deleted) { delete docs[msg.path]; fileList = fileList.filter(f => f.path !== msg.path); }
    else {
      const text = await readFileRel(msg.path);
      if (typeof text === 'string') docs[msg.path] = parseDoc(text);
    }
    rebuildIndex();
    renderFileTree(); renderProjStats(); renderGlobalList(); renderErcList();
    if (msg.path === currentFile && docs[currentFile]) {
      renderDetail(docs[currentFile]); renderNetList(); renderTopology();
    }
    setStatus('✓ 已同步外部修改');
  } catch (e) { setStatus('重载失败: ' + e.message, true); }
});

// ═══════════════════════════════════════════════════════════
//  初始化
// ═══════════════════════════════════════════════════════════

(async () => {
  try {
    if (typeof qevos === 'undefined' || typeof qevos.list !== 'function') {
      setStatus('qevos 桥不可用', true);
      return;
    }
    setStatus('正在加载项目列表...');
    let topDirs = new Set();
    try {
      const allItems = await qevos.list('.');
      (allItems || []).forEach(item => {
        const parts = item.path.split('/');
        if (parts.length > 1 && parts[0] && !parts[0].startsWith('.')) topDirs.add(parts[0]);
        if (item.type === 'dir' && !item.path.includes('/') && !item.path.startsWith('.')) topDirs.add(item.path);
      });
    } catch (e) { console.warn('qevos.list failed:', e); }

    projectSelect.innerHTML = '<option value="">📂 选择项目...</option>';
    const currentOpt = document.createElement('option');
    currentOpt.value = '.';
    currentOpt.textContent = '📂 当前目录（全部）';
    projectSelect.appendChild(currentOpt);
    [...topDirs].sort().forEach(dir => {
      const opt = document.createElement('option');
      opt.value = dir;
      opt.textContent = '📂 ' + dir;
      projectSelect.appendChild(opt);
    });

    // 面板带 root 打开（项目文件夹方式）→ 直接加载
    const root = window.__QEVOS__ && window.__QEVOS__.root;
    if (root) {
      currentProject = '.';
      projectSelect.value = '.';
      await loadProject();
    } else if (topDirs.size === 1) {
      // 只有一个项目目录 → 自动进入
      currentProject = [...topDirs][0];
      projectSelect.value = currentProject;
      await loadProject();
    } else {
      setStatus(`就绪（${topDirs.size} 个项目）`);
    }
  } catch (e) {
    setStatus('初始化失败: ' + e.message, true);
    console.error('init failed:', e);
  }
})();
</script>
</body>
</html>
