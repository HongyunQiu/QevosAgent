---
name: 内置 App 示例
icon: 📦
description: 点开看看 QevosAgent 的「内置 App」是什么、怎么来的（这是示例说明，不执行实际任务）
runtime: web
enabled: true
---
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>内置 App 示例</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px; background: #0d1117; color: #c9d1d9; font: 15px/1.7 -apple-system, 'Segoe UI', Roboto, sans-serif; }
  .wrap { max-width: 720px; margin: 0 auto; }
  h1 { color: #58a6ff; font-size: 22px; margin: 0 0 6px; }
  .lead { background: #1f6feb1a; border: 1px solid #1f6feb55; border-radius: 10px; padding: 14px 16px; margin: 8px 0 22px; }
  .lead b { color: #79c0ff; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 16px 18px; margin-bottom: 14px; }
  .card h2 { font-size: 15px; color: #c9d1d9; margin: 0 0 8px; }
  .tag { display: inline-block; font-size: 11px; font-weight: 700; border-radius: 20px; padding: 1px 9px; margin-left: 6px; vertical-align: 2px; }
  .tag.all { background: #3fb95022; color: #3fb950; }
  .tag.dev { background: #6e768122; color: #8b949e; }
  ul { margin: 6px 0 0 18px; } li { margin: 4px 0; }
  code { background: #0d1117; border: 1px solid #21262d; border-radius: 4px; padding: 1px 5px; font-family: Consolas, monospace; color: #79c0ff; font-size: 13px; }
  pre { background: #0d1117; border: 1px solid #21262d; border-radius: 8px; padding: 12px; overflow: auto; font-family: Consolas, monospace; font-size: 12.5px; color: #adbac7; margin: 8px 0 0; }
  .muted { color: #6e7681; font-size: 13px; }
  .status { margin-top: 6px; font-size: 13px; color: #3fb950; }
</style>
</head>
<body>
<div class="wrap">
  <h1>📦 内置 App 示例</h1>

  <div class="lead">
    你点开的是一张 <b>示例卡片</b>，用来说明 QevosAgent 里的「内置 App」是什么。
    它<b>本身不做任何实际工作</b> —— 只是给你看一眼 App 长什么样、从哪来。放心，它没坏。
  </div>

  <div class="card">
    <h2>什么是「内置 App」？<span class="tag all">给所有人</span></h2>
    <ul>
      <li>就是「Apps」页里的这些卡片。点一张，就在窗口里打开一个<b>小程序面板</b>（不用另开浏览器）。</li>
      <li>比如已有的 <b>🔀 流程图</b>（画流程图）、<b>🔍 Runs 搜索</b>（搜历史记录）—— 都是内置 App。</li>
      <li>它们把数据存成你电脑上的文件，能长期保存、能备份，不依赖联网。</li>
    </ul>
  </div>

  <div class="card">
    <h2>App 是怎么来的？<span class="tag all">给所有人</span></h2>
    <ul>
      <li><b>简单的</b>：一个人手写一个网页，就成了一张卡（比如本卡、流程图）。</li>
      <li><b>复杂的</b>：像开发一个正式网站那样做（用 React/Vue 等），做好后把成品放进来，就成了一张卡。</li>
      <li>两种在你这边用起来<b>一模一样</b>：点卡片 → 开面板。区别只在"怎么做出来的"。</li>
    </ul>
  </div>

  <div class="card">
    <h2>给开发者：复杂 App（前端工程）如何落地<span class="tag dev">技术细节</span></h2>
    <ul>
      <li>源码工程放 <code>app-src/&lt;id&gt;/</code>（独立 git 仓库，不入主库）。</li>
      <li><code>npm run build</code> 输出到 <code>apps-dist/&lt;id&gt;/</code>；面板发现其中的 <code>index.html</code> 就自动服务它。</li>
      <li>只 ship 构建产物，源码与 <code>node_modules</code> 不入库；运行期纯静态、零 npm。</li>
    </ul>
    <pre>// vite.config.js — 必须相对 base，资源才能正确加载
export default { base: './' }</pre>
    <div class="muted">详见 SKILLS/ui_app.md 与 doc/interactive-app.md §7.5。</div>
  </div>

  <div class="status" id="status">正在自检…</div>
</div>
<script>
  // 友好自检：证明这张卡确实能读写它自己的数据（不涉及你的任何文件）。
  (async () => {
    const el = document.getElementById('status');
    try {
      await qevos.writeFile('.qevos/hello.txt', 'hi @ ' + new Date().toLocaleString());
      const back = await qevos.readFile('.qevos/hello.txt');
      el.textContent = '✓ 一切正常：这张示例卡能正常读写自己的数据（“' + back + '”）。';
    } catch (e) {
      el.style.color = '#f85149';
      el.textContent = '✗ 运行异常：' + e.message;
    }
  })();
</script>
</body>
</html>
