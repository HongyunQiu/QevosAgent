/**
 * qevos bridge — the SDK exposed inside every UI App panel (runtime: web).
 *
 * Self-configures from window.__QEVOS__ (injected by the server before this loads).
 * All I/O is scoped to the app's project dir (app-data/<id>/) via same-origin fetch.
 *
 * Surface:
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
  var APP  = CFG.app || '';
  var FBASE = '/api/app-file/'  + encodeURIComponent(APP) + '/';
  var LBASE = '/api/app-files/' + encodeURIComponent(APP);

  function enc(p) { return String(p).split('/').map(encodeURIComponent).join('/'); }

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
      es = new EventSource('/api/app-stream/' + encodeURIComponent(APP));
      es.onmessage = function (e) {
        var msg;
        try { msg = JSON.parse(e.data); } catch (_) { msg = { raw: e.data }; }
        pushCbs.slice().forEach(function (cb) { try { cb(msg); } catch (_) {} });
      };
    } catch (_) { /* SSE unavailable — onPush stays inert */ }
  }

  window.qevos = {
    app: APP,

    readFile: async function (rel) {
      var j = await req('GET', FBASE + enc(rel));
      return j.content;
    },
    writeFile: async function (rel, content) {
      return req('POST', FBASE + enc(rel), { content: String(content) });
    },
    exists: async function (rel) {
      var j = await req('GET', FBASE + enc(rel));
      return j.content !== null && j.exists !== false;
    },
    remove: async function (rel) {
      return req('DELETE', FBASE + enc(rel));
    },
    list: async function (dir) {
      var j = await req('GET', LBASE + (dir ? ('?dir=' + encodeURIComponent(dir)) : ''));
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
      return req('POST', '/api/panel-event', { app: APP, event: event, data: data || {} });
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
