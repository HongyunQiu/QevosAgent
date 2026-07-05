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
 *   qevos.readFile(rel)            -> string | null
 *   qevos.writeFile(rel, content)  -> {ok:true}
 *   qevos.readJSON(rel)            -> object | null      (parsed)
 *   qevos.writeJSON(rel, obj)      -> {ok:true}          (pretty-printed)
 *   qevos.exists(rel)              -> boolean
 *   qevos.remove(rel)              -> {ok:true}
 *   qevos.list(dir?)              -> [{path,type,size}]  (recursive under dir)
 *   qevos.emit(event, data)        -> {ok:true}          (惰性事件日志)
 *   qevos.onPush(cb)               -> unsubscribe()       (server→panel, SSE)
 *
 * See SKILLS/ui_app.md for the authoring contract.
 */
(function () {
  var CFG  = window.__QEVOS__ || {};
  var APP  = CFG.app  || '';
  var ROOT = CFG.root || '';
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
        pushCbs.slice().forEach(function (cb) { try { cb(msg); } catch (_) {} });
      };
    } catch (_) { /* SSE unavailable — onPush stays inert */ }
  }

  window.qevos = {
    app: APP,
    root: ROOT,

    readFile: async function (rel) {
      var j = await req('GET', FBASE + enc(rel) + qs());
      return j.content;
    },
    writeFile: async function (rel, content) {
      return req('POST', FBASE + enc(rel) + qs(), { content: String(content) });
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
})();
