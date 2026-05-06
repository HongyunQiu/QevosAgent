package com.qevos.agent

import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.qevos.agent.databinding.ActivitySettingsBinding

class SettingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySettingsBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setSupportActionBar(binding.toolbar)
        supportActionBar?.apply {
            setDisplayHomeAsUpEnabled(true)
            title = "连接设置"
        }

        val prefs = getSharedPreferences(MainActivity.PREFS_NAME, MODE_PRIVATE)

        binding.etHost.setText(prefs.getString(MainActivity.KEY_HOST, ""))
        binding.etPort.setText(prefs.getString(MainActivity.KEY_PORT, MainActivity.DEFAULT_PORT))

        binding.btnSave.setOnClickListener {
            val host = binding.etHost.text.toString().trim()
            val port = binding.etPort.text.toString().trim()

            if (host.isBlank()) {
                binding.tilHost.error = "请输入主机地址"
                return@setOnClickListener
            }
            binding.tilHost.error = null

            prefs.edit()
                .putString(MainActivity.KEY_HOST, host)
                .putString(MainActivity.KEY_PORT, port.ifBlank { MainActivity.DEFAULT_PORT })
                .apply()

            Toast.makeText(this, "已保存", Toast.LENGTH_SHORT).show()
            finish()
        }
    }

    override fun onSupportNavigateUp(): Boolean {
        @Suppress("DEPRECATION")
        onBackPressed()
        return true
    }
}
