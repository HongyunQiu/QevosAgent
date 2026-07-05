---
name: Runs搜索
icon: 🔍
description: 搜索 runs 历史运行记录（纯前端；索引经「制作索引」按需生成，不依赖 Agent）
runtime: web
enabled: true
---
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Runs 搜索</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0d1117; color: #c9d1d9; min-height: 100vh; padding: 20px; }
  .container { max-width: 900px; margin: 0 auto; }
  h1 { font-size: 24px; color: #58a6ff; margin-bottom: 20px; display: flex; align-items: center; gap: 10px; }
  .search-box { display: flex; gap: 10px; margin-bottom: 20px; }
  #searchInput { flex: 1; padding: 12px 16px; font-size: 16px; background: #161b22; border: 1px solid #30363d; border-radius: 8px; color: #c9d1d9; outline: none; transition: border-color 0.2s; }
  #searchInput:focus { border-color: #58a6ff; box-shadow: 0 0 0 3px rgba(88, 166, 255, 0.1); }
  #searchInput::placeholder { color: #484f58; }
  button.btn { padding: 12px 24px; font-size: 16px; border: none; border-radius: 8px; cursor: pointer; transition: background 0.2s; color: #fff; }
  #searchBtn { background: #238636; }
  #searchBtn:hover { background: #2ea043; }
  #indexBtn { background: #1f6feb; }
  #indexBtn:hover { background: #388bfd; }
  button.btn:disabled { background: #30363d; cursor: not-allowed; }
  .stats { color: #8b949e; font-size: 14px; margin-bottom: 16px; }
  .result-item { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 12px; transition: border-color 0.2s; }
  .result-item:hover { border-color: #58a6ff; }
  .result-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
  .run-name { font-size: 16px; font-weight: 600; color: #58a6ff; font-family: 'Consolas', 'Courier New', monospace; }
  .run-date { font-size: 13px; color: #8b949e; }
  .match-badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: 500; }
  .badge-dir { background: rgba(88, 166, 255, 0.15); color: #58a6ff; }
  .badge-file { background: rgba(63, 185, 80, 0.15); color: #3fb950; }
  .match-context { background: #0d1117; border: 1px solid #21262d; border-radius: 6px; padding: 10px 12px; margin-top: 8px; font-family: 'Consolas', 'Courier New', monospace; font-size: 13px; line-height: 1.5; white-space: pre-wrap; word-break: break-all; max-height: 150px; overflow-y: auto; }
  .match-context .highlight { background: rgba(210, 153, 34, 0.3); color: #f0c674; padding: 0 2px; border-radius: 2px; }
  .file-label { font-size: 12px; color: #8b949e; margin-top: 6px; margin-bottom: 2px; }
  .empty-state { text-align: center; padding: 60px 20px; color: #484f58; }
  .empty-state .icon { font-size: 48px; margin-bottom: 16px; }
  .loading { text-align: center; padding: 40px; color: #58a6ff; }
  .spinner { display: inline-block; width: 32px; height: 32px; border: 3px solid #30363d; border-top-color: #58a6ff; border-radius: 50%; animation: spin 0.8s linear infinite; margin-bottom: 12px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .error { background: rgba(218, 54, 51, 0.1); border: 1px solid #da3633; border-radius: 8px; padding: 12px 16px; color: #f85149; margin-bottom: 16px; }
</style>
</head>
<body>
<div class="container">
  <h1>🔍 Runs 搜索</h1>

  <div class="search-box">
    <input type="text" id="searchInput" placeholder="输入关键词搜索 runs 目录…（回车搜索）" autofocus>
    <button id="searchBtn" class="btn" onclick="doSearch()">搜索</button>
    <button id="indexBtn" class="btn" onclick="buildIndex()" title="扫描 runs/ 重新生成搜索索引">制作索引</button>
  </div>

  <div id="stats" class="stats"></div>
  <div id="results"></div>
</div>

<script>
// —— 纯前端 Runs 搜索 App ——
// 索引是"数据"，存在本 App 的项目目录（app-data/runs_search/index.json），经 qevos 桥读写；
// 「制作索引」调平台端点 POST /api/runs-index 扫描 runs/（确定性、不过 Agent）后缓存。
// 不依赖 Agent，符合"UI App 纯独立"规范。
const searchInput = document.getElementById('searchInput');
const searchBtn   = document.getElementById('searchBtn');
const indexBtn    = document.getElementById('indexBtn');
const resultsDiv  = document.getElementById('results');
const statsDiv    = document.getElementById('stats');

let indexData = null;   // [{dir, content}]

searchInput.addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });
window.addEventListener('load', loadIndex);

// 载入已缓存的索引（若有）
async function loadIndex() {
  try {
    indexData = await qevos.readJSON('index.json');
  } catch (_) { indexData = null; }
  if (indexData && indexData.length) {
    statsDiv.textContent = `索引已加载：${indexData.length} 条运行记录`;
  } else {
    statsDiv.textContent = '尚无索引 —— 点「制作索引」扫描 runs/ 生成。';
  }
}

// 扫描 runs/ 生成索引并缓存到项目目录
async function buildIndex() {
  indexBtn.disabled = true; searchBtn.disabled = true;
  resultsDiv.innerHTML = '<div class="loading"><div class="spinner"></div><div>正在扫描 runs/ 生成索引…</div></div>';
  statsDiv.textContent = '';
  try {
    const r = await fetch('/api/runs-index', { method: 'POST' });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    indexData = j.index || [];
    await qevos.writeJSON('index.json', indexData);   // 缓存为数据文件
    resultsDiv.innerHTML = '';
    statsDiv.textContent = `索引已生成：${indexData.length} 条运行记录`;
  } catch (e) {
    resultsDiv.innerHTML = `<div class="error">制作索引失败：${escapeHtml(e.message)}</div>`;
  } finally {
    indexBtn.disabled = false; searchBtn.disabled = false;
  }
}

function doSearch() {
  const keyword = searchInput.value.trim();
  if (!keyword) return;
  if (!indexData || !indexData.length) {
    resultsDiv.innerHTML = '<div class="error">还没有索引，请先点「制作索引」。</div>';
    return;
  }
  searchBtn.disabled = true;
  resultsDiv.innerHTML = '<div class="loading"><div class="spinner"></div><div>正在搜索…</div></div>';
  statsDiv.textContent = '';
  setTimeout(() => {
    renderResults(searchIndex(keyword), keyword);
    searchBtn.disabled = false;
  }, 50);
}

function searchIndex(keyword) {
  const kw = keyword.toLowerCase();
  const results = [];
  for (const run of indexData) {
    if (!run.content.toLowerCase().includes(kw)) continue;
    const matches = extractMatches(run.content, keyword);
    if (matches.length) {
      results.push({
        run_dir: run.dir,
        dir_match: run.dir.toLowerCase().includes(kw),
        file_matches: matches,
        match_count: matches.length,
      });
    }
  }
  return results;
}

function extractMatches(content, keyword) {
  const kw = keyword.toLowerCase();
  const lines = content.split('\n');
  const matches = [];
  let currentFile = 'unknown';
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith('=== ') && line.endsWith(' ===')) { currentFile = line.slice(4, -4); continue; }
    if (line.toLowerCase().includes(kw)) {
      const context = lines.slice(Math.max(0, i - 2), Math.min(lines.length, i + 3)).join('\n');
      matches.push({ file: currentFile, matches: [{ line_num: i + 1, context: context.substring(0, 200) }] });
      if (matches.length >= 3) break;
    }
  }
  return matches;
}

function renderResults(results, keyword) {
  if (!results || !results.length) {
    resultsDiv.innerHTML = `<div class="empty-state"><div class="icon">🔍</div><div>没有找到包含 "${escapeHtml(keyword)}" 的结果</div></div>`;
    statsDiv.textContent = '';
    return;
  }
  statsDiv.textContent = `找到 ${results.length} 个匹配 "${escapeHtml(keyword)}" 的运行记录`;
  let html = '';
  for (const item of results) {
    html += `<div class="result-item"><div class="result-header">
        <span class="run-name">${escapeHtml(item.run_dir)}</span>
        <div>
          ${item.dir_match ? '<span class="match-badge badge-dir">目录名匹配</span>' : ''}
          ${item.match_count > 0 ? `<span class="match-badge badge-file">${item.match_count} 处匹配</span>` : ''}
          <span class="run-date">${parseRunDate(item.run_dir)}</span>
        </div></div>`;
    for (const fm of (item.file_matches || [])) {
      html += `<div class="file-label">📄 ${escapeHtml(fm.file)}</div>`;
      for (const m of fm.matches) html += `<div class="match-context">${highlightKeyword(m.context, keyword)}</div>`;
    }
    html += '</div>';
  }
  resultsDiv.innerHTML = html;
}

function parseRunDate(runDir) {
  const m = runDir.match(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/);
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}` : '';
}
function highlightKeyword(text, keyword) {
  const escaped = escapeHtml(text);
  if (!keyword) return escaped;
  return escaped.replace(new RegExp(`(${escapeRegex(escapeHtml(keyword))})`, 'gi'), '<span class="highlight">$1</span>');
}
function escapeHtml(str) { const d = document.createElement('div'); d.textContent = str == null ? '' : String(str); return d.innerHTML; }
function escapeRegex(str) { return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
</script>
</body>
</html>
