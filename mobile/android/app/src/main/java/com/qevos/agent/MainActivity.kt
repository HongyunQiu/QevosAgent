package com.qevos.agent

import android.content.Intent
import android.content.SharedPreferences
import android.graphics.Bitmap
import android.net.http.SslError
import android.os.Bundle
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.webkit.SslErrorHandler
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import com.qevos.agent.databinding.ActivityMainBinding

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: SharedPreferences
    private var settingsChanged = false

    companion object {
        const val PREFS_NAME = "qevos_prefs"
        const val KEY_HOST = "host"
        const val KEY_PORT = "port"
        const val DEFAULT_PORT = "8765"
    }

    private val openSettings = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) {
        settingsChanged = true
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setSupportActionBar(binding.toolbar)

        prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)

        setupWebView()

        binding.btnRetry.setOnClickListener { loadDashboard() }
        binding.btnSettings.setOnClickListener { openSettingsActivity() }

        loadDashboard()
    }

    override fun onResume() {
        super.onResume()
        if (settingsChanged) {
            settingsChanged = false
            loadDashboard()
        }
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
                // 允许 WebSocket 连接
                allowContentAccess = true
                allowFileAccess = false
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
                        showError(true)
                    }
                }

                // 局域网自签证书放行（ZeroTier 有时会用到）
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

    private fun loadDashboard() {
        val host = prefs.getString(KEY_HOST, null)
        val port = prefs.getString(KEY_PORT, DEFAULT_PORT)

        if (host.isNullOrBlank()) {
            openSettingsActivity()
            return
        }

        showError(false)
        binding.webView.loadUrl("http://$host:$port")
    }

    private fun showError(show: Boolean) {
        binding.layoutError.visibility = if (show) View.VISIBLE else View.GONE
        binding.webView.visibility = if (show) View.GONE else View.VISIBLE
    }

    private fun openSettingsActivity() {
        openSettings.launch(Intent(this, SettingsActivity::class.java))
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.main_menu, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            R.id.action_refresh -> {
                binding.webView.reload()
                true
            }
            R.id.action_settings -> {
                openSettingsActivity()
                true
            }
            else -> super.onOptionsItemSelected(item)
        }
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
