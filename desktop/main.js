'use strict';

/**
 * QevosAgent Desktop — Electron main process
 *
 * Window layout (content area, below the native title bar):
 *   ┌──────────────────────────────────────────────┐
 *   │ ⚡ │ 看板 │ View A × │ View B × │       ⚙   │  ← tabbar.html  (TAB_H px)
 *   ├──────────────────────────────────────────────┤
 *   │                                              │
 *   │              dashboard / view                │  ← content WebContentsView
 *   │                                              │
 *   └──────────────────────────────────────────────┘
 *
 * Tab system (Electron-only, browser mode unchanged):
 *   - tabbar.html is a separate WebContentsView pinned to the top.
 *   - Each web_show call POSTs to /api/open-view → server.js emits 'open-view'
 *     → main.js creates a new content WebContentsView and updates the tab bar.
 *   - Switching tabs hides/shows views via setBounds() — no page reloads.
 *   - "看板" tab is permanent and cannot be closed.
 *   - Settings (⚙) in the tab bar opens the in-dashboard settings panel.
 *
 * IPC (content views → main, via preload.js):
 *   dialog:pickFolder → native folder picker (LLM config now lives in the
 *                       dashboard's /api/env HTTP API, not in IPC)
 *
 * IPC (tabbar.html → main, via tabbar-preload.js):
 *   tab-activate id → switch to that view
 *   tab-close    id → destroy that view
 *   tab-settings    → open the in-dashboard settings panel (window.openSettings)
 */

const { app, BrowserWindow, WebContentsView, ipcMain, Menu, shell, nativeImage, dialog } = require('electron');
const { t } = require('./i18n');
const path    = require('path');
const http    = require('http');
const net     = require('net');
const fs      = require('fs');
const { getAppIconPath } = require('./icon-path');

// Must be called before app is ready so Windows associates this process
// with the correct AppUserModelID — without it the taskbar button falls
// back to the default Electron icon even when BrowserWindow.icon is set.
app.setAppUserModelId('com.qevosagent.desktop');

// ── Paths ──────────────────────────────────────────────────────────────────

const VENDOR_APP  = path.join(__dirname, 'vendor', 'app');
const APP_ROOT    = app.isPackaged ? VENDOR_APP : path.resolve(__dirname, '..');
// When packaged, .env lives next to the agent on Windows (install dir is writable
// and each installation is independent). On macOS/Linux the app bundle is read-only,
// so .env must live in the OS user-data directory.
const DOT_ENV_DIR = app.isPackaged
  ? (process.platform === 'win32' ? VENDOR_APP : app.getPath('userData'))
  : path.resolve(__dirname, '..');

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

let PORT  = parseInt(process.env.DASHBOARD_PORT || '8765', 10);
const TAB_H = 33; // matches the dashboard topbar height (grid-template-rows: 33px …)

// ── State ──────────────────────────────────────────────────────────────────

const HOME_ID = 'dashboard';
const gViews  = new Map();  // viewId → { view: WebContentsView, title: string }
let gActiveId        = HOME_ID;
let mainWindow       = null;
let tabbarView       = null;
let dashboardStarted = false;


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
    { id: HOME_ID, title: 'Dashboard', isHome: true },
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

// allowNavigation=false (default): content view locked to dashboard URL,
//   all link clicks open in the system browser (original web_show behaviour).
// allowNavigation=true: browser automation view, in-page navigation is
//   allowed; only new-window requests are sent to the system browser.
// activate=true (default): switch the visible tab to this view. Agent-driven
//   web_show passes activate only when the user is on the Dashboard, so a new
//   display pops up but a second one never yanks them off the view they're
//   already watching.
function openElectronView(displayId, url, title, allowNavigation = false, activate = true) {
  const id = 'view-' + displayId;
  if (gViews.has(id)) {
    // Replace mode: reload the existing view with updated content.
    gViews.get(id).view.webContents.loadURL(url);
    if (activate) activateView(id);
    return;
  }

  const view = new WebContentsView({
    webPreferences: { nodeIntegration: false, contextIsolation: true },
  });

  if (!allowNavigation) {
    // Lock content views to their dashboard URL.
    view.webContents.on('will-navigate', (e, targetUrl) => {
      e.preventDefault();
      shell.openExternal(targetUrl);
    });
  }
  // In both modes, target="_blank" / window.open goes to the system browser.
  view.webContents.setWindowOpenHandler(({ url: targetUrl }) => {
    shell.openExternal(targetUrl);
    return { action: 'deny' };
  });

  mainWindow.contentView.addChildView(view);
  view.webContents.loadURL(url);
  gViews.set(id, { view, title: title || displayId });
  if (activate) {
    activateView(id);
  } else {
    // Render the new tab in the bar and lay it out hidden, but stay on the
    // currently active view.
    updateLayout();
    pushTabsUpdate();
  }
}

