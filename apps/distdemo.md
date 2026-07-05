---
name: 构建型示例
icon: 📦
description: 构建型 UI App 说明 —— 把前端工程的构建产物放 apps-dist/<id>/ 即可（此卡自包含）
runtime: web
enabled: true
---
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>构建型 UI App 示例</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px; background: #0d1117; color: #c9d1d9; font: 14px/1.6 -apple-system, 'Segoe UI', Roboto, sans-serif; }
  .wrap { max-width: 720px; margin: 0 auto; }
  h1 { color: #58a6ff; font-size: 20px; margin: 0 0 4px; }
  .sub { color: #6e7681; margin-bottom: 20px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 16px 18px; margin-bottom: 14px; }
  .card h2 { font-size: 14px; color: #f0c674; margin: 0 0 8px; }
  code { background: #0d1117; border: 1px solid #21262d; border-radius: 4px; padding: 1px 5px; font-family: Consolas, monospace; color: #79c0ff; }
  pre { background: #0d1117; border: 1px solid #21262d; border-radius: 8px; padding: 12px; overflow: auto; font-family: Consolas, monospace; font-size: 12.5px; color: #adbac7; }
  .ok { color: #3fb950; } .muted { color: #6e7681; }
  ul { margin: 6px 0 0 18px; } li { margin: 3px 0; }
</style>
</head>
<body>
<div class="wrap">
  <h1>📦 构建型 UI App</h1>
  <div class="sub">当 App 是一整个前端工程（React/Vue/Vite）时如何落地 —— 本卡自包含、可离线看。</div>

  <div class="card">
    <h2>核心：只 ship 构建产物，不 ship 源码/node_modules</h2>
    <ul>
      <li><b>源码工程</b>放 <code>app-src/&lt;id&gt;/</code>（或磁盘任意处），<b>独立 git 仓库</b>，不入主库。</li>
      <li><code>npm run build</code> 输出到 <code>apps-dist/&lt;id&gt;/</code>（含 <code>index.html</code>；此目录被 gitignore）。</li>
      <li>面板发现 <code>apps-dist/&lt;id&gt;/index.html</code> 就<b>自动服务它</b>并注入 <code>&lt;base&gt;</code> 与 <code>qevos</code> 桥；否则回退本卡内联正文。</li>
    </ul>
  </div>

  <div class="card">
    <h2>Vite 配置</h2>
    <pre>// vite.config.js — 必须相对 base，资源才解析到 /api/app/&lt;id&gt;/ 下
export default { base: './' }</pre>
    <div class="muted">运行期纯静态、零 npm；npm 只在构建期用（开发/授权机上）。</div>
  </div>

  <div class="card">
    <h2>桥自检（证明本面板里 qevos 可用）</h2>
    <pre id="probe">运行中…</pre>
  </div>
</div>
<script>
  // 一个最小自检：写一个标记文件再读回，证明 qevos 桥已注入且工作（与是否 dist 无关）。
  (async () => {
    const el = document.getElementById('probe');
    try {
      const stamp = 'built-type demo @ ' + new Date().toISOString();
      await qevos.writeFile('.qevos/probe.txt', stamp);
      const back = await qevos.readFile('.qevos/probe.txt');
      el.innerHTML = '<span class="ok">✓ qevos 桥可用</span>\n' +
        'app  = ' + qevos.app + '\n' +
        'root = ' + (qevos.root || '(默认 app-data/' + qevos.app + '/)') + '\n' +
        'writeFile/readFile 往返: ' + back;
    } catch (e) {
      el.textContent = '✗ 桥不可用: ' + e.message;
    }
  })();
</script>
</body>
</html>
