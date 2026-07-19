/**
 * qevos bridge — the SDK exposed inside every UI App panel (runtime: web).
 *
 * Self-configures from window.__QEVOS__ (injected by the server before this loads):
 *   { app: "<id>", root?: "<abs project folder>" }
 * All I/O is scoped to the project's base dir: `root` (a folder anywhere on disk)
 * if given, else app-data/<id>/. Same-origin fetch; `root` is threaded on every call.
 *
 * Surface:
 *   qevos.app / qevos.root
 *   qevos.readFile(rel)            -> string | null      (utf8 文本)
 *   qevos.writeFile(rel, content)  -> {ok:true}
 *   qevos.readBinary(rel)          -> ArrayBuffer | null (二进制：STL/图片等)
 *   qevos.writeBinary(rel, base64) -> {ok:true}          (base64 → 原始字节落盘)
 *   qevos.readJSON(rel)            -> object | null      (parsed)
 *   qevos.writeJSON(rel, obj)      -> {ok:true}          (pretty-printed)
 *   qevos.exists(rel)              -> boolean
 *   qevos.remove(rel)              -> {ok:true}
 *   qevos.list(dir?)              -> [{path,type,size}]  (recursive under dir)
 *   qevos.emit(event, data)        -> {ok:true}          (惰性事件日志)
 *   qevos.onPush(cb)               -> unsubscribe()       (server→panel, SSE)
 *   qevos.theme                    -> 'dark' | 'light'    (跟随 dashboard 主题)
 *   qevos.onTheme(cb)              -> unsubscribe()       (主题切换回调 cb(theme))
 *
 * See SKILLS/ui_app.md for the authoring contract.
 */
