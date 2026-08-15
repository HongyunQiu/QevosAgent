package com.qevos.agent

import android.graphics.Bitmap
import android.graphics.Canvas
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Base64
import android.util.Log
import android.view.InputDevice
import android.view.KeyEvent
import android.view.MotionEvent
import android.webkit.WebView
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import org.json.JSONTokener
import java.io.ByteArrayOutputStream
import java.util.concurrent.TimeUnit

/**
 * Makes this phone the executor for QevosAgent's `web_interact` tool.
 *
 * Design notes — why it looks like this:
 *
 *  • **No listening port.** Commands ride the dashboard's existing WebSocket
 *    (`ws://host:port/?role=browser-agent`). The phone dials out, so this works
 *    through ZeroTier / NAT / mobile data with no forwarding, and inherits the
 *    server's existing isIpAllowed() upgrade gate. The `role` query param also
 *    opts this socket out of the dashboard state firehose — it would otherwise
 *    receive the whole events array on every poll tick.
 *
 *  • **One executor at a time.** The server keeps a single executor slot; a
 *    second device that opts in displaces this one and we get a `revoked`
 *    message. Broadcasting actions to every phone would run each command N
 *    times.
 *
 *  • **Two coordinate spaces, converted explicitly.** This is the single
 *    easiest place to introduce a silent bug, so both hops are named:
 *
 *      agent coords (screenshot pixels) ──× 1/shotScale──▶ view pixels
 *          view pixels ──× cssScale──▶ CSS pixels
 *
 *    Touch injection (`dispatchTouchEvent`) wants VIEW pixels. Anything that
 *    reaches JS — the cursor overlay, `elementFromPoint` — wants CSS pixels,
 *    which differ by device pixel ratio (2.5–3.5× on a modern phone). Passing
 *    view pixels to the overlay would put the marker a third of the way up the
 *    screen from where the tap actually landed.
 *
 *    `deltaX`/`deltaY` on `scroll` are the exception: they stay CSS pixels, to
 *    match what the CDP and Electron paths already mean by them.
 *
 *  • **No foreground service.** A backgrounded Activity can't be screenshotted
 *    or touched reliably no matter what happens to the socket, so keeping the
 *    connection alive past onStop would only buy a live socket attached to a
 *    dead executor. The channel's lifetime is deliberately the Activity's.
 */
