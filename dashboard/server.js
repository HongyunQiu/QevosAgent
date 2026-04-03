'use strict';

/**
 * simpleAgent Dashboard Server
 * ─────────────────────────────
 * - Polls the runs/ directory every POLL_MS milliseconds
 * - Streams state updates to all WebSocket clients
 * - Accepts POST /api/inject to write web_cmd.txt for agent pickup
 * - Serves static files from ./public/
 */

const http = require('http');
const fs   = require('fs');
const path = require('path');
const WebSocket = require('ws');

const PORT     = parseInt(process.env.DASHBOARD_PORT || '8765', 10);
const RUNS_DIR = path.resolve(process.env.RUNS_DIR || path.join(__dirname, '..', 'runs'));
const PUBLIC   = path.join(__dirname, 'public');
const POLL_MS  = parseInt(process.env.POLL_MS || '500', 10);

// ── State ──────────────────────────────────────────────────────────────────

/** Shared state broadcast to all clients */
let state = {
  runs:        [],
  activeRunId: null,
  status:      null,
  scratchpad:  '',
  events:      [],
  meta:        {},
};

let _linesProcessed = 0;   // lines of short_term.jsonl already parsed
let _mtimes = {};           // { filepath: mtimeMs }
let _iterCounter = 0;       // virtual iteration counter derived from events

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

/** Returns true if file mtime changed (and updates cache). */
function changed(fp) {
  const m = mtime(fp);
  if (_mtimes[fp] !== m) { _mtimes[fp] = m; return true; }
  return false;
}

function findRuns() {
  if (!fs.existsSync(RUNS_DIR)) return [];
  try {
    return fs.readdirSync(RUNS_DIR)
      .filter(d => /^\d{8}-\d{6}$/.test(d))
      .sort();
  } catch { return []; }
}

// ── short_term.jsonl parser ────────────────────────────────────────────────

/**
 * Parse one line from short_term.jsonl into a dashboard event.
 * Returns null if the line should be skipped.
 */
function parseLine(raw, lineIdx) {
  let rec;
  try { rec = JSON.parse(raw); } catch { return null; }
  const { role, content } = rec;
  if (!role || typeof content !== 'string') return null;

  const base = { idx: lineIdx };

  // ── Assistant message: JSON action ──────────────────────────────────────
  if (role === 'assistant') {
    let action;
    try { action = JSON.parse(content); } catch { return null; }

    const thought = action.thought || null;

    if (action.action === 'tool_call') {
      return { ...base, type: 'tool_call', tool: action.tool, args: action.args || {}, thought };
    }
    if (action.action === 'done') {
      return { ...base, type: 'done', answer: action.final_answer || '', thought };
    }
    if (action.action === 'error') {
      return { ...base, type: 'error', text: thought || content, thought: null };
    }
    // Thought-only (rare)
    if (thought) {
      return { ...base, type: 'thought', thought };
    }
    return null;
  }

  // ── User message ─────────────────────────────────────────────────────────
  if (role === 'user') {
    // Tool result: "[工具: toolname] 执行成功/失败\n输出..."
    const toolMatch = content.match(/^\[工具:\s*([^\]]+)\]\s*(执行成功|执行失败)\s*\n?([\s\S]*)/);
    if (toolMatch) {
      const success = toolMatch[2] === '执行成功';
      const raw_out = toolMatch[3].replace(/^输出\(可能已截断\):\n?/, '').replace(/^错误:\n?/, '').trim();
      return { ...base, type: 'tool_result', tool: toolMatch[1].trim(), success, output: raw_out };
    }

    // Injected message: "[用户干预注入]\n..."  or "[Web看板] ..."
    if (content.startsWith('[用户干预注入]') || content.startsWith('[Web看板]')) {
      const text = content.replace(/^\[[^\]]+\]\s*\n?/, '').trim();
      return { ...base, type: 'injected', text };
    }

    // First user message = goal
    if (lineIdx === 0) {
      // Strip AGENTS.md preamble to get just the user goal portion
      const goalMarker = content.match(/请完成以下目标：\s*\n([\s\S]*)/);
      const text = goalMarker ? goalMarker[1].trim() : content.trim();
      // Extract just the actual goal (last non-empty block after last \n\n chain)
      return { ...base, type: 'goal', text };
    }

    // Other user messages (e.g. after /inject or ask_user)
    return { ...base, type: 'user_msg', text: content.trim() };
  }

  return null;
}

/** Incrementally parse new lines from short_term.jsonl. Returns true if new events added. */
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
    // Assign iteration: each tool_call starts a new iteration
    if (ev.type === 'tool_call' || ev.type === 'done' || ev.type === 'error') iter++;
    ev.iter = iter;
    state.events.push(ev);
  }
  _linesProcessed = allLines.length;
  _iterCounter = iter;
  return true;
}

