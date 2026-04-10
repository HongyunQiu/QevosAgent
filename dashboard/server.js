'use strict';

/**
 * simpleAgent Dashboard Server
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

const PORT       = parseInt(process.env.DASHBOARD_PORT || '8765', 10);
const RUNS_DIR   = path.resolve(process.env.RUNS_DIR || path.join(__dirname, '..', 'runs'));
const AGENT_DIR  = path.resolve(process.env.AGENT_DIR || path.join(__dirname, '..'));
const PUBLIC     = path.join(__dirname, 'public');
const POLL_MS    = parseInt(process.env.POLL_MS || '500', 10);
// Python command — default to 'python' so the calling conda env is used
const PYTHON_CMD = process.env.PYTHON_CMD || 'python';

// ── Agent process state ────────────────────────────────────────────────────

let agentProc    = null;   // child_process handle
let isLaunching  = false;  // true from spawn() until first stdout or error

// ── Shared state (broadcast to all WS clients) ────────────────────────────

let state = {
  runs:        [],
  activeRunId: null,
  status:      null,
  scratchpad:  '',
  events:      [],
  meta:        {},
  launching:   false,   // agent is being spawned, not yet writing runs/
  agentPid:    null,
  agentAlive:  false,   // true iff the agent process is confirmed running right now
};

let _linesProcessed = 0;
let _mtimes         = {};
let _iterCounter    = 0;

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

// ── short_term.jsonl parser ────────────────────────────────────────────────

function parseLine(raw, lineIdx) {
  let rec;
  try { rec = JSON.parse(raw); } catch { return null; }
  const { role, content } = rec;
  if (!role) return null;
  if (role === '__token__') {
    return { idx: lineIdx, type: 'token_stats', promptEst: rec.prompt_est, contextWindow: rec.context_window, maxTokens: rec.max_tokens };
  }
  if (typeof content !== 'string') return null;

  const base = { idx: lineIdx };

  if (role === 'assistant') {
    let action;
    try {
      // Strip ```json ... ``` fences — LLM sometimes wraps its JSON in markdown code blocks.
      // This mirrors the fence-stripping logic in llm.py's _parse_llm_output.
      const fenceMatch = content.match(/```(?:json)?\s*([\s\S]*?)```/i);
      const jsonStr = fenceMatch ? fenceMatch[1].trim() : content;
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
      return { ...base, type: 'error', text: thought || content, thought: null };
    }
    if (thought) return { ...base, type: 'thought', thought };
    return null;
  }

  if (role === 'user') {
    const toolMatch = content.match(/^\[工具:\s*([^\]]+)\]\s*(执行成功|执行失败)\s*\n?([\s\S]*)/);
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
    if (content.startsWith('[用户干预注入]') || content.startsWith('[Web看板]')) {
      return { ...base, type: 'injected', text: content.replace(/^\[[^\]]+\]\s*\n?/, '').trim() };
    }
    if (content.startsWith('[用户补充信息]')) {
      return { ...base, type: 'user_answer', text: content.replace(/^\[用户补充信息\]\s*\n?/, '').trim() };
    }
    if (lineIdx === 0) {
      const goalMarker = content.match(/请完成以下目标：\s*\n([\s\S]*)/);
      return { ...base, type: 'goal', text: goalMarker ? goalMarker[1].trim() : content.trim() };
    }
    return { ...base, type: 'user_msg', text: content.trim() };
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
    dirty = true;
  }

  const latest = runs[runs.length - 1] || null;

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
 * Returns { ok, error? }.
 */
function launchAgent(goal) {
  if (isAgentRunning()) {
    return { ok: false, error: 'An agent process is already running.' };
  }
  if (isLaunching) {
    return { ok: false, error: 'Agent is already being started.' };
  }

  isLaunching     = true;
  state.launching = true;
  broadcast();

  broadcastConsole('system', `▶ Launching: ${PYTHON_CMD} run_goal.py "${goal.slice(0, 80)}${goal.length > 80 ? '…' : ''}"`);
  broadcastConsole('system', `  Working dir: ${AGENT_DIR}`);

  // On Windows, PYTHON_CMD may be a multi-word string like "conda run -n myenv python".
  // Split into command + args so spawn works correctly on all platforms.
  const parts    = PYTHON_CMD.trim().split(/\s+/);
  const cmd      = parts[0];
  const cmdArgs  = [...parts.slice(1), 'run_goal.py', goal];

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
  '.svg':  'image/svg+xml',
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
      const { goal } = JSON.parse(await readBody(req));
      if (!goal || !goal.trim()) { json(400, { error: 'goal is required' }); return; }
      const result = launchAgent(goal.trim());
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

  // ── GET /api/snapshot-meta  ──────────────────────────────────────────────
  if (req.method === 'GET' && req.url === '/api/snapshot-meta') {
    const fp = path.join(AGENT_DIR, 'agent_snapshot_meta.json');
    if (!fs.existsSync(fp)) { json(200, { content: null, exists: false }); return; }
    const content = readText(fp) || '';
    json(200, { content });
    return;
  }

  // ── POST /api/snapshot-meta  ─────────────────────────────────────────────
  if (req.method === 'POST' && req.url === '/api/snapshot-meta') {
    try {
      const { content } = JSON.parse(await readBody(req));
      if (typeof content !== 'string') { json(400, { error: 'content required' }); return; }
      fs.writeFileSync(path.join(AGENT_DIR, 'agent_snapshot_meta.json'), content, 'utf8');
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
      if (st.size > 512 * 1024) {
        json(200, { content: `[File too large to display inline: ${(st.size / 1024).toFixed(0)} KB]`, truncated: true });
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

server.listen(PORT, '0.0.0.0', () => {
  console.log('');
  console.log('  ⚡ simpleAgent Dashboard');
  console.log('  ─────────────────────────────────────');
  console.log(`  URL      : http://localhost:${PORT}`);
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
