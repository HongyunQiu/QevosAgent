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
const { spawn }    = require('child_process');
const WebSocket    = require('ws');
const EventEmitter = require('events');

// Emits 'open-view' when Electron should open a view tab.
// main.js listens to this because both files run in the same Node process.
const serverEvents = new EventEmitter();
module.exports = { serverEvents };

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

const PORT       = parseInt(process.env.DASHBOARD_PORT || '8765', 10);
const RUNS_DIR   = path.resolve(process.env.RUNS_DIR || path.join(__dirname, '..', 'runs'));
const AGENT_DIR  = path.resolve(process.env.AGENT_DIR || path.join(__dirname, '..'));
const SKILLS_DIR = path.resolve(process.env.SKILLS_DIR || path.join(AGENT_DIR, 'SKILLS'));
const PUBLIC     = path.join(__dirname, 'public');
const POLL_MS    = parseInt(process.env.POLL_MS || '500', 10);

let APP_VERSION = 'dev';
try {
  const pkgPath = path.join(__dirname, '..', 'desktop', 'package.json');
  APP_VERSION = JSON.parse(fs.readFileSync(pkgPath, 'utf8')).version || 'dev';
} catch {}
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
  events:       [],
  meta:         {},
  launching:    false,  // agent is being spawned, not yet writing runs/
  agentPid:     null,
  agentAlive:   false,  // true iff the agent process is confirmed running right now
  webDisplays:  {},     // { display_id: { content_type, title, content, updated_at } }
};

let _linesProcessed        = 0;
let _mtimes                = {};
let _iterCounter           = 0;
let _webChatLinesProcessed = 0;

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
    const toolMatch = text.match(/^\[工具:\s*([^\]]+)\]\s*(执行成功|执行失败)\s*\n?([\s\S]*)/);
    if (toolMatch) {
      const toolName = toolMatch[1].trim();
      // ask_user tool_result is redundant — the question is already shown in the tool_call event.
      if (toolName === 'ask_user') return null;
      const success = toolMatch[2] === '执行成功';
      const raw_out = toolMatch[3]
        .replace(/^输出\(可能已截断\):\n?/, '')
        .replace(/^错误:\n?/, '')
        .trim();
      return { ...base, type: 'tool_result', tool: toolName, success, output: raw_out };
    }
    if (text.startsWith('[高级指导员')) {
      const reasonMatch = text.match(/\[高级指导员[^\]]*触发:\s*([^\]]+)\]/);
      const reason = reasonMatch ? reasonMatch[1].trim() : 'unknown';
      const bodyMatch = text.match(/^\[[^\]]+\]\s*\n\n([\s\S]*?)(?:\n\n---\n[\s\S]*)?$/);
      const body = bodyMatch ? bodyMatch[1].trim() : text.replace(/^\[[^\]]+\]\s*\n?/, '').trim();
      return { ...base, type: 'advisor', reason, text: body };
    }
    if (text.startsWith('[用户干预注入]') || text.startsWith('[Web看板]')) {
      return { ...base, type: 'injected', text: text.replace(/^\[[^\]]+\]\s*\n?/, '').trim() };
    }
    if (text.startsWith('[用户补充信息]')) {
      return { ...base, type: 'user_answer', text: text.replace(/^\[用户补充信息\]\s*\n?/, '').trim() };
    }
    if (lineIdx === 0) {
      const goalMarker = text.match(/请完成以下目标：\s*\n([\s\S]*)/);
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
    if (ev.type === 'tool_call' || ev.type === 'done' || ev.type === 'error') iter++;
    ev.iter = iter;
    state.events.push(ev);
  }
  _linesProcessed = allLines.length;
  _iterCounter    = iter;
  return true;
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
    state.events      = [];
    state.meta        = {};
    state.webDisplays = {};
    _linesProcessed        = 0;
    _iterCounter           = 0;
    _mtimes                = {};
    _webChatLinesProcessed = 0;
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
    if (changed(path.join(dir, 'meta.json'))) {
      const m = readJSON(path.join(dir, 'meta.json'));
      if (m) { state.meta = m; dirty = true; }
    }
    if (updateShortTerm(dir)) dirty = true;

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
  const msg = JSON.stringify({ type: 'state', ...state });
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
 * Returns { ok, error? }.
 */