class BrowserAgent(
    private val webView: WebView,
    /** Called on the UI thread whenever the connection state changes. */
    private val onState: (State) -> Unit,
    /** Asked to make the browsing WebView visible (new_tab / navigate). */
    private val onNeedShow: () -> Unit,
) {

    enum class State { OFF, CONNECTING, ACTIVE, REVOKED, ERROR }

    companion object {
        private const val TAG = "QevosBrowserAgent"
        /** Longest side of a returned screenshot. Full 1080×2400 PNGs are a
         *  needless vision-token bill; coordinates are remapped to match. */
        private const val MAX_SHOT_PX = 1600
        /** Below the server's 15 s action timeout, which is below the 20 s
         *  HTTP timeout in tool_web_interact. Each layer must give up first. */
        private const val NAV_TIMEOUT_MS = 12000L
        private const val RECONNECT_BASE_MS = 1500L
        private const val RECONNECT_MAX_MS = 20000L
    }

    private val ui = Handler(Looper.getMainLooper())
    private val client = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)   // keep NAT/ZeroTier mappings warm
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    private var ws: WebSocket? = null
    private var enabled = false
    private var host = ""
    private var port = ""
    private var retries = 0

    var state: State = State.OFF
        private set

    /** Scale applied to the last screenshot (screenshot px = view px × this). */
    private var shotScale = 1.0
    /** CSS pixels per view pixel, re-read on each page load. 0 = unknown. */
    private var cssScale = 0.0

    /** Set while a navigate/new_tab is waiting for onPageFinished. */
    private var pendingNav: (() -> Unit)? = null
    private var navTimeout: Runnable? = null

    private val deviceName: String =
        (Build.MANUFACTURER + " " + Build.MODEL).trim().ifBlank { "Android" }
    private val deviceId: String = Build.FINGERPRINT.hashCode().toUInt().toString(16)

    // ── Public API ──────────────────────────────────────────────────────────

    fun isEnabled(): Boolean = enabled

    fun enable(host: String, port: String) {
        this.host = host
        this.port = port
        if (enabled) return
        enabled = true
        retries = 0
        connect()
    }

    fun disable() {
        if (!enabled) return
        enabled = false
        try { ws?.send(JSONObject().put("type", "browser-agent/unregister").toString()) } catch (_: Exception) {}
        try { ws?.close(1000, "user disabled") } catch (_: Exception) {}
        ws = null
        setState(State.OFF)
    }

    fun destroy() {
        enabled = false
        try { ws?.cancel() } catch (_: Exception) {}
        ws = null
        clearNavWait()
    }

    /** Wired to the browsing WebView's WebViewClient. */
    fun onPageFinished() {
        cssScale = 0.0            // layout may have changed → re-measure lazily
        val done = pendingNav ?: return
        clearNavWait()
        done()
    }

    // ── Connection ──────────────────────────────────────────────────────────

    private fun connect() {
        if (!enabled) return
        setState(State.CONNECTING)
        val url = "ws://$host:$port/?role=browser-agent"
        val req = Request.Builder().url(url).build()
        ws = client.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                retries = 0
                ui.post {
                    // Report the browsing view's real size so the desktop side
                    // can show what it's driving.
                    webSocket.send(JSONObject().apply {
                        put("type", "browser-agent/register")
                        put("deviceId", deviceId)
                        put("name", deviceName)
                        put("w", webView.width)
                        put("h", webView.height)
                    }.toString())
                    setState(State.ACTIVE)
                }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val msg = try { JSONObject(text) } catch (_: Exception) { return }
                when (msg.optString("type")) {
                    "browser-agent/action" -> handleAction(
                        msg.optString("reqId"),
                        msg.optString("action"),
                        msg.optJSONObject("payload") ?: JSONObject()
                    )
                    "browser-agent/revoked" -> ui.post {
                        Log.w(TAG, "executor slot revoked: ${msg.optString("reason")}")
                        enabled = false
                        setState(State.REVOKED)
                    }
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "ws failure: ${t.javaClass.simpleName} ${t.message}")
                ui.post { setState(State.ERROR); scheduleReconnect() }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                ui.post { if (enabled) { setState(State.ERROR); scheduleReconnect() } }
            }
        })
    }

    private fun scheduleReconnect() {
        if (!enabled) return
        retries++
        val delay = (RECONNECT_BASE_MS * retries).coerceAtMost(RECONNECT_MAX_MS)
        ui.postDelayed({ if (enabled) connect() }, delay)
    }

    private fun setState(s: State) {
        state = s
        onState(s)
    }

    // ── Action dispatch ─────────────────────────────────────────────────────

    private fun sendResult(reqId: String, result: JSONObject?, error: String?) {
        val msg = JSONObject().apply {
            put("type", "browser-agent/result")
            put("reqId", reqId)
            if (error != null) put("error", error) else put("result", result ?: JSONObject().put("ok", true))
        }
        try { ws?.send(msg.toString()) } catch (e: Exception) { Log.w(TAG, "send result failed: ${e.message}") }
    }

    private fun handleAction(reqId: String, action: String, payload: JSONObject) {
        ui.post {
            try {
                execute(action, payload) { result, error -> sendResult(reqId, result, error) }
            } catch (e: Exception) {
                Log.w(TAG, "action $action failed", e)
                sendResult(reqId, null, "${e.javaClass.simpleName}: ${e.message}")
            }
        }
    }

    private fun ok(vararg pairs: Pair<String, Any?>): JSONObject =
        JSONObject().apply {
            put("ok", true)
            for ((k, v) in pairs) if (v != null) put(k, v)
        }

    /** Runs on the UI thread. `done(result, error)` — exactly one is non-null. */
    private fun execute(
        action: String,
        p: JSONObject,
        done: (JSONObject?, String?) -> Unit,
    ) {
        if (webView.width == 0 || webView.height == 0) {
            done(null, "浏览 WebView 尚未完成布局（宽高为 0），无法执行操作")
            return
        }

        when (action) {
            "new_tab", "navigate" -> {
                val url = p.optString("url").ifBlank { "about:blank" }
                onNeedShow()
                waitForLoad { timedOut ->
                    done(ok("url" to url, "note" to if (timedOut) "加载超时，返回当前状态" else null), null)
                }
                webView.loadUrl(url)
            }

            "eval" -> js(p.optString("code")) { v -> done(ok("result" to v), null) }

            "get_html" -> js("document.documentElement.outerHTML") { v ->
                val html = v as? String ?: ""
                done(ok(
                    "html" to html,
                    // evaluateJavascript hands the result back across a Binder
                    // IPC; very large pages are where this path breaks first,
                    // so say the size out loud rather than return a mystery.
                    "note" to if (html.length > 1_000_000) "HTML ${html.length} 字符，接近 WebView IPC 上限，可能被截断" else null
                ), null)
            }

            "screenshot" -> done(captureScreenshot(), null)

            "click" -> js(
                "document.querySelector(${jsStr(p.optString("selector"))})?.click()"
            ) { done(ok(), null) }

            "fill" -> {
                val sel = jsStr(p.optString("selector"))
                val value = jsStr(p.optString("value"))
                js(
                    "(el => { if (el) { el.focus(); el.value = $value; " +
                    "el.dispatchEvent(new Event('input', {bubbles:true})); " +
                    "el.dispatchEvent(new Event('change', {bubbles:true})); return true } return false })" +
                    "(document.querySelector($sel))"
                ) { v -> done(ok("found" to (v == true)), null) }
            }

            // Touch input has no "move without pressing", so there is no hover
            // to dispatch. We still place the marker so the agent can verify
            // its coordinate mapping — but we say plainly that nothing was
            // dispatched, because reporting a bare ok() here would let the
            // model believe a :hover menu had opened.
            "mouse_move" -> {
                val (vx, vy) = viewXY(p)
                overlay(vx, vy) { code ->
                    done(ok(
                        "cursor" to cursorObj(code, p),
                        "note" to "安卓触摸模型无 hover：仅绘制了坐标标记，未派发任何鼠标移入事件"
                    ), null)
                }
            }

            "mouse_click" -> {
                val (vx, vy) = viewXY(p)
                val count = p.optInt("count", 1).coerceIn(1, 2)
                tap(vx, vy, count)
                overlay(vx, vy) { code -> done(ok(
                    "cursor" to cursorObj(code, p),
                    "note" to if (p.optString("button", "left") != "left")
                        "安卓触摸无左右键之分，已按普通点击处理" else null
                ), null) }
            }

            "mouse_down" -> {
                val (vx, vy) = viewXY(p)
                touchDownTime = SystemClock.uptimeMillis()
                sendTouch(MotionEvent.ACTION_DOWN, vx, vy)
                overlay(vx, vy) { code -> done(ok("cursor" to cursorObj(code, p)), null) }
            }

            "mouse_up" -> {
                val (vx, vy) = viewXY(p)
                sendTouch(MotionEvent.ACTION_UP, vx, vy)
                overlay(vx, vy) { code -> done(ok("cursor" to cursorObj(code, p)), null) }
            }

            "drag" -> {
                val x1 = toViewPx(p.optDouble("x1", 0.0)); val y1 = toViewPx(p.optDouble("y1", 0.0))
                val x2 = toViewPx(p.optDouble("x2", 0.0)); val y2 = toViewPx(p.optDouble("y2", 0.0))
                val steps = p.optInt("steps", 10).coerceIn(2, 60)
                drag(x1, y1, x2, y2, steps)
                overlay(x2, y2) { code ->
                    done(ok("cursor" to JSONObject()
                        .put("code", code)
                        .put("x", p.optDouble("x2", 0.0))
                        .put("y", p.optDouble("y2", 0.0))), null)
                }
            }

            "scroll" -> scroll(p) { where -> done(ok("scrolled" to where), null) }

            // execCommand('insertText') is the closest analogue to Electron's
            // wc.insertText / CDP Input.insertText: it fires real beforeinput +
            // input events, so React/Vue controlled inputs actually update.
            // Per-character KeyEvents were the alternative and cannot express
            // CJK or emoji at all.
            "key_type" -> {
                val text = jsStr(p.optString("text"))
                js("(function(t){ var el=document.activeElement; if(!el) return 'no-focus';" +
                   " return document.execCommand('insertText', false, t) ? 'ok' : 'refused' })($text)"
                ) { v ->
                    when (v) {
                        "ok" -> done(ok(), null)
                        "no-focus" -> done(null, "没有获得焦点的元素，请先 mouse_click 或 eval 聚焦输入框")
                        else -> done(null, "execCommand('insertText') 被拒绝——该元素可能不可编辑")
                    }
                }
            }

            "key_press" -> {
                val kc = androidKeyCode(p.optString("key"))
                    ?: return done(null, "不支持的键名: ${p.optString("key")}")
                sendKey(kc, 0)
                done(ok(), null)
            }

            "key_combo" -> {
                val kc = androidKeyCode(p.optString("key"))
                    ?: return done(null, "不支持的键名: ${p.optString("key")}")
                var meta = 0
                val mods = p.optJSONArray("modifiers")
                for (i in 0 until (mods?.length() ?: 0)) {
                    when (mods!!.optString(i).lowercase()) {
                        "ctrl", "control" -> meta = meta or KeyEvent.META_CTRL_ON
                        "shift"           -> meta = meta or KeyEvent.META_SHIFT_ON
                        "alt"             -> meta = meta or KeyEvent.META_ALT_ON
                        "meta", "command" -> meta = meta or KeyEvent.META_META_ON
                    }
                }
                sendKey(kc, meta)
                done(ok(), null)
            }

            else -> done(null,
                "未知操作: $action。支持: new_tab / navigate / eval / get_html / screenshot / " +
                "click / fill / mouse_move / mouse_click / mouse_down / mouse_up / drag / " +
                "key_type / key_press / key_combo / scroll")
        }
    }

    // ── Coordinates ─────────────────────────────────────────────────────────

    /** agent (screenshot) px → view px. */
    private fun toViewPx(v: Double): Float =
        (v / (if (shotScale > 0) shotScale else 1.0)).toFloat()

    private fun viewXY(p: JSONObject): Pair<Float, Float> =
        Pair(toViewPx(p.optDouble("x", 0.0)), toViewPx(p.optDouble("y", 0.0)))

    /** Echo the agent's own coordinates back, so its frame of reference holds. */
    private fun cursorObj(code: String, p: JSONObject): JSONObject =
        JSONObject().put("code", code).put("x", p.optDouble("x", 0.0)).put("y", p.optDouble("y", 0.0))

    /**
     * Resolve CSS pixels per view pixel, then run [body]. window.innerWidth is
     * in CSS px and the view's width is in physical px, so their ratio is the
     * conversion — this also absorbs any page zoom, which getScale() would not.
     */
    private fun withCssScale(body: (Double) -> Unit) {
        if (cssScale > 0) { body(cssScale); return }
        webView.evaluateJavascript("window.innerWidth") { raw ->
            val inner = raw?.trim('"')?.toDoubleOrNull() ?: 0.0
            cssScale = if (inner > 0 && webView.width > 0) inner / webView.width else 1.0
            body(cssScale)
        }
    }

    // ── Primitives ──────────────────────────────────────────────────────────

    private var touchDownTime = 0L

    private fun sendTouch(action: Int, x: Float, y: Float) {
        val now = SystemClock.uptimeMillis()
        if (action == MotionEvent.ACTION_DOWN) touchDownTime = now
        val ev = MotionEvent.obtain(touchDownTime, now, action, x, y, 0)
        ev.source = InputDevice.SOURCE_TOUCHSCREEN
        webView.dispatchTouchEvent(ev)
        ev.recycle()
    }

    private fun tap(x: Float, y: Float, count: Int) {
        repeat(count) { i ->
            // A second tap only registers as a double-tap if it lands inside
            // the platform double-tap window; posting it immediately is well
            // within that, but the first pair must complete first.
            val delay = i * 60L
            ui.postDelayed({
                sendTouch(MotionEvent.ACTION_DOWN, x, y)
                ui.postDelayed({ sendTouch(MotionEvent.ACTION_UP, x, y) }, 30L)
            }, delay)
        }
    }

    private fun drag(x1: Float, y1: Float, x2: Float, y2: Float, steps: Int) {
        sendTouch(MotionEvent.ACTION_DOWN, x1, y1)
        for (i in 1..steps) {
            val t = i.toFloat() / steps
            sendTouch(MotionEvent.ACTION_MOVE, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
        }
        sendTouch(MotionEvent.ACTION_UP, x2, y2)
    }

    private fun sendKey(keyCode: Int, meta: Int) {
        val now = SystemClock.uptimeMillis()
        webView.dispatchKeyEvent(KeyEvent(now, now, KeyEvent.ACTION_DOWN, keyCode, 0, meta))
        webView.dispatchKeyEvent(KeyEvent(now, now, KeyEvent.ACTION_UP, keyCode, 0, meta))
    }

    private fun androidKeyCode(key: String): Int? {
        if (key.isBlank()) return null
        NAMED_KEYS[key]?.let { return it }
        // Single printable character (ctrl+a, shift+z …) → KEYCODE_A etc.
        val kc = KeyEvent.keyCodeFromString("KEYCODE_" + key.uppercase())
        return if (kc != KeyEvent.KEYCODE_UNKNOWN) kc else null
    }

    /**
     * Scroll the innermost scrollable element under (x, y), falling back to the
     * window. A synthesized touch drag would work too but carries fling inertia,
     * so the distance actually scrolled would not match the requested delta.
     *
     * deltaX/deltaY stay in CSS pixels — same meaning as the CDP and Electron
     * paths — while x/y are converted from the agent's screenshot space.
     */
    private fun scroll(p: JSONObject, done: (String) -> Unit) {
        val (vx, vy) = viewXY(p)
        val dx = p.optDouble("deltaX", 0.0)
        val dy = p.optDouble("deltaY", 0.0)
        withCssScale { s ->
            val cx = vx * s
            val cy = vy * s
            js(
                "(function(x,y,dx,dy){" +
                "  var el=document.elementFromPoint(x,y);" +
                "  while(el&&el!==document.body){" +
                "    var st=getComputedStyle(el);" +
                "    var oy=st.overflowY, ox=st.overflowX;" +
                "    if(((oy==='auto'||oy==='scroll')&&el.scrollHeight>el.clientHeight)||" +
                "       ((ox==='auto'||ox==='scroll')&&el.scrollWidth>el.clientWidth)){" +
                "      el.scrollBy(dx,dy); return 'element';}" +
                "    el=el.parentElement;}" +
                "  window.scrollBy(dx,dy); return 'window';" +
                "})($cx,$cy,$dx,$dy)"
            ) { v -> done(v as? String ?: "window") }
        }
    }

    /**
     * Draw the same orange marker the desktop paths use. On a phone there is no
     * real pointer, so this overlay is the ONLY way a human watching the screen
     * can see where the agent just acted — it earns its place more here than on
     * the desktop. Coordinates must be CSS px: the overlay is positioned with
     * `position:fixed; left:<n>px`.
     */
    private fun overlay(viewX: Float, viewY: Float, done: (String) -> Unit) {
        val code = Integer.toHexString((Math.random() * 0xFFFF).toInt()).uppercase().padStart(4, '0')
        withCssScale { s ->
            val cx = (viewX * s).toInt()
            val cy = (viewY * s).toInt()
            js(
                "(function(x,y,code){" +
                "var c=document.getElementById('__qc__');" +
                "if(!c){c=document.createElement('div');c.id='__qc__';" +
                "c.style.cssText='position:fixed;pointer-events:none;z-index:2147483647;display:flex;align-items:center;gap:4px;transform:translate(4px,-50%)';" +
                "var d=document.createElement('div');" +
                "d.style.cssText='width:14px;height:14px;border-radius:50%;background:rgba(255,90,0,0.9);border:2px solid #fff;box-shadow:0 0 0 1px rgba(0,0,0,0.4),0 2px 5px rgba(0,0,0,0.35);flex-shrink:0';" +
                "var l=document.createElement('div');l.id='__qc_lbl__';" +
                "l.style.cssText='background:rgba(0,0,0,0.72);color:#fff;font:bold 11px monospace;padding:1px 5px;border-radius:3px;white-space:nowrap';" +
                "c.appendChild(d);c.appendChild(l);document.documentElement.appendChild(c);}" +
                "c.style.left=x+'px';c.style.top=y+'px';c.dataset.code=code;" +
                "document.getElementById('__qc_lbl__').textContent='#'+code+' ('+x+','+y+')';" +
                "})($cx,$cy,${jsStr(code)})"
            ) { done(code) }
        }
    }

    /**
     * Capture the browsing WebView. There is no `capturePage()` equivalent on
     * Android; View.draw() into a software Canvas is the portable route, and it
     * requires the view to be laid out — which is why the view is INVISIBLE and
     * never GONE. Content living in a hardware layer (WebGL, <video>) can come
     * back blank through this path.
     */
    private fun captureScreenshot(): JSONObject {
        val w = webView.width
        val h = webView.height
        val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
        webView.draw(Canvas(bmp))

        val longest = maxOf(w, h)
        shotScale = if (longest > MAX_SHOT_PX) MAX_SHOT_PX.toDouble() / longest else 1.0
        val out = if (shotScale < 1.0) {
            Bitmap.createScaledBitmap(bmp, (w * shotScale).toInt(), (h * shotScale).toInt(), true)
        } else bmp

        val buf = ByteArrayOutputStream()
        out.compress(Bitmap.CompressFormat.PNG, 100, buf)
        if (out !== bmp) out.recycle()
        bmp.recycle()

        // `scale` is informational: coordinates in later actions are expected in
        // THIS image's pixel space and converted here, exactly like the desktop
        // paths, so the model never has to do the arithmetic itself.
        return ok(
            "data" to Base64.encodeToString(buf.toByteArray(), Base64.NO_WRAP),
            "scale" to shotScale,
            "viewW" to w,
            "viewH" to h,
        )
    }

    // ── Helpers ─────────────────────────────────────────────────────────────

    /** evaluateJavascript hands back a JSON literal; decode it to a Kotlin value. */
    private fun js(code: String, done: (Any?) -> Unit) {
        webView.evaluateJavascript(code) { raw ->
            val value: Any? = when {
                raw == null || raw == "null" -> null
                else -> try { JSONTokener(raw).nextValue() } catch (_: Exception) { raw }
            }
            done(value)
        }
    }

    private fun jsStr(s: String): String = JSONObject.quote(s)

    private fun waitForLoad(done: (timedOut: Boolean) -> Unit) {
        clearNavWait()
        pendingNav = { done(false) }
        val t = Runnable {
            pendingNav = null
            navTimeout = null
            done(true)
        }
        navTimeout = t
        ui.postDelayed(t, NAV_TIMEOUT_MS)
    }

    private fun clearNavWait() {
        navTimeout?.let { ui.removeCallbacks(it) }
        navTimeout = null
        pendingNav = null
    }
}

/** Key names shared with the desktop paths, mapped to Android key codes. */
private val NAMED_KEYS: Map<String, Int> = mapOf(
    "Enter" to KeyEvent.KEYCODE_ENTER,
    "Tab" to KeyEvent.KEYCODE_TAB,
    "Escape" to KeyEvent.KEYCODE_ESCAPE,
    "Backspace" to KeyEvent.KEYCODE_DEL,
    "Delete" to KeyEvent.KEYCODE_FORWARD_DEL,
    "ArrowUp" to KeyEvent.KEYCODE_DPAD_UP,
    "ArrowDown" to KeyEvent.KEYCODE_DPAD_DOWN,
    "ArrowLeft" to KeyEvent.KEYCODE_DPAD_LEFT,
    "ArrowRight" to KeyEvent.KEYCODE_DPAD_RIGHT,
    "Home" to KeyEvent.KEYCODE_MOVE_HOME,
    "End" to KeyEvent.KEYCODE_MOVE_END,
    "PageUp" to KeyEvent.KEYCODE_PAGE_UP,
    "PageDown" to KeyEvent.KEYCODE_PAGE_DOWN,
    "Space" to KeyEvent.KEYCODE_SPACE,
)
