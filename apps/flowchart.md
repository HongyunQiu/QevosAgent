---
name: 流程图
icon: 🔀
description: 基于 Markdown 的流程图/节点图编辑器（纯前端；语义存 flow.md、几何存 .qevos/view.json）
runtime: web
skill: flowchart
enabled: true
---
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>流程图</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { height: 100%; }
  body { font: 14px -apple-system, 'Segoe UI', Roboto, sans-serif; background: #0d1117; color: #c9d1d9; display: flex; flex-direction: column; overflow: hidden; }
  #bar { display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-bottom: 1px solid #21262d; background: #161b22; flex-shrink: 0; }
  #bar .title { font-weight: 600; color: #58a6ff; margin-right: 6px; }
  button { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; border-radius: 6px; padding: 6px 12px; cursor: pointer; font-size: 13px; }
  button:hover { border-color: #58a6ff; }
  button.on { background: #1f6feb; border-color: #1f6feb; color: #fff; }
  #status { margin-left: auto; color: #6e7681; font-size: 12px; }
  #canvas { position: relative; flex: 1; overflow: auto; background:
      linear-gradient(#161b2233 1px, transparent 1px) 0 0 / 24px 24px,
      linear-gradient(90deg, #161b2233 1px, transparent 1px) 0 0 / 24px 24px, #0d1117; }
  #content { position: relative; width: 3000px; height: 2000px; }
  #edges { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
  #edges path { stroke: #6e7681; stroke-width: 2; fill: none; pointer-events: stroke; cursor: pointer; }
  #edges path:hover { stroke: #f85149; }
  .node { position: absolute; width: 120px; min-height: 46px; background: #161b22; border: 1.5px solid #388bfd;
    border-radius: 8px; display: flex; align-items: center; justify-content: center; text-align: center;
    padding: 6px 22px 6px 8px; cursor: grab; user-select: none; box-shadow: 0 2px 6px #0006; }
  .node:active { cursor: grabbing; }
  .node.sel { border-color: #f0c674; box-shadow: 0 0 0 2px #f0c67455; }
  .node.linksrc { border-color: #3fb950; }
  .node .del { position: absolute; top: 2px; right: 4px; color: #6e7681; font-size: 13px; cursor: pointer; line-height: 1; }
  .node .del:hover { color: #f85149; }
  .node .handle { position: absolute; right: -9px; top: 50%; transform: translateY(-50%); width: 16px; height: 16px;
    background: #238636; border-radius: 50%; color: #fff; font-size: 11px; display: flex; align-items: center; justify-content: center; cursor: crosshair; }
  .node .handle:hover { background: #2ea043; }
  .hint { color: #6e7681; font-size: 12px; }
</style>
</head>
<body>
  <div id="bar">
    <span class="title">🔀 流程图</span>
    <button onclick="addNode()">＋ 节点</button>
    <button id="linkBtn" onclick="toggleLink()">连线</button>
    <span class="hint">拖动=移动 · 绿柄/连线=连接 · 双击=改名 · ×=删除 · 点连线=删边</span>
    <span id="status">就绪</span>
  </div>
  <div id="canvas">
    <div id="content">
      <svg id="edges"><defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" fill="#6e7681"/>
        </marker>
      </defs></svg>
    </div>
  </div>

<script>
// ── 纯前端流程图编辑器 ──
// 语义（节点/连线/标签）→ flow.md；几何（坐标）→ .qevos/view.json。分文件 = 各写各的，
// 拖动只改 view.json、结构改动只改 flow.md。全部确定性、零 Agent（符合"UI App 纯独立"）。
const NODE_W = 120, NODE_H = 46;
const contentEl = document.getElementById('content');
const svg = document.getElementById('edges');
const statusEl = document.getElementById('status');

let nodes = {};   // id -> { label, x, y }
let edges = [];   // { from, to }
let seq = 0;
let linkMode = false, linkSrc = null, selected = null;
let selfWriteAt = 0;  // 抑制自身写入触发的 onPush 重载

const setStatus = m => { statusEl.textContent = m; };

// ── flow.md 解析/序列化（结构化 Markdown）──
function parseFlow(md) {
  const ns = {}, es = []; let mode = '';
  for (const raw of (md || '').split('\n')) {
    const line = raw.trim();
    if (line.startsWith('## ')) { mode = line.slice(3).trim(); continue; }
    if (mode === '节点' || mode.toLowerCase() === 'nodes') {
      const m = line.match(/^-\s*([\w-]+)\s*:\s*(.*)$/);
      if (m) ns[m[1]] = { label: m[2] || m[1] };
    } else if (mode === '连线' || mode.toLowerCase() === 'edges') {
      const m = line.match(/^-\s*([\w-]+)\s*->\s*([\w-]+)\s*$/);
      if (m) es.push({ from: m[1], to: m[2] });
    }
  }
  return { ns, es };
}
function serializeFlow() {
  let s = '# 流程图\n\n## 节点\n';
  for (const id in nodes) s += `- ${id}: ${nodes[id].label}\n`;
  s += '\n## 连线\n';
  for (const e of edges) s += `- ${e.from} -> ${e.to}\n`;
  return s;
}

// ── 保存（防抖）──
let tSem, tGeo;
function saveSemantic() { clearTimeout(tSem); tSem = setTimeout(async () => {
  selfWriteAt = Date.now(); await qevos.writeFile('flow.md', serializeFlow()); setStatus('已保存 flow.md');
}, 150); }
function saveGeometry() { clearTimeout(tGeo); tGeo = setTimeout(async () => {
  const view = { nodes: {} };
  for (const id in nodes) view.nodes[id] = { x: nodes[id].x, y: nodes[id].y };
  selfWriteAt = Date.now(); await qevos.writeJSON('.qevos/view.json', view);
}, 200); }

// ── 载入 ──
async function load() {
  const md = await qevos.readFile('flow.md');
  const view = (await qevos.readJSON('.qevos/view.json')) || { nodes: {} };
  const { ns, es } = parseFlow(md);
  nodes = {}; edges = es;
  let i = 0;
  for (const id in ns) {
    const pos = (view.nodes && view.nodes[id]) || { x: 80 + (i % 5) * 170, y: 80 + Math.floor(i / 5) * 120 };
    nodes[id] = { label: ns[id].label, x: pos.x, y: pos.y };
    const n = parseInt((id.match(/\d+/) || [0])[0], 10); if (n > seq) seq = n;
    i++;
  }
  if (!md) setStatus('新流程图 —— 点「＋ 节点」开始'); else setStatus(`已载入 ${Object.keys(nodes).length} 节点`);
  render();
}

// ── 渲染 ──
function render() {
  contentEl.querySelectorAll('.node').forEach(el => el.remove());
  for (const id in nodes) {
    const nd = nodes[id];
    const el = document.createElement('div');
    el.className = 'node' + (selected === id ? ' sel' : '') + (linkSrc === id ? ' linksrc' : '');
    el.dataset.id = id;
    el.style.left = nd.x + 'px'; el.style.top = nd.y + 'px';
    el.innerHTML = `<span class="del" title="删除">×</span><span class="lbl"></span><span class="handle" title="连线">→</span>`;
    el.querySelector('.lbl').textContent = nd.label;
    el.querySelector('.del').addEventListener('pointerdown', e => e.stopPropagation());
    el.querySelector('.del').addEventListener('click', e => { e.stopPropagation(); delNode(id); });
    el.querySelector('.handle').addEventListener('pointerdown', e => e.stopPropagation());
    el.querySelector('.handle').addEventListener('click', e => { e.stopPropagation(); startLink(id); });
    el.addEventListener('pointerdown', e => onNodeDown(e, id));
    el.addEventListener('dblclick', () => renameNode(id));
    el.addEventListener('click', () => onNodeClick(id));
    contentEl.appendChild(el);
  }
  renderEdges();
}
function borderPt(cx, cy, tx, ty) {
  const dx = tx - cx, dy = ty - cy; if (!dx && !dy) return { x: cx, y: cy };
  const s = Math.min((NODE_W / 2) / (Math.abs(dx) || 1e-9), (NODE_H / 2) / (Math.abs(dy) || 1e-9));
  return { x: cx + dx * s, y: cy + dy * s };
}
function renderEdges() {
  svg.querySelectorAll('path').forEach(p => p.remove());
  for (const e of edges) {
    const a = nodes[e.from], b = nodes[e.to]; if (!a || !b) continue;
    const ac = { x: a.x + NODE_W / 2, y: a.y + NODE_H / 2 }, bc = { x: b.x + NODE_W / 2, y: b.y + NODE_H / 2 };
    const p1 = borderPt(ac.x, ac.y, bc.x, bc.y), p2 = borderPt(bc.x, bc.y, ac.x, ac.y);
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', `M${p1.x},${p1.y} L${p2.x},${p2.y}`);
    path.setAttribute('marker-end', 'url(#arrow)');
    path.addEventListener('click', () => { if (confirm(`删除连线 ${e.from} → ${e.to}？`)) { edges = edges.filter(x => x !== e); saveSemantic(); render(); } });
    svg.appendChild(path);
  }
}

// ── 交互 ──
function onNodeDown(e, id) {
  if (linkMode || e.target.classList.contains('handle') || e.target.classList.contains('del')) return;
  const nd = nodes[id]; const sx = e.clientX, sy = e.clientY, ox = nd.x, oy = nd.y;
  const el = e.currentTarget; el.setPointerCapture(e.pointerId); let moved = false;
  const mv = ev => { moved = true; nd.x = Math.max(0, ox + (ev.clientX - sx)); nd.y = Math.max(0, oy + (ev.clientY - sy));
    el.style.left = nd.x + 'px'; el.style.top = nd.y + 'px'; renderEdges(); };
  const up = () => { el.removeEventListener('pointermove', mv); el.removeEventListener('pointerup', up); if (moved) saveGeometry(); };
  el.addEventListener('pointermove', mv); el.addEventListener('pointerup', up);
}
function onNodeClick(id) {
  if (linkMode && linkSrc && linkSrc !== id) {
    if (!edges.some(e => e.from === linkSrc && e.to === id)) { edges.push({ from: linkSrc, to: id }); saveSemantic(); }
    linkSrc = null; setLinkMode(false); render();
  } else { selected = id; render(); }
}
function startLink(id) { setLinkMode(true); linkSrc = id; setStatus('点击目标节点完成连线（Esc 取消）'); render(); }
function toggleLink() { setLinkMode(!linkMode); if (!linkMode) linkSrc = null; setStatus(linkMode ? '连线模式：点源节点的绿柄或先点一个节点' : '就绪'); render(); }
function setLinkMode(on) { linkMode = on; document.getElementById('linkBtn').classList.toggle('on', on); }

function addNode() {
  const cv = document.getElementById('canvas');
  const id = 'n' + (++seq);
  nodes[id] = { label: '节点 ' + seq, x: cv.scrollLeft + 120, y: cv.scrollTop + 80 };
  selected = id; saveSemantic(); saveGeometry(); render();
}
function renameNode(id) {
  const v = prompt('节点名称：', nodes[id].label); if (v != null) { nodes[id].label = v.trim() || id; saveSemantic(); render(); }
}
function delNode(id) {
  delete nodes[id]; edges = edges.filter(e => e.from !== id && e.to !== id);
  if (selected === id) selected = null; if (linkSrc === id) linkSrc = null;
  saveSemantic(); saveGeometry(); render();
}
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { linkSrc = null; setLinkMode(false); render(); }
  else if ((e.key === 'Delete' || e.key === 'Backspace') && selected && document.activeElement === document.body) delNode(selected);
});

// ── 外部变更刷新（onPush）：别人（或 Agent 未来）改了 flow.md → 重载 ──
qevos.onPush(msg => {
  if (msg.type === 'file-changed' && msg.path === 'flow.md' && Date.now() - selfWriteAt > 1200) {
    setStatus('检测到外部修改，重载…'); load();
  }
});

load();
</script>
</body>
</html>