// ── Poll loop ──────────────────────────────────────────────────────────────

function poll() {
  const runs = findRuns();
  let dirty = false;

  if (JSON.stringify(runs) !== JSON.stringify(state.runs)) {
    state.runs = runs;
    dirty = true;
  }

  const latest = runs[runs.length - 1] || null;

  // New run started → reset all per-run state
  if (latest !== state.activeRunId) {
    state.activeRunId = latest;
    state.status      = null;
    state.scratchpad  = '';
    state.events      = [];
    state.meta        = {};
    _linesProcessed   = 0;
    _iterCounter      = 0;
    _mtimes           = {};
    dirty = true;
  }

  if (!state.activeRunId) {
    if (dirty) broadcast();
    return;
  }

  const dir = path.join(RUNS_DIR, state.activeRunId);

  if (changed(path.join(dir, 'status.json'))) {
    const s = readJSON(path.join(dir, 'status.json'));
    if (s) { state.status = s; dirty = true; }
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

  if (dirty) broadcast();
}

// ── WebSocket ──────────────────────────────────────────────────────────────

const clients = new Set();

function broadcast() {
  const msg = JSON.stringify({ type: 'state', ...state });
  for (const ws of clients) {
    if (ws.readyState === WebSocket.OPEN) ws.send(msg);
  }
}

// ── Load a historical run (no live updates) ────────────────────────────────

function loadRun(runId) {
  const dir = path.join(RUNS_DIR, runId);
  if (!fs.existsSync(dir)) return null;

  const status    = readJSON(path.join(dir, 'status.json'));
  const scratchpad = readText(path.join(dir, 'scratchpad.md')) || '';
  const meta      = readJSON(path.join(dir, 'meta.json')) || {};

  const raw    = readText(path.join(dir, 'short_term.jsonl')) || '';
  const lines  = raw.split('\n').filter(l => l.trim());
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
  '.svg':  'image/svg+xml',
};

function serveStatic(req, res) {
  const urlPath = req.url === '/' ? '/index.html' : req.url.split('?')[0];
  const fp = path.join(PUBLIC, urlPath);
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

const server = http.createServer((req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  // POST /api/inject  — write web_cmd.txt for agent pickup
  if (req.method === 'POST' && req.url === '/api/inject') {
    let body = '';
    req.on('data', c => (body += c));
    req.on('end', () => {
      try {
        const { command, runId } = JSON.parse(body);
        const target = runId || state.activeRunId;
        if (!target || !command) {
          res.writeHead(400);
          res.end(JSON.stringify({ error: 'missing command or active run' }));
          return;
        }
        const cmdFile = path.join(RUNS_DIR, target, 'web_cmd.txt');
        fs.writeFileSync(cmdFile, command.trim() + '\n', 'utf8');

        // Echo into live event log immediately
        state.events.push({ type: 'injected', text: command.trim(), iter: _iterCounter, idx: -1 });
        broadcast();

        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true }));
      } catch (e) {
        res.writeHead(500);
        res.end(JSON.stringify({ error: String(e) }));
      }
    });
    return;
  }

  // GET /api/state  — full current state (REST fallback)
  if (req.method === 'GET' && req.url === '/api/state') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ type: 'state', ...state }));
    return;
  }

  // GET /api/run/:runId  — historical run data
  const runMatch = req.url.match(/^\/api\/run\/([^/?]+)/);
  if (req.method === 'GET' && runMatch) {
    const data = loadRun(runMatch[1]);
    if (!data) { res.writeHead(404); res.end('{}'); return; }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ type: 'historical', ...data }));
    return;
  }

  // Static files
  serveStatic(req, res);
});

// ── WebSocket upgrade ──────────────────────────────────────────────────────

const wss = new WebSocket.Server({ server });

wss.on('connection', (ws) => {
  clients.add(ws);
  // Send current state immediately on connect
  ws.send(JSON.stringify({ type: 'state', ...state }));

  ws.on('message', (raw) => {
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

// ── Start ──────────────────────────────────────────────────────────────────

setInterval(poll, POLL_MS);
poll(); // initial load

server.listen(PORT, '0.0.0.0', () => {
  console.log('');
  console.log('  ⚡ simpleAgent Dashboard');
  console.log('  ─────────────────────────────────────');
  console.log(`  URL  : http://localhost:${PORT}`);
  console.log(`  Runs : ${RUNS_DIR}`);
  console.log(`  Poll : every ${POLL_MS}ms`);
  console.log('');
  console.log('  Press Ctrl+C to stop.');
  console.log('');
});
