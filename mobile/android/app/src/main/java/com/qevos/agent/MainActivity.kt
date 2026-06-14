package com.qevos.agent

import android.content.Intent
import android.content.SharedPreferences
import android.graphics.Bitmap
import android.graphics.drawable.GradientDrawable
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.http.SslError
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.TypedValue
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.ViewGroup
import android.webkit.SslErrorHandler
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.qevos.agent.databinding.ActivityMainBinding

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: SharedPreferences
    private var settingsChanged = false

    private var connectivityManager: ConnectivityManager? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null
    // True while the WebView is showing the error overlay (main-frame load failed).
    // We only auto-reload on network-restore when we know the page is broken —
    // otherwise we'd nuke the in-page WebSocket / pending send-message fetch.
    private var inErrorState = false

    companion object {
        const val PREFS_NAME = "qevos_prefs"
        const val KEY_HOST = "host"
        const val KEY_PORT = "port"
        // Stable id of the currently-selected server row. host:port is NOT a
        // valid identity key (port-forwarding makes collisions legitimate),
        // so the menu picks the active row by this id.
        const val KEY_SERVER_ID = "server_id"
        const val DEFAULT_PORT = "8765"
        const val KEY_HANDLE_Y = "handle_y"
    }

    private val openSettings = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) {
        settingsChanged = true
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)

        setupWebView()
        setupEdgeHandle()

        binding.btnRetry.setOnClickListener { loadDashboard() }
        binding.btnSettings.setOnClickListener { openSettingsActivity() }

        registerNetworkCallback()

        loadDashboard()
    }

    override fun onResume() {
        super.onResume()
        if (settingsChanged) {
            settingsChanged = false
            loadDashboard()
            return
        }
        // If the page died while we were backgrounded (Wi-Fi switch, Doze, etc.)
        // and the error overlay is showing, try to recover automatically.
        // When the page is healthy, the in-page JS handles reconnect on its own.
        if (inErrorState) loadDashboard()
    }

    override fun onDestroy() {
        menuPollToken++          // stop any in-flight status-monitor poll loop
        unregisterNetworkCallback()
        super.onDestroy()
    }

    // ── Network-change recovery ─────────────────────────────────────────────
    // Only reloads when the WebView is already in the error state. A healthy
    // page has its own JS-layer reconnect (online / visibilitychange events),
    // and reloading would interrupt any in-flight send.
    private fun registerNetworkCallback() {
        val cm = getSystemService(CONNECTIVITY_SERVICE) as? ConnectivityManager ?: return
        connectivityManager = cm
        val req = NetworkRequest.Builder()
            .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .build()
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                runOnUiThread {
                    if (inErrorState) loadDashboard()
                }
            }
        }
        try {
            cm.registerNetworkCallback(req, cb)
            networkCallback = cb
        } catch (_: SecurityException) {
            // Some OEM ROMs reject this without ACCESS_NETWORK_STATE — fail quietly.
        }
    }

    private fun unregisterNetworkCallback() {
        val cb = networkCallback ?: return
        try { connectivityManager?.unregisterNetworkCallback(cb) } catch (_: Exception) {}
        networkCallback = null
    }

    private fun setupWebView() {
        binding.webView.apply {
            settings.apply {
                javaScriptEnabled = true
                domStorageEnabled = true
                mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
                setSupportZoom(true)
                builtInZoomControls = true
                displayZoomControls = false
                loadWithOverviewMode = true
                useWideViewPort = true
                mediaPlaybackRequiresUserGesture = false
                allowContentAccess = true
                allowFileAccess = false
                // Never serve a stale cached dashboard. Without this, killing
                // the server and reopening the app would show the last good
                // page from disk cache with no indication the agent is down.
                cacheMode = WebSettings.LOAD_NO_CACHE
            }

            webViewClient = object : WebViewClient() {
                override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
                    showError(false)
                    binding.progressBar.visibility = View.VISIBLE
                }

                override fun onPageFinished(view: WebView, url: String) {
                    binding.progressBar.visibility = View.GONE
                }

                override fun onReceivedError(
                    view: WebView,
                    request: WebResourceRequest,
                    error: WebResourceError
                ) {
                    if (request.isForMainFrame) {
                        binding.progressBar.visibility = View.GONE
                        // Replace Chromium's default ERR_* page with a blank
                        // canvas so our in-app overlay is what the user sees,
                        // not the system error page peeking through.
                        view.stopLoading()
                        view.loadUrl("about:blank")
                        showError(true)
                    }
                }

                override fun onReceivedSslError(
                    view: WebView,
                    handler: SslErrorHandler,
                    error: SslError
                ) {
                    handler.proceed()
                }
            }

            webChromeClient = object : WebChromeClient() {
                override fun onProgressChanged(view: WebView, newProgress: Int) {
                    binding.progressBar.progress = newProgress
                    if (newProgress >= 100) {
                        binding.progressBar.visibility = View.GONE
                    }
                }
            }
        }
    }

    // ── Right-edge floating handle ──────────────────────────────────────────
    private fun setupEdgeHandle() {
        val handle = binding.edgeHandle

        // Restore saved vertical position (default: vertically centered).
        handle.post {
            val parentH = binding.root.height
            val hH = handle.height
            val def = ((parentH - hH) / 2).coerceAtLeast(0)
            val y = prefs.getInt(KEY_HANDLE_Y, def).coerceIn(0, (parentH - hH).coerceAtLeast(0))
            setHandleTop(handle, y)
        }

        val slop = ViewConfiguration.get(this).scaledTouchSlop
        var downRawY = 0f
        var startTop = 0
        var dragged = false

        handle.setOnTouchListener { v, e ->
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    downRawY = e.rawY
                    startTop = (v.layoutParams as FrameLayout.LayoutParams).topMargin
                    dragged = false
                    v.alpha = 0.85f
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    val dy = e.rawY - downRawY
                    if (!dragged && kotlin.math.abs(dy) > slop) dragged = true
                    if (dragged) {
                        val parentH = binding.root.height
                        val hH = v.height
                        val newTop = (startTop + dy).toInt()
                            .coerceIn(0, (parentH - hH).coerceAtLeast(0))
                        setHandleTop(v, newTop)
                    }
                    true
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    v.alpha = 0.4f
                    if (dragged) {
                        val top = (v.layoutParams as FrameLayout.LayoutParams).topMargin
                        prefs.edit().putInt(KEY_HANDLE_Y, top).apply()
                    } else {
                        v.performClick()
                    }
                    true
                }
                else -> false
            }
        }
        handle.setOnClickListener { showActionMenu() }
    }

    private fun setHandleTop(v: View, top: Int) {
        val lp = v.layoutParams as FrameLayout.LayoutParams
        lp.topMargin = top
        lp.gravity = Gravity.END or Gravity.TOP
        v.layoutParams = lp
    }

    // ── Handle action menu: server switch + refresh + settings ──────────────
    private fun showActionMenu() {
        val servers = Servers.load(prefs)
        // Identify the active row by stable id. host:port match would falsely
        // light up multiple rows when the user intentionally points different
        // entries at the same forwarded endpoint.
        var curId = prefs.getString(KEY_SERVER_ID, "") ?: ""
        // Migration for users upgrading from id-less prefs: pick the FIRST row
        // whose host:port matches what's persisted, then pin its id so future
        // opens are unambiguous even if the user later adds a duplicate.
        if (curId.isBlank()) {
            val curHost = prefs.getString(KEY_HOST, "") ?: ""
            val curPort = prefs.getString(KEY_PORT, DEFAULT_PORT) ?: DEFAULT_PORT
            val match = servers.firstOrNull { it.host == curHost && it.port == curPort }
            if (match != null) {
                curId = match.id
                prefs.edit().putString(KEY_SERVER_ID, curId).apply()
            }
        }

        val container = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(6), dp(6), dp(6), dp(6))
        }
        val dialog = AlertDialog.Builder(this)
            .setTitle("QevosAgent")
            .setView(ScrollView(this).apply { addView(container) })
            .create()

        // Saved servers — tap to switch. Each row carries a status dot.
        val rowRefs = mutableListOf<Triple<Server, TextView, View>>()
        for (s in servers) {
            val isCurrent = s.id == curId
            val (row, tv, dot) = makeServerRow(
                (if (isCurrent) "✓  " else "      ") + s.label()
            ) {
                prefs.edit()
                    .putString(KEY_HOST, s.host)
                    .putString(KEY_PORT, s.port)
                    .putString(KEY_SERVER_ID, s.id)
                    .apply()
                dialog.dismiss()
                loadDashboard()
            }
            container.addView(row)
            rowRefs.add(Triple(s, tv, dot))
        }

        if (servers.isNotEmpty()) {
            container.addView(View(this).apply {
                layoutParams = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, dp(1)
                ).apply { topMargin = dp(4); bottomMargin = dp(4) }
                setBackgroundColor(0xFFDDDDDD.toInt())
            })
        }

        container.addView(makeMenuItem("↻  刷新") {
            dialog.dismiss()
            binding.webView.reload()
        })
        container.addView(makeMenuItem("⚙  服务器设置") {
            dialog.dismiss()
            openSettingsActivity()
        })

        // The menu doubles as a live status monitor: keep polling every server
        // while it's open so transient states (asking-user, task start/finish)
        // show up without reopening. Polling stops the moment the dialog closes.
        val pollToken = ++menuPollToken
        // Paint each dot with its last-known color immediately so reopening the
        // menu doesn't flash grey→color (and a momentary blip can't wipe a
        // good reading).
        for ((s, _, dot) in rowRefs) {
            setDot(dot, statCache.getOrPut(s.id) { SrvStat() }.color)
        }
        val handler = Handler(Looper.getMainLooper())
        val poll = object : Runnable {
            override fun run() {
                if (pollToken != menuPollToken) return  // dialog closed / superseded
                // Stagger probes so N servers don't all hit the radio at once
                // (a simultaneous burst on freshly-woken Wi-Fi is a top cause
                // of false timeouts).
                rowRefs.forEachIndexed { i, (s, tv, dot) ->
                    handler.postDelayed({
                        if (pollToken == menuPollToken) {
                            probeServer(s, tv, dot, s.id == curId)
                        }
                    }, i * PROBE_STAGGER_MS)
                }
                handler.postDelayed(this, POLL_INTERVAL_MS)
            }
        }
        dialog.setOnDismissListener { menuPollToken++ }  // stop the loop
        dialog.show()
        handler.post(poll)  // first round immediately
    }

    /**
     * One monitor tick for a single server. Reliability layer on top of the raw
     * probe:
     *  - keeps a per-server last-known color (so a single timeout never flashes
     *    red over a previously-good reading)
     *  - only declares OFFLINE on a definitive connection-refused, or after
     *    several consecutive uncertain (timeout/IO) results
     *  - skips servers whose previous probe is still in flight (no pile-up)
     */
    private fun probeServer(s: Server, tv: TextView, dot: View, isCurrent: Boolean) {
        val st = statCache.getOrPut(s.id) { SrvStat() }
        if (st.inFlight) return
        st.inFlight = true
        fetchStatus(s) { r ->
            st.inFlight = false
            val newColor = when (r.kind) {
                ProbeKind.OK -> {
                    st.timeouts = 0
                    when {
                        r.asking -> DOT_ASKING
                        r.busy   -> DOT_BUSY
                        else     -> DOT_IDLE
                    }
                }
                ProbeKind.REFUSED -> { st.timeouts = 0; DOT_OFFLINE }  // port closed → really down
                ProbeKind.UNCERTAIN -> {
                    st.timeouts++
                    if (st.timeouts >= OFFLINE_AFTER_UNCERTAIN || st.color == DOT_PROBING)
                        DOT_OFFLINE
                    else st.color   // keep last good color through a transient blip
                }
            }
            st.color = newColor
            runOnUiThread {
                if (r.name.isNotBlank()) {
                    Servers.updateName(prefs, s.id, r.name)
                    tv.text = (if (isCurrent) "✓  " else "      ") + r.name
                }
                setDot(dot, newColor)
            }
        }
    }

    // Status-dot colors: probing (grey), unreachable (red), asking user (yellow),
    // busy (green), idle (blue).
    private val DOT_PROBING = 0xFFBDBDBD.toInt()
    private val DOT_OFFLINE = 0xFFE53935.toInt()
    private val DOT_ASKING  = 0xFFFBC02D.toInt()
    private val DOT_BUSY    = 0xFF43A047.toInt()
    private val DOT_IDLE    = 0xFF42A5F5.toInt()

    // ── Live status monitor (while the action menu is open) ─────────────────
    private val POLL_INTERVAL_MS = 3000L   // re-probe every server this often
    private val PROBE_STAGGER_MS = 150L    // space out probes within a round
    private val CONNECT_TIMEOUT_MS = 4000
    private val READ_TIMEOUT_MS    = 4000
    private val PROBE_RETRIES = 1          // extra attempts on timeout (not on refused)
    private val OFFLINE_AFTER_UNCERTAIN = 2  // uncertain rounds before showing red

    // Bumped every time the menu opens or closes; the poll loop stops as soon
    // as its captured token no longer matches.
    private var menuPollToken = 0
    // Per-server last-known status, keyed by server id; survives menu reopens so
    // the dots don't flash grey and a blip can't erase a good reading.
    private val statCache = HashMap<String, SrvStat>()

    private inner class SrvStat {
        var color: Int = DOT_PROBING
        var timeouts: Int = 0      // consecutive uncertain results
        @Volatile var inFlight: Boolean = false
    }

    private enum class ProbeKind { OK, REFUSED, UNCERTAIN }
    private class ProbeResult(
        val kind: ProbeKind,
        val name: String = "",
        val busy: Boolean = false,
        val asking: Boolean = false,
    )

    private fun setDot(dot: View, color: Int) {
        dot.background = GradientDrawable().apply {
            shape = GradientDrawable.OVAL
            setColor(color)
        }
    }

    // A server menu row = [status dot] [label], horizontally laid out, tappable.
    // Returns the row, its label TextView, and the dot View so the async probe
    // can update both once it returns.
    private fun makeServerRow(
        label: String,
        onClick: () -> Unit
    ): Triple<LinearLayout, TextView, View> {
        val dot = View(this).apply {
            layoutParams = LinearLayout.LayoutParams(dp(9), dp(9)).apply {
                rightMargin = dp(10)
                gravity = Gravity.CENTER_VERTICAL
            }
        }
        setDot(dot, DOT_PROBING)

        val tv = TextView(this).apply {
            text = label
            textSize = 15f
            setTextColor(ContextCompat.getColor(this@MainActivity, R.color.text_primary))
            layoutParams = LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f
            )
        }

        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(14), dp(14), dp(14), dp(14))
            isClickable = true
            val a = TypedValue()
            context.theme.resolveAttribute(android.R.attr.selectableItemBackground, a, true)
            setBackgroundResource(a.resourceId)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
            )
            addView(dot)
            addView(tv)
            setOnClickListener { onClick() }
        }
        return Triple(row, tv, dot)
    }

    private fun makeMenuItem(label: String, onClick: () -> Unit): TextView {
        return TextView(this).apply {
            text = label
            textSize = 15f
            setPadding(dp(14), dp(14), dp(14), dp(14))
            setTextColor(ContextCompat.getColor(this@MainActivity, R.color.text_primary))
            isClickable = true
            val tv = TypedValue()
            context.theme.resolveAttribute(android.R.attr.selectableItemBackground, tv, true)
            setBackgroundResource(tv.resourceId)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
            )
            setOnClickListener { onClick() }
        }
    }

    // Probe http://host:port/api/version on a background thread, retrying on
    // timeout/IO so a single dropped packet doesn't read as "offline". Calls
    // back exactly once with a classified result:
    //   OK        — server answered (carries name/busy/asking)
    //   REFUSED   — connection refused (port closed → dashboard not running)
    //   UNCERTAIN — timeout / unreachable / other IO after retries (could be a
    //               transient blip; caller decides whether to show red yet)
    private fun fetchStatus(s: Server, cb: (ProbeResult) -> Unit) {
        Thread {
            var result = probeOnce(s)
            var tries = 0
            // Only retry the ambiguous case; a refused port is a firm answer.
            while (result.kind == ProbeKind.UNCERTAIN && tries < PROBE_RETRIES) {
                tries++
                try { Thread.sleep(300) } catch (_: InterruptedException) {}
                result = probeOnce(s)
            }
            cb(result)
        }.start()
    }

    private fun probeOnce(s: Server): ProbeResult {
        var conn: java.net.HttpURLConnection? = null
        return try {
            val url = java.net.URL("${s.url()}/api/version")
            conn = url.openConnection() as java.net.HttpURLConnection
            conn.connectTimeout = CONNECT_TIMEOUT_MS
            conn.readTimeout = READ_TIMEOUT_MS
            conn.requestMethod = "GET"
            conn.useCaches = false
            val code = conn.responseCode
            if (code == 200) {
                val body = conn.inputStream.bufferedReader().use { it.readText() }
                val o = org.json.JSONObject(body)
                ProbeResult(
                    ProbeKind.OK,
                    name = o.optString("instanceName", "").trim(),
                    busy = o.optBoolean("busy", false),
                    asking = o.optBoolean("asking", false),
                )
            } else {
                // Any HTTP reply means the port is open and the dashboard is up;
                // treat as reachable-but-idle (older servers without busy/asking
                // land here too via 200 with the fields simply absent).
                ProbeResult(ProbeKind.OK)
            }
        } catch (e: java.net.ConnectException) {
            // "Connection refused" = port closed (server down). "Network is
            // unreachable" = the phone's problem → ambiguous, allow retry/keep.
            if (e.message?.contains("refused", ignoreCase = true) == true)
                ProbeResult(ProbeKind.REFUSED)
            else ProbeResult(ProbeKind.UNCERTAIN)
        } catch (_: Exception) {
            // SocketTimeout, UnknownHost, NoRouteToHost, SSL, etc. → uncertain
            ProbeResult(ProbeKind.UNCERTAIN)
        } finally {
            try { conn?.disconnect() } catch (_: Exception) {}
        }
    }

    // Monotonic token: only the most-recent probe is allowed to act on its result.
    // Without this, switching servers quickly could let a slow probe of an old
    // host arrive after a successful new probe and flip the UI back to error.
    private var loadToken = 0

    private fun loadDashboard() {
        val host = prefs.getString(KEY_HOST, null)
        val port = prefs.getString(KEY_PORT, DEFAULT_PORT) ?: DEFAULT_PORT

        if (host.isNullOrBlank()) {
            openSettingsActivity()
            return
        }

        val base = "http://$host:$port"
        val myToken = ++loadToken

        // Show the progress bar while probing so the user sees something is
        // happening (otherwise a 3s probe feels like a frozen app).
        binding.progressBar.visibility = View.VISIBLE
        binding.progressBar.progress = 10

        // Reachability probe: hit /api/version with a short timeout. Only
        // hand the URL to the WebView once the server has actually answered —
        // that's what prevents (a) the WebView serving a stale cached page
        // when the server is dead, and (b) Chromium's default ERR_* page
        // flashing before our overlay can replace it.
        Thread {
            val reachable = try {
                val url = java.net.URL("$base/api/version")
                val conn = url.openConnection() as java.net.HttpURLConnection
                conn.connectTimeout = 3000
                conn.readTimeout = 3000
                conn.requestMethod = "GET"
                conn.useCaches = false
                val ok = conn.responseCode in 200..399
                try { conn.inputStream.close() } catch (_: Exception) {}
                conn.disconnect()
                ok
            } catch (_: Exception) { false }

            runOnUiThread {
                if (myToken != loadToken) return@runOnUiThread  // superseded
                binding.progressBar.visibility = View.GONE
                if (reachable) {
                    showError(false)
                    binding.webView.loadUrl(base)
                } else {
                    // Make sure the WebView isn't still showing the previous
                    // server's page or a cached copy of this one.
                    binding.webView.stopLoading()
                    binding.webView.loadUrl("about:blank")
                    showError(true)
                }
            }
        }.start()
    }

    private fun showError(show: Boolean) {
        inErrorState = show
        binding.layoutError.visibility = if (show) View.VISIBLE else View.GONE
        binding.webView.visibility = if (show) View.GONE else View.VISIBLE
    }

    private fun openSettingsActivity() {
        openSettings.launch(Intent(this, SettingsActivity::class.java))
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (binding.webView.canGoBack()) {
            binding.webView.goBack()
        } else {
            super.onBackPressed()
        }
    }
}
