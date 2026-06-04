package com.qevos.agent

import android.content.Intent
import android.content.SharedPreferences
import android.graphics.Bitmap
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.http.SslError
import android.os.Bundle
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

        // Saved servers — tap to switch.
        val rowRefs = mutableListOf<Pair<Server, TextView>>()
        for (s in servers) {
            val isCurrent = s.id == curId
            val row = makeMenuItem((if (isCurrent) "✓  " else "      ") + s.label()) {
                prefs.edit()
                    .putString(KEY_HOST, s.host)
                    .putString(KEY_PORT, s.port)
                    .putString(KEY_SERVER_ID, s.id)
                    .apply()
                dialog.dismiss()
                loadDashboard()
            }
            container.addView(row)
            rowRefs.add(s to row)
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

        dialog.show()

        // Fetch each server's instance nickname in the background, then update its row + cache.
        for ((s, tv) in rowRefs) {
            val isCurrent = s.id == curId
            fetchInstanceName(s) { name ->
                Servers.updateName(prefs, s.id, name)
                runOnUiThread { tv.text = (if (isCurrent) "✓  " else "      ") + name }
            }
        }
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

    // GET http://host:port/api/version → instanceName. Calls back only on non-blank name.
    private fun fetchInstanceName(s: Server, cb: (String) -> Unit) {
        Thread {
            try {
                val url = java.net.URL("${s.url()}/api/version")
                val conn = url.openConnection() as java.net.HttpURLConnection
                // Mobile networks need real headroom — 1.5s used to fail on
                // freshly-associated Wi-Fi and fall back to the IP label.
                conn.connectTimeout = 4000
                conn.readTimeout = 4000
                conn.requestMethod = "GET"
                if (conn.responseCode == 200) {
                    val body = conn.inputStream.bufferedReader().use { it.readText() }
                    val name = org.json.JSONObject(body).optString("instanceName", "").trim()
                    if (name.isNotBlank()) cb(name)
                }
                conn.disconnect()
            } catch (_: Exception) { /* unreachable / old host → keep URL label */ }
        }.start()
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