function closeView(id) {
  if (id === HOME_ID) return; // Dashboard is permanent
  const entry = gViews.get(id);
  if (!entry) return;
  mainWindow.contentView.removeChildView(entry.view);
  entry.view.webContents.close();
  gViews.delete(id);
  if (gActiveId === id) activateView(HOME_ID);
  else pushTabsUpdate();
}

// ── Cursor overlay helper ──────────────────────────────────────────────────
// Injects / updates a lightweight DOM element that shows an orange dot and
// (x, y) label at the given page coordinates.  pointer-events:none means it
// never blocks real input events, but it DOES appear in capturePage() PNGs so
// the Agent can verify cursor position from a screenshot.

function cursorOverlayJS(x, y, code) {
  return `(function(x,y,code){
    var c=document.getElementById('__qc__');
    if(!c){
      c=document.createElement('div'); c.id='__qc__';
      c.style.cssText='position:fixed;pointer-events:none;z-index:2147483647;display:flex;align-items:center;gap:4px;transform:translate(4px,-50%)';
      var dot=document.createElement('div');
      dot.style.cssText='width:14px;height:14px;border-radius:50%;background:rgba(255,90,0,0.9);border:2px solid #fff;box-shadow:0 0 0 1px rgba(0,0,0,0.4),0 2px 5px rgba(0,0,0,0.35);flex-shrink:0';
      var lbl=document.createElement('div'); lbl.id='__qc_lbl__';
      lbl.style.cssText='background:rgba(0,0,0,0.72);color:#fff;font:bold 11px monospace;padding:1px 5px;border-radius:3px;white-space:nowrap';
      c.appendChild(dot);c.appendChild(lbl);
      document.documentElement.appendChild(c);
    }
    c.style.left=x+'px'; c.style.top=y+'px';
    c.dataset.code=code;
    document.getElementById('__qc_lbl__').textContent='#'+code+' ('+x+','+y+')';
  })(${x},${y},${JSON.stringify(code)})`;
}

/** Generate a 4-char uppercase hex nonce for one cursor overlay update. */
function cursorCode() {
  return Math.random().toString(16).slice(2, 6).toUpperCase();
}

// ── Dashboard server ───────────────────────────────────────────────────────

function findFreePort(startPort) {
  return new Promise(resolve => {
    const sock = net.connect(startPort, '127.0.0.1');
    sock.once('connect', () => { sock.destroy(); findFreePort(startPort + 1).then(resolve); });
    sock.once('error',   () => { sock.destroy(); resolve(startPort); });
  });
}

