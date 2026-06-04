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

    fun load(prefs: SharedPreferences): MutableList<Server> {
        val list = mutableListOf<Server>()
        val raw = prefs.getString(KEY_SERVERS, null)
        var needsResave = false
        if (!raw.isNullOrBlank()) {
            try {
                val arr = JSONArray(raw)
                for (i in 0 until arr.length()) {
                    val o = arr.getJSONObject(i)
                    val host = o.optString("host").trim()
                    if (host.isBlank()) continue
                    val port = o.optString("port", MainActivity.DEFAULT_PORT)
                        .trim().ifBlank { MainActivity.DEFAULT_PORT }
                    // Backfill id for rows saved by older versions.
                    val id = o.optString("id", "").ifBlank {
                        needsResave = true
                        newId()
                    }
                    list.add(Server(id, host, port, o.optString("name", "")))
                }
            } catch (_: Exception) { /* corrupt → treat as empty */ }
        }
        // Migrate a pre-existing single host/port into the list.
        if (list.isEmpty()) {
            val h = prefs.getString(MainActivity.KEY_HOST, null)?.trim()
            if (!h.isNullOrBlank()) {
                val p = prefs.getString(MainActivity.KEY_PORT, MainActivity.DEFAULT_PORT)
                    ?: MainActivity.DEFAULT_PORT
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
