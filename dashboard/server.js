'use strict';

/**
 * QevosAgent Dashboard Server
 * ─────────────────────────────
 * - Polls the runs/ directory every POLL_MS milliseconds
 * - Streams state updates to all WebSocket clients
 * - Accepts POST /api/inject  — inject a slash command into running agent
 * - Accepts POST /api/launch  — spawn a new agent process with a goal
 * - Accepts POST /api/kill    — terminate the running agent process
 * - Serves static files from ./public/
 *
 * Auto-launch prerequisite:
 *   Start this server from within the correct conda/venv environment so that
 *   `python` resolves to the right interpreter.  Override with PYTHON_CMD env var.
 *   Example:
 *     conda activate myenv && node dashboard/server.js
 */

const http         = require('http');
const fs           = require('fs');
const path         = require('path');
const os           = require('os');
const { spawn }    = require('child_process');
const WebSocket    = require('ws');
const EventEmitter = require('events');

// Emits 'open-view' when Electron should open a view tab.
// main.js listens to this because both files run in the same Node process.
const serverEvents = new EventEmitter();
module.exports = { serverEvents };

// ── Load .env (standalone mode — Electron already loads it in main.js) ────────
// Only sets keys that are not already present in process.env.
try {
  const envPath = path.join(__dirname, '..', '.env');
  for (const rawLine of fs.readFileSync(envPath, 'utf8').split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
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
} catch { /* .env not found — fine */ }

// ── Language ──────────────────────────────────────────────────────────────────
const LANG = (() => {
  const override = process.env.QEVOS_LANG || '';
  if (override) return override.toLowerCase().startsWith('zh') ? 'zh' : 'en';
  try {
    const sys = Intl.DateTimeFormat().resolvedOptions().locale || process.env.LANG || '';
    return sys.toLowerCase().startsWith('zh') ? 'zh' : 'en';
  } catch { return 'zh'; }
})();

// ── CDP browser automation (non-Electron mode) ─────────────────────────────
const CDP_PORT = parseInt(process.env.CDP_PORT || '9222', 10);
const cdpTargets = new Map(); // display_id → CDP targetId

function cdpHttp(urlPath) {
  return new Promise((resolve, reject) => {
    http.get(`http://127.0.0.1:${CDP_PORT}${urlPath}`, res => {
      let data = '';
      res.on('data', c => data += c);
      res.on('end', () => { try { resolve(JSON.parse(data)); } catch (e) { reject(e); } });
    }).on('error', reject);
  });
}

// CDP key map: web standard key names → { key, code, windowsVirtualKeyCode }
const CDP_KEY_MAP = {
  Enter:      { key: 'Enter',     code: 'Enter',      windowsVirtualKeyCode: 13  },
  Tab:        { key: 'Tab',       code: 'Tab',        windowsVirtualKeyCode: 9   },
  Escape:     { key: 'Escape',    code: 'Escape',     windowsVirtualKeyCode: 27  },
  Backspace:  { key: 'Backspace', code: 'Backspace',  windowsVirtualKeyCode: 8   },
  Delete:     { key: 'Delete',    code: 'Delete',     windowsVirtualKeyCode: 46  },
  ArrowUp:    { key: 'ArrowUp',   code: 'ArrowUp',    windowsVirtualKeyCode: 38  },
  ArrowDown:  { key: 'ArrowDown', code: 'ArrowDown',  windowsVirtualKeyCode: 40  },
  ArrowLeft:  { key: 'ArrowLeft', code: 'ArrowLeft',  windowsVirtualKeyCode: 37  },
  ArrowRight: { key: 'ArrowRight',code: 'ArrowRight', windowsVirtualKeyCode: 39  },
  Home:       { key: 'Home',      code: 'Home',       windowsVirtualKeyCode: 36  },
  End:        { key: 'End',       code: 'End',        windowsVirtualKeyCode: 35  },
  PageUp:     { key: 'PageUp',    code: 'PageUp',     windowsVirtualKeyCode: 33  },
  PageDown:   { key: 'PageDown',  code: 'PageDown',   windowsVirtualKeyCode: 34  },
  Space:      { key: ' ',         code: 'Space',      windowsVirtualKeyCode: 32  },
};

// One-shot: open WS, send one command, get result, close.
function cdpSend(wsUrl, method, params = {}) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const timer = setTimeout(() => { ws.terminate(); reject(new Error('CDP timeout')); }, 10000);
    ws.once('open', () => ws.send(JSON.stringify({ id: 1, method, params })));
    ws.on('message', raw => {
      const msg = JSON.parse(String(raw));
      if (msg.id === 1) {
        clearTimeout(timer); ws.close();
        if (msg.error) reject(new Error(msg.error.message));
        else resolve(msg.result || {});
      }
    });
    ws.once('error', err => { clearTimeout(timer); ws.close(); reject(err); });
  });
}