async function startDashboard() {
  if (dashboardStarted) return;
  dashboardStarted = true;

  PORT = await findFreePort(PORT);
  const userData   = app.getPath('userData');
  // On Windows the install directory is writable and each installation is
  // independent, so SKILLS live directly in APP_ROOT (no seeding needed).
  // On macOS/Linux the app bundle is read-only; seed built-in skills into
  // userData on first launch so they remain editable across upgrades.
  const userSkills = process.platform === 'win32'
    ? path.join(APP_ROOT, 'SKILLS')
    : path.join(userData, 'SKILLS');
  fs.mkdirSync(userData, { recursive: true });

  if (process.platform !== 'win32' && !fs.existsSync(userSkills)) {
    const bundleSkills = path.join(APP_ROOT, 'SKILLS');
    if (fs.existsSync(bundleSkills)) {
      fs.mkdirSync(userSkills, { recursive: true });
      for (const f of fs.readdirSync(bundleSkills)) {
        fs.copyFileSync(path.join(bundleSkills, f), path.join(userSkills, f));
      }
    }
  }

  // Same first-launch seeding for the bundled example cron files. On Windows
  // the install dir is writable and is itself APP_ROOT, so the bundled
  // crons/ folder is already in place.
  if (process.platform !== 'win32') {
    const userCrons   = path.join(userData, 'crons');
    const bundleCrons = path.join(APP_ROOT, 'crons');
    if (!fs.existsSync(userCrons) && fs.existsSync(bundleCrons)) {
      fs.mkdirSync(userCrons, { recursive: true });
      for (const f of fs.readdirSync(bundleCrons)) {
        if (f.startsWith('.')) continue;
        fs.copyFileSync(path.join(bundleCrons, f), path.join(userCrons, f));
      }
    }
  }

  process.env.DASHBOARD_PORT   = String(PORT);
  process.env.PYTHONUTF8       = '1';
  process.env.PYTHONIOENCODING = 'utf-8';
  process.env.ELECTRON         = '1';
  process.env.AGENT_DIR        = APP_ROOT;
  process.env.PYTHONPATH       = APP_ROOT;
  process.env.APP_VERSION      = app.getVersion();
  // On macOS/Linux the app bundle is read-only, so user data must live in userData.
  // On Windows the install directory is writable; keep runs/ there so existing
  // runs stay visible after upgrades.
  const userDataDir = process.platform === 'win32' ? APP_ROOT : userData;
  process.env.RUNS_DIR         = path.join(userDataDir, 'runs');
  process.env.AGENT_CONCEPT    = path.join(userDataDir, 'memory_macro.md');
  process.env.AGENT_EPISODIC   = path.join(userDataDir, 'memory_episodic.jsonl');
  process.env.SKILLS_DIR       = userSkills;
  process.env.CRONS_DIR        = path.join(userDataDir, 'crons');
  // Tell server.js where the real .env lives so its /api/env write endpoint
  // (used by the in-dashboard settings panel) targets the same file main.js reads.
  process.env.DOTENV_PATH      = path.join(DOT_ENV_DIR, '.env');

  const serverPath = path.join(APP_ROOT, 'dashboard', 'server.js');
  try {
    const { serverEvents } = require(serverPath);
    serverEvents.on('open-view', ({ url, title, displayId }) => {
      if (!mainWindow || mainWindow.isDestroyed()) return;
      // Pop the new view to the front only if the user is currently on the
      // Dashboard (so the first web_show actually shows up). If they're already
      // watching another agent view, just add/refresh the tab so we don't yank
      // them away from what they're watching.
      const activate = gActiveId === HOME_ID;
      openElectronView(displayId, url, title || displayId, false, activate);
    });

    serverEvents.on('browser-action', async ({ displayId, action, payload }, callback) => {
      if (!mainWindow || mainWindow.isDestroyed()) return callback({ error: '主窗口不可用' });
      const id = 'view-' + displayId;
      const entry = gViews.get(id);
      if (!entry && action !== 'new_tab') {
        return callback({ error: `视图 ${displayId} 不存在，请先调用 web_show 创建` });
      }
      try {
        const wc = entry?.view.webContents;
        switch (action) {
          case 'new_tab': {
            openElectronView(displayId, payload.url || 'about:blank', payload.title || displayId, true);
            callback({ ok: true });
            break;
          }
          case 'navigate': {
            await new Promise((resolve) => {
              wc.once('did-finish-load', resolve);
              wc.loadURL(payload.url);
              setTimeout(resolve, 15000);
            });
            callback({ ok: true });
            break;
          }
          case 'eval': {
            const result = await wc.executeJavaScript(payload.code);
            callback({ ok: true, result });
            break;
          }
          case 'get_html': {
            const html = await wc.executeJavaScript('document.documentElement.outerHTML');
            callback({ ok: true, html });
            break;
          }
          case 'screenshot': {
            const img = await wc.capturePage();
            callback({ ok: true, data: img.toPNG().toString('base64') });
            break;
          }
          case 'click': {
            await wc.executeJavaScript(
              `document.querySelector(${JSON.stringify(payload.selector)})?.click()`
            );
            callback({ ok: true });
            break;
          }
          case 'fill': {
            const expr = `(el => { if (el) { el.focus(); el.value = ${JSON.stringify(payload.value)}; ` +
              `el.dispatchEvent(new Event('input', {bubbles:true})); ` +
              `el.dispatchEvent(new Event('change', {bubbles:true})); } })` +
              `(document.querySelector(${JSON.stringify(payload.selector)}))`;
            await wc.executeJavaScript(expr);
            callback({ ok: true });
            break;
          }
          case 'mouse_move': {
            const code = cursorCode();
            wc.sendInputEvent({ type: 'mouseMove', x: payload.x, y: payload.y });
            wc.executeJavaScript(cursorOverlayJS(payload.x, payload.y, code)).catch(() => {});
            callback({ ok: true, cursor: { code, x: payload.x, y: payload.y } });
            break;
          }
          case 'mouse_click': {
            const { x, y, button = 'left', count = 1 } = payload;
            const code = cursorCode();
            wc.sendInputEvent({ type: 'mouseMove', x, y });
            wc.sendInputEvent({ type: 'mouseDown', x, y, button, clickCount: count });
            wc.sendInputEvent({ type: 'mouseUp',   x, y, button, clickCount: count });
            wc.executeJavaScript(cursorOverlayJS(x, y, code)).catch(() => {});
            callback({ ok: true, cursor: { code, x, y } });
            break;
          }
          case 'mouse_down': {
            const code = cursorCode();
            wc.sendInputEvent({ type: 'mouseDown', x: payload.x, y: payload.y, button: payload.button || 'left', clickCount: 1 });
            wc.executeJavaScript(cursorOverlayJS(payload.x, payload.y, code)).catch(() => {});
            callback({ ok: true, cursor: { code, x: payload.x, y: payload.y } });
            break;
          }
          case 'mouse_up': {
            const code = cursorCode();
            wc.sendInputEvent({ type: 'mouseUp', x: payload.x, y: payload.y, button: payload.button || 'left', clickCount: 1 });
            wc.executeJavaScript(cursorOverlayJS(payload.x, payload.y, code)).catch(() => {});
            callback({ ok: true, cursor: { code, x: payload.x, y: payload.y } });
            break;
          }
          case 'drag': {
            const { x1, y1, x2, y2, steps = 10, button = 'left' } = payload;
            const code = cursorCode();
            wc.sendInputEvent({ type: 'mouseMove', x: x1, y: y1 });
            wc.sendInputEvent({ type: 'mouseDown', x: x1, y: y1, button, clickCount: 1 });
            for (let i = 1; i <= steps; i++) {
              const x = Math.round(x1 + (x2 - x1) * i / steps);
              const y = Math.round(y1 + (y2 - y1) * i / steps);
              wc.sendInputEvent({ type: 'mouseMove', x, y });
            }
            wc.sendInputEvent({ type: 'mouseUp', x: x2, y: y2, button, clickCount: 1 });
            wc.executeJavaScript(cursorOverlayJS(x2, y2, code)).catch(() => {});
            callback({ ok: true, cursor: { code, x: x2, y: y2 } });
            break;
          }
          case 'key_type': {
            // insertText bypasses JS event layers — works with React/Vue contenteditable.
            await wc.insertText(payload.text);
            callback({ ok: true });
            break;
          }
          case 'key_press': {
            const ELECTRON_KEY_MAP = {
              Enter: 'Return', Tab: 'Tab', Escape: 'Escape', Backspace: 'Backspace',
              Delete: 'Delete', ArrowUp: 'Up', ArrowDown: 'Down',
              ArrowLeft: 'Left', ArrowRight: 'Right',
              Home: 'Home', End: 'End', PageUp: 'Prior', PageDown: 'Next', Space: 'Space',
            };
            const keyCode = ELECTRON_KEY_MAP[payload.key] || payload.key;
            wc.sendInputEvent({ type: 'keyDown', keyCode });
            wc.sendInputEvent({ type: 'keyUp',   keyCode });
            callback({ ok: true });
            break;
          }
          case 'key_combo': {
            const ELECTRON_KEY_MAP = {
              Enter: 'Return', Tab: 'Tab', Escape: 'Escape', Backspace: 'Backspace',
              Delete: 'Delete', ArrowUp: 'Up', ArrowDown: 'Down',
              ArrowLeft: 'Left', ArrowRight: 'Right',
              Home: 'Home', End: 'End', PageUp: 'Prior', PageDown: 'Next', Space: 'Space',
            };
            const ELECTRON_MOD = { ctrl: 'control', control: 'control', shift: 'shift', alt: 'alt', meta: 'meta', command: 'meta' };
            const keyCode = ELECTRON_KEY_MAP[payload.key] || payload.key;
            const modifiers = (payload.modifiers || []).map(m => ELECTRON_MOD[m.toLowerCase()] || m);
            wc.sendInputEvent({ type: 'keyDown', keyCode, modifiers });
            wc.sendInputEvent({ type: 'keyUp',   keyCode, modifiers });
            callback({ ok: true });
            break;
          }
          case 'scroll': {
            wc.sendInputEvent({
              type: 'mouseWheel', x: payload.x || 0, y: payload.y || 0,
              deltaX: payload.deltaX || 0, deltaY: payload.deltaY || 0,
            });
            callback({ ok: true });
            break;
          }
          default:
            callback({ error: `未知操作: ${action}` });
        }
      } catch (e) {
        callback({ error: e.message });
      }
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
          t('app.server_not_ready', { port: PORT, secs: (maxRetries * intervalMs / 1000).toFixed(0) })
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
      notifyLoadingError(t('app.load_error', { error: startDashboard._error }));
    });
    return;
  }

  waitForServer(30, 500, err => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (err) { notifyLoadingError(err.message); return; }
    mv.webContents.loadURL(`http://127.0.0.1:${PORT}`);
  });
}

