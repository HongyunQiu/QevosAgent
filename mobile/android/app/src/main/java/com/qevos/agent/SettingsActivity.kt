package com.qevos.agent

import android.Manifest
import android.app.AlertDialog
import android.content.ContentValues
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.text.InputType
import android.view.Gravity
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.qevos.agent.databinding.ActivitySettingsBinding
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class SettingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySettingsBinding
    private val prefs by lazy { getSharedPreferences(MainActivity.PREFS_NAME, MODE_PRIVATE) }

    private class Row(val id: String, val host: EditText, val port: EditText, val name: String)
    private val rows = mutableListOf<Pair<LinearLayout, Row>>()

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    // On API ≤ 28 the public Download dir needs WRITE_EXTERNAL_STORAGE. API 29+
    // writes via MediaStore with no permission at all. We remember which action
    // asked for the permission so we can resume it once granted.
    private var pendingAction: (() -> Unit)? = null
    private val permLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        val action = pendingAction; pendingAction = null
        if (granted) action?.invoke()
        else Toast.makeText(this, "未授予存储权限，无法读写配置文件", Toast.LENGTH_LONG).show()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setSupportActionBar(binding.toolbar)
        supportActionBar?.apply {
            setDisplayHomeAsUpEnabled(true)
            title = "服务器设置"
        }

        val servers = Servers.load(prefs)
        if (servers.isEmpty()) addRow(Server(Servers.newId(), "", MainActivity.DEFAULT_PORT))
        else servers.forEach { addRow(it) }

        binding.btnAdd.setOnClickListener { addRow(Server(Servers.newId(), "", MainActivity.DEFAULT_PORT)) }
        // Save: persist UI edits to prefs AND write the config file, then close.
        binding.btnSave.setOnClickListener { saveConfig(closeAfter = true) }
        // Load: pull the whole list back from the config file (stays open).
        binding.btnLoad.setOnClickListener { loadConfig() }
    }

    private fun addRow(server: Server) {
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), dp(10), dp(12), dp(10))
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply { bottomMargin = dp(10) }
            background = ContextCompat.getDrawable(this@SettingsActivity, R.drawable.bg_server_row)
        }

        // Row 1: nickname (read-only) + 连接 + 删除
        val top = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        val nameTv = TextView(this).apply {
            text = if (server.name.isNotBlank()) server.name else "（未命名）"
            setTextColor(ContextCompat.getColor(this@SettingsActivity, R.color.text_primary))
            textSize = 14f
            setTypeface(typeface, Typeface.BOLD)
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        val connectBtn = Button(this).apply { text = "连接"; isAllCaps = false }
        val delBtn = Button(this).apply { text = "删除"; isAllCaps = false }
        top.addView(nameTv); top.addView(connectBtn); top.addView(delBtn)

        // Row 2: host + port
        val bottom = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        val hostEt = EditText(this).apply {
            hint = "IP 地址（如 192.168.1.100）"
            setText(server.host)
            inputType = InputType.TYPE_TEXT_VARIATION_URI
            setSingleLine()
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        val portEt = EditText(this).apply {
            hint = "端口"
            setText(server.port)
            inputType = InputType.TYPE_CLASS_NUMBER
            setSingleLine()
            layoutParams = LinearLayout.LayoutParams(dp(80), ViewGroup.LayoutParams.WRAP_CONTENT)
                .apply { marginStart = dp(8) }
        }
        bottom.addView(hostEt); bottom.addView(portEt)

        card.addView(top); card.addView(bottom)

        val row = Row(server.id, hostEt, portEt, server.name)
        val pair = card to row
        rows.add(pair)
        binding.serverList.addView(card)

        delBtn.setOnClickListener {
            binding.serverList.removeView(card)
            rows.remove(pair)
        }
        connectBtn.setOnClickListener {
            val host = hostEt.text.toString().trim()
            if (host.isBlank()) { hostEt.error = "请输入主机地址"; return@setOnClickListener }
            saveAll()
            val port = portEt.text.toString().trim().ifBlank { MainActivity.DEFAULT_PORT }
            prefs.edit()
                .putString(MainActivity.KEY_HOST, host)
                .putString(MainActivity.KEY_PORT, port)
                .putString(MainActivity.KEY_SERVER_ID, row.id)
                .apply()
            setResult(RESULT_OK)
            finish()
        }
    }

    private fun collect(): List<Server> {
        val list = mutableListOf<Server>()
        for ((_, r) in rows) {
            val host = r.host.text.toString().trim()
            if (host.isBlank()) continue
            val port = r.port.text.toString().trim().ifBlank { MainActivity.DEFAULT_PORT }
            list.add(Server(r.id, host, port, r.name))
        }
        return list
    }

    private fun saveAll() = Servers.save(prefs, collect())

    override fun onSupportNavigateUp(): Boolean {
        saveAll()
        @Suppress("DEPRECATION")
        onBackPressed()
        return true
    }

    // ── One-tap save / load to a single fixed config file ───────────────────
    // No picker, no filename, no folder choice: always Download/qevos-servers.json.
    // Save overwrites it; load replaces the whole list from it.

    private fun saveConfig(closeAfter: Boolean) {
        if (!ensureLegacyPermission { saveConfig(closeAfter) }) return
        // Persist current UI edits first so both prefs and the file reflect
        // what's on screen.
        saveAll()
        val servers = Servers.load(prefs)
        val arr = JSONArray()
        for (s in servers) {
            arr.put(JSONObject().apply {
                put("id", s.id); put("host", s.host)
                put("port", s.port); put("name", s.name)
            })
        }
        val payload = JSONObject().apply {
            put("format", BACKUP_FORMAT)
            put("savedAt", SimpleDateFormat(
                "yyyy-MM-dd'T'HH:mm:ssXXX", Locale.US).format(Date()))
            put("servers", arr)
        }.toString(2)

        try {
            writeDownload(payload)
            Toast.makeText(this, "已保存 ${servers.size} 条到 Download/$CONFIG_FILENAME",
                Toast.LENGTH_SHORT).show()
            if (closeAfter) finish()
        } catch (e: Exception) {
            Toast.makeText(this, "保存失败: ${e.message}", Toast.LENGTH_LONG).show()
        }
    }

    private fun loadConfig() {
        if (!ensureLegacyPermission { loadConfig() }) return
        val raw = try {
            readDownload() ?: run {
                Toast.makeText(this, "没找到配置文件 Download/$CONFIG_FILENAME",
                    Toast.LENGTH_LONG).show()
                return
            }
        } catch (e: Exception) {
            Toast.makeText(this, "读取失败: ${e.message}", Toast.LENGTH_LONG).show()
            return
        }
        val parsed = try { parseConfig(raw) } catch (e: Exception) {
            Toast.makeText(this, "配置文件解析失败: ${e.message}", Toast.LENGTH_LONG).show()
            return
        }
        if (parsed.isEmpty()) {
            Toast.makeText(this, "配置文件里没有服务器", Toast.LENGTH_LONG).show()
            return
        }
        // Straight replace — the whole point is "load this config". Recreate the
        // activity so the rows rebuild cleanly from the persisted list.
        Servers.save(prefs, parsed)
        Toast.makeText(this, "已读取 ${parsed.size} 条配置", Toast.LENGTH_SHORT).show()
        recreate()
    }

    // ── Fixed-file IO: MediaStore (API 29+) / legacy File (API ≤ 28) ─────────

    /**
     * API ≤ 28 needs WRITE_EXTERNAL_STORAGE to touch public Download. Returns
     * true if we can proceed now; false if we kicked off a permission request
     * (the granted callback re-runs [action]). API 29+ always returns true.
     */
    private fun ensureLegacyPermission(action: () -> Unit): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) return true
        val perm = Manifest.permission.WRITE_EXTERNAL_STORAGE
        if (ContextCompat.checkSelfPermission(this, perm) ==
            PackageManager.PERMISSION_GRANTED) return true
        pendingAction = action
        permLauncher.launch(perm)
        return false
    }

    /** Overwrite Download/qevos-servers.json with [text]. */
    private fun writeDownload(text: String) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val resolver = contentResolver
            val uri = findDownloadUri() ?: resolver.insert(
                MediaStore.Downloads.EXTERNAL_CONTENT_URI,
                ContentValues().apply {
                    put(MediaStore.MediaColumns.DISPLAY_NAME, CONFIG_FILENAME)
                    put(MediaStore.MediaColumns.MIME_TYPE, "application/json")
                    put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS)
                }
            ) ?: throw RuntimeException("无法在 Download 创建文件")
            // "wt" truncates so an existing (possibly longer) file is overwritten.
            resolver.openOutputStream(uri, "wt")?.use {
                it.write(text.toByteArray(Charsets.UTF_8))
            } ?: throw RuntimeException("无法写入文件")
        } else {
            @Suppress("DEPRECATION")
            val dir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
            if (!dir.exists()) dir.mkdirs()
            File(dir, CONFIG_FILENAME).writeText(text, Charsets.UTF_8)
        }
    }

    /** Read Download/qevos-servers.json, or null if it doesn't exist. */
    private fun readDownload(): String? {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val uri = findDownloadUri() ?: return null
            return contentResolver.openInputStream(uri)?.use {
                it.readBytes().toString(Charsets.UTF_8)
            }
        }
        @Suppress("DEPRECATION")
        val dir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
        val f = File(dir, CONFIG_FILENAME)
        return if (f.exists()) f.readText(Charsets.UTF_8) else null
    }

    /** Locate the existing config file in Download via MediaStore (API 29+). */
    private fun findDownloadUri(): Uri? {
        val proj = arrayOf(MediaStore.MediaColumns._ID)
        val sel = "${MediaStore.MediaColumns.DISPLAY_NAME}=? AND " +
                  "${MediaStore.MediaColumns.RELATIVE_PATH}=?"
        // RELATIVE_PATH comes back with a trailing slash.
        val args = arrayOf(CONFIG_FILENAME, "${Environment.DIRECTORY_DOWNLOADS}/")
        contentResolver.query(
            MediaStore.Downloads.EXTERNAL_CONTENT_URI, proj, sel, args, null
        )?.use { c ->
            if (c.moveToFirst()) {
                val id = c.getLong(c.getColumnIndexOrThrow(MediaStore.MediaColumns._ID))
                return android.content.ContentUris.withAppendedId(
                    MediaStore.Downloads.EXTERNAL_CONTENT_URI, id)
            }
        }
        return null
    }

    private fun parseConfig(raw: String): List<Server> {
        val text = raw.trim()
        if (text.isEmpty()) throw RuntimeException("文件为空")
        // Accept our { format, servers: [...] } object or a bare array.
        val arr: JSONArray = when (text[0]) {
            '{' -> JSONObject(text).optJSONArray("servers")
                ?: throw RuntimeException("缺少 servers 字段")
            '[' -> JSONArray(text)
            else -> throw RuntimeException("不是 JSON")
        }
        val out = mutableListOf<Server>()
        for (i in 0 until arr.length()) {
            val o = arr.getJSONObject(i)
            val host = o.optString("host").trim()
            if (host.isBlank()) continue
            val port = o.optString("port", MainActivity.DEFAULT_PORT)
                .trim().ifBlank { MainActivity.DEFAULT_PORT }
            val id = o.optString("id", "").ifBlank { Servers.newId() }
            out.add(Server(id, host, port, o.optString("name", "")))
        }
        return out
    }

    companion object {
        private const val CONFIG_FILENAME = "qevos-servers.json"
        private const val BACKUP_FORMAT = "qevos-agent-servers/v1"
    }
}
