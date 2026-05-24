package com.qevos.agent

import android.graphics.Typeface
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.qevos.agent.databinding.ActivitySettingsBinding

class SettingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySettingsBinding
    private val prefs by lazy { getSharedPreferences(MainActivity.PREFS_NAME, MODE_PRIVATE) }

    private class Row(val host: EditText, val port: EditText, val name: String)
    private val rows = mutableListOf<Pair<LinearLayout, Row>>()

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

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
        if (servers.isEmpty()) addRow(Server("", MainActivity.DEFAULT_PORT))
        else servers.forEach { addRow(it) }

        binding.btnAdd.setOnClickListener { addRow(Server("", MainActivity.DEFAULT_PORT)) }
        binding.btnSave.setOnClickListener {
            saveAll()
            Toast.makeText(this, "已保存", Toast.LENGTH_SHORT).show()
            finish()
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

        val row = Row(hostEt, portEt, server.name)
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
            list.add(Server(host, port, r.name))
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
}