function launchAgent(goal, nostop = false, skills = []) {
  if (isAgentRunning()) {
    return { ok: false, error: 'An agent process is already running.' };
  }
  if (isLaunching) {
    return { ok: false, error: 'Agent is already being started.' };
  }

  isLaunching     = true;
  state.launching = true;
  broadcast();

  const nostopLabel  = nostop ? ' --nostop' : '';
  const skillsLabel  = skills.length ? ` --skills ${skills.join(',')}` : '';
  broadcastConsole('system', `▶ Launching: ${PYTHON_CMD} run_goal.py${nostopLabel}${skillsLabel} "${goal.slice(0, 80)}${goal.length > 80 ? '…' : ''}"`);
  broadcastConsole('system', `  Working dir: ${AGENT_DIR}`);

  // On Windows, PYTHON_CMD may be a multi-word string like "conda run -n myenv python",
  // or a quoted path with spaces like "\"C:\\Programme Files\\python.exe\"".
  // parsePythonCmd() handles both cases correctly.
  const parts    = parsePythonCmd(PYTHON_CMD);
  const cmd      = parts[0];
  const cmdArgs  = [...parts.slice(1), 'run_goal.py'];
  if (nostop) cmdArgs.push('--nostop');
  if (skills.length) { cmdArgs.push('--skills'); cmdArgs.push(skills.join(',')); }
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
  return { runId, status, scratchpad, meta, events };
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
    const content = fs.readFileSync(fp);
    res.writeHead(200, { 'Content-Type': MIME[ext] || 'text/plain' });
    res.end(content);
  } catch {
    res.writeHead(404);
    res.end('Not found');
  }
}

const server = http.createServer(async (req, res) => {
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
      const { goal, nostop, skills } = JSON.parse(await readBody(req));
      if (!goal || !goal.trim()) { json(400, { error: 'goal is required' }); return; }
      const skillList = Array.isArray(skills) ? skills.filter(Boolean) : [];
      const result = launchAgent(goal.trim(), !!nostop, skillList);
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
      fs.writeFileSync(cmdFile, command.trim() + '\n', 'utf8');
      state.events.push({ type: 'injected', text: command.trim(), iter: _iterCounter, idx: -1 });
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
      fs.writeFileSync(path.join(RUNS_DIR, target, 'web_cmd.txt'), cmd.trim() + '\n', 'utf8');

      state.events.push({ type: 'injected', text: userText, iter: _iterCounter, idx: -1 });
      broadcast();
      json(200, { ok: true, path: relPath });
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── GET /api/version ─────────────────────────────────────────────────────
  if (req.method === 'GET' && req.url === '/api/version') {
    json(200, { version: APP_VERSION });
    return;
  }

  // ── GET /api/state  ───────────────────────────────────────────────────────
  if (req.method === 'GET' && req.url === '/api/state') {
    json(200, { type: 'state', ...state });
    return;
  }

  // ── GET /api/run/:runId  ──────────────────────────────────────────────────
  const runMatch = req.url.match(/^\/api\/run\/([^/?]+)/);
  if (req.method === 'GET' && runMatch) {
    const data = loadRun(runMatch[1]);
    if (!data) { res.writeHead(404); res.end('{}'); return; }
    json(200, { type: 'historical', ...data });
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
    const fp = path.join(AGENT_DIR, 'memory_macro.md');
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
      fs.writeFileSync(path.join(AGENT_DIR, 'memory_macro.md'), content, 'utf8');
      json(200, { ok: true });
    } catch (e) { json(500, { error: String(e) }); }
    return;
  }

  // ── GET /api/memory-episodic  ────────────────────────────────────────────
  if (req.method === 'GET' && req.url === '/api/memory-episodic') {
    const fp = path.join(AGENT_DIR, 'memory_episodic.jsonl');
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
      fs.writeFileSync(path.join(AGENT_DIR, 'memory_episodic.jsonl'), content, 'utf8');
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

  // ── POST /api/open-view — tell Electron to open a view tab ──────────────
  if (req.method === 'POST' && req.url === '/api/open-view') {
    try {
      const body = JSON.parse(await readBody(req));
      serverEvents.emit('open-view', {
        url:       body.url,
        title:     body.title || body.display_id,
        displayId: body.display_id,
      });
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

  serveStatic(req, res);
});

// ── WebSocket ──────────────────────────────────────────────────────────────

const wss = new WebSocket.Server({ server });

wss.on('connection', ws => {
  clients.add(ws);
  ws.send(JSON.stringify({ type: 'state', ...state }));

  ws.on('message', raw => {
    try {
      const msg = JSON.parse(raw);
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
  server.listen(port, '0.0.0.0', () => {
    console.log('');
    console.log(`  🦊 QevosAgent Dashboard  v${APP_VERSION}`);
    console.log('  ─────────────────────────────────────');
    console.log(`  URL      : http://localhost:${port}`);
    console.log(`  Runs     : ${RUNS_DIR}`);
    console.log(`  Agent    : ${AGENT_DIR}`);
    console.log(`  Python   : ${PYTHON_CMD}`);
    console.log(`  Poll     : every ${POLL_MS}ms`);
    console.log('');
    console.log('  Tip: activate your conda env before running this server');
    console.log('       so that the correct python is used when launching agents.');
    console.log('');
    console.log('  Press Ctrl+C to stop.');
    console.log('');
  });
});
