'use strict';

/**
 * QevosAgent Desktop — Electron main process
 *
 * Window layout (content area, below the native title bar):
 *   ┌──────────────────────────────────────────────┐
 *   │ ⚡ │ 看板 │ View A × │ View B × │       ⚙   │  ← tabbar.html  (TAB_H px)
 *   ├──────────────────────────────────────────────┤
 *   │                                              │
 *   │          setup.html / dashboard / view       │  ← content WebContentsView
 *   │                                              │
 *   └──────────────────────────────────────────────┘
 *
 * Tab system (Electron-only, browser mode unchanged):
 *   - tabbar.html is a separate WebContentsView pinned to the top.
 *   - Each web_show call POSTs to /api/open-view → server.js emits 'open-view'
 *     → main.js creates a new content WebContentsView and updates the tab bar.
 *   - Switching tabs hides/shows views via setBounds() — no page reloads.
 *   - "看板" tab is permanent and cannot be closed.
 *   - Settings (⚙) in the tab bar loads setup.html into the home view.
 *
 * IPC (content views → main, via preload.js):
 *   env:read        → return current .env values
 *   env:save        → write .env and sync process.env
 *   env:test        → connectivity check against the LLM endpoint
 *   dashboard:open  → start dashboard server + navigate home view
 *
 * IPC (tabbar.html → main, via tabbar-preload.js):
 *   tab-activate id → switch to that view
 *   tab-close    id → destroy that view
 *   tab-settings    → load setup.html in the home view
 */

const { app, BrowserWindow, WebContentsView, ipcMain, Menu, shell } = require('electron');
const path    = require('path');
const http    = require('http');
const fs      = require('fs');
const Updater = require('./updater');

// ── Paths ──────────────────────────────────────────────────────────────────

const VENDOR_APP  = path.join(__dirname, 'vendor', 'app');
const APP_ROOT    = app.isPackaged ? VENDOR_APP : path.resolve(__dirname, '..');
const DOT_ENV_DIR = path.resolve(__dirname, '..');

// ── Load .env ──────────────────────────────────────────────────────────────

function loadDotenv() {
  const envFile = path.join(DOT_ENV_DIR, '.env');
  let raw;
  try { raw = fs.readFileSync(envFile, 'utf8'); } catch { return; }
  for (const rawLine of raw.split(/\r?\n/)) {
    let line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    if (line.startsWith('export ')) line = line.slice(7).trim();
    const eq = line.indexOf('=');
    if (eq < 1) continue;
    const key = line.slice(0, eq).trim();
    if (!key || key in process.env) continue;
    let val = line.slice(eq + 1).trim();
    if (val.length >= 2 && val[0] === val[val.length - 1] && (val[0] === '"' || val[0] === "'")) {
      val = val.slice(1, -1);
    }
    process.env[key] = val;
  }
}

loadDotenv();

// ── Embedded Python ────────────────────────────────────────────────────────

const EMBEDDED_PYTHON = process.platform === 'win32'
  ? path.join(__dirname, 'vendor', 'python', 'python.exe')
  : path.join(__dirname, 'vendor', 'python', 'bin', 'python3');

if (fs.existsSync(EMBEDDED_PYTHON)) {
  // Quote the path so server.js parsePythonCmd() can recover it even when
  // it contains spaces (e.g. "C:\Programme Files\QevosAgent\...\python.exe").
  process.env.PYTHON_CMD = EMBEDDED_PYTHON.includes(' ')
    ? `"${EMBEDDED_PYTHON}"`
    : EMBEDDED_PYTHON;
  console.log('[desktop] Embedded Python:', EMBEDDED_PYTHON);
} else if (!process.env.PYTHON_CMD) {
  console.warn(
    '[desktop] No embedded Python found and PYTHON_CMD is not set.\n' +
    '          Run "npm run setup" first, or set PYTHON_CMD in .env.'
  );
}

// ── Config ─────────────────────────────────────────────────────────────────

const PORT  = parseInt(process.env.DASHBOARD_PORT || '8765', 10);
const TAB_H = 33; // matches the dashboard topbar height (grid-template-rows: 33px …)

// ── State ──────────────────────────────────────────────────────────────────

const HOME_ID = 'dashboard';
const gViews  = new Map();  // viewId → { view: WebContentsView, title: string }
let gActiveId        = HOME_ID;
let mainWindow       = null;
let tabbarView       = null;
let dashboardStarted = false;

const updater        = new Updater(VENDOR_APP);
let pendingManifest  = null;

// ── Layout ─────────────────────────────────────────────────────────────────

