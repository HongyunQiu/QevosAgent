package com.qevos.agent

import android.content.SharedPreferences
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

/**
 * One saved QevosAgent server entry.
 *  - id:        stable per-row identifier — host:port is NOT unique (port-forwarding
 *               / SSH tunnels can legitimately map two distinct instances to the
 *               same host:port from the phone's POV), so we identify the
 *               currently-selected row by id instead of by connection target.
 *  - host/port: the connection target (URL = http://host:port)
 *  - name:      cached instance nickname (display-only; fetched from the server's
 *               /api/version, never edited in the app)
 */
data class Server(
    val id: String,
    val host: String,
    val port: String,
    val name: String = ""
) {
    fun url(): String = "http://$host:$port"
    fun key(): String = "$host:$port"
    /** What to show in lists: nickname when known, otherwise host:port. */
    fun label(): String = if (name.isNotBlank()) name else key()
}

/** Persists the server list as JSON in SharedPreferences. */
object Servers {
    const val KEY_SERVERS = "servers_json"

    fun newId(): String = UUID.randomUUID().toString()

    // ── Defensive host/port validation & normalization ──────────────────────
    // These guard against hand-typed config errors (the recurring cause of the
    // "app closes right after launch" symptom): a port accidentally typed into
    // the host field, stray whitespace, an over-long port, etc. They are used
    // at three layers — input (SettingsActivity), persistence (load/parseConfig)
    // and launch (MainActivity.loadDashboard) — so one bad entry can no longer
    // take the whole app down.

    /** True if [h] looks like a bare IPv4 address or hostname: no whitespace,
     *  no colon (a port belongs in the port field, not the host), no slash,
     *  and dot/label characters only. */
    fun isValidHost(h: String): Boolean {
        val s = h.trim()
        if (s.isEmpty() || s.length > 253) return false
        if (s.any { it.isWhitespace() || it == ':' || it == '/' }) return false
        return Regex("^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$").matches(s)
    }

    /** Clean up a host string: trim, and if the user accidentally pasted a
     *  "host:port" into the host field, keep the host part (the port belongs in
     *  its own column). Returns the cleaned value; caller still validates. */
    fun normalizeHost(h: String): String {
        var s = h.trim()
        val c = s.indexOf(':')
        if (c > 0 && c < s.length - 1 && s.substring(c + 1).all { it.isDigit() }) {
            s = s.substring(0, c)   // strip an accidental ":port" tail
        }
        return s
    }

    /** True if [p] is a pure-numeric TCP/UDP port within 1..65535. */
    fun isValidPort(p: String): Boolean {
        val s = p.trim()
        if (s.isEmpty() || !s.all { it.isDigit() }) return false
        val n = s.toIntOrNull() ?: return false
        return n in 1..65535
    }

    /** Trim a port; fall back to the default when it is missing or malformed
     *  (e.g. an over-typed "87655"). */
    fun normalizePort(p: String): String {
        val s = p.trim()
        return if (isValidPort(s)) s else MainActivity.DEFAULT_PORT
    }

    fun load(prefs: SharedPreferences): MutableList<Server> {
        val list = mutableListOf<Server>()
        val raw = prefs.getString(KEY_SERVERS, null)
        var needsResave = false
        if (!raw.isNullOrBlank()) {
            try {
                val arr = JSONArray(raw)
                for (i in 0 until arr.length()) {
                    val o = arr.getJSONObject(i)
                    val host = normalizeHost(o.optString("host"))
                    // Skip rows whose host is unusable even after cleanup, so a
                    // hand-corrupted entry can't poison the whole list. Other
                    // rows still load fine.
                    if (!isValidHost(host)) continue
                    val port = normalizePort(o.optString("port", MainActivity.DEFAULT_PORT))
                    // Backfill id for rows saved by older versions.
                    val id = o.optString("id", "").ifBlank {
                        needsResave = true
                        newId()
                    }
                    list.add(Server(id, host, port, o.optString("name", "")))
                }
            } catch (_: Exception) { /* corrupt → treat as empty */ }
        }
        // Migrate a pre-existing single host/port into the list, cleaning up
        // the value (e.g. a port accidentally embedded in the host) as we go.
        if (list.isEmpty()) {
            val h = normalizeHost(prefs.getString(MainActivity.KEY_HOST, "") ?: "")
            if (isValidHost(h)) {
                val p = normalizePort(
                    prefs.getString(MainActivity.KEY_PORT, MainActivity.DEFAULT_PORT)
                        ?: MainActivity.DEFAULT_PORT)
                list.add(Server(newId(), h, p))
                save(prefs, list)
            }
        } else if (needsResave) {
            save(prefs, list)
        }
        return list
    }

    fun save(prefs: SharedPreferences, list: List<Server>) {
        val arr = JSONArray()
        for (s in list) {
            arr.put(JSONObject().apply {
                put("id", s.id)
                put("host", s.host)
                put("port", s.port)
                put("name", s.name)
            })
        }
        prefs.edit().putString(KEY_SERVERS, arr.toString()).apply()
    }

    /** Update the cached nickname for a given row id. Returns true if it changed. */
    fun updateName(prefs: SharedPreferences, id: String, name: String): Boolean {
        val list = load(prefs)
        var changed = false
        val newList = list.map {
            if (it.id == id && it.name != name) {
                changed = true; it.copy(name = name)
            } else it
        }
        if (changed) save(prefs, newList)
        return changed
    }
}
