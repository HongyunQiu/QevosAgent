'use strict';

/**
 * End-to-end smoke test for auto-followup on weak pass.
 *
 * Boots a real server.js against a throwaway RUNS_DIR and drives it purely
 * through the filesystem, which is exactly how it observes the agent in
 * production. PYTHON_CMD points at a stub that dumps its argv and exits, so the
 * goal the followup was launched with can be asserted end to end.
 *
 * Run:  node --test dashboard/followup.test.js
 */

const test   = require('node:test');
const assert = require('node:assert/strict');
const fs     = require('fs');
const os     = require('os');
const path   = require('path');
const { spawn } = require('child_process');

const SERVER = path.join(__dirname, 'server.js');

function mkTmp() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'qevos-followup-'));
}

/** Poll a predicate until it holds or the deadline passes. */
async function until(fn, ms = 15000, label = 'condition') {
  const deadline = Date.now() + ms;
  for (;;) {
    let v;
    try { v = fn(); } catch { v = false; }
    if (v) return v;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${label}`);
    await new Promise(r => setTimeout(r, 100));
  }
}

function writeRun(runsDir, id, { status, run_outcome, gaps = [], goal = '写一个解析器', alivePid = null }) {
  const dir = path.join(runsDir, id);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'status.json'), JSON.stringify({
    status,
    run_outcome,
    resumable: run_outcome === 'partial' || run_outcome === 'blocked',
    run_outcome_detail: run_outcome ? { outcome: run_outcome, resumable: true, gaps } : null,
    run_id: id,
    goal,
    summary: goal,
  }));
  fs.writeFileSync(path.join(dir, 'meta.json'), JSON.stringify({ _user_goal: goal }));
  if (alivePid) fs.writeFileSync(path.join(dir, 'agent.pid'), String(alivePid));
  return dir;
}

function ledger(runsDir) {
  try {
    return fs.readFileSync(path.join(runsDir, '.followup.jsonl'), 'utf8')
      .split(/\r?\n/).filter(Boolean).map(l => JSON.parse(l));
  } catch { return []; }
}

/**
 * Stand-in for `python run_goal.py`: records the argv it was launched with and
 * exits. Its path must be space-free — parsePythonCmd() splits args on
 * whitespace — which tmpdir satisfies on both platforms.
 */
function writeLaunchStub(tmp) {
  const stub = path.join(tmp, 'stub.js');
  const out  = path.join(tmp, 'argv.json');
  fs.writeFileSync(stub,
    `require('fs').writeFileSync(${JSON.stringify(out)}, JSON.stringify(process.argv.slice(2)));\n`);
  return { cmd: `"${process.execPath}" ${stub}`, argvFile: out };
}

/** Boot server.js sandboxed to `tmp`; resolves once it is listening. */
async function boot(tmp, extraEnv = {}) {
  const proc = spawn(process.execPath, [SERVER], {
    cwd: __dirname,
    env: {
      ...process.env,
      RUNS_DIR:     path.join(tmp, 'runs'),
      AGENT_DIR:    tmp,
      CRONS_DIR:    path.join(tmp, 'crons'),
      APPS_DIR:     path.join(tmp, 'apps'),
      APP_DATA_DIR: path.join(tmp, 'app-data'),
      SKILLS_DIR:   path.join(tmp, 'SKILLS'),
      DOTENV_PATH:  path.join(tmp, '.env'),
      DASHBOARD_PORT: '0',           // findFreePort walks up from here
      PYTHON_CMD:   process.execPath, // a "launch" runs node run_goal.py → exits
      POLL_MS:      '200',
      ...extraEnv,
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let out = '';
  proc.stdout.on('data', d => { out += d; });
  proc.stderr.on('data', d => { out += d; });
  proc.on('exit', code => { if (code) console.error(`[server exited ${code}]\n${out}`); });
  await until(() => out.includes('QevosAgent Dashboard'), 15000, 'server startup');
  return proc;
}

test('weak pass is nudged to finish, then a followup run is launched', async t => {
  const tmp = mkTmp();
  const runsDir = path.join(tmp, 'runs');
  fs.mkdirSync(runsDir, { recursive: true });

  // A live run parked on a weak pass. Our own pid stands in for the agent's:
  // it is genuinely alive, so the dashboard sees agentAlive.
  const dir = writeRun(runsDir, '20260101-000000', {
    status: 'paused', run_outcome: 'partial', gaps: ['C 格式未实现'], alivePid: process.pid,
  });

  const stub = writeLaunchStub(tmp);
  const server = await boot(tmp, { PYTHON_CMD: stub.cmd });
  t.after(() => { server.kill(); });

  // Step 1: the dashboard answers the weak-pass question on the user's behalf.
  const cmdFile = path.join(dir, 'web_cmd.txt');
  await until(() => fs.existsSync(cmdFile), 15000, 'web_cmd.txt');
  assert.equal(fs.readFileSync(cmdFile, 'utf8').trim(), '/inject 完成');

  const nudged = ledger(runsDir).find(r => r.event === 'nudged');
  assert.ok(nudged, 'nudge is recorded in the ledger');
  assert.equal(nudged.parent, '20260101-000000');
  assert.equal(nudged.depth, 1);

  // Step 2: the agent exits (pid file gone) → the followup generation launches.
  fs.writeFileSync(path.join(dir, 'status.json'), JSON.stringify({
    status: 'done', run_outcome: 'partial', resumable: true, run_id: '20260101-000000',
    goal: '写一个解析器', summary: '写一个解析器',
  }));
  fs.unlinkSync(path.join(dir, 'agent.pid'));

  const launched = await until(
    () => ledger(runsDir).find(r => r.event === 'launched'), 15000, 'followup launch',
  );
  assert.equal(launched.parent, '20260101-000000');
  assert.equal(launched.depth, 1);

  // Step 3: the goal it was launched with. The original user goal comes first —
  // it is what the run list shows, and it must not be replaced by the followup
  // preamble. Then the two distilled files, the one file to stay out of, and the
  // opening think step.
  const argv = await until(
    () => fs.existsSync(stub.argvFile) && JSON.parse(fs.readFileSync(stub.argvFile, 'utf8')),
    15000, 'stub argv',
  );
  assert.equal(argv[0], 'run_goal.py');
  const goal = argv[argv.length - 1];
  assert.ok(goal.startsWith('写一个解析器'), 'original goal leads');
  assert.ok(goal.includes('runs/20260101-000000/execution_summary.md'), 'points at the summary');
  assert.ok(goal.includes('handoff_'), 'offers the handoff docs for depth');
  assert.ok(goal.includes('不要读 short_term.jsonl'), 'rules out the raw transcript');
  assert.ok(goal.includes('先调用一次 think'), 'asks for the think step');
  assert.ok(!argv.includes('--nostop'), 'followups never run in nostop');
});

test('a weak pass is only ever nudged once', async t => {
  const tmp = mkTmp();
  const runsDir = path.join(tmp, 'runs');
  fs.mkdirSync(runsDir, { recursive: true });
  const dir = writeRun(runsDir, '20260101-000000', {
    status: 'paused', run_outcome: 'partial', alivePid: process.pid,
  });

  const server = await boot(tmp);
  t.after(() => { server.kill(); });

  await until(() => fs.existsSync(path.join(dir, 'web_cmd.txt')), 15000, 'web_cmd.txt');
  // Stay paused and alive across many poll cycles — the nudge must not repeat.
  await new Promise(r => setTimeout(r, 2000));
  assert.equal(ledger(runsDir).filter(r => r.event === 'nudged').length, 1);
});

test('an ordinary ask_user pause is left alone', async t => {
  const tmp = mkTmp();
  const runsDir = path.join(tmp, 'runs');
  fs.mkdirSync(runsDir, { recursive: true });
  // paused, but no weak-pass outcome — this is the agent asking a question.
  const dir = writeRun(runsDir, '20260101-000000', {
    status: 'paused', run_outcome: null, alivePid: process.pid,
  });

  const server = await boot(tmp);
  t.after(() => { server.kill(); });

  await new Promise(r => setTimeout(r, 2500));
  assert.equal(fs.existsSync(path.join(dir, 'web_cmd.txt')), false);
  assert.deepEqual(ledger(runsDir), []);
});

test('a completed run is left alone', async t => {
  const tmp = mkTmp();
  const runsDir = path.join(tmp, 'runs');
  fs.mkdirSync(runsDir, { recursive: true });
  const dir = writeRun(runsDir, '20260101-000000', {
    status: 'done', run_outcome: 'completed', alivePid: process.pid,
  });

  const server = await boot(tmp);
  t.after(() => { server.kill(); });

  await new Promise(r => setTimeout(r, 2500));
  assert.equal(fs.existsSync(path.join(dir, 'web_cmd.txt')), false);
});

test('AUTO_FOLLOWUP=0 disables the whole mechanism', async t => {
  const tmp = mkTmp();
  const runsDir = path.join(tmp, 'runs');
  fs.mkdirSync(runsDir, { recursive: true });
  const dir = writeRun(runsDir, '20260101-000000', {
    status: 'paused', run_outcome: 'partial', alivePid: process.pid,
  });

  const server = await boot(tmp, { AUTO_FOLLOWUP: '0' });
  t.after(() => { server.kill(); });

  await new Promise(r => setTimeout(r, 2500));
  assert.equal(fs.existsSync(path.join(dir, 'web_cmd.txt')), false);
  assert.deepEqual(ledger(runsDir), []);
});

test('a run that already used its generation is handed back to the user', async t => {
  const tmp = mkTmp();
  const runsDir = path.join(tmp, 'runs');
  fs.mkdirSync(runsDir, { recursive: true });
  // Pre-seed the ledger: this run IS the first followup, so it is at depth 1
  // and a second weak pass must not spawn a third generation.
  fs.writeFileSync(path.join(runsDir, '.followup.jsonl'),
    JSON.stringify({ event: 'linked', parent: '20251231-000000', child: '20260101-000000', depth: 1 }) + '\n');
  const dir = writeRun(runsDir, '20260101-000000', {
    status: 'paused', run_outcome: 'partial', alivePid: process.pid,
  });

  const server = await boot(tmp);
  t.after(() => { server.kill(); });

  const skipped = await until(
    () => ledger(runsDir).find(r => r.event === 'skipped'), 15000, 'skip record',
  );
  assert.equal(skipped.parent, '20260101-000000');
  assert.equal(skipped.depth, 2);
  // Left paused for the human, exactly as before this feature existed.
  assert.equal(fs.existsSync(path.join(dir, 'web_cmd.txt')), false);
});