function getTabbarBounds() {
  if (!mainWindow || mainWindow.isDestroyed()) return { x: 0, y: 0, width: 0, height: TAB_H };
  const [w] = mainWindow.getContentSize();
  return { x: 0, y: 0, width: w, height: TAB_H };
}

function getContentBounds() {
  if (!mainWindow || mainWindow.isDestroyed()) return { x: 0, y: TAB_H, width: 0, height: 0 };
  const [w, h] = mainWindow.getContentSize();
  return { x: 0, y: TAB_H, width: w, height: Math.max(0, h - TAB_H) };
}

function updateLayout() {
  if (tabbarView && !tabbarView.webContents.isDestroyed()) {
    tabbarView.setBounds(getTabbarBounds());
  }
  const full   = getContentBounds();
  const hidden = { x: 0, y: TAB_H, width: 0, height: 0 };
  for (const [id, entry] of gViews) {
    entry.view.setBounds(id === gActiveId ? full : hidden);
  }
}

// ── Tab state → tabbar ─────────────────────────────────────────────────────

function pushTabsUpdate() {
  if (!tabbarView || tabbarView.webContents.isDestroyed()) return;
  const tabs = [
    { id: HOME_ID, title: '看板', isHome: true },
    ...Array.from(gViews.entries())
      .filter(([id]) => id !== HOME_ID)
      .map(([id, { title }]) => ({ id, title, isHome: false })),
  ];
  tabbarView.webContents.send('tabs-update', { tabs, activeId: gActiveId });
}

// ── View tab management ────────────────────────────────────────────────────

function activateView(id) {
  if (!gViews.has(id)) return;
  gActiveId = id;
  updateLayout();
  pushTabsUpdate();
}

function openElectronView(displayId, url, title) {
  const id = 'view-' + displayId;
  if (gViews.has(id)) {
    // Replace mode: reload the existing view with updated content.
    gViews.get(id).view.webContents.loadURL(url);
    activateView(id);
    return;
  }

  const view = new WebContentsView({
    webPreferences: { nodeIntegration: false, contextIsolation: true },
  });

  // Keep this view locked to its view URL — any link the user clicks inside
  // the rendered HTML content opens in the system browser instead of
  // navigating the view or spawning a new Electron window.
  view.webContents.on('will-navigate', (e, targetUrl) => {
    e.preventDefault();
    shell.openExternal(targetUrl);
  });
  view.webContents.setWindowOpenHandler(({ url: targetUrl }) => {
    shell.openExternal(targetUrl);
    return { action: 'deny' };
  });

  mainWindow.contentView.addChildView(view);
  view.webContents.loadURL(url);
  gViews.set(id, { view, title: title || displayId });
  activateView(id);
}

function closeView(id) {
  if (id === HOME_ID) return; // 看板 is permanent
  const entry = gViews.get(id);
  if (!entry) return;
  mainWindow.contentView.removeChildView(entry.view);
  entry.view.webContents.close();
  gViews.delete(id);
  if (gActiveId === id) activateView(HOME_ID);
  else pushTabsUpdate();
}

// ── Dashboard server ───────────────────────────────────────────────────────

function startDashboard() {
  if (dashboardStarted) return;
  dashboardStarted = true;

  process.env.DASHBOARD_PORT   = String(PORT);
  process.env.PYTHONUTF8       = '1';
  process.env.PYTHONIOENCODING = 'utf-8';
  process.env.ELECTRON         = '1';
  process.env.AGENT_DIR        = APP_ROOT;
  process.env.PYTHONPATH       = APP_ROOT;

  const serverPath = path.join(APP_ROOT, 'dashboard', 'server.js');
  try {
    const { serverEvents } = require(serverPath);
    serverEvents.on('open-view', ({ url, title, displayId }) => {
      if (!mainWindow || mainWindow.isDestroyed()) return;
      openElectronView(displayId, url, title || displayId);
    });
    console.log('[desktop] Dashboard server started from', serverPath);
  } catch (err) {
    console.error('[desktop] Failed to load dashboard server:', err.message);
    startDashboard._error = err.message;
  }
}

// ── Poll until HTTP server is ready ───────────────────────────────────────

function waitForServer(maxRetries, intervalMs, callback) {
  let retries = maxRetries;
  const attempt = () => {
    const req = http.get(`http://127.0.0.1:${PORT}/api/state`, res => {
      res.resume();
      callback(null);
    });
    req.setTimeout(800, () => req.destroy());
    req.on('error', () => {
      if (--retries <= 0) {
        callback(new Error(
          `Dashboard 未能在端口 ${PORT} 启动\n（已等待 ${(maxRetries * intervalMs / 1000).toFixed(0)} 秒）`
        ));
        return;
      }
      setTimeout(attempt, intervalMs);
    });
  };
  attempt();
}

