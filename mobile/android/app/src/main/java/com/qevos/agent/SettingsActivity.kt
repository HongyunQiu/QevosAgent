package com.qevos.agent

import android.app.AlertDialog
import android.content.Intent
import android.graphics.Typeface
import android.net.Uri
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.Menu
import android.view.MenuItem
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
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class SettingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySettingsBinding
    private val prefs by lazy { getSharedPreferences(MainActivity.PREFS_NAME, MODE_PRIVATE) }

    private class Row(val id: String, val host: EditText, val port: EditText, val name: String)
    private val rows = mutableListOf<Pair<LinearLayout, Row>>()

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    // ── SAF launchers ──────────────────────────────────────────────────────
    // CreateDocument lets the user pick any location (internal, SD card,
    // Documents/, even cloud providers if they have a doc-provider app
    // installed). No storage permission needed.
    private val exportLauncher = registerForActivityResult(
        ActivityResultContracts.CreateDocument("application/json")
    ) { uri: Uri? -> if (uri != null) writeExport(uri) }

    private val importLauncher = registerForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri: Uri? -> if (uri != null) readImport(uri) }

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
        binding.btnSave.setOnClickListener {
            saveAll()
            Toast.makeText(this, "已保存", Toast.LENGTH_SHORT).show()
            finish()
        }
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.menu_settings, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            R.id.action_export -> { startExport(); true }
            R.id.action_import -> { startImport(); true }
            else -> super.onOptionsItemSelected(item)
        }
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

    // ── Export ─────────────────────────────────────────────────────────────
    private fun startExport() {
        // Persist what's currently in the UI so the export reflects unsaved edits.
        saveAll()
        val stamp = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
        exportLauncher.launch("qevos-servers-$stamp.json")
    }

    private fun writeExport(uri: Uri) {
        try {
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
                put("exportedAt", SimpleDateFormat(
                    "yyyy-MM-dd'T'HH:mm:ssXXX", Locale.US).format(Date()))
                put("servers", arr)
            }
            contentResolver.openOutputStream(uri, "wt")?.use { os ->
                os.write(payload.toString(2).toByteArray(Charsets.UTF_8))
            } ?: throw RuntimeException("无法打开目标文件")
            Toast.makeText(this, "已导出 ${servers.size} 条", Toast.LENGTH_SHORT).show()
        } catch (e: Exception) {
            Toast.makeText(this, "导出失败: ${e.message}", Toast.LENGTH_LONG).show()
        }
    }

    // ── Import ─────────────────────────────────────────────────────────────
    private fun startImport() {
        // application/json is the canonical type, but some pickers/file
        // managers expose .json files only under */* or text/*. We accept
        // anything and validate the contents instead.
        importLauncher.launch(arrayOf("application/json", "text/plain", "*/*"))
    }

    private fun readImport(uri: Uri) {
        val parsed: List<Server> = try {
            val raw = contentResolver.openInputStream(uri)?.use {
                it.readBytes().toString(Charsets.UTF_8)
            } ?: throw RuntimeException("无法读取文件")
            parseBackup(raw)
        } catch (e: Exception) {
            Toast.makeText(this, "导入失败: ${e.message}", Toast.LENGTH_LONG).show()
            return
        }
        if (parsed.isEmpty()) {
            Toast.makeText(this, "文件里没有可导入的服务器", Toast.LENGTH_LONG).show()
            return
        }

        val current = collect()  // UI state, not persisted state
        val mergedCount = parsed.count { p -> current.none { it.id == p.id } } + current.size

        // Three choices: replace everything, merge by id, or cancel.
        // Merge keeps existing rows and only adds NEW ids — safe default for
        // "I exported from another device and want to add what I'm missing."
        AlertDialog.Builder(this)
            .setTitle("导入 ${parsed.size} 条服务器")
            .setMessage(
                "当前有 ${current.size} 条。\n" +
                "• 覆盖：清空后只保留导入的 ${parsed.size} 条\n" +
                "• 合并：在现有列表基础上追加新条目（id 已存在的跳过），合并后约 $mergedCount 条"
            )
            .setPositiveButton("合并") { _, _ -> applyImport(parsed, replace = false) }
            .setNeutralButton("覆盖") { _, _ -> applyImport(parsed, replace = true) }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun applyImport(imported: List<Server>, replace: Boolean) {
        val finalList: List<Server> = if (replace) {
            imported
        } else {
            val current = collect()
            val existingIds = current.mapTo(HashSet()) { it.id }
            current + imported.filter { it.id !in existingIds }
        }
        Servers.save(prefs, finalList)

        // Rebuild the UI from the persisted list so the user immediately sees
        // the result. Recreating the activity is the simplest correct option
        // (handles row removal, prevents stale Row objects pointing at
        // detached EditTexts).
        Toast.makeText(this, "已导入，列表已更新（${finalList.size} 条）", Toast.LENGTH_SHORT).show()
        recreate()
    }

    private fun parseBackup(raw: String): List<Server> {
        val text = raw.trim()
        if (text.isEmpty()) throw RuntimeException("文件为空")
        // Accept either { format, servers: [...] } (our v1 format) or a bare
        // JSON array of server objects — so users can hand-edit a list and
        // it'll still import.
        val arr: JSONArray = when (val first = text[0]) {
            '{' -> {
                val obj = JSONObject(text)
                if (obj.has("format") &&
                    !obj.optString("format").startsWith("qevos-agent-servers/")) {
                    throw RuntimeException("文件格式不识别: ${obj.optString("format")}")
                }
                obj.optJSONArray("servers") ?: throw RuntimeException("缺少 servers 字段")
            }
            '[' -> JSONArray(text)
            else -> throw RuntimeException("不是 JSON（开头是 '$first'）")
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
        // Bump if the schema changes incompatibly. parseBackup also accepts a
        // bare array (no format field) for hand-edited imports.
        private const val BACKUP_FORMAT = "qevos-agent-servers/v1"
    }
}
