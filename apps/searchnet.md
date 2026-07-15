---
name: SearchNet
icon: 🔍
description: SearchNet 去中心化搜索网络 - 极简搜索界面
runtime: web
enabled: true
---
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SearchNet</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #fff; color: #333; min-height: 100vh; display: flex; flex-direction: column; }
  
  /* Top bar */
  .topbar { display: flex; justify-content: space-between; align-items: center; padding: 12px 20px; background: #fff; border-bottom: 1px solid #eee; }
  .topbar-left { display: flex; align-items: center; gap: 12px; }
  .topbar-right { display: flex; align-items: center; gap: 16px; }
  
  /* Status indicator */
  .status-indicator { display: flex; align-items: center; gap: 8px; font-size: 13px; color: #666; }
  .status-dot { width: 10px; height: 10px; border-radius: 50%; background: #ccc; transition: background 0.3s; }
  .status-dot.online { background: #34a853; box-shadow: 0 0 6px #34a853; }
  .status-dot.offline { background: #ea4335; }
  .status-dot.checking { background: #fbbc04; animation: pulse 1s infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
  
  /* Start button */
  .start-btn { padding: 8px 16px; background: #4285f4; color: #fff; border: none; border-radius: 4px; cursor: pointer; font-size: 13px; transition: background 0.2s; }
  .start-btn:hover { background: #3367d6; }
  .start-btn:disabled { background: #ccc; cursor: not-allowed; }
  .start-btn.starting { background: #fbbc04; }
  
  /* Main content */
  .main { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 0 20px; margin-top: -60px; }
  
  /* Logo */
  .logo { font-size: 72px; font-weight: 500; margin-bottom: 30px; letter-spacing: -3px; }
  .logo span:nth-child(1) { color: #4285f4; }
  .logo span:nth-child(2) { color: #ea4335; }
  .logo span:nth-child(3) { color: #fbbc04; }
  .logo span:nth-child(4) { color: #4285f4; }
  .logo span:nth-child(5) { color: #34a853; }
  .logo span:nth-child(6) { color: #ea4335; }
  .logo span:nth-child(7) { color: #4285f4; }
  .logo span:nth-child(8) { color: #fbbc04; }
  .logo span:nth-child(9) { color: #34a853; }
  .logo span:nth-child(10) { color: #ea4335; }
  .logo span:nth-child(11) { color: #4285f4; }
  .logo span:nth-child(12) { color: #ea4335; }
  .logo span:nth-child(13) { color: #fbbc04; }
  
  /* Search box */
  .search-container { width: 100%; max-width: 584px; margin-bottom: 20px; }
  .search-box { width: 100%; padding: 14px 20px; border: 1px solid #dfe1e5; border-radius: 24px; font-size: 16px; outline: none; transition: box-shadow 0.2s; }
  .search-box:focus { box-shadow: 0 2px 8px rgba(0,0,0,0.15); border-color: transparent; }
  .search-box::placeholder { color: #9aa0a6; }
  
  /* Search buttons */
  .search-buttons { display: flex; gap: 12px; margin-top: 20px; }
  .search-btn { padding: 10px 20px; background: #f8f9fa; border: 1px solid #f8f9fa; border-radius: 4px; color: #3c4043; font-size: 14px; cursor: pointer; transition: all 0.2s; }
  .search-btn:hover { border-color: #dadce0; box-shadow: 0 1px 1px rgba(0,0,0,0.1); }
  
  /* Results */
  .results { width: 100%; max-width: 652px; padding: 20px 0; display: none; }
  .results.show { display: block; }
  .result-item { padding: 16px 0; border-bottom: 1px solid #eee; }
  .result-title { color: #1a0dab; font-size: 20px; text-decoration: none; display: block; margin-bottom: 4px; cursor: pointer; }
  .result-title:hover { text-decoration: underline; }
  .result-url { color: #006621; font-size: 14px; margin-bottom: 8px; }
  .result-snippet { color: #545454; font-size: 14px; line-height: 1.5; }
  .result-meta { color: #70757a; font-size: 12px; margin-top: 4px; }
  
  /* Loading */
  .loading { display: none; text-align: center; padding: 40px; color: #666; }
  .loading.show { display: block; }
  .spinner { width: 40px; height: 40px; border: 3px solid #f3f3f3; border-top: 3px solid #4285f4; border-radius: 50%; animation: spin 1s linear infinite; margin: 0 auto 16px; }
  @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
  
  /* Error */
  .error-msg { color: #ea4335; text-align: center; padding: 20px; display: none; }
  .error-msg.show { display: block; }
  
  /* Footer */
  .footer { padding: 16px; text-align: center; color: #999; font-size: 12px; border-top: 1px solid #eee; }
  
  /* Server config */
  .server-config { display: none; position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); background: #fff; padding: 24px; border-radius: 8px; box-shadow: 0 4px 20px rgba(0,0,0,0.2); z-index: 100; min-width: 360px; }
  .server-config.show { display: block; }
  .config-overlay { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); z-index: 99; }
  .config-overlay.show { display: block; }
  .config-title { font-size: 18px; margin-bottom: 16px; }
  .config-field { margin-bottom: 12px; }
  .config-field label { display: block; font-size: 13px; color: #666; margin-bottom: 4px; }
  .config-field input { width: 100%; padding: 8px 12px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; }
  .config-buttons { display: flex; gap: 8px; justify-content: flex-end; margin-top: 16px; }
  .config-btn { padding: 8px 16px; border-radius: 4px; cursor: pointer; font-size: 13px; border: 1px solid #ddd; background: #fff; }
  .config-btn.primary { background: #4285f4; color: #fff; border-color: #4285f4; }
  
  /* Settings button */
  .settings-btn { background: none; border: none; cursor: pointer; font-size: 18px; color: #666; padding: 4px 8px; border-radius: 4px; }
  .settings-btn:hover { background: #f1f3f4; }
  
  /* Result count */
  .result-count { color: #70757a; font-size: 14px; margin-bottom: 16px; }
  
  /* Pagination */
  .pagination { display: flex; justify-content: center; align-items: center; gap: 8px; margin-top: 24px; padding: 16px 0; }
  .page-btn { padding: 8px 14px; border: 1px solid #dadce0; border-radius: 4px; background: #fff; color: #3c4043; font-size: 14px; cursor: pointer; transition: all 0.2s; }
  .page-btn:hover { background: #f8f9fa; border-color: #4285f4; }
  .page-btn.active { background: #4285f4; color: #fff; border-color: #4285f4; }
  .page-btn:disabled { background: #f1f3f4; color: #9aa0a6; cursor: not-allowed; border-color: #e8eaed; }
  .page-info { color: #70757a; font-size: 14px; margin: 0 8px; }
</style>
</head>
<body>

<!-- Top bar -->
<div class="topbar">
  <div class="topbar-left">
    <div class="status-indicator">
      <div class="status-dot" id="statusDot"></div>
      <span id="statusText">检测中...</span>
    </div>
  </div>
  <div class="topbar-right">
    <button class="start-btn" id="startBtn" onclick="startService()">🚀 启动服务</button>
    <button class="settings-btn" onclick="toggleConfig()" title="服务器配置">⚙️</button>
  </div>
</div>

<!-- Main content -->
<div class="main" id="mainContent">
  <div class="logo">
    <span>S</span><span>e</span><span>a</span><span>r</span><span>c</span><span>h</span><span>N</span><span>e</span><span>t</span>
  </div>
  
  <div class="search-container">
    <input type="text" class="search-box" id="searchInput" placeholder="输入搜索关键词..." autocomplete="off">
  </div>
  
  <div class="search-buttons">
    <button class="search-btn" onclick="doSearch()">SearchNet 搜索</button>
    <button class="search-btn" onclick="doSearch()">手气不错</button>
  </div>
  
  <!-- Loading -->
  <div class="loading" id="loading">
    <div class="spinner"></div>
    <div>搜索中...</div>
  </div>
  
  <!-- Error -->
  <div class="error-msg" id="errorMsg"></div>
  
  <!-- Results -->
  <div class="results" id="results"></div>
</div>

<!-- Footer -->
<div class="footer">
  SearchNet v1.0 — 去中心化个人主权搜索网络
</div>

<!-- Server config modal -->
<div class="config-overlay" id="configOverlay" onclick="toggleConfig()"></div>
<div class="server-config" id="serverConfig">
  <div class="config-title">服务器配置</div>
  <div class="config-field">
    <label>服务器地址</label>
    <input type="text" id="serverHost" value="172.24.217.99" placeholder="如 172.24.217.99">
  </div>
  <div class="config-field">
    <label>API 端口</label>
    <input type="text" id="serverPort" value="8080" placeholder="如 8080">
  </div>
  <div class="config-buttons">
    <button class="config-btn" onclick="toggleConfig()">取消</button>
    <button class="config-btn primary" onclick="saveConfig()">保存</button>
  </div>
</div>

<script>
// Configuration
let config = {
  host: localStorage.getItem('sn_host') || '172.24.217.99',
  port: localStorage.getItem('sn_port') || '8080'
};

// Load config
function loadConfig() {
  document.getElementById('serverHost').value = config.host;
  document.getElementById('serverPort').value = config.port;
}

function saveConfig() {
  config.host = document.getElementById('serverHost').value;
  config.port = document.getElementById('serverPort').value;
  localStorage.setItem('sn_host', config.host);
  localStorage.setItem('sn_port', config.port);
  toggleConfig();
  checkHealth();
}

function toggleConfig() {
  document.getElementById('serverConfig').classList.toggle('show');
  document.getElementById('configOverlay').classList.toggle('show');
}

// API base
function apiBase() {
  return `http://${config.host}:${config.port}`;
}

// Health check
let healthCheckTimer = null;

async function checkHealth() {
  const dot = document.getElementById('statusDot');
  const text = document.getElementById('statusText');
  
  dot.className = 'status-dot checking';
  text.textContent = '检测中...';
  
  try {
    const resp = await fetch(`${apiBase()}/api/v1/admin/health`, { 
      method: 'GET',
      mode: 'cors'
    });
    
    if (resp.ok) {
      const data = await resp.json();
      dot.className = 'status-dot online';
      text.textContent = `服务就绪 (${data.service})`;
      document.getElementById('startBtn').textContent = '✅ 服务运行中';
      document.getElementById('startBtn').disabled = true;
    } else {
      throw new Error('Not healthy');
    }
  } catch (e) {
    dot.className = 'status-dot offline';
    text.textContent = '服务未启动';
    document.getElementById('startBtn').textContent = '🚀 启动服务';
    document.getElementById('startBtn').disabled = false;
  }
}

// Start service
async function startService() {
  const btn = document.getElementById('startBtn');
  btn.disabled = true;
  btn.className = 'start-btn starting';
  btn.textContent = '⏳ 启动中...';
  
  // Show SSH command for manual start
  const sshCmd = `ssh q@${config.host} "cd /home/q/SearchNet && echo q | sudo -S docker-compose up -d"`;
  
  // Try fetching a start endpoint if available
  try {
    const resp = await fetch(`${apiBase()}/api/v1/admin/start`, { method: 'POST', mode: 'cors' });
    if (resp.ok) {
      btn.textContent = '✅ 启动成功';
      setTimeout(() => checkHealth(), 3000);
      return;
    }
  } catch (e) {
    // Expected - service not running yet
  }
  
  // Show instructions
  btn.className = 'start-btn';
  btn.disabled = false;
  btn.textContent = '📋 复制启动命令';
  btn.onclick = function() {
    navigator.clipboard.writeText(sshCmd).then(() => {
      btn.textContent = '✅ 命令已复制';
      setTimeout(() => {
        btn.textContent = '📋 复制启动命令';
        btn.onclick = startService;
      }, 2000);
    }).catch(() => {
      // Fallback
      const ta = document.createElement('textarea');
      ta.value = sshCmd;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      btn.textContent = '✅ 命令已复制';
      setTimeout(() => {
        btn.textContent = '📋 复制启动命令';
        btn.onclick = startService;
      }, 2000);
    });
  };
  
  // Show start guide
  showStartGuide();
  
  // Start health check polling
  if (!healthCheckTimer) {
    healthCheckTimer = setInterval(checkHealth, 3000);
  }
}

function showStartGuide() {
  const guide = document.createElement('div');
  guide.id = 'startGuide';
  guide.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:#fff;padding:24px;border-radius:8px;box-shadow:0 4px 20px rgba(0,0,0,0.2);z-index:100;max-width:500px;width:90%;max-height:80vh;overflow-y:auto;';
  guide.innerHTML = `
    <div style="font-size:18px;margin-bottom:16px;">🚀 启动 SearchNet 服务</div>
    <div style="color:#666;font-size:14px;line-height:1.8;">
      <p>服务当前未运行，请选择以下方式启动：</p>
      <p><strong>方式一：复制命令到终端</strong></p>
      <div style="background:#f5f5f5;padding:12px;border-radius:4px;font-family:monospace;font-size:12px;word-break:break-all;margin:8px 0;cursor:pointer;" onclick="navigator.clipboard.writeText(this.textContent);this.textContent+=' ✅ 已复制'">
        ssh q@${config.host} "cd /home/q/SearchNet && echo q | sudo -S docker-compose up -d"
      </div>
      <p><strong>方式二：使用 Dashboard Apps 面板</strong></p>
      <p>在 Dashboard 的「Apps」Tab 中找到「🚀 SearchNet 启动」并运行</p>
      <p><strong>方式三：SSH 到服务器</strong></p>
      <div style="background:#f5f5f5;padding:12px;border-radius:4px;font-family:monospace;font-size:12px;word-break:break-all;margin:8px 0;">
        ssh q@${config.host}<br>
        cd /home/q/SearchNet<br>
        sudo docker-compose up -d<br>
        <span style="color:#999;">密码: q</span>
      </div>
      <p style="color:#ea4335;font-size:12px;">⚠️ 如果无法连接，请检查 ZeroTier 是否已连接</p>
    </div>
    <div style="text-align:right;margin-top:16px;">
      <button onclick="document.getElementById('startGuide').remove();document.getElementById('guideOverlay').remove();" style="padding:8px 20px;background:#4285f4;color:#fff;border:none;border-radius:4px;cursor:pointer;">我知道了</button>
    </div>
  `;
  document.body.appendChild(guide);
  
  const overlay = document.createElement('div');
  overlay.id = 'guideOverlay';
  overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);z-index:99;';
  overlay.onclick = function() { guide.remove(); overlay.remove(); };
  document.body.appendChild(overlay);
}

// Search state
let searchState = {
  allResults: [],
  currentPage: 1,
  perPage: 20,
  totalPages: 5,
  query: '',
  elapsed: 0
};

// Search
async function doSearch(page) {
  const query = document.getElementById('searchInput').value.trim();
  if (!query) return;
  
  const loading = document.getElementById('loading');
  const results = document.getElementById('results');
  const errorMsg = document.getElementById('errorMsg');
  
  const startTime = performance.now();
  loading.classList.add('show');
  results.classList.remove('show');
  errorMsg.classList.remove('show');
  
  try {
    const resp = await fetch(`${apiBase()}/api/v1/search`, {
      method: 'POST',
      mode: 'cors',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query: query,
        type: 'text',
        limit: 100
      })
    });
    
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    
    const data = await resp.json();
    const elapsed = ((performance.now() - startTime) / 1000).toFixed(3);
    loading.classList.remove('show');
    
    if (data.results && data.results.length > 0) {
      searchState.allResults = data.results;
      searchState.query = query;
      searchState.elapsed = elapsed;
      searchState.totalPages = Math.min(5, Math.ceil(data.results.length / searchState.perPage));
      searchState.currentPage = page || 1;
      renderResults();
    } else {
      results.innerHTML = `<div class="result-count">未找到相关结果 (${elapsed} 秒)</div>`;
      results.classList.add('show');
    }
  } catch (e) {
    loading.classList.remove('show');
    errorMsg.textContent = `搜索失败: ${e.message}。请检查服务器是否已启动，或配置服务器地址。`;
    errorMsg.classList.add('show');
  }
}

function goToPage(page) {
  if (page < 1 || page > searchState.totalPages) return;
  searchState.currentPage = page;
  renderResults();
  // Scroll to top of results
  document.getElementById('results').scrollIntoView({ behavior: 'smooth' });
}

function renderResults() {
  const results = document.getElementById('results');
  const { allResults, currentPage, perPage, totalPages, query, elapsed } = searchState;
  
  const start = (currentPage - 1) * perPage;
  const end = start + perPage;
  const pageResults = allResults.slice(start, end);
  
  let html = `<div class="result-count">找到约 ${allResults.length} 条结果，显示 ${start+1}-${Math.min(end, allResults.length)} 条 (${elapsed} 秒)</div>`;
  
  pageResults.forEach(r => {
    html += `
      <div class="result-item">
        <a class="result-title" href="#">${escapeHtml(r.title || '无标题')}</a>
        <div class="result-url">${escapeHtml(r.source || 'local')} › ${escapeHtml(r.category || '')}</div>
        <div class="result-snippet">${escapeHtml(r.content || '').substring(0, 200)}...</div>
        ${r.created_at ? `<div class="result-meta">${escapeHtml(r.created_at)}</div>` : ''}
      </div>
    `;
  });
  
  // Pagination
  if (totalPages > 1) {
    html += '<div class="pagination">';
    html += `<button class="page-btn" onclick="goToPage(${currentPage-1})" ${currentPage===1?'disabled':''}>‹ 上一页</button>`;
    for (let i = 1; i <= totalPages; i++) {
      html += `<button class="page-btn ${i===currentPage?'active':''}" onclick="goToPage(${i})">${i}</button>`;
    }
    html += `<button class="page-btn" onclick="goToPage(${currentPage+1})" ${currentPage===totalPages?'disabled':''}>下一页 ›</button>`;
    html += '</div>';
  }
  
  results.innerHTML = html;
  results.classList.add('show');
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// Event listeners
document.getElementById('searchInput').addEventListener('keypress', function(e) {
  if (e.key === 'Enter') doSearch();
});

// Init
loadConfig();
checkHealth();
// Periodic health check
setInterval(checkHealth, 10000);
</script>
</body>
</html>