// ── Helpers for the home (看板) view ──────────────────────────────────────

function getMainView() {
  const entry = gViews.get(HOME_ID);
  return entry ? entry.view : null;
}

function notifyLoadingError(msg) {
  const mv = getMainView();
  if (!mv || !mainWindow || mainWindow.isDestroyed()) return;
  const escaped = msg.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n');
  mv.webContents
    .executeJavaScript(`typeof showError === 'function' && showError("${escaped}")`)
    .catch(() => {});
}

function navigateToDashboard() {
  const mv = getMainView();
  if (!mv) return;
  activateView(HOME_ID);
  mv.webContents.loadFile(path.join(__dirname, 'loading.html'));

  if (startDashboard._error) {
    mv.webContents.once('did-finish-load', () => {
      notifyLoadingError(`无法加载 Dashboard：${startDashboard._error}`);
    });
    return;
  }

  waitForServer(30, 500, err => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (err) { notifyLoadingError(err.message); return; }
    mv.webContents.loadURL(`http://127.0.0.1:${PORT}`);
  });
}

function showSetup() {
  const mv = getMainView();
  if (!mv || !mainWindow || mainWindow.isDestroyed()) return;
  activateView(HOME_ID);
  mv.webContents.loadFile(path.join(__dirname, 'setup.html'));
}

// ── Check if the LLM endpoint is configured ────────────────────────────────

function isConfigured() {
  return !!(
    process.env.OPENAI_BASE_URL ||
    process.env.OPENAI_PROFILE_OSS120B_BASE_URL ||
    process.env.OPENAI_PROFILE_QWEN3527DGX_BASE_URL
  );
}

// ── Native menu — minimal; tabs live in tabbar.html ───────────────────────

function setupNativeMenu() {
  if (process.platform === 'darwin') {
    // macOS requires an application menu for Cmd+Q and system conventions.
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      {
        label: 'QevosAgent',
        submenu: [
          { role: 'about',     label: '关于 QevosAgent' },
          { type: 'separator' },
          { role: 'quit',      label: '退出' },
        ],
      },
    ]));
  } else {
    // Windows / Linux: remove the menu bar entirely — tabs replace it.
    Menu.setApplicationMenu(null);
  }
}

// ── IPC handlers ──────────────────────────────────────────────────────────

