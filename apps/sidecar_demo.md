---
name: Sidecar 示例
icon: 🛰️
description: UI App + 常驻后台 worker 的最小示例 —— RPC 调用、后台线程推进度、进程内状态、崩溃自愈
runtime: web
sidecar: worker.py
sidecar_linger: 120
enabled: true
---
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sidecar 示例</title>
<style>
  /* 颜色一律用 --q-* 变量：平台注入、随 dashboard 主题自动翻转，零 JS 换肤 */
  * { box-sizing: border-box; }
  body { margin: 0; padding: 22px; background: var(--q-bg); color: var(--q-text);
         font: 14.5px/1.7 var(--q-sans); }
  .wrap { max-width: 760px; margin: 0 auto; }
  h1 { color: var(--q-blue); font-size: 21px; margin: 0 0 6px; }
  .lead { background: var(--q-bg2); border: 1px solid var(--q-border); border-left: 3px solid var(--q-blue);
          border-radius: 8px; padding: 12px 15px; margin: 8px 0 20px; }
  .card { background: var(--q-bg2); border: 1px solid var(--q-border); border-radius: 10px;
          padding: 15px 17px; margin-bottom: 14px; }
  .card h2 { font-size: 14.5px; margin: 0 0 10px; color: var(--q-text); }
  .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  button { font: inherit; font-size: 13.5px; padding: 5px 13px; border-radius: 6px; cursor: pointer;
           background: var(--q-bg3); color: var(--q-text); border: 1px solid var(--q-border); }
  button:hover:not(:disabled) { background: var(--q-bg4); border-color: var(--q-blue); }
  button:disabled { opacity: .5; cursor: default; }
  button.danger:hover:not(:disabled) { border-color: var(--q-red); color: var(--q-red); }
  .muted { color: var(--q-muted); font-size: 13px; }
  code { font-family: var(--q-mono); font-size: 12.5px; color: var(--q-cyan);
         background: var(--q-bg); border: 1px solid var(--q-border); border-radius: 4px; padding: 1px 5px; }
  #bar { height: 6px; border-radius: 3px; background: var(--q-bg4); overflow: hidden; margin: 10px 0 4px; }
  #bar > i { display: block; height: 100%; width: 0; background: var(--q-green); transition: width .15s; }
  #log { font-family: var(--q-mono); font-size: 12.5px; background: var(--q-bg); border: 1px solid var(--q-border);
         border-radius: 8px; padding: 10px 12px; height: 190px; overflow: auto; white-space: pre-wrap;
         margin-top: 12px; color: var(--q-muted); }
  #log b { color: var(--q-text); font-weight: 600; }
  .ev { color: var(--q-purple); }
  .err { color: var(--q-red); }
  .ok  { color: var(--q-green); }
</style>
</head>
<body>
<div class="wrap">
  <h1>🛰️ Sidecar 示例</h1>

  <div class="lead">
    这张卡演示的是 <b>UI App + 常驻后台进程</b>：面板是前端，<code>apps-dist/sidecar_demo/worker.py</code>
    是一个由平台托管的 Python 进程 —— 首次调用时懒启动、空闲 120 秒回收、崩了自动重启。
    真实用途是那些「句柄必须一直活着」的东西（相机 SDK、串口、加载好的模型）。
  </div>

  <div class="card">
    <h2>① RPC：面板 → worker → 面板</h2>
    <div class="row">
      <button id="bPing">ping</button>
      <button id="bTick">tick（计数 +1）</button>
      <button id="bStatus">读进程状态</button>
    </div>
    <div class="muted" style="margin-top:8px">
      计数器只活在 worker 内存里。它能一路累加，就说明<b>是同一个进程</b>在服务你 ——
      这正是 sidecar 与「每次调用起一个脚本」的区别。
    </div>
  </div>

  <div class="card">
    <h2>② 长任务：后台线程跑，进度经事件推回来</h2>
    <div class="row">
      <button id="bWork">跑一个 5 步的任务</button>
      <button id="bRead" disabled>读取产出的文件</button>
    </div>
    <div id="bar"><i></i></div>
    <div class="muted">
      worker 立刻回 ack 就把活丢给线程（主循环一阻塞，所有调用都会排队到超时）。
      进度经 <code>emit()</code> 推给面板；结果文件落盘到本 App 的数据目录，<b>不走 RPC</b> —— 大件一律走文件通道。
    </div>
  </div>

  <div class="card">
    <h2>③ 生命周期：崩溃与回收</h2>
    <div class="row">
      <button id="bWho">$status（平台视角）</button>
      <button id="bStop">$stop（杀掉）</button>
      <button id="bBoom" class="danger">让它崩溃</button>
    </div>
    <div class="muted" style="margin-top:8px">
      <code>$</code> 前缀是平台保留方法，不进 worker。杀掉或崩溃后，下一次调用会拉起一个<b>新进程</b>
      （计数器归零、pid 变了）—— 改完 worker 代码就是这样热换的。
    </div>
  </div>

  <div id="log"></div>
</div>

<script>
  const logEl = document.getElementById('log');
  const bar   = document.querySelector('#bar > i');

  function log(text, cls) {
    const t = new Date().toLocaleTimeString();
    const span = document.createElement('div');
    if (cls) span.className = cls;
    span.textContent = `${t}  ${text}`;
    logEl.appendChild(span);
    logEl.scrollTop = logEl.scrollHeight;
  }

  // 统一包一层：sidecar 调用失败会抛 Error（worker 没起来、方法不存在、超时…）
  async function call(method, params, opts) {
    try {
      const r = await qevos.call(method, params || {}, opts);
      log(`→ ${method}  ⇒  ${JSON.stringify(r)}`, 'ok');
      return r;
    } catch (e) {
      log(`→ ${method}  ✗  ${e.message}`, 'err');
      throw e;
    }
  }

  // worker 的主动事件和崩溃通知都经 onPush 到达
  qevos.onPush(msg => {
    if (msg.type === 'sidecar-event') {
      if (msg.event === 'progress') {
        const { done, total } = msg.data;
        bar.style.width = (done / total * 100) + '%';
        log(`⇠ progress ${done}/${total}`, 'ev');
      } else if (msg.event === 'done') {
        log(`⇠ done：已写出 ${msg.data.file}`, 'ev');
        document.getElementById('bRead').disabled = !msg.data.file;
      } else {
        log(`⇠ ${msg.event} ${JSON.stringify(msg.data)}`, 'ev');
      }
    } else if (msg.type === 'sidecar-exit') {
      bar.style.width = '0';
      log(`✖ worker 退出（code ${msg.code}）—— 下次调用会自动拉起新进程`, 'err');
      if (msg.stderr) log(msg.stderr.trim(), 'err');
    }
  });

  document.getElementById('bPing').onclick   = () => call('ping');
  document.getElementById('bTick').onclick   = () => call('tick');
  document.getElementById('bStatus').onclick = () => call('status');
  document.getElementById('bBoom').onclick   = () => call('boom').catch(() => {});
  document.getElementById('bStop').onclick   = () => call('$stop');
  document.getElementById('bWho').onclick    = () => call('$status');

  document.getElementById('bWork').onclick = async () => {
    bar.style.width = '0';
    document.getElementById('bRead').disabled = true;
    await call('work', { steps: 5 }).catch(() => {});
  };

  document.getElementById('bRead').onclick = async () => {
    const text = await qevos.readFile('sidecar_report.txt');
    log('文件内容：\n' + (text || '(空)'));
  };

  log('面板就绪。点 ping 会懒启动 worker —— 第一次会慢一点点。');
</script>
</body>
</html>