// ── Native menu — minimal; tabs live in tabbar.html ───────────────────────

function setupNativeMenu() {
  if (process.platform === 'darwin') {
    // macOS requires an application menu for Cmd+Q and system conventions.
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      {
        label: 'QevosAgent',
        submenu: [
          { role: 'about',     label: t('menu.about') },
          { type: 'separator' },
          { role: 'quit',      label: t('menu.quit') },
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
  // LLM/connection config is now handled entirely by the dashboard's HTTP API
  // (/api/env in dashboard/server.js); the native folder picker remains because
  // it gives a better UX than the browser-mode server-side directory browser.

  ipcMain.handle('dialog:pickFolder', async () => {
    const res = await dialog.showOpenDialog(mainWindow, {
      properties: ['openDirectory'],
    });
    if (res.canceled || !res.filePaths.length) return { canceled: true };
    return { canceled: false, path: res.filePaths[0] };
  });

  // ── Tab bar IPC (tabbar-preload.js) ──────────────────────────────────────

  ipcMain.on('tab-activate', (_, id)  => activateView(id));
  ipcMain.on('tab-close',    (_, id)  => closeView(id));
  // Open the in-dashboard settings panel instead of the old setup.html page.
  ipcMain.on('tab-settings', () => {
    const mv = getMainView();
    if (!mv) return;
    activateView(HOME_ID);
    mv.webContents.executeJavaScript('window.openSettings && window.openSettings()').catch(() => {});
  });
}

// ── BrowserWindow + WebContentsViews ──────────────────────────────────────

function createWindow() {
  const iconPath = getAppIconPath(__dirname, process.platform, app.isPackaged);
  const appIcon  = fs.existsSync(iconPath) ? nativeImage.createFromPath(iconPath) : undefined;

  mainWindow = new BrowserWindow({
    width:           1400,
    height:          900,
    minWidth:        800,
    minHeight:       600,
    title:           'QevosAgent',
    icon:            appIcon || iconPath,
    backgroundColor: '#0d1117',
  });

  // Explicitly set the icon after HWND is created so the taskbar button
  // picks up the custom icon even when Windows has a stale icon cache.
  if (appIcon && !appIcon.isEmpty()) {
    mainWindow.setIcon(appIcon);
  }

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
  tabbarView.webContents.once('did-finish-load', () => {
    pushTabsUpdate();
  });

  // ── Home (Dashboard) content view ──────────────────────────────────────────────
  const mainView = new WebContentsView({
    webPreferences: {
      nodeIntegration:  false,
      contextIsolation: true,
      preload:          path.join(__dirname, 'preload.js'),
    },
  });
  mainWindow.contentView.addChildView(mainView);
  gViews.set(HOME_ID, { view: mainView, title: 'Dashboard' });
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

  // Always start the dashboard; first-run configuration is handled by the
  // in-dashboard settings panel (which auto-opens when OPENAI_BASE_URL is unset).
  // This unifies Electron and browser mode on one settings flow — setup.html is
  // no longer used as the first-run gate.
  startDashboard().then(() => navigateToDashboard());
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