function registerIPC() {
  // ── Content view IPC (preload.js) ────────────────────────────────────────

  ipcMain.handle('env:read', () => ({
    OPENAI_BASE_URL: process.env.OPENAI_BASE_URL || '',
    OPENAI_API_KEY:  process.env.OPENAI_API_KEY  || '',
    OPENAI_MODEL:    process.env.OPENAI_MODEL     || '',
    MAX_ITERS:       process.env.MAX_ITERS        || '100',
  }));

  ipcMain.handle('env:save', (_, data) => {
    const envPath = path.join(DOT_ENV_DIR, '.env');
    let existing = {};
    try {
      for (const line of fs.readFileSync(envPath, 'utf8').split(/\r?\n/)) {
        const eq = line.indexOf('=');
        if (eq > 0 && !line.trim().startsWith('#')) {
          existing[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
        }
      }
    } catch {}
    const merged = { ...existing };
    for (const [k, v] of Object.entries(data)) {
      if (v && v.trim()) merged[k] = v.trim();
    }
    const content = Object.entries(merged).map(([k, v]) => `${k}=${v}`).join('\n') + '\n';
    fs.writeFileSync(envPath, content, 'utf8');
    for (const [k, v] of Object.entries(merged)) process.env[k] = v;
    return { ok: true };
  });

  ipcMain.handle('dashboard:open', () => {
    startDashboard();
    navigateToDashboard();
    return { ok: true };
  });

  ipcMain.handle('env:test', (_, { baseUrl, apiKey }) => {
    return new Promise(resolve => {
      let url;
      try {
        const base = baseUrl.endsWith('/') ? baseUrl.slice(0, -1) : baseUrl;
        url = new URL(base + '/models');
      } catch {
        return resolve({ ok: false, error: '无效的 API 地址格式' });
      }
      const mod = url.protocol === 'https:' ? require('https') : require('http');
      const options = {
        hostname: url.hostname,
        port:     url.port || (url.protocol === 'https:' ? 443 : 80),
        path:     url.pathname + url.search,
        method:   'GET',
        headers:  { Authorization: `Bearer ${apiKey || 'local'}` },
        timeout:  8000,
      };
      const req = mod.request(options, res => {
        res.resume();
        resolve(res.statusCode >= 200 && res.statusCode < 300
          ? { ok: true,  status: res.statusCode }
          : { ok: false, status: res.statusCode, error: `HTTP ${res.statusCode}` });
      });
      req.on('timeout', () => { req.destroy(); resolve({ ok: false, error: '连接超时（8 秒）' }); });
      req.on('error',   err => resolve({ ok: false, error: err.message }));
      req.end();
    });
  });

  // ── Tab bar IPC (tabbar-preload.js) ──────────────────────────────────────

  ipcMain.on('tab-activate', (_, id)  => activateView(id));
  ipcMain.on('tab-close',    (_, id)  => closeView(id));
  ipcMain.on('tab-settings', ()       => showSetup());

  // ── Update IPC ────────────────────────────────────────────────────────────

  ipcMain.handle('update:start', async () => {
    if (!pendingManifest) return { ok: false, error: '没有待执行的更新' };
    try {
      await updater.downloadUpdate(pendingManifest, (percent, file) => {
        if (tabbarView && !tabbarView.webContents.isDestroyed()) {
          tabbarView.webContents.send('update:progress', { percent, file });
        }
      });
      updater.applyUpdate(pendingManifest);
      pendingManifest = null;
      return { ok: true };
    } catch (err) {
      return { ok: false, error: err.message };
    }
  });

  ipcMain.on('update:relaunch', () => {
    app.relaunch();
    app.quit();
  });

  ipcMain.on('update:open-releases', () => {
    shell.openExternal('https://github.com/HongyunQiu/QevosAgent/releases/latest');
  });
}

// ── BrowserWindow + WebContentsViews ──────────────────────────────────────

function createWindow() {
  mainWindow = new BrowserWindow({
    width:           1400,
    height:          900,
    minWidth:        800,
    minHeight:       600,
    title:           'QevosAgent',
    icon:            path.join(__dirname, 'build',
                       process.platform === 'darwin' ? 'icon.icns'
                     : process.platform === 'linux'  ? 'icon.png'
                     : 'icon.ico'),
    backgroundColor: '#0d1117',
  });

  // ── Tab bar (always visible, pinned to top) ───────────────────────────────
  tabbarView = new WebContentsView({
    webPreferences: {
      nodeIntegration:  false,
      contextIsolation: true,
      preload:          path.join(__dirname, 'tabbar-preload.js'),
    },
  });
  mainWindow.contentView.addChildView(tabbarView);
  tabbarView.webContents.loadFile(path.join(__dirname, 'tabbar.html'));
  // Push initial state once the tab bar has finished rendering, then check
  // for content updates in the background (3 s delay to not block startup).
  tabbarView.webContents.once('did-finish-load', () => {
    pushTabsUpdate();
    setTimeout(async () => {
      try {
        const result = await updater.checkForUpdate();
        if (result.hasUpdate && !tabbarView.webContents.isDestroyed()) {
          pendingManifest = result.manifest;
          tabbarView.webContents.send('update:available', {
            current:         result.current,
            latest:          result.latest,
            isContentUpdate: result.isContentUpdate,
          });
        }
      } catch (e) {
        console.log('[updater] 检查更新失败:', e.message);
      }
    }, 3000);
  });

  // ── Home (看板) content view ──────────────────────────────────────────────
  const mainView = new WebContentsView({
    webPreferences: {
      nodeIntegration:  false,
      contextIsolation: true,
      preload:          path.join(__dirname, 'preload.js'),
    },
  });
  mainWindow.contentView.addChildView(mainView);
  gViews.set(HOME_ID, { view: mainView, title: '看板' });
  gActiveId = HOME_ID;
  updateLayout();

  // External links from the dashboard open in the system browser.
  mainView.webContents.on('will-navigate', (e, targetUrl) => {
    try {
      const { hostname, protocol } = new URL(targetUrl);
      if (protocol !== 'file:' && hostname !== '127.0.0.1' && hostname !== 'localhost') {
        e.preventDefault();
        shell.openExternal(targetUrl);
      }
    } catch { e.preventDefault(); }
  });
  mainView.webContents.setWindowOpenHandler(({ url: targetUrl }) => {
    shell.openExternal(targetUrl);
    return { action: 'deny' };
  });

  mainWindow.on('resize',     updateLayout);
  mainWindow.on('maximize',   updateLayout);
  mainWindow.on('unmaximize', updateLayout);
  mainWindow.on('closed',     () => { mainWindow = null; });

  if (isConfigured()) {
    startDashboard();
    navigateToDashboard();
  } else {
    mainView.webContents.loadFile(path.join(__dirname, 'setup.html'));
  }
}

// ── App lifecycle ──────────────────────────────────────────────────────────

app.whenReady().then(() => {
  registerIPC();
  setupNativeMenu();
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