(function () {
  var CFG  = window.__QEVOS__ || {};
  var APP  = CFG.app  || '';
  var ROOT = CFG.root || '';

  // ── theme：面板跟随 dashboard 的 light/dark ──
  // dashboard 把主题存在 localStorage('dashTheme')；面板 iframe 与其同源 → 直接读，
  // 且切换时 storage 事件会广播到所有其它同源浏览上下文（含本 iframe）→ 实时跟随。
  // 桥把 data-theme 写在面板自己的 <html> 上，并注入 /qevos-theme.css（--q-* 变量表，
  // 先于 App 样式，App 可覆盖）。纯 CSS 的 App 零 JS 即自动换肤；canvas/WebGL 类
  // 用 qevos.theme / qevos.onTheme(cb) 重绘。
  var themeCbs = [];
  function currentTheme() {
    try { return localStorage.getItem('dashTheme') === 'light' ? 'light' : 'dark'; }
    catch (_) { return 'dark'; }
  }
  function applyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    if (window.qevos) window.qevos.theme = t;
    themeCbs.slice().forEach(function (cb) { try { cb(t); } catch (_) {} });
  }
  (function initTheme() {
    var link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/qevos-theme.css';
    (document.head || document.documentElement).appendChild(link);
    applyTheme(currentTheme());
    window.addEventListener('storage', function (e) {
      if (e.key === 'dashTheme') applyTheme(currentTheme());
    });
  })();
  var FBASE = '/api/app-file/'  + encodeURIComponent(APP) + '/';
  var LBASE = '/api/app-files/' + encodeURIComponent(APP);

  function enc(p) { return String(p).split('/').map(encodeURIComponent).join('/'); }

  // Build a query string, always folding in `root` when set.
  function qs(params) {
    var p = {};
    if (params) for (var k in params) if (params[k] != null) p[k] = params[k];
    if (ROOT) p.root = ROOT;
    var keys = Object.keys(p);
    if (!keys.length) return '';
    return '?' + keys.map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(p[k]); }).join('&');
  }

  async function req(method, url, body) {
    var opt = { method: method };
    if (body !== undefined) {
      opt.headers = { 'Content-Type': 'application/json' };
      opt.body = JSON.stringify(body);
    }
    var r = await fetch(url, opt);
    var j = null;
    try { j = await r.json(); } catch (e) { /* non-JSON */ }
    if (!r.ok) throw new Error((j && j.error) || ('HTTP ' + r.status));
    return j || {};
  }

  // ── server → panel push (SSE) ──
  var pushCbs = [];
  var es = null;
  function ensureStream() {
    if (es || !window.EventSource) return;
    try {
      es = new EventSource('/api/app-stream/' + encodeURIComponent(APP) + qs());
      es.onmessage = function (e) {
        var msg;
        try { msg = JSON.parse(e.data); } catch (_) { msg = { raw: e.data }; }
        if (msg && msg.type === '__ctl') { handleCtl(msg); return; }   // control, not an app push
        pushCbs.slice().forEach(function (cb) { try { cb(msg); } catch (_) {} });
      };
    } catch (_) { /* SSE unavailable — onPush stays inert */ }
  }

  // ── Agent → panel control (first-party automation; no CDP) ──
  // The server pushes {type:'__ctl', id, action, args}; we run it against the real
  // DOM and POST the result back, correlated by id. Works in Electron & browser alike.
  function q(sel) { var el = document.querySelector(sel); if (!el) throw new Error('元素未找到: ' + sel); return el; }
  function waitFor(sel, ms) {
    return new Promise(function (res, rej) {
      var t0 = Date.now();
      (function chk() {
        if (document.querySelector(sel)) return res(true);
        if (Date.now() - t0 > (ms || 4000)) return rej(new Error('等待超时: ' + sel));
        setTimeout(chk, 80);
      })();
    });
  }
  // 面板自截图（DOM→图；非抓屏——浏览器禁止页面像素级截自己）。返回 {image: base64, mime}。
  function ensureH2C() {
    if (window.html2canvas) return Promise.resolve();
    return new Promise(function (res, rej) {
      var s = document.createElement('script');
      s.src = '/vendor/html2canvas.min.js';
      s.onload = res; s.onerror = function () { rej(new Error('加载 html2canvas 失败')); };
      document.head.appendChild(s);
    });
  }
  async function screenshot(a) {
    var target = (a && a.selector) ? q(a.selector) : document.body;
    // canvas 应用捷径：像素级完美、无需 html2canvas
    if (target.tagName === 'CANVAS') {
      return { image: target.toDataURL('image/png').split(',')[1], mime: 'image/png' };
    }
    await ensureH2C();
    var bg = getComputedStyle(document.body).backgroundColor;
    var canvas = await window.html2canvas(target, {
      backgroundColor: (bg && bg !== 'rgba(0, 0, 0, 0)') ? bg : (currentTheme() === 'light' ? '#ffffff' : '#0d1117'),
      scale: Math.min(window.devicePixelRatio || 1, 2), useCORS: true, logging: false,
    });
    var url = canvas.toDataURL('image/png');   // 跨域资源会 SecurityError（被 handleCtl 捕获回报）
    return { image: url.split(',')[1], mime: 'image/png' };
  }
  function runCtl(action, a) {
    switch (action) {
      case 'click':   { q(a.selector).click(); return true; }
      case 'fill':    { var el = q(a.selector); el.focus(); el.value = a.value == null ? '' : String(a.value);
                        el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); return true; }
      case 'value':   return q(a.selector).value;
      case 'getText': return q(a.selector).textContent;
      case 'getHtml': return (a.selector ? q(a.selector) : document.documentElement).outerHTML;
      case 'exists':  return !!document.querySelector(a.selector);
      case 'count':   return document.querySelectorAll(a.selector).length;
      case 'waitFor': return waitFor(a.selector, a.timeout);
      case 'screenshot': return screenshot(a);
      case 'eval':    return (0, eval)(a.code);   // expression; returns JSON-serializable value
      default: throw new Error('未知控制动作: ' + action);
    }
  }
  async function handleCtl(msg) {
    var out = { id: msg.id, ok: true, result: null };
    try { out.result = await runCtl(msg.action, msg.args || {}); }
    catch (e) { out.ok = false; out.error = String((e && e.message) || e); }
    try { JSON.stringify(out.result); } catch (_) { out.result = String(out.result); }  // keep serializable
    try {
      await fetch('/api/panel-control-result', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: out.id, ok: out.ok, result: out.result, error: out.error }),
      });
    } catch (_) { /* server gone */ }
  }

  window.qevos = {
    app: APP,
    root: ROOT,
    theme: document.documentElement.getAttribute('data-theme') || 'dark',

    onTheme: function (cb) {
      themeCbs.push(cb);
      return function () {
        var i = themeCbs.indexOf(cb);
        if (i >= 0) themeCbs.splice(i, 1);
      };
    },

    readFile: async function (rel) {
      var j = await req('GET', FBASE + enc(rel) + qs());
      return j.content;
    },
    writeFile: async function (rel, content) {
      return req('POST', FBASE + enc(rel) + qs(), { content: String(content) });
    },
    readBinary: async function (rel) {
      var r = await fetch(FBASE + enc(rel) + qs({ raw: 1 }));
      if (r.status === 404) return null;
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.arrayBuffer();
    },
    writeBinary: async function (rel, base64) {
      return req('POST', FBASE + enc(rel) + qs(), { content_b64: String(base64) });
    },
    exists: async function (rel) {
      var j = await req('GET', FBASE + enc(rel) + qs());
      return j.content !== null && j.exists !== false;
    },
    remove: async function (rel) {
      return req('DELETE', FBASE + enc(rel) + qs());
    },
    list: async function (dir) {
      var j = await req('GET', LBASE + qs(dir ? { dir: dir } : null));
      return j.files || [];
    },
    readJSON: async function (rel) {
      var c = await this.readFile(rel);
      return c == null ? null : JSON.parse(c);
    },
    writeJSON: async function (rel, obj) {
      return this.writeFile(rel, JSON.stringify(obj, null, 2));
    },

    emit: async function (event, data) {
      return req('POST', '/api/panel-event', { app: APP, event: event, data: data || {}, root: ROOT || undefined });
    },

    onPush: function (cb) {
      pushCbs.push(cb);
      ensureStream();
      return function () {
        var i = pushCbs.indexOf(cb);
        if (i >= 0) pushCbs.splice(i, 1);
      };
    },
  };

  // Open the SSE stream at init so the panel is always reachable for Agent control
  // (and file-changed pushes), even if the app never registers an onPush callback.
  ensureStream();
})();
