'use strict';

/**
 * simpleAgent Desktop — Electron main process
 *
 * Startup flow:
 *   1. Load .env from resources/ (packaged) or simpleAgent/ (dev)
 *   2. If OPENAI_BASE_URL is not set → show setup.html (settings page)
 *      else → show loading.html → start dashboard → navigate to http://localhost:PORT
 *
 * IPC channels (see preload.js):
 *   env:read        → return current settings
 *   env:save        → write .env file and update process.env
 *   dashboard:open  → start server + navigate window to dashboard
 *
 * Application menu:
 *   simpleAgent > 设置  → reopen setup.html at any time
 */

const { app, BrowserWindow, ipcMain, Menu } = require('electron');
const path = require('path');
const http = require('http');
const fs   = require('fs');

// ── Paths ──────────────────────────────────────────────────────────────────

// vendor/app/ is created by `npm run setup` and holds agent/dashboard/run_goal.py.
// Used as APP_ROOT when packaged; in dev mode use the repo root instead.
const VENDOR_APP  = path.join(__dirname, 'vendor', 'app');
const APP_ROOT    = app.isPackaged ? VENDOR_APP : path.resolve(__dirname, '..');

// .env lives one level above __dirname:
//   dev      → simpleAgent/          (standard location)
//   packaged → resources/            (accessible after install)
const DOT_ENV_DIR = path.resolve(__dirname, '..');

// ── Load .env ──────────────────────────────────────────────────────────────
// Mirrors run_goal.py's load_dotenv_if_present().
// Existing process.env values always win.

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
// vendor/python/python.exe is created by `npm run setup`.
// It takes priority over any PYTHON_CMD in .env.

const EMBEDDED_PYTHON = path.join(__dirname, 'vendor', 'python', 'python.exe');
if (fs.existsSync(EMBEDDED_PYTHON)) {
  process.env.PYTHON_CMD = EMBEDDED_PYTHON;
  console.log('[desktop] Embedded Python:', EMBEDDED_PYTHON);
} else if (!process.env.PYTHON_CMD) {
  console.warn(
    '[desktop] No embedded Python found and PYTHON_CMD is not set.\n' +
    '          Run "npm run setup" first, or set PYTHON_CMD in .env.'
  );
}

// ── Config ─────────────────────────────────────────────────────────────────

const PORT = parseInt(process.env.DASHBOARD_PORT || '8765', 10);

// ── State ──────────────────────────────────────────────────────────────────

let mainWindow       = null;
let dashboardStarted = false;

// ── Dashboard server ───────────────────────────────────────────────────────
// server.js is require()'d in-process — no separate node binary needed.

function startDashboard() {
  if (dashboardStarted) return;
  dashboardStarted = true;

  process.env.DASHBOARD_PORT   = String(PORT);
  process.env.PYTHONUTF8       = '1';
  process.env.PYTHONIOENCODING = 'utf-8';
  process.env.AGENT_DIR        = APP_ROOT;
  process.env.PYTHONPATH       = APP_ROOT;

  const serverPath = path.join(APP_ROOT, 'dashboard', 'server.js');
  try {
    require(serverPath);
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

// ── Error display in loading.html ─────────────────────────────────────────

function notifyLoadingError(msg) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const escaped = msg.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n');
  mainWindow.webContents
    .executeJavaScript(`typeof showError === 'function' && showError("${escaped}")`)
    .catch(() => {});
}

// ── Navigate window to the running dashboard ──────────────────────────────

function navigateToDashboard() {
  if (!mainWindow || mainWindow.isDestroyed()) return;

  mainWindow.loadFile(path.join(__dirname, 'loading.html'));

  if (startDashboard._error) {
    mainWindow.webContents.once('did-finish-load', () => {
      notifyLoadingError(`无法加载 Dashboard：${startDashboard._error}`);
    });
    return;
  }

  waitForServer(30, 500, err => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (err) { notifyLoadingError(err.message); return; }
    mainWindow.loadURL(`http://127.0.0.1:${PORT}`);
  });
}