// JS injected into the page to show/update the cursor position overlay.
// Uses pointer-events:none so it never blocks clicks; z-index max so always on top.
// The orange dot + label "#CODE (x, y)" appears in screenshots taken by the agent.
// `code` is a fresh 4-hex nonce generated per action so the LLM can unambiguously
// locate *this* overlay rather than any similar-looking element on the page.
function cursorOverlayJS(x, y, code) {
  return `(function(x,y,code){
    var c=document.getElementById('__qc__');
    if(!c){
      c=document.createElement('div');
      c.id='__qc__';
      c.style.cssText='position:fixed;pointer-events:none;z-index:2147483647;display:flex;align-items:center;gap:4px;transform:translate(4px,-50%)';
      var dot=document.createElement('div');
      dot.style.cssText='width:14px;height:14px;border-radius:50%;background:rgba(255,90,0,0.9);border:2px solid #fff;box-shadow:0 0 0 1px rgba(0,0,0,0.4),0 2px 5px rgba(0,0,0,0.35);flex-shrink:0';
      var lbl=document.createElement('div');
      lbl.id='__qc_lbl__';
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

// Send multiple commands sequentially on one WebSocket connection.
function cdpSendSeq(wsUrl, commands) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const results = [];
    let idx = 0;
    const timer = setTimeout(() => { ws.terminate(); reject(new Error('CDP timeout')); }, 10000);
    const sendNext = () => ws.send(JSON.stringify({ id: idx + 1, method: commands[idx].method, params: commands[idx].params || {} }));
    ws.once('open', sendNext);
    ws.on('message', raw => {
      const msg = JSON.parse(String(raw));
      if (msg.id === idx + 1) {
        if (msg.error) { clearTimeout(timer); ws.close(); return reject(new Error(msg.error.message)); }
        results.push(msg.result || {});
        idx++;
        if (idx < commands.length) sendNext();
        else { clearTimeout(timer); ws.close(); resolve(results); }
      }
    });
    ws.once('error', err => { clearTimeout(timer); ws.close(); reject(err); });
  });
}

// Navigate and wait for Page.loadEventFired (max 15 s).
function cdpNavigate(wsUrl, url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    let navigated = false;
    const timer = setTimeout(() => { ws.close(); resolve({ ok: true, note: 'load timeout' }); }, 15000);
    ws.once('open', () => ws.send(JSON.stringify({ id: 1, method: 'Page.enable', params: {} })));
    ws.on('message', raw => {
      const msg = JSON.parse(String(raw));
      if (msg.id === 1) {
        ws.send(JSON.stringify({ id: 2, method: 'Page.navigate', params: { url } }));
      } else if (msg.id === 2) {
        navigated = true;
      } else if (msg.method === 'Page.loadEventFired' && navigated) {
        clearTimeout(timer); ws.close(); resolve({ ok: true });
      }
    });
    ws.once('error', err => { clearTimeout(timer); ws.close(); reject(err); });
  });
}

async function cdpBrowserAction(displayId, action, payload) {
  let pages;
  try {
    const all = await cdpHttp('/json');
    pages = all.filter(t => t.type === 'page');
  } catch {
    throw new Error(
      `无法连接到浏览器 CDP（端口 ${CDP_PORT}）。` +
      `请以 --remote-debugging-port=${CDP_PORT} 启动 Chrome/Edge 后重试。`
    );
  }

  if (action === 'new_tab') {
    const url = payload.url || 'about:blank';
    const tab = await cdpHttp(`/json/new?${encodeURIComponent(url)}`);
    cdpTargets.set(displayId, tab.id);
    return { ok: true, targetId: tab.id, url: tab.url };
  }

  const targetId = cdpTargets.get(displayId);
  const target = pages.find(t => t.id === targetId) || pages[0];
  if (!target) throw new Error('没有可用的浏览器标签页，请先调用 new_tab');
  cdpTargets.set(displayId, target.id);
  const wsUrl = target.webSocketDebuggerUrl;

  switch (action) {
    case 'navigate':
      return await cdpNavigate(wsUrl, payload.url);
    case 'eval': {
      const r = await cdpSend(wsUrl, 'Runtime.evaluate', {
        expression: payload.code, returnByValue: true, awaitPromise: true,
      });
      return { ok: true, result: r.result?.value ?? r.result };
    }
    case 'get_html': {
      const r = await cdpSend(wsUrl, 'Runtime.evaluate', {
        expression: 'document.documentElement.outerHTML', returnByValue: true,
      });
      return { ok: true, html: r.result?.value };
    }
    case 'screenshot': {
      const r = await cdpSend(wsUrl, 'Page.captureScreenshot', { format: 'png' });
      return { ok: true, data: r.data };
    }
    case 'click': {
      await cdpSend(wsUrl, 'Runtime.evaluate', {
        expression: `document.querySelector(${JSON.stringify(payload.selector)})?.click()`,
      });
      return { ok: true };
    }
    case 'fill': {
      const expr = `(el => { if (el) { el.focus(); el.value = ${JSON.stringify(payload.value)}; ` +
        `el.dispatchEvent(new Event('input', {bubbles:true})); ` +
        `el.dispatchEvent(new Event('change', {bubbles:true})); } })` +
        `(document.querySelector(${JSON.stringify(payload.selector)}))`;
      await cdpSend(wsUrl, 'Runtime.evaluate', { expression: expr });
      return { ok: true };
    }
    case 'mouse_move': {
      const code = cursorCode();
      await cdpSendSeq(wsUrl, [
        { method: 'Input.dispatchMouseEvent', params: { type: 'mouseMoved', x: payload.x, y: payload.y, button: 'none' } },
        { method: 'Runtime.evaluate', params: { expression: cursorOverlayJS(payload.x, payload.y, code) } },
      ]);
      return { ok: true, cursor: { code, x: payload.x, y: payload.y } };
    }
    case 'mouse_click': {
      const { x, y, button = 'left', count = 1 } = payload;
      const code = cursorCode();
      await cdpSendSeq(wsUrl, [
        { method: 'Input.dispatchMouseEvent', params: { type: 'mouseMoved',   x, y, button: 'none' } },
        { method: 'Input.dispatchMouseEvent', params: { type: 'mousePressed', x, y, button, clickCount: count } },
        { method: 'Input.dispatchMouseEvent', params: { type: 'mouseReleased',x, y, button, clickCount: count } },
        { method: 'Runtime.evaluate', params: { expression: cursorOverlayJS(x, y, code) } },
      ]);
      return { ok: true, cursor: { code, x, y } };
    }
    case 'mouse_down': {
      const code = cursorCode();
      await cdpSendSeq(wsUrl, [
        { method: 'Input.dispatchMouseEvent', params: { type: 'mousePressed', x: payload.x, y: payload.y, button: payload.button || 'left', clickCount: 1 } },
        { method: 'Runtime.evaluate', params: { expression: cursorOverlayJS(payload.x, payload.y, code) } },
      ]);
      return { ok: true, cursor: { code, x: payload.x, y: payload.y } };
    }
    case 'mouse_up': {
      const code = cursorCode();
      await cdpSendSeq(wsUrl, [
        { method: 'Input.dispatchMouseEvent', params: { type: 'mouseReleased', x: payload.x, y: payload.y, button: payload.button || 'left', clickCount: 1 } },
        { method: 'Runtime.evaluate', params: { expression: cursorOverlayJS(payload.x, payload.y, code) } },
      ]);
      return { ok: true, cursor: { code, x: payload.x, y: payload.y } };
    }
    case 'drag': {
      const { x1, y1, x2, y2, steps = 10, button = 'left' } = payload;
      const code = cursorCode();
      const cmds = [
        { method: 'Input.dispatchMouseEvent', params: { type: 'mouseMoved',   x: x1, y: y1, button: 'none' } },
        { method: 'Input.dispatchMouseEvent', params: { type: 'mousePressed', x: x1, y: y1, button, clickCount: 1 } },
      ];
      for (let i = 1; i <= steps; i++) {
        const x = Math.round(x1 + (x2 - x1) * i / steps);
        const y = Math.round(y1 + (y2 - y1) * i / steps);
        cmds.push({ method: 'Input.dispatchMouseEvent', params: { type: 'mouseMoved', x, y, button } });
      }
      cmds.push({ method: 'Input.dispatchMouseEvent', params: { type: 'mouseReleased', x: x2, y: y2, button, clickCount: 1 } });
      cmds.push({ method: 'Runtime.evaluate', params: { expression: cursorOverlayJS(x2, y2, code) } });
      await cdpSendSeq(wsUrl, cmds);
      return { ok: true, cursor: { code, x: x2, y: y2 } };
    }
    case 'key_type': {
      // Input.insertText bypasses JS event layers — works with React/Vue contenteditable.
      await cdpSend(wsUrl, 'Input.insertText', { text: payload.text });
      return { ok: true };
    }
    case 'key_press': {
      const kdef = CDP_KEY_MAP[payload.key];
      if (!kdef) throw new Error(`不支持的键名: ${payload.key}。支持: ${Object.keys(CDP_KEY_MAP).join(', ')}`);
      await cdpSendSeq(wsUrl, [
        { method: 'Input.dispatchKeyEvent', params: { type: 'keyDown', ...kdef } },
        { method: 'Input.dispatchKeyEvent', params: { type: 'keyUp',   ...kdef } },
      ]);
      return { ok: true };
    }
    case 'key_combo': {
      const kdef = CDP_KEY_MAP[payload.key] || { key: payload.key, code: payload.key, windowsVirtualKeyCode: 0 };
      // CDP modifiers bitmask: Alt=1, Ctrl=2, Meta=4, Shift=8
      const CDP_MOD = { alt: 1, ctrl: 2, control: 2, meta: 4, command: 4, shift: 8 };
      const modBits = (payload.modifiers || []).reduce((acc, m) => acc | (CDP_MOD[m.toLowerCase()] || 0), 0);
      await cdpSendSeq(wsUrl, [
        { method: 'Input.dispatchKeyEvent', params: { type: 'keyDown', ...kdef, modifiers: modBits } },
        { method: 'Input.dispatchKeyEvent', params: { type: 'keyUp',   ...kdef, modifiers: modBits } },
      ]);
      return { ok: true };
    }
    case 'scroll': {
      await cdpSend(wsUrl, 'Input.dispatchMouseEvent', {
        type: 'mouseWheel', x: payload.x || 0, y: payload.y || 0,
        deltaX: payload.deltaX || 0, deltaY: payload.deltaY || 0,
      });
      return { ok: true };
    }
    default:
      throw new Error(`未知操作: ${action}。支持: new_tab / navigate / eval / get_html / screenshot / click / fill / mouse_move / mouse_click / mouse_down / mouse_up / drag / key_type / key_press / key_combo / scroll`);
  }
}

// ── Network info (computed once at startup) ────────────────────────────────

function getLanIps() {
  const ips = [];
  for (const ifaces of Object.values(os.networkInterfaces())) {
    for (const iface of ifaces) {
      if (iface.family !== 'IPv4' || iface.internal) continue;
      const p = iface.address.split('.').map(Number);
      const isPrivate = p[0] === 10
        || (p[0] === 172 && p[1] >= 16 && p[1] <= 31)
        || (p[0] === 192 && p[1] === 168);
      if (isPrivate) ips.push(iface.address);
    }
  }
  return ips;
}

const NETWORK_INFO = { hostname: os.hostname(), ips: getLanIps() };

const PORT       = parseInt(process.env.DASHBOARD_PORT || '8765', 10);
// 默认只绑回环地址，避免在没有鉴权时被内网其他主机直接访问。
// 等加上密码鉴权后，再通过 DASHBOARD_HOST=0.0.0.0 显式开放局域网。
const HOST       = process.env.DASHBOARD_HOST || '127.0.0.1';

// ── Access control: IP 白名单 / 黑名单 ──────────────────────────────────────
// DASHBOARD_HOST 只决定 socket 绑到哪个网卡（要不要暴露到局域网）；更细的
// 「谁能访问」由下面两个变量控制，逗号/空格分隔，支持精确 IP 或 IPv4 CIDR：
//   DASHBOARD_ALLOW=192.168.1.0/24,10.0.0.5      白名单
//   DASHBOARD_DENY=192.168.1.66,192.168.2.0/24   黑名单
// 每个远端 IP 的判定顺序：
//   1. 回环地址 (127.0.0.1 / ::1) 永远放行——避免把本机/桌面端自己锁在门外。
//   2. 命中 DENY  → 拒绝（黑名单优先于白名单）。
//   3. ALLOW 非空且未命中 → 拒绝（白名单模式：只放行名单内的）。
//   4. 其余放行。
// ALLOW 为空 = 不启用白名单，行为与旧版完全一致（DASHBOARD_HOST=0.0.0.0 时
// 局域网全网可达）。规则里可用 `*` 表示匹配所有。
function parseIpList(raw) {
  return (raw || '').split(/[\s,]+/).map(s => s.trim()).filter(Boolean);
}
const ALLOW_LIST = parseIpList(process.env.DASHBOARD_ALLOW);
const DENY_LIST  = parseIpList(process.env.DASHBOARD_DENY);

// 把 IPv4-mapped IPv6 (::ffff:1.2.3.4) 还原成纯 IPv4，方便规则匹配。
function normalizeIp(ip) {
  if (!ip) return '';
  const m = /^::ffff:(\d+\.\d+\.\d+\.\d+)$/i.exec(ip);
  return m ? m[1] : ip;
}
function ipv4ToInt(ip) {
  const p = String(ip).split('.');
  if (p.length !== 4) return null;
  let n = 0;
  for (const part of p) {
    const b = Number(part);
    if (!Number.isInteger(b) || b < 0 || b > 255) return null;
    n = (n * 256) + b;
  }
  return n >>> 0;
}
// 单条规则匹配：`*` 匹配所有；`a.b.c.d` 精确；`a.b.c.d/N` CIDR；其余按精确字符串
// 比较（覆盖 ::1 等 IPv6 字面量）。
function ipMatchesRule(ip, rule) {
  if (rule === '*') return true;
  if (rule === ip) return true;
  const slash = rule.indexOf('/');
  if (slash > 0) {
    const base = ipv4ToInt(rule.slice(0, slash));
    const bits = Number(rule.slice(slash + 1));
    const addr = ipv4ToInt(ip);
    if (base == null || addr == null || !Number.isInteger(bits) || bits < 0 || bits > 32) return false;
    const mask = bits === 0 ? 0 : (0xffffffff << (32 - bits)) >>> 0;
    return (base & mask) === (addr & mask);
  }
  return false;
}
function ipInList(ip, list) {
  return list.some(rule => ipMatchesRule(ip, rule));
}
function isIpAllowed(rawIp) {
  const ip = normalizeIp(rawIp);
  if (ip === '127.0.0.1' || ip === '::1' || ip === 'localhost') return true;
  if (DENY_LIST.length && ipInList(ip, DENY_LIST)) return false;
  if (ALLOW_LIST.length && !ipInList(ip, ALLOW_LIST)) return false;
  return true;
}
const RUNS_DIR        = path.resolve(process.env.RUNS_DIR        || path.join(__dirname, '..', 'runs'));
const AGENT_DIR       = path.resolve(process.env.AGENT_DIR       || path.join(__dirname, '..'));
const SKILLS_DIR      = path.resolve(process.env.SKILLS_DIR      || path.join(AGENT_DIR, 'SKILLS'));
const CRONS_DIR       = path.resolve(process.env.CRONS_DIR       || path.join(AGENT_DIR, 'crons'));
const APPS_DIR        = path.resolve(process.env.APPS_DIR        || path.join(AGENT_DIR, 'apps'));
const CRONS_HISTORY   = path.join(CRONS_DIR, '.history.jsonl');
const CRONS_PENDING   = path.join(CRONS_DIR, '.pending.json');
const MEMORY_CONCEPT  = path.resolve(process.env.AGENT_CONCEPT   || path.join(AGENT_DIR, 'memory_macro.md'));
const MEMORY_EPISODIC = path.resolve(process.env.AGENT_EPISODIC  || path.join(AGENT_DIR, 'memory_episodic.jsonl'));
// .env file managed by the in-dashboard settings panel. In Electron, main.js sets
// DOTENV_PATH (it knows the real location — install dir on Windows, userData on
// macOS). Standalone/browser mode falls back to the repo-root .env.
const DOTENV_PATH = process.env.DOTENV_PATH || path.join(__dirname, '..', '.env');
const PUBLIC     = path.join(__dirname, 'public');
const POLL_MS    = parseInt(process.env.POLL_MS || '500', 10);

// Version resolution order:
//   1. APP_VERSION env var (set by desktop main.js via app.getVersion(), or
//      by the release workflow which syncs package.json to the pushed tag).
//   2. desktop/package.json — the single source of truth locally. The release
//      flow is: bump this file → commit → tag vX.Y.Z → push. The workflow
//      then re-syncs it from the tag at build time as a forget-proofing net.
let APP_VERSION = process.env.APP_VERSION || 'dev';
if (APP_VERSION === 'dev') {
  try {
    const pkgPath = path.join(__dirname, '..', 'desktop', 'package.json');
    APP_VERSION = JSON.parse(fs.readFileSync(pkgPath, 'utf8')).version || 'dev';
  } catch {}
}
// Python command — default to 'python' so the calling conda env is used
const PYTHON_CMD = process.env.PYTHON_CMD || 'python';

/**
 * Split PYTHON_CMD into [executable, ...args], respecting quoted paths.
 * Handles: plain "python", conda "conda run -n env python",
 * and quoted paths like "\"C:\\Program Files\\python.exe\"".
 */
function parsePythonCmd(cmd) {
  const t = cmd.trim();
  if (t.startsWith('"')) {
    const end = t.indexOf('"', 1);
    if (end > 0) {
      const exe  = t.slice(1, end);
      const rest = t.slice(end + 1).trim();
      return rest ? [exe, ...rest.split(/\s+/)] : [exe];
    }
  }
  return t.split(/\s+/);
}

// ── Agent process state ────────────────────────────────────────────────────

let agentProc    = null;   // child_process handle
let isLaunching  = false;  // true from spawn() until first stdout or error

// ── Shared state (broadcast to all WS clients) ────────────────────────────

let state = {
  runs:         [],
  runSummaries: {},     // { runId: summaryString } — short label for each run
  activeRunId:  null,
  status:       null,
  scratchpad:   '',
  systemPrompt: '',     // assembled system prompt snapshot (run_dir/system_prompt.md)
  patchEvents:  [],     // runtime-patch timeline events derived from patch_log.jsonl
  events:       [],
  meta:         {},
  launching:    false,  // agent is being spawned, not yet writing runs/
  agentPid:     null,
  agentAlive:   false,  // true iff the agent process is confirmed running right now
  webDisplays:  {},     // { display_id: { content_type, title, content, updated_at } }
  fileTabs:     null,   // { tabs: [...], active: string } loaded from file_tabs.json
  networkInfo:  NETWORK_INFO,
  teamNodeId:   null,
  // Advisor observability:
  //   advisorLast    = run_dir/advisor_last.json (latest snapshot, full system+context+advice)
  //   advisorHistory = compact summary of every entry in advisor_log.jsonl
  // The full per-entry payload is available via GET /api/run/:runId/advisor/:idx
  advisorLast:    null,
  advisorHistory: [],
  instanceName: process.env.INSTANCE_NAME || '',  // display-only label shown as "name:port"
};

let _linesProcessed        = 0;
let _mtimes                = {};
let _iterCounter           = 0;
let _webChatLinesProcessed = 0;
let _patchLinesProcessed   = 0;
let _advisorLinesProcessed = 0;

// ── Utilities ──────────────────────────────────────────────────────────────

function readJSON(fp) {
  try { return JSON.parse(fs.readFileSync(fp, 'utf8')); }
  catch { return null; }
}

function readText(fp) {
  try { return fs.readFileSync(fp, 'utf8'); }
  catch { return null; }
}

function mtime(fp) {
  try { return fs.statSync(fp).mtimeMs; }
  catch { return 0; }
}

/**
 * Check whether a PID is alive using signal 0 (no-op signal).
 * Returns true if the process exists, false if ESRCH (no such process).
 * EPERM means the process exists but we can't signal it → still alive.
 */
function isPidAlive(pid) {
  if (!pid || typeof pid !== 'number') return false;
  try { process.kill(pid, 0); return true; }
  catch (e) { return e.code === 'EPERM'; }
}

/**
 * Read the agent.pid file for the given run directory.
 * Returns the PID as a number, or null if missing/invalid.
 */
function readPidFile(runDir) {
  try {
    const raw = fs.readFileSync(path.join(runDir, 'agent.pid'), 'utf8').trim();
    const pid = parseInt(raw, 10);
    return isNaN(pid) ? null : pid;
  } catch { return null; }
}

function changed(fp) {
  const m = mtime(fp);
  if (_mtimes[fp] !== m) { _mtimes[fp] = m; return true; }
  return false;
}

/** Strip ANSI escape codes from a string. */
function stripAnsi(str) {
  // eslint-disable-next-line no-control-regex
  return str.replace(/\x1B\[[0-9;]*[A-Za-z]/g, '').replace(/\x1B\][^\x07]*\x07/g, '');
}

function findRuns() {
  if (!fs.existsSync(RUNS_DIR)) return [];
  try {
    return fs.readdirSync(RUNS_DIR)
      .filter(d => /^\d{8}-\d{6}$/.test(d))
      .sort();
  } catch { return []; }
}

// ── JSONL display helper ───────────────────────────────────────────────────

/**
 * Strip embedded base64 image data from a JSONL string for dashboard display.
 * Replaces {"type":"image","data":"<long_b64>",...} data fields with a size
 * placeholder so the file stays readable without the multi-hundred-KB blobs.
 * The on-disk file is never modified.
 */
function stripBase64FromJsonl(text) {
  return text.split('\n').map(line => {
    if (!line.trim()) return line;
    try {
      const rec = JSON.parse(line);
      if (!Array.isArray(rec.content)) return line;
      let changed = false;
      const stripped = rec.content.map(block => {
        if (block && block.type === 'image' && typeof block.data === 'string' && block.data.length > 256) {
          const kb = Math.round(block.data.length * 3 / 4 / 1024);
          changed = true;
          return { ...block, data: `[base64 ~${kb} KB, stripped for display]` };
        }
        return block;
      });
      if (!changed) return line;
      return JSON.stringify({ ...rec, content: stripped });
    } catch { return line; }
  }).join('\n');
}

// ── short_term.jsonl parser ────────────────────────────────────────────────

function parseLine(raw, lineIdx) {
  let rec;
  try { rec = JSON.parse(raw); } catch { return null; }
  const { role, content } = rec;
  if (!role) return null;
  if (role === '__token__') {
    return { idx: lineIdx, type: 'token_stats', promptEst: rec.prompt_est, contextWindow: rec.context_window, maxTokens: rec.max_tokens };
  }

  // Multimodal messages have content as an array of blocks (text + image).
  // Extract the text blocks for further parsing; note image presence separately.
  let text;
  let hasImages = false;
  if (Array.isArray(content)) {
    hasImages = content.some(b => b && b.type === 'image');
    text = content.filter(b => b && b.type === 'text').map(b => b.text || '').join('\n');
  } else if (typeof content === 'string') {
    text = content;
  } else {
    return null;
  }

  const base = { idx: lineIdx, hasImages };

  if (role === 'assistant') {
    let action;
    try {
      // Strip ```json ... ``` fences — LLM sometimes wraps its JSON in markdown code blocks.
      // This mirrors the fence-stripping logic in llm.py's _parse_llm_output.
      const fenceMatch = text.match(/```(?:json)?\s*([\s\S]*?)```/i);
      const jsonStr = fenceMatch ? fenceMatch[1].trim() : text;
      action = JSON.parse(jsonStr);
      if (!action || typeof action !== 'object') return null;
    } catch { return null; }
    const thought = action.thought || null;
    if (action.action === 'tool_call') {
      return { ...base, type: 'tool_call', tool: action.tool, args: action.args || {}, thought };
    }
    if (action.action === 'done') {
      return { ...base, type: 'done', answer: action.final_answer || '', thought };
    }
    if (action.action === 'error') {
      return { ...base, type: 'error', text: thought || text, thought: null };
    }
    if (thought) return { ...base, type: 'thought', thought };
    return null;
  }

  if (role === 'user') {
    const toolMatch = text.match(/^\[(?:工具|Tool):\s*([^\]]+)\]\s*(执行成功|执行失败|executed successfully|execution failed)\s*\n?([\s\S]*)/);
    if (toolMatch) {
      const toolName = toolMatch[1].trim();
      // ask_user tool_result is redundant — the question is already shown in the tool_call event.
      if (toolName === 'ask_user') return null;
      const success = toolMatch[2] === '执行成功' || toolMatch[2] === 'executed successfully';
      const raw_out = toolMatch[3]
        .replace(/^(?:输出\(可能已截断\)|Output \(may be truncated\)):\n?/, '')
        .replace(/^(?:输出|Output):\n?/, '')
        .replace(/^(?:错误|Error):\n?/, '')
        .trim();
      return { ...base, type: 'tool_result', tool: toolName, success, output: raw_out };
    }
    if (text.startsWith('[高级指导员') || text.startsWith('[Advisor')) {
      const reasonMatch = text.match(/\[(?:高级指导员[^\]]*触发|Advisor[^\]]*trigger):\s*([^\]]+)\]/);
      const reason = reasonMatch ? reasonMatch[1].trim() : 'unknown';
      const bodyMatch = text.match(/^\[[^\]]+\]\s*\n\n([\s\S]*?)(?:\n\n---\n[\s\S]*)?$/);
      const body = bodyMatch ? bodyMatch[1].trim() : text.replace(/^\[[^\]]+\]\s*\n?/, '').trim();
      return { ...base, type: 'advisor', reason, text: body };
    }
    if (text.startsWith('[用户干预注入]') || text.startsWith('[User Injection]') || text.startsWith('[Web看板]')) {
      return { ...base, type: 'injected', text: text.replace(/^\[[^\]]+\]\s*\n?/, '').trim() };
    }
    if (text.startsWith('[用户补充信息]') || text.startsWith('[User input]')) {
      // Marker written by run_goal.py via t("marker.user_info") — covers both CLI
      // ask_user answers AND /inject messages absorbed by run_goal.py:505 polling
      // loop when agent is paused on ask_user.
      return { ...base, type: 'user_answer', text: text.replace(/^\[(?:用户补充信息|User input)\]\s*\n?/, '').trim() };
    }
    if (text.startsWith('[环境]') || text.startsWith('[Environment]')) {
      // Watcher output injected by the framework (see core/watcher.py). Rendered
      // with a distinct breathing-pink style to stand out from plain user msgs.
      return { ...base, type: 'env_obs', text: text.trim() };
    }
    if (lineIdx === 0) {
      const goalMarker = text.match(/(?:请完成以下目标：|Please complete the following goal:)\s*\n?([\s\S]*)/);
      return { ...base, type: 'goal', text: goalMarker ? goalMarker[1].trim() : text.trim() };
    }
    return { ...base, type: 'user_msg', text: text.trim() };
  }
  return null;
}

function updateShortTerm(runDir) {
  const fp = path.join(runDir, 'short_term.jsonl');
  if (!changed(fp)) return false;
  const raw = readText(fp);
  if (!raw) return false;
  const allLines = raw.split('\n').filter(l => l.trim());
  const newLines = allLines.slice(_linesProcessed);
  if (!newLines.length) return false;
  let iter = _iterCounter;
  for (let i = 0; i < newLines.length; i++) {
    const ev = parseLine(newLines[i], _linesProcessed + i);
    if (!ev) continue;
    if (ev.type === 'injected' || ev.type === 'user_answer') {
      // Real /inject landed — drop the matching optimistic placeholder. Two paths:
      //   normal: agent writes [用户干预注入]\n{arg}        → type 'injected'
      //   ask_user pause: run_goal.py:511 absorbs /inject  → [User input]\n{arg} → 'user_answer'
      // Image-inject's real text appends a [系统提示] block, so fall back to startsWith.
      let k = state.events.findIndex(e => e && e.optimistic && e.type === 'injected' && e.text === ev.text);
      if (k === -1) k = state.events.findIndex(e => e && e.optimistic && e.type === 'injected' && ev.text.startsWith(e.text));
      if (k !== -1) state.events.splice(k, 1);
    }
    if (ev.type === 'tool_call' || ev.type === 'done' || ev.type === 'error') iter++;
    ev.iter = iter;
    state.events.push(ev);
  }
  _linesProcessed = allLines.length;
  _iterCounter    = iter;
  return true;
}

// Derive runtime-patch timeline events from patch_log.jsonl.
// Only meaningful "patch took effect" events are surfaced (rule_added,
// candidate_promoted); skipped/candidate/diagnosis noise is dropped.
const _PATCH_SHOWN_EVENTS = new Set(['rule_added', 'candidate_promoted']);
function updatePatchEvents(runDir) {
  const fp = path.join(runDir, 'patch_log.jsonl');
  if (!changed(fp)) return false;
  const raw = readText(fp);
  if (!raw) return false;
  const allLines = raw.split('\n').filter(l => l.trim());
  const newLines = allLines.slice(_patchLinesProcessed);
  if (!newLines.length) return false;
  let added = false;
  for (const line of newLines) {
    let rec;
    try { rec = JSON.parse(line); } catch { continue; }
    if (!_PATCH_SHOWN_EVENTS.has(rec.event)) continue;
    state.patchEvents.push({
      type: 'runtime_patch',
      iter: rec.iteration || 0,
      // Timeline anchor = short_term line index at patch time. Falls back to the
      // current processed-line count for old logs without the field (best effort).
      anchorIdx: (typeof rec.short_term_len === 'number') ? rec.short_term_len : _linesProcessed,
      event: rec.event,
      errorType: rec.error_type || '',
      rule: rec.rule || '',
      ts: rec.ts || '',
    });
    added = true;
  }
  _patchLinesProcessed = allLines.length;
  return added;
}

// ── Advisor observability ──────────────────────────────────────────────────
// Surfaces what advisor sees and says so the user can directly observe:
//   - advisor_last.json    : full snapshot of latest call (system + context + advice)
//   - advisor_log.jsonl    : every call's metadata, summarised for the history list
// Both files are written by agent/core/advisor.py.

function updateAdvisor(runDir) {
  let changedAny = false;

  // ── advisor_last.json: full snapshot of most recent call ──────────────────
  const lastFp = path.join(runDir, 'advisor_last.json');
  if (changed(lastFp)) {
    const snap = readJSON(lastFp);
    if (snap) {
      state.advisorLast = snap;
      changedAny = true;
    }
  }

  // ── advisor_log.jsonl: append-only history, send compact summaries ────────
  const logFp = path.join(runDir, 'advisor_log.jsonl');
  if (changed(logFp)) {
    const raw = readText(logFp);
    if (raw) {
      const allLines = raw.split('\n').filter(l => l.trim());
      const newLines = allLines.slice(_advisorLinesProcessed);
      for (const line of newLines) {
        let rec;
        try { rec = JSON.parse(line); } catch { continue; }
        const advice = (typeof rec.advice === 'string') ? rec.advice : '';
        const ctx    = (typeof rec.context === 'string') ? rec.context : '';
        state.advisorHistory.push({
          ts:           rec.ts || '',
          iteration:    rec.iteration || 0,
          trigger:      rec.trigger || '',
          status:       rec.status || '',
          hasAdvice:    !!advice,
          advicePreview: advice ? advice.slice(0, 200) : '',
          contextLen:   ctx.length,
          systemLen:    (typeof rec.system === 'string') ? rec.system.length : 0,
        });
      }
      _advisorLinesProcessed = allLines.length;
      // Cap history broadcast at last 200 entries to keep WS payload small.
      // Full history is always available via GET /api/run/:runId/advisor.
      if (state.advisorHistory.length > 200) {
        state.advisorHistory = state.advisorHistory.slice(-200);
      }
      if (newLines.length) changedAny = true;
    }
  }

  return changedAny;
}

// ── Poll loop ──────────────────────────────────────────────────────────────

function poll() {
  const runs = findRuns();
  let dirty   = false;

  if (JSON.stringify(runs) !== JSON.stringify(state.runs)) {
    state.runs = runs;
    // Load summary for any run not yet cached
    for (const rid of runs) {
      if (!(rid in state.runSummaries)) {
        const s = readJSON(path.join(RUNS_DIR, rid, 'status.json'));
        state.runSummaries[rid] = (s && s.summary) || '';
      }
    }
    dirty = true;
  }

  const latest = runs[runs.length - 1] || null;

  if (latest !== state.activeRunId) {
    state.activeRunId = latest;
    state.status      = null;
    state.scratchpad  = '';
    state.systemPrompt = '';
    state.patchEvents  = [];
    state.events      = [];
    state.meta        = {};
    state.webDisplays = {};
    state.fileTabs    = null;
    state.teamNodeId  = null;
    _linesProcessed        = 0;
    _iterCounter           = 0;
    _mtimes                = {};
    _webChatLinesProcessed = 0;
    _patchLinesProcessed   = 0;
    _advisorLinesProcessed = 0;
    state.advisorLast     = null;
    state.advisorHistory  = [];
    dirty = true;
    // Once a new run directory appears, launching phase is over
    if (isLaunching) {
      isLaunching      = false;
      state.launching  = false;
    }
  }

  if (state.activeRunId) {
    const dir = path.join(RUNS_DIR, state.activeRunId);
    if (changed(path.join(dir, 'status.json'))) {
      const s = readJSON(path.join(dir, 'status.json'));
      if (s) {
        state.status = s;
        state.runSummaries[state.activeRunId] = s.summary || '';
        dirty = true;
      }
    }
    if (changed(path.join(dir, 'scratchpad.md'))) {
      const s = readText(path.join(dir, 'scratchpad.md'));
      if (s !== null) { state.scratchpad = s; dirty = true; }
    }
    if (changed(path.join(dir, 'system_prompt.md'))) {
      const s = readText(path.join(dir, 'system_prompt.md'));
      if (s !== null) { state.systemPrompt = s; dirty = true; }
    }
    if (changed(path.join(dir, 'meta.json'))) {
      const m = readJSON(path.join(dir, 'meta.json'));
      if (m) { state.meta = m; dirty = true; }
    }
    if (updateShortTerm(dir)) dirty = true;
    // After short_term so the fallback anchor (_linesProcessed) is current.
    if (updatePatchEvents(dir)) dirty = true;
    if (updateAdvisor(dir)) dirty = true;

    // ── web_display_*.json ───────────────────────────────────────────────────
    let dispFiles;
    try { dispFiles = fs.readdirSync(dir).filter(f => /^web_display_(.+)\.json$/.test(f)); }
    catch { dispFiles = []; }
    for (const fname of dispFiles) {
      const fp = path.join(dir, fname);
      if (changed(fp)) {
        const data = readJSON(fp);
        if (data && data.display_id) {
          state.webDisplays[data.display_id] = data;
          dirty = true;
        }
      }
    }

    // ── file_tabs.json ───────────────────────────────────────────────────────
    const tabsFp = path.join(dir, 'file_tabs.json');
    if (changed(tabsFp)) {
      const t = readJSON(tabsFp);
      if (t) { state.fileTabs = t; dirty = true; }
    }

    // ── team_node.json ───────────────────────────────────────────────────────
    const teamNodeFp = path.join(dir, 'team_node.json');
    if (changed(teamNodeFp)) {
      const tn = readJSON(teamNodeFp);
      const newId = (tn && tn.id) || null;
      if (newId !== state.teamNodeId) { state.teamNodeId = newId; dirty = true; }
    }

    // ── web_chat.jsonl ───────────────────────────────────────────────────────
    const chatFp = path.join(dir, 'web_chat.jsonl');
    if (changed(chatFp)) {
      const raw = readText(chatFp);
      if (raw) {
        const lines = raw.split('\n').filter(l => l.trim());
        const newLines = lines.slice(_webChatLinesProcessed);
        for (const line of newLines) {
          try { broadcastWebChat(JSON.parse(line)); } catch {}
        }
        _webChatLinesProcessed = lines.length;
      }
    }
  }

  // Pick up live nickname edits from the settings panel (POST /api/env updates
  // process.env in this same process, so it takes effect without a restart).
  const newInstanceName = process.env.INSTANCE_NAME || '';
  if (state.instanceName !== newInstanceName) {
    state.instanceName = newInstanceName;
    dirty = true;
  }

  // Keep launching / agentPid in sync
  const newLaunching = isLaunching;
  const newPid       = agentProc && !agentProc.killed ? agentProc.pid : null;
  if (state.launching !== newLaunching || state.agentPid !== newPid) {
    state.launching = newLaunching;
    state.agentPid  = newPid;
    dirty = true;
  }

  // Determine if any agent process is actually alive right now.
  // Priority: (1) process we spawned this session, (2) PID file in run dir.
  // This lets the dashboard recover from external kills and server restarts.
  let newAlive = false;
  if (agentProc && !agentProc.killed) {
    // We own the process — Node already knows it's running
    newAlive = true;
  } else if (state.activeRunId) {
    const runDir = path.join(RUNS_DIR, state.activeRunId);
    const pidFromFile = readPidFile(runDir);
    if (pidFromFile) {
      newAlive = isPidAlive(pidFromFile);
      // If process died but PID file still exists, delete it to avoid re-checking
      if (!newAlive) {
        try { fs.unlinkSync(path.join(runDir, 'agent.pid')); } catch {}
      }
    }
  }
  if (state.agentAlive !== newAlive) {
    state.agentAlive = newAlive;
    dirty = true;
  }

  if (dirty) broadcast();
}

// ── WebSocket broadcast ────────────────────────────────────────────────────

const clients = new Set();

function broadcast() {
  const msg = JSON.stringify({ type: 'state', ...state, terminals: termListPublic() });
  for (const ws of clients) {
    if (ws.readyState === WebSocket.OPEN) ws.send(msg);
  }
}

/** Push a web_chat message (agent → user) to all WebSocket clients. */
function broadcastWebChat(msg) {
  const data = JSON.stringify({ type: 'web_chat', ...msg });
  for (const ws of clients) {
    if (ws.readyState === WebSocket.OPEN) ws.send(data);
  }
}

/** Notify browser clients to open a view tab (remote-browser support). */
function broadcastOpenView(displayId, path, title) {
  const data = JSON.stringify({ type: 'open-view', displayId, path, title });
  for (const ws of clients) {
    if (ws.readyState === WebSocket.OPEN) ws.send(data);
  }
}

/** Push a live app-run event (start/stdout/stderr/end) to all clients, keyed by token. */
function broadcastAppRun(token, phase, extra = {}) {
  if (!token) return;
  const data = JSON.stringify({ type: 'app-run', token, phase, ...extra, ts: Date.now() });
  for (const ws of clients) {
    if (ws.readyState === WebSocket.OPEN) ws.send(data);
  }
}

/** Push a console line to all clients (stdout/stderr/system from agent process). */
function broadcastConsole(stream, text) {
  const clean = stripAnsi(text);
  if (!clean.trim()) return;
  const msg = JSON.stringify({ type: 'console', stream, text: clean, ts: Date.now() });
  for (const ws of clients) {
    if (ws.readyState === WebSocket.OPEN) ws.send(msg);
  }
}

// ── Agent process management ───────────────────────────────────────────────

function isAgentRunning() {
  return agentProc !== null && !agentProc.killed;
}

/**
 * Spawn a new agent process with the given goal string.
 * @param {string}   goal    - Natural language goal for the agent.
 * @param {boolean}  nostop  - If true, pass --nostop flag (continuous conversation mode).
 * @param {string[]} skills  - List of skill names to activate (passed via --skills).
 * @param {string}   agentsProfile  - Optional profile name; selects AGENTS_<name>.md to override AGENTS.md (--agents-profile).
 * @param {string}   advisorProfile - Optional profile name; selects ADVISOR_<name>.md to override ADVISOR.md (--advisor-profile).
 * Returns { ok, error? }.
 */
function launchAgent(goal, nostop = false, skills = [], agentsProfile = '', advisorProfile = '') {
  if (isAgentRunning()) {
    return { ok: false, error: 'An agent process is already running.' };
  }
  if (isLaunching) {
    return { ok: false, error: 'Agent is already being started.' };
  }

  isLaunching     = true;
  state.launching = true;
  broadcast();

  const nostopLabel    = nostop ? ' --nostop' : '';
  const skillsLabel    = skills.length ? ` --skills ${skills.join(',')}` : '';
  const agentsPLabel   = agentsProfile  ? ` --agents-profile ${agentsProfile}`   : '';
  const advisorPLabel  = advisorProfile ? ` --advisor-profile ${advisorProfile}` : '';
  broadcastConsole('system', `▶ Launching: ${PYTHON_CMD} run_goal.py${nostopLabel}${skillsLabel}${agentsPLabel}${advisorPLabel} "${goal.slice(0, 80)}${goal.length > 80 ? '…' : ''}"`);
  broadcastConsole('system', `  Working dir: ${AGENT_DIR}`);

  // On Windows, PYTHON_CMD may be a multi-word string like "conda run -n myenv python",
  // or a quoted path with spaces like "\"C:\\Programme Files\\python.exe\"".
  // parsePythonCmd() handles both cases correctly.
  const parts    = parsePythonCmd(PYTHON_CMD);
  const cmd      = parts[0];
  const cmdArgs  = [...parts.slice(1), 'run_goal.py'];
  if (nostop) cmdArgs.push('--nostop');
  if (skills.length) { cmdArgs.push('--skills'); cmdArgs.push(skills.join(',')); }
  if (agentsProfile)  { cmdArgs.push('--agents-profile');  cmdArgs.push(agentsProfile); }
  if (advisorProfile) { cmdArgs.push('--advisor-profile'); cmdArgs.push(advisorProfile); }
  cmdArgs.push(goal);

  agentProc = spawn(cmd, cmdArgs, {
    cwd:         AGENT_DIR,
    // Force UTF-8 I/O so emoji / CJK in loop.py don't crash on Windows (GBK default).
    // PYTHONUTF8=1  → Python 3.7+ UTF-8 mode (affects open(), stdin, stdout, stderr)
    // PYTHONIOENCODING → explicit codec for stdin/stdout/stderr streams
    env:         { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
    windowsHide: false,
    // Ignore stdin: the agent receives its goal as a CLI argument and dashboard
    // commands via web_cmd.txt (watched by UserInterruptHandler._web_cmd_watcher).
    // Keeping stdin as 'pipe' (Node default) but never writing to it causes
    // _read_loop_pipe in the agent to block forever on readline(), which then
    // races with subprocess.Popen inheriting the same pipe handle in tool_run_python
    // → the child Python process hangs on startup → 30s timeout every time.
    stdio:       ['ignore', 'pipe', 'pipe'],
  });

  agentProc.stdout.setEncoding('utf8');
  agentProc.stderr.setEncoding('utf8');

  agentProc.stdout.on('data', chunk => {
    // Once we see any output the spawn succeeded; clear the launching flag.
    // poll() will detect the state change and broadcast on next tick.
    if (isLaunching) isLaunching = false;
    broadcastConsole('stdout', chunk);
  });

  agentProc.stderr.on('data', chunk => {
    if (isLaunching) isLaunching = false;
    broadcastConsole('stderr', chunk);
  });

  agentProc.on('error', err => {
    // Only reset the internal tracking vars; DO NOT touch state.* directly.
    // poll() will diff state.agentPid vs the new null value and broadcast.
    isLaunching = false;
    agentProc   = null;
    broadcastConsole('system', `✗ Failed to start agent: ${err.message}`);
    broadcastConsole('system', `  Hint: make sure "${cmd}" is on PATH (activate your conda env first).`);
    poll(); // detects agentPid/launching changed → sets dirty → broadcasts
  });

  agentProc.on('close', code => {
    broadcastConsole('system', `■ Agent process exited (code ${code ?? '?'})`);
    // Only reset the internal tracking vars; DO NOT touch state.* directly.
    // poll() will diff state.agentPid (old PID) vs newPid (null) → dirty → broadcast.
    isLaunching = false;
    agentProc   = null;
    poll(); // detects agentPid/launching changed → sets dirty → broadcasts
    // Drain any cron triggers that piled up while we were busy.
    setImmediate(cronsProcessPending);
  });

  state.agentPid = agentProc.pid || null;
  broadcast();
  return { ok: true, pid: agentProc.pid };
}

/** Kill the running agent process (SIGTERM, then SIGKILL after 3 s). */
function killAgent() {
  if (!isAgentRunning()) return { ok: false, error: 'No agent process running.' };
  broadcastConsole('system', `⏹ Killing agent process (PID ${agentProc.pid})…`);
  agentProc.kill('SIGTERM');
  setTimeout(() => {
    if (agentProc && !agentProc.killed) agentProc.kill('SIGKILL');
  }, 3000);
  return { ok: true };
}

// ── Cron tasks ─────────────────────────────────────────────────────────────
// Each cron is a single MD file in CRONS_DIR with YAML frontmatter:
//   ---
//   name: 每天早报
//   cron: "0 9 * * *"          # 5-field cron (minute hour day month weekday)
//   enabled: true
//   on_conflict: skip          # skip | queue
//   skills: [s1, s2]
//   # timezone: Asia/Shanghai  # optional
//   ---
//   <goal / prompt body in markdown>
//
// Behaviour:
//  - Trigger fires → append a pending entry (regardless of agent state).
//  - On IDLE (agent process closed AND not launching), drain pending in order:
//      - on_conflict=queue → launch
//      - on_conflict=skip  AND agent was busy at trigger time → mark skipped
//      - otherwise (idle at trigger) → launch
//  - Missed triggers while dashboard offline are NOT caught up.
//  - History is appended to CRONS_HISTORY (jsonl) for the UI to read.

let cronLib = null;
try { cronLib = require('node-cron'); } catch { /* optional dependency missing */ }

let cronstrue = null;
try {
  cronstrue = require('cronstrue');
  // Preload zh locale; cronstrue auto-falls-back to English when locale unknown.
  try { require('cronstrue/locales/zh_CN'); } catch {}
} catch { /* optional */ }

function cronToHuman(expr) {
  if (!cronstrue || !expr) return '';
  try {
    return cronstrue.toString(expr, {
      locale: LANG === 'zh' ? 'zh_CN' : 'en',
      use24HourTimeFormat: true,
    });
  } catch { return ''; }
}

/** id → { task: node-cron task, meta: parsed frontmatter, body, mtime } */
const cronJobs = new Map();

/** Array<{ id, triggeredAt, on_conflict, wasBusy }> */
let cronPending = [];

function cronsEnsureDir() {
  try { fs.mkdirSync(CRONS_DIR, { recursive: true }); } catch {}
}

/** Very small YAML-frontmatter parser. Supports the limited fields we use. */
function parseCronFile(content) {
  const meta = { name: '', cron: '', enabled: true, on_conflict: 'skip', skills: [], timezone: '' };
  let body = content;
  const m = content.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
  if (m) {
    body = m[2] || '';
    for (const rawLine of m[1].split(/\r?\n/)) {
      const line = rawLine.replace(/#.*$/, '').trim();
      if (!line) continue;
      const eq = line.indexOf(':');
      if (eq < 1) continue;
      const key = line.slice(0, eq).trim();
      let val = line.slice(eq + 1).trim();
      if (val.length >= 2 && ((val[0] === '"' && val.endsWith('"')) || (val[0] === "'" && val.endsWith("'")))) {
        val = val.slice(1, -1);
      }
      if (key === 'enabled')      meta.enabled = !/^(false|no|0|off)$/i.test(val);
      else if (key === 'skills') {
        if (val.startsWith('[') && val.endsWith(']')) {
          meta.skills = val.slice(1, -1).split(',').map(s => s.trim().replace(/^["']|["']$/g, '')).filter(Boolean);
        } else if (val) {
          meta.skills = val.split(',').map(s => s.trim()).filter(Boolean);
        }
      } else if (key in meta) {
        meta[key] = val;
      }
    }
  }
  if (!['skip', 'queue'].includes(meta.on_conflict)) meta.on_conflict = 'skip';
  return { meta, body: body.replace(/^\s+/, '') };
}

// ── Apps: parse + execute ────────────────────────────────────────────────
const APP_RUNTIMES = ['python', 'powershell', 'shell'];

/** Parse an app file: YAML-ish frontmatter + script body. */
function parseAppFile(content) {
  const meta = { name: '', icon: '📦', description: '', runtime: 'shell', enabled: true, timeout: 120 };
  let body = content;
  const m = content.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
  if (m) {
    body = m[2] || '';
    for (const rawLine of m[1].split(/\r?\n/)) {
      const line = rawLine.replace(/#.*$/, '').trim();
      if (!line) continue;
      const eq = line.indexOf(':');
      if (eq < 1) continue;
      const key = line.slice(0, eq).trim();
      let val = line.slice(eq + 1).trim();
      if (val.length >= 2 && ((val[0] === '"' && val.endsWith('"')) || (val[0] === "'" && val.endsWith("'")))) {
        val = val.slice(1, -1);
      }
      if (key === 'enabled')       meta.enabled = !/^(false|no|0|off)$/i.test(val);
      else if (key === 'timeout')  { const n = parseInt(val, 10); if (n > 0) meta.timeout = n; }
      else if (key in meta)        meta[key] = val;
    }
  }
  // Strip a single wrapping ```lang ... ``` fence (agents often emit fenced code).
  const fence = body.match(/^\s*```([a-zA-Z0-9_+-]*)\r?\n([\s\S]*?)\r?\n```\s*$/);
  if (fence) {
    body = fence[2];
    const lang = (fence[1] || '').toLowerCase();
    if (lang === 'python' || lang === 'py') meta.runtime = 'python';
    else if (lang === 'powershell' || lang === 'ps1' || lang === 'ps') meta.runtime = 'powershell';
    else if (lang === 'shell' || lang === 'sh' || lang === 'bash' || lang === 'bat' || lang === 'cmd') meta.runtime = 'shell';
  }
  if (!APP_RUNTIMES.includes(meta.runtime)) meta.runtime = 'shell';
  return { meta, body: body.replace(/^\s+/, '') };
}

/**
 * Execute an app's script in a child process.
 * If `token` is given, streams live start/stdout/stderr/end events over WS so the
 * dashboard can show real-time output. Always resolves with the full final result
 * {ok,code,stdout,stderr,durationMs,timedOut} for the HTTP response.
 */
function runAppScript(meta, body, token = '') {
  return new Promise((resolve) => {
    const runtime  = APP_RUNTIMES.includes(meta.runtime) ? meta.runtime : 'shell';
    const isWin    = process.platform === 'win32';
    const ext      = runtime === 'python' ? '.py' : runtime === 'powershell' ? '.ps1' : (isWin ? '.bat' : '.sh');
    const tmpFile  = path.join(os.tmpdir(), `qevos_app_${Date.now()}_${Math.random().toString(36).slice(2, 8)}${ext}`);
    let cmd, args;
    // On Windows, cmd /c echoes each command of a .bat unless suppressed.
    const fileBody = (runtime === 'shell' && isWin && !/^\s*@echo\s+off/i.test(body))
      ? '@echo off\r\n' + body : body;
    try {
      fs.writeFileSync(tmpFile, fileBody, 'utf8');
    } catch (e) { resolve({ ok: false, code: -1, stdout: '', stderr: String(e), durationMs: 0, timedOut: false }); return; }

    if (runtime === 'python') {
      const parts = parsePythonCmd(PYTHON_CMD);
      cmd = parts[0]; args = [...parts.slice(1), tmpFile];
    } else if (runtime === 'powershell') {
      cmd = isWin ? 'powershell' : 'pwsh';
      args = ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', tmpFile];
    } else { // shell
      if (isWin) { cmd = 'cmd'; args = ['/c', tmpFile]; }
      else       { cmd = 'bash'; args = [tmpFile]; }
    }

    const timeoutMs = (meta.timeout > 0 ? meta.timeout : 120) * 1000;
    const t0 = Date.now();
    let stdout = '', stderr = '', timedOut = false, done = false;
    let child;
    try {
      child = spawn(cmd, args, {
        cwd: AGENT_DIR,
        env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
        windowsHide: true,
      });
    } catch (e) {
      try { fs.unlinkSync(tmpFile); } catch {}
      broadcastAppRun(token, 'end', { ok: false, code: -1, durationMs: 0, timedOut: false });
      resolve({ ok: false, code: -1, stdout: '', stderr: String(e), durationMs: 0, timedOut: false });
      return;
    }
    broadcastAppRun(token, 'start', { name: meta.name || '', runtime });
    const finish = (code) => {
      if (done) return; done = true;
      clearTimeout(timer);
      try { fs.unlinkSync(tmpFile); } catch {}
      const result = { ok: code === 0 && !timedOut, code, stdout, stderr, durationMs: Date.now() - t0, timedOut };
      broadcastAppRun(token, 'end', { ok: result.ok, code: result.code, durationMs: result.durationMs, timedOut: result.timedOut });
      resolve(result);
    };
    const timer = setTimeout(() => { timedOut = true; try { child.kill(); } catch {} }, timeoutMs);
    child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8');
    child.stdout.on('data', d => { stdout += d; if (stdout.length > 200000) stdout = stdout.slice(-200000); broadcastAppRun(token, 'stdout', { text: stripAnsi(d) }); });
    child.stderr.on('data', d => { stderr += d; if (stderr.length > 200000) stderr = stderr.slice(-200000); broadcastAppRun(token, 'stderr', { text: stripAnsi(d) }); });
    child.on('error', e => { stderr += String(e); finish(-1); });
    child.on('close', code => finish(code == null ? -1 : code));
  });
}

function cronsAppendHistory(entry) {
  try {
    cronsEnsureDir();
    fs.appendFileSync(CRONS_HISTORY, JSON.stringify({ ...entry, ts: entry.ts || Date.now() }) + '\n', 'utf8');
  } catch (e) { /* ignore */ }
}

function cronsSavePending() {
  try {
    cronsEnsureDir();
    fs.writeFileSync(CRONS_PENDING, JSON.stringify(cronPending), 'utf8');
  } catch {}
}

function cronsLoadPending() {
  try {
    const raw = fs.readFileSync(CRONS_PENDING, 'utf8');
    const arr = JSON.parse(raw);
    if (Array.isArray(arr)) cronPending = arr;
  } catch { cronPending = []; }
}

function cronsList() {
  cronsEnsureDir();
  let files = [];
  try {
    files = fs.readdirSync(CRONS_DIR).filter(f => f.endsWith('.md'));
  } catch { return []; }
  return files.sort().map(f => {
    const fp = path.join(CRONS_DIR, f);
    const id = f.replace(/\.md$/, '');
    const job = cronJobs.get(id);
    const stat = (() => { try { return fs.statSync(fp); } catch { return null; } })();
    return {
      id,
      filename: f,
      size: stat ? stat.size : 0,
      meta: job ? job.meta : null,
      valid: job ? !!job.task : false,
      error: job ? (job.error || null) : null,
      humanCron: job && job.meta && job.meta.cron ? cronToHuman(job.meta.cron) : '',
    };
  });
}

function cronsUnregister(id) {
  const job = cronJobs.get(id);
  if (job && job.task) {
    try { job.task.stop(); } catch {}
  }
  cronJobs.delete(id);
}

/** Read + register a single cron file. Returns the registered record. */
function cronsRegister(id) {
  cronsUnregister(id);
  const fp = path.join(CRONS_DIR, id + '.md');
  if (!fs.existsSync(fp)) return null;
  const content = readText(fp) || '';
  const { meta, body } = parseCronFile(content);
  const rec = { id, meta, body, task: null, error: null };

  if (!meta.enabled) {
    cronJobs.set(id, rec);
    return rec;
  }
  if (!meta.cron) { rec.error = 'cron expression missing'; cronJobs.set(id, rec); return rec; }
  if (!cronLib)   { rec.error = 'node-cron not installed';  cronJobs.set(id, rec); return rec; }
  if (!cronLib.validate(meta.cron)) { rec.error = 'invalid cron expression: ' + meta.cron; cronJobs.set(id, rec); return rec; }

  try {
    const opts = meta.timezone ? { timezone: meta.timezone } : {};
    rec.task = cronLib.schedule(meta.cron, () => cronsOnTrigger(id), opts);
  } catch (e) {
    rec.error = String(e && e.message || e);
  }
  cronJobs.set(id, rec);
  return rec;
}

function cronsRegisterAll() {
  for (const id of Array.from(cronJobs.keys())) cronsUnregister(id);
  cronsEnsureDir();
  let files = [];
  try { files = fs.readdirSync(CRONS_DIR).filter(f => f.endsWith('.md')); } catch {}
  for (const f of files) cronsRegister(f.replace(/\.md$/, ''));
}

function cronsBroadcast() {
  const msg = JSON.stringify({ type: 'crons', list: cronsList(), pending: cronPending });
  for (const ws of clients) {
    if (ws.readyState === WebSocket.OPEN) ws.send(msg);
  }
}

/** Called by node-cron when a schedule fires. */
function cronsOnTrigger(id) {
  const job = cronJobs.get(id);
  if (!job) return;
  const wasBusy = isAgentRunning() || isLaunching;
  const entry = {
    id,
    triggeredAt: Date.now(),
    on_conflict: job.meta.on_conflict,
    wasBusy,
  };
  cronPending.push(entry);
  cronsSavePending();
  cronsAppendHistory({ event: 'triggered', id, wasBusy, on_conflict: job.meta.on_conflict });
  broadcastConsole('system', `⏰ Cron triggered: ${id} (${wasBusy ? 'busy → pending' : 'idle → will launch'})`);
  cronsBroadcast();
  cronsProcessPending();
}

/** Drain pending entries when the agent is idle. */
function cronsProcessPending() {
  if (isAgentRunning() || isLaunching) return;
  if (!cronPending.length) return;
  const entry = cronPending[0];
  cronPending.shift();
  cronsSavePending();

  const job = cronJobs.get(entry.id);
  if (!job) {
    cronsAppendHistory({ event: 'dropped', id: entry.id, reason: 'cron file no longer exists' });
    cronsBroadcast();
    return setImmediate(cronsProcessPending);
  }

  // skip policy: if it was busy at trigger time, drop it now
  if (entry.on_conflict === 'skip' && entry.wasBusy) {
    cronsAppendHistory({ event: 'skipped', id: entry.id, triggeredAt: entry.triggeredAt });
    broadcastConsole('system', `⏭ Cron skipped (busy at trigger): ${entry.id}`);
    cronsBroadcast();
    return setImmediate(cronsProcessPending);
  }

  const goal = (job.body || '').trim();
  if (!goal) {
    cronsAppendHistory({ event: 'dropped', id: entry.id, reason: 'empty goal' });
    cronsBroadcast();
    return setImmediate(cronsProcessPending);
  }

  const result = launchAgent(goal, false, job.meta.skills || [], job.meta.agentsProfile || '', job.meta.advisorProfile || '');
  if (result.ok) {
    cronsAppendHistory({ event: 'launched', id: entry.id, pid: result.pid, triggeredAt: entry.triggeredAt });
    broadcastConsole('system', `▶ Cron launched: ${entry.id}`);
  } else {
    // Lost the race — put it back at the head for retry on next idle
    cronPending.unshift(entry);
    cronsSavePending();
    cronsAppendHistory({ event: 'retry', id: entry.id, error: result.error });
  }
  cronsBroadcast();
}

/** Run a cron now (bypass schedule), respecting current busy state via the same pending path. */
function cronsRunNow(id) {
  const job = cronJobs.get(id);
  if (!job) return { ok: false, error: 'cron not found' };
  // Force queue semantics for explicit "run now" so it's never silently skipped.
  const wasBusy = isAgentRunning() || isLaunching;
  const entry = { id, triggeredAt: Date.now(), on_conflict: 'queue', wasBusy };
  cronPending.push(entry);
  cronsSavePending();
  cronsAppendHistory({ event: 'manual', id, wasBusy });
  cronsBroadcast();
  cronsProcessPending();
  return { ok: true, queued: wasBusy };
}

function cronsReadHistory(limit) {
  try {
    const raw = fs.readFileSync(CRONS_HISTORY, 'utf8');
    const lines = raw.split(/\r?\n/).filter(Boolean);
    const tail = limit > 0 ? lines.slice(-limit) : lines;
    return tail.map(l => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
  } catch { return []; }
}

cronsLoadPending();
cronsRegisterAll();
// In case the server restarted with pending entries and the agent is already idle.
setImmediate(cronsProcessPending);

// ── Load a historical run ──────────────────────────────────────────────────

function loadRun(runId) {
  const dir = path.join(RUNS_DIR, runId);
  if (!fs.existsSync(dir)) return null;
  const status     = readJSON(path.join(dir, 'status.json'));
  const scratchpad = readText(path.join(dir, 'scratchpad.md')) || '';
  const meta       = readJSON(path.join(dir, 'meta.json')) || {};
  const raw        = readText(path.join(dir, 'short_term.jsonl')) || '';
  const lines      = raw.split('\n').filter(l => l.trim());
  let iter = 0;
  const events = [];
  for (let i = 0; i < lines.length; i++) {
    const ev = parseLine(lines[i], i);
    if (!ev) continue;
    if (ev.type === 'tool_call' || ev.type === 'done' || ev.type === 'error') iter++;
    ev.iter = iter;
    events.push(ev);
  }
  // Advisor observability for historical run view
  const advisorLast = readJSON(path.join(dir, 'advisor_last.json')) || null;
  const advisorHistory = [];
  const advLogRaw = readText(path.join(dir, 'advisor_log.jsonl')) || '';
  for (const line of advLogRaw.split('\n')) {
    if (!line.trim()) continue;
    try {
      const rec = JSON.parse(line);
      const advice = (typeof rec.advice === 'string') ? rec.advice : '';
      advisorHistory.push({
        ts:            rec.ts || '',
        iteration:     rec.iteration || 0,
        trigger:       rec.trigger || '',
        status:        rec.status || '',
        hasAdvice:     !!advice,
        advicePreview: advice ? advice.slice(0, 200) : '',
        contextLen:    (typeof rec.context === 'string') ? rec.context.length : 0,
        systemLen:     (typeof rec.system === 'string')  ? rec.system.length  : 0,
      });
    } catch {}
  }
  return { runId, status, scratchpad, meta, events, advisorLast, advisorHistory };
}

// ── HTTP server ────────────────────────────────────────────────────────────

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js':   'application/javascript; charset=utf-8',
  '.css':  'text/css; charset=utf-8',
  '.json': 'application/json',
  '.ico':  'image/x-icon',
  '.png':  'image/png',
  '.jpg':  'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif':  'image/gif',
  '.webp': 'image/webp',
  '.bmp':  'image/bmp',
  '.svg':  'image/svg+xml',
  '.mp4':  'video/mp4',
  '.webm': 'video/webm',
};

function readBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', c => (body += c));
    req.on('end', () => resolve(body));
    req.on('error', reject);
  });
}

function serveStatic(req, res) {
  const urlPath = req.url === '/' ? '/index.html' : req.url.split('?')[0];
  const fp  = path.join(PUBLIC, urlPath);
  const ext = path.extname(fp).toLowerCase();
  try {
    let content = fs.readFileSync(fp);
    if (ext === '.html') {
      const langScript = `<script>window.QEVOS_LANG="${LANG}";</script>`;
      // Inject right after <head> so QEVOS_LANG is defined before ui_i18n.js
      // runs; otherwise UI_LANG freezes to the default before the value is set.
      const html = content.toString('utf8').replace('<head>', '<head>' + langScript);
      res.writeHead(200, { 'Content-Type': MIME[ext] || 'text/plain' });
      res.end(html, 'utf8');
    } else {
      res.writeHead(200, { 'Content-Type': MIME[ext] || 'text/plain' });
      res.end(content);
    }
  } catch {
    res.writeHead(404);
    res.end('Not found');
  }
}

const server = http.createServer(async (req, res) => {
  if (!isIpAllowed(req.socket.remoteAddress)) {
    res.writeHead(403, { 'Content-Type': 'text/plain; charset=utf-8' });
    res.end('403 Forbidden: 你的 IP 不在本看板的访问名单内。');
    return;
  }
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }

  const json = (code, obj) => {
    res.writeHead(code, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(obj));
  };

  // ── POST /api/launch  ─────────────────────────────────────────────────────
  if (req.method === 'POST' && req.url === '/api/launch') {
    try {
      const { goal, nostop, skills, agentsProfile, advisorProfile } = JSON.parse(await readBody(req));
      if (!goal || !goal.trim()) { json(400, { error: 'goal is required' }); return; }
      const skillList = Array.isArray(skills) ? skills.filter(Boolean) : [];
      const ap = (typeof agentsProfile  === 'string') ? agentsProfile.trim()  : '';
      const vp = (typeof advisorProfile === 'string') ? advisorProfile.trim() : '';
      const result = launchAgent(goal.trim(), !!nostop, skillList, ap, vp);
      json(result.ok ? 200 : 409, result);
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── POST /api/kill  ───────────────────────────────────────────────────────
  if (req.method === 'POST' && req.url === '/api/kill') {
    json(200, killAgent());
    return;
  }

  // ── POST /api/inject  ─────────────────────────────────────────────────────
  if (req.method === 'POST' && req.url === '/api/inject') {
    try {
      const { command, runId } = JSON.parse(await readBody(req));
      const target = runId || state.activeRunId;
      if (!target || !command) { json(400, { error: 'missing command or active run' }); return; }
      const cmdFile = path.join(RUNS_DIR, target, 'web_cmd.txt');
      const trimmed = command.trim();
      // Push the optimistic placeholder BEFORE triggering the agent. Otherwise a fast
      // agent can append the real injected line to short_term.jsonl, updateShortTerm()
      // runs dedup, finds no optimistic yet, and the optimistic — pushed after —
      // lingers at the bottom forever. Only for /inject — other slash commands don't
      // land in short_term and would never get deduped.
      if (trimmed.startsWith('/inject')) {
        state.events.push({ type: 'injected', text: trimmed.replace(/^\/inject\s+/, ''), iter: _iterCounter, idx: Number.MAX_SAFE_INTEGER, optimistic: true });
      }
      fs.writeFileSync(cmdFile, trimmed + '\n', 'utf8');
      broadcast();
      json(200, { ok: true });
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── POST /api/inject-image  ───────────────────────────────────────────────
  if (req.method === 'POST' && req.url === '/api/inject-image') {
    try {
      const { imageData, imageType, filename, message, runId } = JSON.parse(await readBody(req));
      const target = runId || state.activeRunId;
      if (!target || !imageData) { json(400, { error: 'missing imageData or active run' }); return; }

      // Save image file to artifacts/
      const ts = Date.now();
      const ext = (imageType || 'image/jpeg').split('/')[1]?.replace('jpeg', 'jpg') || 'jpg';
      const imgName = `web_img_${ts}.${ext}`;
      const artifactsDir = path.join(RUNS_DIR, target, 'artifacts');
      fs.mkdirSync(artifactsDir, { recursive: true });
      fs.writeFileSync(path.join(artifactsDir, imgName), Buffer.from(imageData, 'base64'));

      // Inject command: agent will call load_image() on its next turn
      const relPath = `artifacts/${imgName}`;
      const userText = message ? `[Web用户]: ${message}` : '[Web用户]: （图片）';
      const cmd = `/inject ${userText}\n[系统提示]: 用户通过看板上传了图片，已保存至 ${relPath}，请调用 load_image(path="${relPath}") 加载后分析。`;
      // Push optimistic BEFORE triggering the agent — otherwise updateShortTerm()
      // can dedup against an empty list before this push lands. See /api/inject above.
      state.events.push({ type: 'injected', text: userText, iter: _iterCounter, idx: Number.MAX_SAFE_INTEGER, optimistic: true });
      fs.writeFileSync(path.join(RUNS_DIR, target, 'web_cmd.txt'), cmd.trim() + '\n', 'utf8');
      broadcast();
      json(200, { ok: true, path: relPath });
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── GET /api/version ─────────────────────────────────────────────────────
  if (req.method === 'GET' && req.url === '/api/version') {
    // Busy = actively working on a task, not merely alive. An agent in nostop
    // continuous mode sits alive-but-idle between tasks (nostop_idle + status
    // 'idle'); that's "空闲", not "在执行任务" — mirror the dashboard's own
    // isNostopIdle() so the mobile status dot agrees with the web UI.
    const nostopIdle = !!(state.agentAlive
      && state.meta && state.meta.nostop_idle
      && state.status && state.status.status === 'idle');
    // Asking = paused mid-run waiting for an ask_user answer (mirrors the
    // dashboard's isAwaitingInput). The mobile dot shows this as yellow and
    // takes priority over busy.
    const asking = !!(state.agentAlive
      && state.status && state.status.status === 'paused'
      && state.meta && state.meta.awaiting_input);
    json(200, {
      version: APP_VERSION,
      instanceName: state.instanceName || '',
      // Mobile uses these to color the per-server status dot in its menu.
      busy: !!(state.launching || (state.agentAlive && !nostopIdle)),
      asking,
    });
    return;
  }

  // ── GET /api/state  ───────────────────────────────────────────────────────
  if (req.method === 'GET' && req.url === '/api/state') {
    json(200, { type: 'state', ...state, terminals: termListPublic() });
    return;
  }

  // ── GET /api/env  ─────────────────────────────────────────────────────────
  // Current LLM/connection settings for the in-dashboard settings panel.
  // `configured` lets the frontend auto-open settings on first run.
  if (req.method === 'GET' && req.url === '/api/env') {
    json(200, {
      OPENAI_BASE_URL: process.env.OPENAI_BASE_URL || '',
      OPENAI_API_KEY:  process.env.OPENAI_API_KEY  || '',
      OPENAI_MODEL:    process.env.OPENAI_MODEL    || '',
      MAX_ITERS:       process.env.MAX_ITERS       || '100',
      INSTANCE_NAME:   process.env.INSTANCE_NAME   || '',
      HTTPS_PROXY:     process.env.HTTPS_PROXY     || '',
      HTTP_PROXY:      process.env.HTTP_PROXY      || '',
      BACKUP_OPENAI_BASE_URL: process.env.BACKUP_OPENAI_BASE_URL || '',
      BACKUP_OPENAI_API_KEY:  process.env.BACKUP_OPENAI_API_KEY  || '',
      BACKUP_OPENAI_MODEL:    process.env.BACKUP_OPENAI_MODEL    || '',
      // 顾问模型 1 / 2 —— 仅供 consult_advisor 工具按需调用，不参与主备 fallback
      ADVISOR1_OPENAI_BASE_URL: process.env.ADVISOR1_OPENAI_BASE_URL || '',
      ADVISOR1_OPENAI_API_KEY:  process.env.ADVISOR1_OPENAI_API_KEY  || '',
      ADVISOR1_OPENAI_MODEL:    process.env.ADVISOR1_OPENAI_MODEL    || '',
      ADVISOR2_OPENAI_BASE_URL: process.env.ADVISOR2_OPENAI_BASE_URL || '',
      ADVISOR2_OPENAI_API_KEY:  process.env.ADVISOR2_OPENAI_API_KEY  || '',
      ADVISOR2_OPENAI_MODEL:    process.env.ADVISOR2_OPENAI_MODEL    || '',
      // 高级设置 —— 局域网可见性
      DASHBOARD_HOST:  process.env.DASHBOARD_HOST  || '',
      DASHBOARD_PORT:  process.env.DASHBOARD_PORT  || '',
      DASHBOARD_ALLOW: process.env.DASHBOARD_ALLOW || '',
      DASHBOARD_DENY:  process.env.DASHBOARD_DENY  || '',
      // 高级设置 —— Agent 运行参数
      MAX_TOOL_FEEDBACK_CHARS: process.env.MAX_TOOL_FEEDBACK_CHARS || '',
      LLM_MAX_TOKENS:  process.env.LLM_MAX_TOKENS  || '',
      LLM_TEMPERATURE: process.env.LLM_TEMPERATURE || '',
      configured: !!process.env.OPENAI_BASE_URL,
    });
    return;
  }

  // ── POST /api/env  ────────────────────────────────────────────────────────
  // Merge-write the managed keys into .env and sync the live process.env so the
  // next agent run picks them up without a restart (spawn inherits process.env).
  if (req.method === 'POST' && req.url === '/api/env') {
    try {
      const data = JSON.parse(await readBody(req));
      const existing = {};
      try {
        for (const line of fs.readFileSync(DOTENV_PATH, 'utf8').split(/\r?\n/)) {
          const eq = line.indexOf('=');
          if (eq > 0 && !line.trim().startsWith('#')) {
            existing[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
          }
        }
      } catch { /* first run, no .env yet */ }
      // Form fields are authoritative: a non-empty value sets the key, an empty
      // value removes it. Keys the form doesn't manage are preserved.
      const merged = { ...existing };
      const cleared = [];
      for (const [k, v] of Object.entries(data)) {
        const val = (v == null ? '' : String(v)).trim();
        if (val) merged[k] = val;
        else { delete merged[k]; cleared.push(k); }
      }
      const content = Object.entries(merged).map(([k, v]) => `${k}=${v}`).join('\n') + '\n';
      fs.mkdirSync(path.dirname(DOTENV_PATH), { recursive: true });
      fs.writeFileSync(DOTENV_PATH, content, 'utf8');
      for (const [k, v] of Object.entries(merged)) process.env[k] = v;
      for (const k of cleared) delete process.env[k];
      json(200, { ok: true });
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── POST /api/env/test  ───────────────────────────────────────────────────
  // Server-side connectivity probe against {baseUrl}/models (avoids browser CORS).
  if (req.method === 'POST' && req.url === '/api/env/test') {
    try {
      const { baseUrl, apiKey } = JSON.parse(await readBody(req));
      let url;
      try {
        const base = (baseUrl || '').endsWith('/') ? baseUrl.slice(0, -1) : (baseUrl || '');
        url = new URL(base + '/models');
      } catch { json(200, { ok: false, error: 'URL 格式无效' }); return; }
      const mod = url.protocol === 'https:' ? require('https') : require('http');
      const preq = mod.request({
        hostname: url.hostname,
        port:     url.port || (url.protocol === 'https:' ? 443 : 80),
        path:     url.pathname + url.search,
        method:   'GET',
        headers:  { Authorization: `Bearer ${apiKey || 'local'}` },
        timeout:  8000,
      }, pres => {
        let body = '';
        pres.setEncoding('utf8');
        pres.on('data', c => { body += c; });
        pres.on('end', () => {
          if (pres.statusCode >= 200 && pres.statusCode < 300) {
            let models = [];
            try {
              const j = JSON.parse(body);
              if (Array.isArray(j.data)) models = j.data.map(m => m.id || m.name).filter(Boolean);
            } catch { /* non-JSON, ignore */ }
            json(200, { ok: true, status: pres.statusCode, models });
          } else {
            json(200, { ok: false, status: pres.statusCode, error: `HTTP ${pres.statusCode}` });
          }
        });
      });
      preq.on('timeout', () => { preq.destroy(); json(200, { ok: false, error: '连接超时' }); });
      preq.on('error',   err => json(200, { ok: false, error: err.message }));
      preq.end();
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── GET /api/run/:runId  ──────────────────────────────────────────────────
  // Anchored with end-of-path so it doesn't swallow nested routes like
  // /api/run/:runId/advisor/:idx (which used to return the whole historical run
  // blob because this regex was unanchored).
  const runMatch = req.url.match(/^\/api\/run\/([^/?]+)(?:\?.*)?$/);
  if (req.method === 'GET' && runMatch) {
    const data = loadRun(runMatch[1]);
    if (!data) { res.writeHead(404); res.end('{}'); return; }
    json(200, { type: 'historical', ...data });
    return;
  }

  // ── DELETE /api/run/:runId  — permanently remove a run directory ──────────
  if (req.method === 'DELETE' && runMatch) {
    try {
      const runId = decodeURIComponent(runMatch[1]);
      if (!/^\d{8}-\d{6}$/.test(runId)) { json(400, { error: 'invalid run id' }); return; }
      const runDir = path.resolve(path.join(RUNS_DIR, runId));
      const rel    = path.relative(RUNS_DIR, runDir);
      if (rel.startsWith('..') || path.isAbsolute(rel)) { json(403, { error: 'forbidden' }); return; }
      if (!fs.existsSync(runDir)) { json(404, { error: 'run not found' }); return; }
      // Refuse to delete a run whose agent is still alive — deleting the dir it
      // is actively writing to would corrupt the live process.
      if (runId === state.activeRunId && state.agentAlive) {
        json(409, { error: 'run is live; stop the agent first' });
        return;
      }
      fs.rmSync(runDir, { recursive: true, force: true });
      // Reflect immediately; the next poll reconciles the rest.
      state.runs = state.runs.filter(r => r !== runId);
      if (state.activeRunId === runId) state.activeRunId = state.runs[state.runs.length - 1] || null;
      json(200, { ok: true, runId });
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── GET /api/run/:runId/advisor[/:idx] ───────────────────────────────────
  // Returns either the full advisor_log.jsonl (parsed as array), or a single
  // entry by 0-based index. Used by the Advisor tab to inspect any past call.
  const advisorRunMatch = req.url.match(/^\/api\/run\/([^/?]+)\/advisor(?:\/(\d+))?$/);
  if (req.method === 'GET' && advisorRunMatch) {
    const rid = advisorRunMatch[1];
    const idx = advisorRunMatch[2] !== undefined ? parseInt(advisorRunMatch[2], 10) : null;
    const fp = path.join(RUNS_DIR, rid, 'advisor_log.jsonl');
    if (!fs.existsSync(fp)) { json(404, { error: 'advisor_log.jsonl not found' }); return; }
    const raw = readText(fp) || '';
    const lines = raw.split('\n').filter(l => l.trim());
    if (idx !== null) {
      if (idx < 0 || idx >= lines.length) { json(404, { error: 'index out of range' }); return; }
      try { json(200, JSON.parse(lines[idx])); }
      catch (e) { json(500, { error: 'parse failure: ' + e.message }); }
      return;
    }
    // Whole log
    const parsed = [];
    for (const line of lines) {
      try { parsed.push(JSON.parse(line)); } catch {}
    }
    json(200, { entries: parsed });
    return;
  }

  // ── GET /api/agents-md  ──────────────────────────────────────────────────
  if (req.method === 'GET' && req.url === '/api/agents-md') {
    const content = readText(path.join(AGENT_DIR, 'AGENTS.md')) || '';
    json(200, { content });
    return;
  }

  // ── GET /api/advisor-md  ─────────────────────────────────────────────────
  if (req.method === 'GET' && req.url === '/api/advisor-md') {
    const content = readText(path.join(AGENT_DIR, 'ADVISOR.md')) || '';
    json(200, { content });
    return;
  }

  // ── GET /api/memory-concept  ─────────────────────────────────────────────
  if (req.method === 'GET' && req.url === '/api/memory-concept') {
    const fp = MEMORY_CONCEPT;
    if (!fs.existsSync(fp)) { json(200, { content: null, exists: false }); return; }
    const content = readText(fp) || '';
    json(200, { content });
    return;
  }

  // ── POST /api/memory-concept  ────────────────────────────────────────────
  if (req.method === 'POST' && req.url === '/api/memory-concept') {
    try {
      const { content } = JSON.parse(await readBody(req));
      if (typeof content !== 'string') { json(400, { error: 'content required' }); return; }
      fs.mkdirSync(path.dirname(MEMORY_CONCEPT), { recursive: true });
      fs.writeFileSync(MEMORY_CONCEPT, content, 'utf8');
      json(200, { ok: true });
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── GET /api/memory-episodic  ────────────────────────────────────────────
  if (req.method === 'GET' && req.url === '/api/memory-episodic') {
    const fp = MEMORY_EPISODIC;
    if (!fs.existsSync(fp)) { json(200, { content: null, exists: false }); return; }
    const content = readText(fp) || '';
    json(200, { content });
    return;
  }

  // ── POST /api/memory-episodic  ───────────────────────────────────────────
  if (req.method === 'POST' && req.url === '/api/memory-episodic') {
    try {
      const { content } = JSON.parse(await readBody(req));
      if (typeof content !== 'string') { json(400, { error: 'content required' }); return; }
      fs.mkdirSync(path.dirname(MEMORY_EPISODIC), { recursive: true });
      fs.writeFileSync(MEMORY_EPISODIC, content, 'utf8');
      json(200, { ok: true });
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── POST /api/agents-md  ─────────────────────────────────────────────────
  if (req.method === 'POST' && req.url === '/api/agents-md') {
    try {
      const { content } = JSON.parse(await readBody(req));
      if (typeof content !== 'string') { json(400, { error: 'content required' }); return; }
      fs.writeFileSync(path.join(AGENT_DIR, 'AGENTS.md'), content, 'utf8');
      json(200, { ok: true });
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── POST /api/advisor-md  ────────────────────────────────────────────────
  if (req.method === 'POST' && req.url === '/api/advisor-md') {
    try {
      const { content } = JSON.parse(await readBody(req));
      if (typeof content !== 'string') { json(400, { error: 'content required' }); return; }
      fs.writeFileSync(path.join(AGENT_DIR, 'ADVISOR.md'), content, 'utf8');
      json(200, { ok: true });
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── GET /api/profiles — list AGENTS.md / AGENTS_*.md / ADVISOR.md / ADVISOR_*.md
  // in the repo root. Used by the dashboard to populate the profile dropdowns.
  if (req.method === 'GET' && req.url === '/api/profiles') {
    try {
      const all = fs.existsSync(AGENT_DIR) ? fs.readdirSync(AGENT_DIR) : [];
      const pick = (base) => all
        .filter(f => f === `${base}.md` || (f.startsWith(`${base}_`) && f.endsWith('.md')))
        .sort((a, b) => {
          // base file always first, then alpha
          if (a === `${base}.md`) return -1;
          if (b === `${base}.md`) return 1;
          return a.localeCompare(b);
        });
      json(200, { agents: pick('AGENTS'), advisor: pick('ADVISOR') });
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── Profile-file CRUD (AGENTS.md / AGENTS_*.md / ADVISOR.md / ADVISOR_*.md) ─
  // Used by the AGENTS.md / ADVISOR.md tabs to manage profile variants in-place.
  // Filename must match /^(AGENTS|ADVISOR)(_[A-Za-z0-9._-]+)?\.md$/ — strict so
  // no path traversal and no accidental writes outside the profile family.
  const profileFileMatch = req.url.match(/^\/api\/profile-file\/([^/?]+)$/);
  if (profileFileMatch) {
    const raw = decodeURIComponent(profileFileMatch[1]);
    if (!/^(AGENTS|ADVISOR)(_[A-Za-z0-9._-]+)?\.md$/.test(raw) || raw.includes('..')) {
      json(400, { error: 'invalid profile filename: ' + raw });
      return;
    }
    const fp = path.join(AGENT_DIR, raw);
    if (req.method === 'GET') {
      if (!fs.existsSync(fp)) { json(404, { error: 'file not found' }); return; }
      json(200, { filename: raw, content: readText(fp) || '' });
      return;
    }
    if (req.method === 'POST') {
      try {
        const { content } = JSON.parse(await readBody(req));
        if (typeof content !== 'string') { json(400, { error: 'content required' }); return; }
        fs.writeFileSync(fp, content, 'utf8');
        json(200, { ok: true, filename: raw });
      } catch (e) { json(500, { error: String(e) }); }
      return;
    }
    if (req.method === 'DELETE') {
      // Refuse to delete the base files — they're the fallback when no profile is active.
      if (raw === 'AGENTS.md' || raw === 'ADVISOR.md') {
        json(400, { error: 'cannot delete base file ' + raw });
        return;
      }
      try {
        if (!fs.existsSync(fp)) { json(404, { error: 'file not found' }); return; }
        fs.unlinkSync(fp);
        json(200, { ok: true, filename: raw });
      } catch (e) { json(500, { error: String(e) }); }
      return;
    }
  }

  // ── Apps (user-space executable programs) ──────────────────────────────────
  // GET    /api/apps              — list all apps (parsed meta)
  // GET    /api/app/:id           — read raw file (for editor)
  // POST   /api/app/:id           — create / update
  // DELETE /api/app/:id           — delete
  // POST   /api/app/:id/run       — execute the script directly (NO LLM/agent)
  if (req.method === 'GET' && req.url === '/api/apps') {
    try {
      if (!fs.existsSync(APPS_DIR)) { json(200, { apps: [] }); return; }
      const apps = fs.readdirSync(APPS_DIR)
        .filter(f => f.endsWith('.md'))
        .sort()
        .map(f => {
          const fp   = path.join(APPS_DIR, f);
          const id   = f.replace(/\.md$/, '');
          const stat = (() => { try { return fs.statSync(fp); } catch { return null; } })();
          const { meta } = parseAppFile(readText(fp) || '');
          return {
            id,
            name: meta.name || id,
            icon: meta.icon || '📦',
            description: meta.description || '',
            runtime: meta.runtime,
            enabled: meta.enabled,
            size: stat ? stat.size : 0,
          };
        });
      json(200, { apps });
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // POST /api/app/:id/run  — execute (match BEFORE the bare :id routes)
  const appRunMatch = req.url.match(/^\/api\/app\/([^/?]+)\/run$/);
  if (req.method === 'POST' && appRunMatch) {
    const id = decodeURIComponent(appRunMatch[1]).replace(/\.md$/, '');
    const fp = path.join(APPS_DIR, id + '.md');
    if (!fs.existsSync(fp)) { json(404, { error: 'app not found' }); return; }
    let token = '';
    try { const raw = await readBody(req); if (raw) token = (JSON.parse(raw).token) || ''; } catch {}
    try {
      const { meta, body } = parseAppFile(readText(fp) || '');
      const r = await runAppScript(meta, body, token);
      broadcastConsole('system', `▶ Run app: ${meta.name || id} → exit ${r.code}${r.timedOut ? ' (timeout)' : ''}`);
      json(200, r);
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  const appGetMatch = req.url.match(/^\/api\/app\/([^/?]+)$/);
  if (req.method === 'GET' && appGetMatch) {
    const id = decodeURIComponent(appGetMatch[1]).replace(/\.md$/, '');
    const fp = path.join(APPS_DIR, id + '.md');
    if (!fs.existsSync(fp)) { json(404, { error: 'app not found' }); return; }
    json(200, { id, content: readText(fp) || '' });
    return;
  }

  const appPostMatch = req.url.match(/^\/api\/app\/([^/?]+)$/);
  if (req.method === 'POST' && appPostMatch) {
    try {
      const id = decodeURIComponent(appPostMatch[1]).replace(/\.md$/, '').replace(/[^a-zA-Z0-9_\-]/g, '_');
      if (!id) { json(400, { error: 'invalid id' }); return; }
      const { content } = JSON.parse(await readBody(req));
      if (typeof content !== 'string') { json(400, { error: 'content required' }); return; }
      if (!fs.existsSync(APPS_DIR)) fs.mkdirSync(APPS_DIR, { recursive: true });
      fs.writeFileSync(path.join(APPS_DIR, id + '.md'), content, 'utf8');
      json(200, { ok: true, id });
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  const appDelMatch = req.url.match(/^\/api\/app\/([^/?]+)$/);
  if (req.method === 'DELETE' && appDelMatch) {
    try {
      const id = decodeURIComponent(appDelMatch[1]).replace(/\.md$/, '');
      const fp = path.join(APPS_DIR, id + '.md');
      if (!fs.existsSync(fp)) { json(404, { error: 'app not found' }); return; }
      fs.unlinkSync(fp);
      json(200, { ok: true, id });
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── GET /api/skills  — list all skill files ───────────────────────────────
  if (req.method === 'GET' && req.url === '/api/skills') {
    try {
      if (!fs.existsSync(SKILLS_DIR)) { json(200, { skills: [] }); return; }
      const skills = fs.readdirSync(SKILLS_DIR)
        .filter(f => f.endsWith('.md'))
        .sort()
        .map(f => {
          const fp   = path.join(SKILLS_DIR, f);
          const stat = fs.statSync(fp);
          return { name: f.replace(/\.md$/, ''), filename: f, size: stat.size };
        });
      json(200, { skills });
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── GET /api/skill/:name  — read a skill file ─────────────────────────────
  const skillGetMatch = req.url.match(/^\/api\/skill\/([^/?]+)$/);
  if (req.method === 'GET' && skillGetMatch) {
    const name = decodeURIComponent(skillGetMatch[1]).replace(/\.md$/, '');
    const fp   = path.join(SKILLS_DIR, name + '.md');
    if (!fs.existsSync(fp)) { json(404, { error: 'skill not found' }); return; }
    json(200, { name, content: readText(fp) || '' });
    return;
  }

  // ── POST /api/skill/:name  — create or update a skill file ───────────────
  const skillPostMatch = req.url.match(/^\/api\/skill\/([^/?]+)$/);
  if (req.method === 'POST' && skillPostMatch) {
    try {
      const name = decodeURIComponent(skillPostMatch[1]).replace(/\.md$/, '');
      const { content } = JSON.parse(await readBody(req));
      if (typeof content !== 'string') { json(400, { error: 'content required' }); return; }
      if (!fs.existsSync(SKILLS_DIR)) fs.mkdirSync(SKILLS_DIR, { recursive: true });
      fs.writeFileSync(path.join(SKILLS_DIR, name + '.md'), content, 'utf8');
      json(200, { ok: true, name });
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── DELETE /api/skill/:name  — delete a skill file ───────────────────────
  const skillDelMatch = req.url.match(/^\/api\/skill\/([^/?]+)$/);
  if (req.method === 'DELETE' && skillDelMatch) {
    try {
      const name = decodeURIComponent(skillDelMatch[1]).replace(/\.md$/, '');
      const fp   = path.join(SKILLS_DIR, name + '.md');
      if (!fs.existsSync(fp)) { json(404, { error: 'skill not found' }); return; }
      fs.unlinkSync(fp);
      json(200, { ok: true, name });
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── GET /api/crons  — list all cron files (incl. parsed meta) ─────────────
  if (req.method === 'GET' && req.url === '/api/crons') {
    try { json(200, { crons: cronsList(), pending: cronPending, cronAvailable: !!cronLib }); }
    catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── GET /api/cron-history?limit=N  ────────────────────────────────────────
  if (req.method === 'GET' && req.url.startsWith('/api/cron-history')) {
    const m = req.url.match(/limit=(\d+)/);
    const limit = m ? parseInt(m[1], 10) : 100;
    json(200, { history: cronsReadHistory(limit) });
    return;
  }

  // ── GET /api/cron/:name  — read a cron file ───────────────────────────────
  const cronGetMatch = req.url.match(/^\/api\/cron\/([^/?]+)$/);
  if (req.method === 'GET' && cronGetMatch) {
    const id = decodeURIComponent(cronGetMatch[1]).replace(/\.md$/, '');
    const fp = path.join(CRONS_DIR, id + '.md');
    if (!fs.existsSync(fp)) { json(404, { error: 'cron not found' }); return; }
    const content = readText(fp) || '';
    const job     = cronJobs.get(id);
    json(200, {
      id, content,
      meta: job ? job.meta : null,
      error: job ? job.error : null,
      humanCron: job && job.meta && job.meta.cron ? cronToHuman(job.meta.cron) : '',
    });
    return;
  }

  // ── POST /api/cron/:name  — create / update a cron file (re-register) ────
  const cronPostMatch = req.url.match(/^\/api\/cron\/([^/?]+)$/);
  if (req.method === 'POST' && cronPostMatch) {
    try {
      const id = decodeURIComponent(cronPostMatch[1]).replace(/\.md$/, '').replace(/[^a-zA-Z0-9_\-]/g, '_');
      if (!id) { json(400, { error: 'invalid id' }); return; }
      const { content } = JSON.parse(await readBody(req));
      if (typeof content !== 'string') { json(400, { error: 'content required' }); return; }
      cronsEnsureDir();
      fs.writeFileSync(path.join(CRONS_DIR, id + '.md'), content, 'utf8');
      const rec = cronsRegister(id);
      cronsBroadcast();
      json(200, { ok: true, id, error: rec ? rec.error : null });
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── DELETE /api/cron/:name  ────────────────────────────────────────────────
  const cronDelMatch = req.url.match(/^\/api\/cron\/([^/?]+)$/);
  if (req.method === 'DELETE' && cronDelMatch) {
    try {
      const id = decodeURIComponent(cronDelMatch[1]).replace(/\.md$/, '');
      const fp = path.join(CRONS_DIR, id + '.md');
      if (!fs.existsSync(fp)) { json(404, { error: 'cron not found' }); return; }
      cronsUnregister(id);
      fs.unlinkSync(fp);
      // Also purge any pending entries for this id
      const before = cronPending.length;
      cronPending = cronPending.filter(p => p.id !== id);
      if (cronPending.length !== before) cronsSavePending();
      cronsBroadcast();
      json(200, { ok: true, id });
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── POST /api/cron/:name/run  — run immediately (queue semantics) ────────
  const cronRunMatch = req.url.match(/^\/api\/cron\/([^/?]+)\/run$/);
  if (req.method === 'POST' && cronRunMatch) {
    try {
      const id = decodeURIComponent(cronRunMatch[1]).replace(/\.md$/, '');
      const r  = cronsRunNow(id);
      json(r.ok ? 200 : 404, r);
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── GET /api/run-files/:runId  ────────────────────────────────────────────
  const runFilesMatch = req.url.match(/^\/api\/run-files\/([^/?]+)/);
  if (req.method === 'GET' && runFilesMatch) {
    const runDir = path.resolve(path.join(RUNS_DIR, runFilesMatch[1]));
    if (!fs.existsSync(runDir)) { json(404, { error: 'run not found' }); return; }
    const files = [];
    function walkDir(dir, rel) {
      try {
        const entries = fs.readdirSync(dir, { withFileTypes: true });
        for (const e of entries) {
          const relPath = rel ? rel + '/' + e.name : e.name;
          if (e.isDirectory()) {
            files.push({ path: relPath, type: 'dir' });
            walkDir(path.join(dir, e.name), relPath);
          } else {
            const st = fs.statSync(path.join(dir, e.name));
            files.push({ path: relPath, type: 'file', size: st.size });
          }
        }
      } catch {}
    }
    walkDir(runDir, '');
    json(200, { files });
    return;
  }

  // ── POST /api/run-file/:runId/*  ─────────────────────────────────────────
  const runFileWriteMatch = req.url.match(/^\/api\/run-file\/([^/]+)\/(.+)/);
  if (req.method === 'POST' && runFileWriteMatch) {
    try {
      const runDir  = path.resolve(path.join(RUNS_DIR, runFileWriteMatch[1]));
      const relFile = decodeURIComponent(runFileWriteMatch[2]);
      const fullPath = path.resolve(path.join(runDir, relFile));
      const rel = path.relative(runDir, fullPath);
      if (rel.startsWith('..') || path.isAbsolute(rel)) { json(403, { error: 'forbidden' }); return; }
      const { content } = JSON.parse(await readBody(req));
      if (typeof content !== 'string') { json(400, { error: 'content required' }); return; }
      fs.writeFileSync(fullPath, content, 'utf8');
      json(200, { ok: true });
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── GET /api/job-output/:runId/:jobId  ────────────────────────────────────
  const jobOutputMatch = req.url.match(/^\/api\/job-output\/([^/]+)\/([^/?]+)/);
  if (req.method === 'GET' && jobOutputMatch) {
    const runDir = path.resolve(path.join(RUNS_DIR, jobOutputMatch[1]));
    const jobId  = jobOutputMatch[2];
    if (!/^job_[0-9a-f]+$/.test(jobId)) { json(400, { error: 'invalid job id' }); return; }
    const jobFile = path.join(runDir, 'jobs', jobId + '.txt');
    const rel = path.relative(runDir, jobFile);
    if (rel.startsWith('..') || path.isAbsolute(rel)) { json(403, { error: 'forbidden' }); return; }
    try {
      const content = fs.readFileSync(jobFile, 'utf8');
      json(200, { content, exists: true });
    } catch (e) {
      if (e.code === 'ENOENT') { json(200, { content: '', exists: false }); return; }
      json(500, { error: String(e) });
    }
    return;
  }

  // ── GET /api/run-file/:runId/*  ───────────────────────────────────────────
  const runFileMatch = req.url.match(/^\/api\/run-file\/([^/]+)\/(.+)/);
  if (req.method === 'GET' && runFileMatch) {
    const runDir  = path.resolve(path.join(RUNS_DIR, runFileMatch[1]));
    const relFile = decodeURIComponent(runFileMatch[2]);
    const fullPath = path.resolve(path.join(runDir, relFile));
    const rel = path.relative(runDir, fullPath);
    if (rel.startsWith('..') || path.isAbsolute(rel)) {
      json(403, { error: 'forbidden' }); return;
    }
    try {
      const st = fs.statSync(fullPath);
      // For JSONL files strip embedded base64 blobs before the size check so
      // image-heavy conversation logs remain displayable in the dashboard.
      if (relFile.endsWith('.jsonl')) {
        const raw = fs.readFileSync(fullPath, 'utf8');
        json(200, { content: stripBase64FromJsonl(raw), truncated: false });
        return;
      }
      const content = fs.readFileSync(fullPath, 'utf8');
      json(200, { content });
    } catch (e) {
      if (e.code === 'ENOENT') { json(200, { content: null, exists: false }); return; }
      json(500, { error: String(e) });
    }
    return;
  }

  // ── GET /api/run-file-raw/:runId/*  — serve binary files with correct MIME ─
  const runFileRawMatch = req.url.match(/^\/api\/run-file-raw\/([^/]+)\/(.+)/);
  if (req.method === 'GET' && runFileRawMatch) {
    const runDir  = path.resolve(path.join(RUNS_DIR, runFileRawMatch[1]));
    const relFile = decodeURIComponent(runFileRawMatch[2]);
    const fullPath = path.resolve(path.join(runDir, relFile));
    const rel = path.relative(runDir, fullPath);
    if (rel.startsWith('..') || path.isAbsolute(rel)) {
      res.writeHead(403); res.end('forbidden'); return;
    }
    try {
      const ext = path.extname(fullPath).toLowerCase();
      const mime = MIME[ext] || 'application/octet-stream';
      const data = fs.readFileSync(fullPath);
      res.writeHead(200, { 'Content-Type': mime, 'Cache-Control': 'no-cache' });
      res.end(data);
    } catch (e) {
      res.writeHead(e.code === 'ENOENT' ? 404 : 500); res.end(String(e));
    }
    return;
  }

  // ── GET /api/download-file/:runId/*  — force-download with Content-Disposition ─
  const downloadFileMatch = req.url.match(/^\/api\/download-file\/([^/]+)\/(.+)/);
  if (req.method === 'GET' && downloadFileMatch) {
    const runDir  = path.resolve(path.join(RUNS_DIR, downloadFileMatch[1]));
    const relFile = decodeURIComponent(downloadFileMatch[2]);
    const fullPath = path.resolve(path.join(runDir, relFile));
    const rel = path.relative(runDir, fullPath);
    if (rel.startsWith('..') || path.isAbsolute(rel)) {
      res.writeHead(403); res.end('forbidden'); return;
    }
    try {
      const ext = path.extname(fullPath).toLowerCase();
      const mime = MIME[ext] || 'application/octet-stream';
      const filename = encodeURIComponent(path.basename(fullPath));
      const data = fs.readFileSync(fullPath);
      res.writeHead(200, {
        'Content-Type': mime,
        'Content-Disposition': `attachment; filename*=UTF-8''${filename}`,
        'Cache-Control': 'no-cache',
      });
      res.end(data);
    } catch (e) {
      res.writeHead(e.code === 'ENOENT' ? 404 : 500); res.end(String(e));
    }
    return;
  }

  // ── POST /api/file-tab — manage Files panel sub-tabs ────────────────────
  if (req.method === 'POST' && req.url === '/api/file-tab') {
    try {
      const body  = JSON.parse(await readBody(req));
      const { action, path: tabPath, label, runId } = body;
      const run   = runId || state.activeRunId;
      if (!run) return json(400, { error: 'no active run' });
      const tabsFp = path.join(RUNS_DIR, run, 'file_tabs.json');
      const DEFAULT_TABS = { tabs: [{ id: 'run', type: 'run', label: 'Run Files', pinned: true }], active: 'run' };
      let tabs = readJSON(tabsFp) || DEFAULT_TABS;

      if (action === 'list') {
        return json(200, tabs);
      } else if (action === 'open') {
        if (!tabPath) return json(400, { error: 'path required for open' });
        const existing = tabs.tabs.find(t => t.path === tabPath);
        if (existing) {
          tabs.active = existing.id;
        } else {
          const id = 'dir-' + Date.now();
          tabs.tabs.push({ id, type: 'dir', label: label || path.basename(tabPath) || tabPath, path: tabPath });
          tabs.active = id;
        }
      } else if (action === 'close') {
        if (!tabPath) return json(400, { error: 'path required for close' });
        const idx = tabs.tabs.findIndex(t => t.path === tabPath && !t.pinned);
        if (idx !== -1) {
          const closedId = tabs.tabs[idx].id;
          tabs.tabs.splice(idx, 1);
          if (tabs.active === closedId) {
            tabs.active = tabs.tabs[tabs.tabs.length - 1]?.id || 'run';
          }
        }
      } else {
        return json(400, { error: `unknown action: ${action}` });
      }

      fs.writeFileSync(tabsFp, JSON.stringify(tabs, null, 2), 'utf8');
      state.fileTabs = tabs;
      broadcast();
      return json(200, { ok: true, tabs });
    } catch (e) { return json(400, { error: String(e) }); }
  }

  // ── GET /api/fs/list?path=... — list any directory ───────────────────────
  if (req.method === 'GET' && req.url.startsWith('/api/fs/list')) {
    try {
      const u       = new URL(req.url, 'http://x');
      const dirPath = u.searchParams.get('path');
      if (!dirPath) return json(400, { error: 'path required' });
      const entries = fs.readdirSync(dirPath, { withFileTypes: true });
      const files = entries.map(e => {
        const fullPath = path.join(dirPath, e.name);
        let size = 0;
        if (e.isFile()) { try { size = fs.statSync(fullPath).size; } catch {} }
        return { name: e.name, type: e.isDirectory() ? 'dir' : 'file', size, fullPath };
      }).sort((a, b) => {
        if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
        return a.name.localeCompare(b.name);
      });
      return json(200, { path: dirPath, files });
    } catch (e) { return json(400, { error: String(e.message) }); }
  }

  // ── GET /api/fs/roots — drives (win) / "/" (unix) + home + cwd ───────────
  if (req.method === 'GET' && req.url === '/api/fs/roots') {
    try {
      const roots = [];
      if (process.platform === 'win32') {
        for (let c = 65; c <= 90; c++) {
          const drive = String.fromCharCode(c) + ':\\';
          try { fs.accessSync(drive); roots.push(drive); } catch {}
        }
      } else {
        roots.push('/');
      }
      return json(200, {
        roots,
        home: os.homedir(),
        cwd:  process.cwd(),
        agentDir: AGENT_DIR,
        sep:  path.sep,
      });
    } catch (e) { return json(400, { error: String(e.message) }); }
  }

  // ── GET /api/fs/read?path=... — read any text file ───────────────────────
  if (req.method === 'GET' && req.url.startsWith('/api/fs/read')) {
    try {
      const u        = new URL(req.url, 'http://x');
      const filePath = u.searchParams.get('path');
      if (!filePath) return json(400, { error: 'path required' });
      const content  = fs.readFileSync(filePath, 'utf8');
      return json(200, { content });
    } catch (e) { return json(400, { error: String(e.message) }); }
  }

  // ── PUT /api/fs/write — write any text file ──────────────────────────────
  if (req.method === 'PUT' && req.url === '/api/fs/write') {
    try {
      const body     = JSON.parse(await readBody(req));
      const filePath = body.path;
      if (!filePath) return json(400, { error: 'path required' });
      fs.writeFileSync(filePath, body.content ?? '', 'utf8');
      return json(200, { ok: true });
    } catch (e) { return json(400, { error: String(e.message) }); }
  }

  // ── GET /api/fs/raw?path=... — serve binary file with MIME type ──────────
  if (req.method === 'GET' && req.url.startsWith('/api/fs/raw')) {
    try {
      const u        = new URL(req.url, 'http://x');
      const filePath = u.searchParams.get('path');
      if (!filePath) { res.writeHead(400); res.end('path required'); return; }
      const ext  = path.extname(filePath).toLowerCase();
      const mime = MIME[ext] || 'application/octet-stream';
      const data = fs.readFileSync(filePath);
      res.writeHead(200, { 'Content-Type': mime, 'Cache-Control': 'no-cache' });
      res.end(data);
    } catch (e) { res.writeHead(404); res.end(String(e)); }
    return;
  }

  // ── POST /api/open-view — tell Electron to open a view tab ──────────────
  if (req.method === 'POST' && req.url === '/api/open-view') {
    try {
      const body = JSON.parse(await readBody(req));
      serverEvents.emit('open-view', {
        url:       body.url,
        title:     body.title || body.display_id,
        displayId: body.display_id,
      });
      // Also notify browser clients so remote browsers (e.g. computer A) can open the view.
      // Use pathname only so the client reconstructs the URL with its own origin/host.
      let viewPath = body.url;
      try { viewPath = new URL(body.url).pathname; } catch {}
      broadcastOpenView(body.display_id, viewPath, body.title || body.display_id);
      json(200, { ok: true });
    } catch (e) { json(400, { error: String(e) }); }
    return;
  }

  // ── POST /api/browser-action ─────────────────────────────────────────────
  if (req.method === 'POST' && req.url === '/api/browser-action') {
    try {
      const body = JSON.parse(await readBody(req));
      const { display_id = 'default', action, payload = {} } = body;
      if (!action) return json(400, { error: 'action is required' });

      if (process.env.ELECTRON) {
        serverEvents.emit('browser-action', { displayId: display_id, action, payload },
          result => { if (result.error) json(500, result); else json(200, result); }
        );
      } else {
        const result = await cdpBrowserAction(display_id, action, payload);
        json(200, result);
      }
    } catch (e) { json(500, { error: String(e.message || e) }); }
    return;
  }

  // ── GET /view/:runId  or  /view/:runId/:displayId  ───────────────────────
  const viewMatch = req.url.match(/^\/view\/([^/?]+)(?:\/([^/?]+))?/);
  if (req.method === 'GET' && viewMatch) {
    const fp = path.join(PUBLIC, 'view.html');
    try {
      const content = fs.readFileSync(fp);
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(content);
    } catch {
      res.writeHead(404); res.end('view.html not found — dashboard may need restart');
    }
    return;
  }

  // ── Terminal session control API (used by the agent's terminal_* tools) ────
  if (req.method === 'GET' && req.url === '/api/term') {
    json(200, { sessions: [...termSessions.values()].map(s => ({
      id: s.id, title: s.title, cwd: s.cwd, cols: s.cols, rows: s.rows,
      owner: s.owner, alive: s.alive, subscribers: s.subscribers.size,
    })) });
    return;
  }
  if (req.method === 'POST' && req.url === '/api/term') {
    let body = {}; try { body = JSON.parse(await readBody(req) || '{}'); } catch {}
    const s = createTermSession({ title: body.title, cols: body.cols, rows: body.rows, cwd: body.cwd });
    if (s.error) { json(503, { error: s.error }); return; }
    // createTermSession already broadcast()s the new session list, so every
    // frontend surfaces a tab for it — no separate open-terminal message needed.
    json(200, { id: s.id, title: s.title });
    return;
  }
  const termInputMatch = req.url.match(/^\/api\/term\/([^/?]+)\/input$/);
  if (req.method === 'POST' && termInputMatch) {
    const sess = termSessions.get(termInputMatch[1]);
    if (!sess) { json(404, { error: 'no such session' }); return; }
    let body = {}; try { body = JSON.parse(await readBody(req) || '{}'); } catch {}
    try { sess.pty.write(body.data || ''); } catch (e) { json(500, { error: e.message }); return; }
    json(200, { ok: true, seq: sess.totalSeq });
    return;
  }
  const termOutMatch = req.url.match(/^\/api\/term\/([^/?]+)\/output(?:\?.*)?$/);
  if (req.method === 'GET' && termOutMatch) {
    const sess = termSessions.get(termOutMatch[1]);
    if (!sess) { json(404, { error: 'no such session' }); return; }
    const since = new URL(req.url, 'http://x').searchParams.get('since');
    const r = termReadSince(sess, since);
    json(200, { data: r.data, seq: r.seq, alive: sess.alive, owner: sess.owner });
    return;
  }
  const termOwnerMatch = req.url.match(/^\/api\/term\/([^/?]+)\/owner$/);
  if (req.method === 'POST' && termOwnerMatch) {
    let body = {}; try { body = JSON.parse(await readBody(req) || '{}'); } catch {}
    if (!setTermOwner(termOwnerMatch[1], body.who)) { json(404, { error: 'no such session' }); return; }
    json(200, { ok: true });
    return;
  }
  const termKillMatch = req.url.match(/^\/api\/term\/([^/?]+)$/);
  if (req.method === 'DELETE' && termKillMatch) {
    json(200, { ok: killTermSession(termKillMatch[1]) });
    return;
  }

  serveStatic(req, res);
});

// ── Terminal (named, shareable PTY sessions) ────────────────────────────────
//
// A terminal is a *named session* (id) that any number of browser tabs AND the
// agent (over HTTP /api/term) can attach to at once — so the agent and the user
// drive the SAME shell and watch each other. Each session keeps a ring buffer of
// recent output so a (re)attaching client or the agent can read history, and an
// `owner` flag ('user'|'agent') that the UI uses to tint the background when the
// agent is "holding the mic".
//
// node-pty is an optional native dependency (prebuilt-multiarch fork — no local
// toolchain needed). If it fails to load, the terminal degrades gracefully: the
// endpoint stays up and tells the client to install it.
let pty = null;
let ptyLoadError = null;
try {
  pty = require('@homebridge/node-pty-prebuilt-multiarch');
} catch (e) {
  ptyLoadError = e;
  console.warn('  ⚠ 终端不可用：node-pty 加载失败 —', e.message);
}

// Default shell per platform. powershell.exe is always present on Windows;
// elsewhere honour $SHELL, falling back to bash → sh.
function defaultShell() {
  if (process.env.TERMINAL_SHELL) return process.env.TERMINAL_SHELL;
  if (os.platform() === 'win32')  return 'powershell.exe';
  return process.env.SHELL || 'bash';
}

const TERM_BUF_MAX = 200000;          // chars of output kept per session
const termSessions = new Map();        // id → session
let   termSeq      = 0;
const nextTermId = () => 't' + (++termSeq) + '-' + Date.now().toString(36);

function termBroadcast(sess, obj) {
  const msg = JSON.stringify(obj);
  for (const ws of sess.subscribers) { try { ws.send(msg); } catch {} }
}

// Create a new PTY-backed session. Returns the session, or { error } on failure.
function createTermSession({ title, cols, rows, cwd } = {}) {
  if (!pty) return { error: ptyLoadError ? ptyLoadError.message : 'node-pty 不可用' };
  const startCwd = cwd || process.env.TERMINAL_CWD || os.homedir();
  const shell = defaultShell();
  let term;
  try {
    term = pty.spawn(shell, [], {
      name: 'xterm-256color',
      cols: cols || 80, rows: rows || 24,
      cwd: startCwd,
      // Windows: keep ConPTY (default). winpty produces no output when the host
      // has no attached console (the GUI Electron case). ConPTY works there; its
      // only quirk is a cosmetic AttachConsole crash in a child on kill.
      env: { ...process.env, TERM: 'xterm-256color' },
    });
  } catch (e) { return { error: e.message }; }

  const sess = {
    id: nextTermId(), pty: term,
    title: title || 'Terminal',
    cwd: startCwd, cols: cols || 80, rows: rows || 24,
    buf: '', totalSeq: 0,            // ring buffer + absolute char counter
    subscribers: new Set(),          // attached browser WS clients
    owner: 'user', alive: true,
  };
  term.onData(data => {
    sess.buf += data;
    if (sess.buf.length > TERM_BUF_MAX) sess.buf = sess.buf.slice(-TERM_BUF_MAX);
    sess.totalSeq += data.length;
    termBroadcast(sess, { type: 'output', data });
  });
  term.onExit(({ exitCode }) => {
    sess.alive = false;
    termBroadcast(sess, { type: 'exit', code: exitCode });
    termSessions.delete(sess.id);
    broadcast();   // session gone → sync the tab away in every frontend
  });
  termSessions.set(sess.id, sess);
  broadcast();     // new session → surface a tab in every frontend
  return sess;
}

// Read output recorded at/after absolute offset `since`. Clamps to the start of
// the (capped) ring buffer when `since` is older than what we still hold.
function termReadSince(sess, since) {
  const bufStart = sess.totalSeq - sess.buf.length;
  const from = Math.max(0, (Number(since) || 0) - bufStart);
  return { data: sess.buf.slice(from), seq: sess.totalSeq };
}

function killTermSession(id) {
  const sess = termSessions.get(id);
  if (!sess) return false;
  try { sess.pty.kill(); } catch {}
  sess.alive = false;
  termSessions.delete(id);
  broadcast();   // sync the tab away in every frontend
  return true;
}

// Flip the "mic" owner and tell every attached client so the UI can tint.
function setTermOwner(id, who) {
  const sess = termSessions.get(id);
  if (!sess) return false;
  sess.owner = (who === 'agent') ? 'agent' : 'user';
  termBroadcast(sess, { type: 'owner', who: sess.owner });
  return true;
}

// Public session list — rides the state broadcast so EVERY frontend (incl. ones
// opened later) can rebuild its terminal tabs and stay in sync. Tiny metadata;
// the live output stream is a separate per-session fan-out, untouched.
function termListPublic() {
  return [...termSessions.values()].map(s => ({
    id: s.id, title: s.title, owner: s.owner, alive: s.alive,
  }));
}

// Browser WS protocol:
//   client → server : {type:'start', id?, cols, rows, title?}  (attach if id known, else create)
//                     {type:'input', data} | {type:'resize',cols,rows} | {type:'kill'}
//   server → client : {type:'session', id, title, owner}  (once, on attach; followed by buffered history)
//                     {type:'output', data} | {type:'owner', who} | {type:'exit', code}
function handleTerminalConnection(ws) {
  if (!pty) {
    try { ws.send(JSON.stringify({ type: 'output',
      data: `\r\n\x1b[31m终端不可用：node-pty 未能加载。\x1b[0m\r\n请在 dashboard/ 目录执行: npm install\r\n` +
            (ptyLoadError ? `(${ptyLoadError.message})\r\n` : '') })); } catch {}
    ws.close();
    return;
  }

  let sess = null;
  const attach = (s) => {
    sess = s;
    s.subscribers.add(ws);
    ws.send(JSON.stringify({ type: 'session', id: s.id, title: s.title, owner: s.owner }));
    if (s.buf) ws.send(JSON.stringify({ type: 'output', data: s.buf }));  // replay history
  };

  ws.on('message', raw => {
    let msg; try { msg = JSON.parse(raw); } catch { return; }
    if (msg.type === 'start') {
      if (sess) return;
      let s = msg.id ? termSessions.get(msg.id) : null;
      if (!s) {
        s = createTermSession({ title: msg.title, cols: msg.cols, rows: msg.rows });
        if (s.error) {
          try { ws.send(JSON.stringify({ type: 'output', data: `\r\n\x1b[31m无法启动 shell: ${s.error}\x1b[0m\r\n` })); } catch {}
          ws.close(); return;
        }
      }
      attach(s);
    } else if (msg.type === 'input') {
      if (sess && sess.alive) { try { sess.pty.write(msg.data); } catch {} }
    } else if (msg.type === 'resize') {
      if (sess && sess.alive && msg.cols && msg.rows) {
        try { sess.pty.resize(msg.cols, msg.rows); sess.cols = msg.cols; sess.rows = msg.rows; } catch {}
      }
    } else if (msg.type === 'kill') {
      if (sess) killTermSession(sess.id);
    }
  });

  // A closing tab only DETACHES — the session lives on so the agent (or a
  // reopened tab) can keep using it. Explicit teardown is {type:'kill'} or
  // DELETE /api/term/:id; PTY exit reaps it.
  const detach = () => { if (sess) sess.subscribers.delete(ws); };
  ws.on('close', detach);
  ws.on('error', detach);
}

// ── WebSocket ──────────────────────────────────────────────────────────────
//
// Two WS endpoints share the one HTTP server:
//   • default path  → live state stream (broadcast, one-to-many)
//   • /ws/term      → interactive terminal (per-client PTY, two-way)
//
// Both run in `noServer` mode so a single `upgrade` handler can apply the SAME
// isIpAllowed() gate before routing by path. The terminal is deliberately gated
// no more strictly than the rest of the dashboard: the file-manager tab already
// exposes the whole filesystem to anyone past the IP gate, so a shell is an
// equivalent exposure, not a greater one. LAN exposure is governed solely by
// DASHBOARD_HOST (bind address) + DASHBOARD_ALLOW/DENY, exactly like everything
// else.

const wss     = new WebSocket.Server({ noServer: true });
const termWss = new WebSocket.Server({ noServer: true });

server.on('upgrade', (req, socket, head) => {
  if (!isIpAllowed(req.socket.remoteAddress)) {
    socket.write('HTTP/1.1 403 Forbidden\r\n\r\n');
    socket.destroy();
    return;
  }
  const pathName = req.url.split('?')[0];
  if (pathName === '/ws/term') {
    termWss.handleUpgrade(req, socket, head, ws => termWss.emit('connection', ws, req));
  } else {
    wss.handleUpgrade(req, socket, head, ws => wss.emit('connection', ws, req));
  }
});

termWss.on('connection', handleTerminalConnection);

wss.on('connection', ws => {
  clients.add(ws);
  ws.send(JSON.stringify({ type: 'state', ...state, terminals: termListPublic() }));

  ws.on('message', raw => {
    try {
      const msg = JSON.parse(raw);
      // Heartbeat: the browser's WS layer pings every 25s and force-closes the
      // socket if nothing comes back within 10s. With no pong reply, an idle
      // agent would make every client self-disconnect every ~35s, triggering
      // a fresh full-state broadcast (incl. the entire events array) on each
      // reconnect — on mobile that loops faster than a long events log can
      // even finish transferring, so the log "times out" and never renders.
      if (msg.type === 'ping') {
        try { ws.send(JSON.stringify({ type: 'pong', ts: msg.ts })); } catch {}
        return;
      }
      if (msg.type === 'select_run') {
        const data = loadRun(msg.runId);
        if (data) ws.send(JSON.stringify({ type: 'historical', ...data }));
      }
    } catch {}
  });

  ws.on('close', () => clients.delete(ws));
  ws.on('error', () => clients.delete(ws));
});

// ── Cleanup on server exit ─────────────────────────────────────────────────

function cleanup() {
  if (isAgentRunning()) {
    console.log('\n  Terminating agent process…');
    agentProc.kill('SIGTERM');
  }
}
process.on('SIGINT',  () => { cleanup(); process.exit(0); });
process.on('SIGTERM', () => { cleanup(); process.exit(0); });
process.on('exit',    cleanup);

// ── Start ──────────────────────────────────────────────────────────────────

setInterval(poll, POLL_MS);
poll();

const net = require('net');

// Connect-probe: tries to reach the port. If connect succeeds → port is in
// use; if it errors → port is free. Works correctly on Windows where
// SO_REUSEADDR lets multiple sockets bind to the same port, which makes the
// common createServer-probe approach return false "free" results.
function findFreePort(startPort) {
  return new Promise(resolve => {
    const sock = net.connect(startPort, '127.0.0.1');
    sock.once('connect', () => { sock.destroy(); findFreePort(startPort + 1).then(resolve); });
    sock.once('error',   () => { sock.destroy(); resolve(startPort); });
  });
}

findFreePort(PORT).then(port => {
  if (port !== PORT) console.log(`  端口 ${PORT} 已被占用，改用 ${port}`);
  process.env.DASHBOARD_PORT = String(port);
  server.listen(port, HOST, () => {
    const lanNote = (HOST === '127.0.0.1' || HOST === 'localhost')
      ? ' (仅本机可访问；设置 DASHBOARD_HOST=0.0.0.0 可开放局域网)'
      : ` (绑定 ${HOST}，局域网可达)`;
    console.log('');
    console.log(`  🦊 QevosAgent Dashboard  v${APP_VERSION}`);
    console.log('  ─────────────────────────────────────');
    console.log(`  URL      : http://localhost:${port}${lanNote}`);
    if (ALLOW_LIST.length) console.log(`  Allow    : ${ALLOW_LIST.join(', ')} (仅放行白名单 + 本机)`);
    if (DENY_LIST.length)  console.log(`  Deny     : ${DENY_LIST.join(', ')} (黑名单优先拒绝)`);
    console.log(`  Runs     : ${RUNS_DIR}`);
    console.log(`  Agent    : ${AGENT_DIR}`);
    console.log(`  Python   : ${PYTHON_CMD}`);
    console.log(`  Team API : port ${process.env.TEAM_PORT || '9100'} (TEAM_PORT，端口占用时由 Agent 自动改用空闲端口)`);
    console.log(`  Poll     : every ${POLL_MS}ms`);
    console.log(`  Language : ${LANG}`);
    console.log('');
    console.log('  Tip: activate your conda env before running this server');
    console.log('       so that the correct python is used when launching agents.');
    console.log('');
    console.log('  Press Ctrl+C to stop.');
    console.log('');
  });
});
