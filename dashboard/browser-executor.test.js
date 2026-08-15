/**
 * End-to-end check of the mobile browser-executor channel in server.js.
 *
 * Boots a real server, plays the part of the Android app over WebSocket, and
 * drives it through POST /api/browser-action — the same path web_interact uses.
 * Everything here is protocol-level, so it needs no phone and no Android SDK.
 *
 * Run:  node dashboard/browser-executor.test.js
 */
const { spawn } = require('child_process');
const http = require('http');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const WebSocket = require('ws');
const PORT = 8799;

let failures = 0;
function check(name, cond, extra) {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? '  — ' + extra : ''}`);
  if (!cond) failures++;
}

function post(pathname, body) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const req = http.request(
      { host: '127.0.0.1', port: PORT, path: pathname, method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) } },
      res => {
        let out = '';
        res.on('data', c => out += c);
        res.on('end', () => resolve({ status: res.statusCode, body: JSON.parse(out || '{}') }));
      });
    req.on('error', reject);
    req.end(data);
  });
}

function waitForServer() {
  return new Promise((resolve, reject) => {
    let tries = 0;
    const tick = () => {
      http.get({ host: '127.0.0.1', port: PORT, path: '/api/version' }, res => { res.resume(); resolve(); })
        .on('error', () => (++tries > 60 ? reject(new Error('server never came up')) : setTimeout(tick, 500)));
    };
    tick();
  });
}

const srv = spawn(process.execPath, [path.join(ROOT, 'dashboard/server.js')], {
  cwd: ROOT,
  env: { ...process.env, DASHBOARD_PORT: String(PORT), DASHBOARD_HOST: '127.0.0.1' },
  stdio: ['ignore', 'pipe', 'pipe'],
});
srv.stdout.on('data', () => {});
srv.stderr.on('data', d => process.stderr.write('[srv] ' + d));

(async () => {
  await waitForServer();

  // ── 1. No executor registered → falls through to the CDP path untouched ──
  const noAgent = await post('/api/browser-action', { action: 'screenshot' });
  check('no executor → existing CDP path still used (no regression)',
    noAgent.status === 500 && /CDP/.test(noAgent.body.error || ''),
    JSON.stringify(noAgent.body).slice(0, 90));

  // ── 2. Register a fake phone ─────────────────────────────────────────────
  const ws = new WebSocket(`ws://127.0.0.1:${PORT}/?role=browser-agent`);
  const seen = [];
  await new Promise(r => ws.once('open', r));
  ws.on('message', raw => {
    const m = JSON.parse(String(raw));
    seen.push(m.type);
    // mouse_move is answered by the dedicated error-path handler below.
    if (m.type === 'browser-agent/action' && m.action !== 'mouse_move') {
      // Echo a plausible result back, keyed by the reqId we were given.
      ws.send(JSON.stringify({
        type: 'browser-agent/result', reqId: m.reqId,
        result: { ok: true, data: 'FAKEPNG', scale: 0.66, echoAction: m.action, echoDisplay: m.displayId },
      }));
    }
  });
  ws.send(JSON.stringify({ type: 'browser-agent/register', deviceId: 'dev1', name: '测试手机', w: 1080, h: 2160 }));
  await new Promise(r => setTimeout(r, 400));
  check('register acknowledged', seen.includes('browser-agent/registered'), seen.join(','));
  check('executor socket is NOT in the state firehose', !seen.includes('state'), seen.join(','));

  // ── 3. Action round-trip ─────────────────────────────────────────────────
  const shot = await post('/api/browser-action', { action: 'screenshot', display_id: 'd7' });
  check('action routed to phone and result returned',
    shot.status === 200 && shot.body.data === 'FAKEPNG' && shot.body.echoAction === 'screenshot',
    JSON.stringify(shot.body).slice(0, 90));
  check('display_id forwarded', shot.body.echoDisplay === 'd7', String(shot.body.echoDisplay));

  // ── 4. Phone-reported error surfaces as a tool error ─────────────────────
  const errWs = new Promise(resolve => {
    const h = raw => {
      const m = JSON.parse(String(raw));
      if (m.type === 'browser-agent/action' && m.action === 'mouse_move') {
        ws.off('message', h);
        ws.send(JSON.stringify({ type: 'browser-agent/result', reqId: m.reqId, error: '安卓无 hover' }));
        resolve();
      }
    };
    ws.on('message', h);
  });
  const [errRes] = await Promise.all([post('/api/browser-action', { action: 'mouse_move' }), errWs]);
  check('phone error → 500 with its message',
    errRes.status === 500 && /hover/.test(errRes.body.error || ''),
    JSON.stringify(errRes.body).slice(0, 90));

  // ── 5. Second device displaces the first ─────────────────────────────────
  const ws2 = new WebSocket(`ws://127.0.0.1:${PORT}/?role=browser-agent`);
  await new Promise(r => ws2.once('open', r));
  const revoked = new Promise(r => ws.on('message', raw => {
    if (JSON.parse(String(raw)).type === 'browser-agent/revoked') r(true);
  }));
  ws2.send(JSON.stringify({ type: 'browser-agent/register', deviceId: 'dev2', name: '第二台' }));
  const gotRevoked = await Promise.race([revoked, new Promise(r => setTimeout(() => r(false), 1500))]);
  check('second device displaces the first (no N-way fan-out)', gotRevoked === true);

  // ── 6. Dropping the socket fails in-flight requests fast ─────────────────
  const pending = post('/api/browser-action', { action: 'get_html' });
  await new Promise(r => setTimeout(r, 200));
  ws2.terminate();
  const t0 = Date.now();
  const dropped = await pending;
  const dt = Date.now() - t0;
  check('in-flight request fails immediately on disconnect (not after 15s)',
    dropped.status === 500 && dt < 5000, `${dt}ms  ${JSON.stringify(dropped.body).slice(0, 70)}`);

  // ── 7. Same device reconnecting must NOT read as a takeover ──────────────
  // A foldable rebuilds its Activity on unfold, so the phone reconnects with a
  // fresh socket. Telling it "you were displaced" made it drop its own role
  // while the new socket kept the slot — the device revoked itself.
  const wsA = new WebSocket(`ws://127.0.0.1:${PORT}/?role=browser-agent`);
  await new Promise(r => wsA.once('open', r));
  let aRevoked = false;
  wsA.on('message', raw => {
    if (JSON.parse(String(raw)).type === 'browser-agent/revoked') aRevoked = true;
  });
  wsA.send(JSON.stringify({ type: 'browser-agent/register', deviceId: 'phone1', name: '手机' }));
  await new Promise(r => setTimeout(r, 300));

  const wsB = new WebSocket(`ws://127.0.0.1:${PORT}/?role=browser-agent`);
  await new Promise(r => wsB.once('open', r));
  let bRegistered = false;
  wsB.on('message', raw => {
    const m = JSON.parse(String(raw));
    if (m.type === 'browser-agent/registered') bRegistered = true;
    if (m.type === 'browser-agent/action') {
      wsB.send(JSON.stringify({ type: 'browser-agent/result', reqId: m.reqId, result: { ok: true, from: 'B' } }));
    }
  });
  wsB.send(JSON.stringify({ type: 'browser-agent/register', deviceId: 'phone1', name: '手机' }));
  await new Promise(r => setTimeout(r, 500));
  check('same-device reconnect is not reported as a takeover', !aRevoked);
  check('reconnected socket holds the slot', bRegistered);
  const viaB = await post('/api/browser-action', { action: 'screenshot' });
  check('actions go to the new socket', viaB.body.from === 'B', JSON.stringify(viaB.body).slice(0, 60));

  // ── 8. A frozen phone is dropped by the heartbeat, not left to stall ─────
  // pause() stops the client reading, so it never answers the server's ping
  // while TCP stays up — exactly what an OS-frozen app looks like from here.
  wsB._socket.pause();
  const frozenAt = Date.now();
  let slotFreed = false;
  for (let i = 0; i < 40; i++) {
    await new Promise(r => setTimeout(r, 1000));
    const probe = new WebSocket(`ws://127.0.0.1:${PORT}/`);
    const st = await new Promise(res => {
      probe.once('message', raw => { probe.close(); res(JSON.parse(String(raw))); });
      probe.once('error', () => res(null));
      setTimeout(() => { try { probe.close(); } catch {} ; res(null); }, 3000);
    });
    if (st && st.browserAgent === null) { slotFreed = true; break; }
  }
  const frozenDt = ((Date.now() - frozenAt) / 1000).toFixed(1);
  check('heartbeat drops a frozen executor', slotFreed, `after ${frozenDt}s`);
  check('…and fast enough to matter', slotFreed && (Date.now() - frozenAt) < 30000, `${frozenDt}s`);

  // ── 9. `via` names whoever actually ran the action ───────────────────────
  check('a FAILED action still names its executor', noAgent.body.via === 'cdp',
    String(noAgent.body.via));
  check('mobile results are labelled with the device', /^mobile:/.test(shot.body.via || ''),
    String(shot.body.via));

  const viaAfterLoss = await post('/api/browser-action', { action: 'screenshot' });
  check('after the executor is gone the reply says cdp, not mobile',
    viaAfterLoss.body.via === 'cdp' || /CDP/.test(viaAfterLoss.body.error || ''),
    JSON.stringify(viaAfterLoss.body).slice(0, 80));

  try { wsA.close(); } catch {}
  try { wsB.terminate(); } catch {}
  ws.close();
  srv.kill();
  await new Promise(r => setTimeout(r, 400));

  // ── 10. Strict mode refuses to fall back after an unexpected loss ────────
  const srv2 = spawn(process.execPath, [path.join(ROOT, 'dashboard/server.js')], {
    cwd: ROOT,
    env: { ...process.env, DASHBOARD_PORT: String(PORT + 1), DASHBOARD_HOST: '127.0.0.1',
           BROWSER_EXECUTOR_STRICT: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  srv2.stdout.on('data', () => {});
  srv2.stderr.on('data', d => process.stderr.write('[srv2] ' + d));
  const P2 = PORT + 1;
  const post2 = (p, b) => new Promise((resolve, reject) => {
    const data = JSON.stringify(b);
    const rq = http.request({ host: '127.0.0.1', port: P2, path: p, method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) } },
      res => { let o = ''; res.on('data', c => o += c);
               res.on('end', () => resolve({ status: res.statusCode, body: JSON.parse(o || '{}') })); });
    rq.on('error', reject); rq.end(data);
  });
  await new Promise(res => {
    const tick = () => http.get({ host: '127.0.0.1', port: P2, path: '/api/version' },
      r => { r.resume(); res(); }).on('error', () => setTimeout(tick, 400));
    tick();
  });

  // Before any phone has ever connected, strict mode must not block anything.
  const beforeAny = await post2('/api/browser-action', { action: 'screenshot' });
  check('strict: no block before any executor has connected',
    /CDP/.test(beforeAny.body.error || ''), JSON.stringify(beforeAny.body).slice(0, 70));

  const sWs = new WebSocket(`ws://127.0.0.1:${P2}/?role=browser-agent`);
  await new Promise(r => sWs.once('open', r));
  sWs.send(JSON.stringify({ type: 'browser-agent/register', deviceId: 'p9', name: '严格手机' }));
  await new Promise(r => setTimeout(r, 400));
  sWs.terminate();                                   // unexpected loss
  await new Promise(r => setTimeout(r, 600));
  const blocked = await post2('/api/browser-action', { action: 'screenshot' });
  check('strict: unexpected loss refuses to fall back',
    blocked.status === 503 && /严格手机/.test(blocked.body.error || ''),
    `${blocked.status} ${JSON.stringify(blocked.body).slice(0, 80)}`);

  // A deliberate opt-out is the user saying "go back to normal" — never a block.
  const sWs2 = new WebSocket(`ws://127.0.0.1:${P2}/?role=browser-agent`);
  await new Promise(r => sWs2.once('open', r));
  sWs2.send(JSON.stringify({ type: 'browser-agent/register', deviceId: 'p9', name: '严格手机' }));
  await new Promise(r => setTimeout(r, 400));
  sWs2.send(JSON.stringify({ type: 'browser-agent/unregister' }));
  await new Promise(r => setTimeout(r, 500));
  const afterOptOut = await post2('/api/browser-action', { action: 'screenshot' });
  check('strict: a deliberate opt-out still allows fallback',
    afterOptOut.status !== 503, `${afterOptOut.status} ${JSON.stringify(afterOptOut.body).slice(0, 70)}`);

  try { sWs2.close(); } catch {}
  srv2.kill();
  setTimeout(() => {
    console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURE(S)`);
    process.exit(failures === 0 ? 0 : 1);
  }, 300);
})().catch(e => { console.error('ERROR', e); srv.kill(); process.exit(1); });