// ── Check if the LLM endpoint is configured ────────────────────────────────

function isConfigured() {
  return !!(
    process.env.OPENAI_BASE_URL ||
    process.env.OPENAI_PROFILE_OSS120B_BASE_URL ||
    process.env.OPENAI_PROFILE_QWEN3527DGX_BASE_URL
  );
}

// ── IPC handlers ──────────────────────────────────────────────────────────

function registerIPC() {
  // Return current settings to the renderer
  ipcMain.handle('env:read', () => ({
    OPENAI_BASE_URL: process.env.OPENAI_BASE_URL || '',
    OPENAI_API_KEY:  process.env.OPENAI_API_KEY  || '',
    OPENAI_MODEL:    process.env.OPENAI_MODEL     || '',
    MAX_ITERS:       process.env.MAX_ITERS        || '100',
  }));

  // Write .env file and update process.env immediately
  ipcMain.handle('env:save', (_, data) => {
    const envPath = path.join(DOT_ENV_DIR, '.env');

    // Read existing .env to preserve unrelated keys (e.g. PYTHON_CMD)
    let existing = {};
    try {
      for (const line of fs.readFileSync(envPath, 'utf8').split(/\r?\n/)) {
        const eq = line.indexOf('=');
        if (eq > 0 && !line.trim().startsWith('#')) {
          existing[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
        }
      }
    } catch {}

    // Merge: form data overwrites existing
    const merged = { ...existing };
    for (const [k, v] of Object.entries(data)) {
      if (v && v.trim()) merged[k] = v.trim();
    }

    const content = Object.entries(merged)
      .map(([k, v]) => `${k}=${v}`)
      .join('\n') + '\n';

    fs.writeFileSync(envPath, content, 'utf8');

    // Sync into process.env so the running process picks them up
    for (const [k, v] of Object.entries(merged)) {
      process.env[k] = v;
    }

    return { ok: true };
  });

  // Start dashboard server and navigate the window to it
  ipcMain.handle('dashboard:open', () => {
    startDashboard();
    navigateToDashboard();
    return { ok: true };
  });

}

// ── Application menu ───────────────────────────────────────────────────────

function buildMenu() {
  return Menu.buildFromTemplate([
    {
      label: 'simpleAgent',
      submenu: [
        {
          label: '⚙  设置',
          accelerator: 'CmdOrCtrl+,',
          click: () => {
            if (mainWindow && !mainWindow.isDestroyed()) {
              mainWindow.loadFile(path.join(__dirname, 'setup.html'));
            }
          },
        },
        { type: 'separator' },
        { role: 'quit', label: '退出' },
      ],
    },
    {
      label: '视图',
      submenu: [
        { role: 'reload',         label: '刷新' },
        { role: 'toggleDevTools', label: '开发者工具' },
        { type: 'separator' },
        { role: 'resetZoom',      label: '重置缩放' },
        { role: 'zoomIn',         label: '放大' },
        { role: 'zoomOut',        label: '缩小' },
      ],
    },
  ]);
}

// ── BrowserWindow ──────────────────────────────────────────────────────────

function createWindow() {
  mainWindow = new BrowserWindow({
    width:           1400,
    height:          900,
    minWidth:        800,
    minHeight:       600,
    title:           'simpleAgent',
    backgroundColor: '#0d1117',
    webPreferences: {
      nodeIntegration:  false,
      contextIsolation: true,
      preload:          path.join(__dirname, 'preload.js'),
    },
  });

  if (isConfigured()) {
    // Already configured: go straight to dashboard
    startDashboard();
    navigateToDashboard();
  } else {
    // First run: show settings page
    mainWindow.loadFile(path.join(__dirname, 'setup.html'));
  }

  mainWindow.on('closed', () => { mainWindow = null; });
}

// ── App lifecycle ──────────────────────────────────────────────────────────

app.whenReady().then(() => {
  registerIPC();
  Menu.setApplicationMenu(buildMenu());
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  // server.js registers process.on('exit', cleanup) to kill any running agent.
  if (process.platform !== 'darwin') app.quit();
});
