package com.qevos.agent

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import java.net.Inet4Address
import java.util.ArrayDeque

data class DiscoveredServer(
    val serviceName: String,
    val instanceName: String,
    val instanceId: String,
    val host: String,
    val port: Int,
)

/** Foreground DNS-SD browser for `_qevosagent._tcp.local`. */
@Suppress("DEPRECATION")
class LanDiscovery(
    context: Context,
    private val onChanged: (List<DiscoveredServer>) -> Unit,
    private val onError: (String) -> Unit,
) {
    private val appContext = context.applicationContext
    private val nsd = appContext.getSystemService(Context.NSD_SERVICE) as NsdManager
    private val found = linkedMapOf<String, DiscoveredServer>()
    private val resolveQueue = ArrayDeque<Pair<String, NsdServiceInfo>>()
    private val queuedKeys = mutableSetOf<String>()
    private var resolvingKey: String? = null
    private var running = false
    private var restartWhenStopped = false
    private var multicastLock: WifiManager.MulticastLock? = null

    private val listener = object : NsdManager.DiscoveryListener {
        override fun onDiscoveryStarted(type: String) { running = true }
        override fun onDiscoveryStopped(type: String) {
            running = false
            releaseLock()
            if (restartWhenStopped) {
                restartWhenStopped = false
                start()
            }
        }
        override fun onStartDiscoveryFailed(type: String, code: Int) {
            running = false
            releaseLock()
            onError("局域网扫描启动失败（$code）")
        }
        override fun onStopDiscoveryFailed(type: String, code: Int) {
            running = false
            releaseLock()
        }
        override fun onServiceFound(service: NsdServiceInfo) {
            if (!service.serviceType.startsWith(SERVICE_TYPE)) return
            val key = "${service.serviceName}|${service.serviceType}"
            if (key == resolvingKey || !queuedKeys.add(key)) return
            resolveQueue.addLast(key to service)
            resolveNext()
        }

        override fun onServiceLost(service: NsdServiceInfo) {
            // A lost callback doesn't reliably carry resolved addresses. Keep
            // the result for this short foreground scan; Refresh starts clean.
        }
    }

    private fun resolveNext() {
        if (resolvingKey != null) return
        val next = resolveQueue.pollFirst() ?: return
        val (key, service) = next
        resolvingKey = key
        queuedKeys.remove(key)
        nsd.resolveService(service, object : NsdManager.ResolveListener {
                override fun onResolveFailed(info: NsdServiceInfo, code: Int) {
                    resolvingKey = null
                    resolveNext()
                }

                override fun onServiceResolved(info: NsdServiceInfo) {
                    resolvingKey = null
                    val addresses = if (Build.VERSION.SDK_INT >= 34) info.hostAddresses
                        else listOfNotNull(info.host)
                    val attrs = info.attributes.mapValues { (_, value) ->
                        value.toString(Charsets.UTF_8)
                    }
                    for (address in addresses.filterIsInstance<Inet4Address>()) {
                        val host = address.hostAddress ?: continue
                        val item = DiscoveredServer(
                            serviceName = info.serviceName,
                            instanceName = attrs["name"].orEmpty().ifBlank { info.serviceName },
                            instanceId = attrs["id"].orEmpty(),
                            host = host,
                            port = info.port,
                        )
                        // Deliberately keep every address as a separate entry so
                        // users can explicitly choose LAN, VPN, or hotspot paths.
                        found["$host:${info.port}"] = item
                    }
                    onChanged(found.values.toList())
                    resolveNext()
                }
            })
    }

    fun start() {
        if (running) return
        found.clear()
        resolveQueue.clear()
        queuedKeys.clear()
        resolvingKey = null
        onChanged(emptyList())
        acquireLock()
        try {
            nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
        } catch (error: Exception) {
            releaseLock()
            onError("无法扫描局域网：${error.message}")
        }
    }

    fun stop() {
        if (running) {
            try { nsd.stopServiceDiscovery(listener) } catch (_: Exception) {}
        }
        running = false
        releaseLock()
    }

    fun restart() {
        if (running) {
            restartWhenStopped = true
            try { nsd.stopServiceDiscovery(listener) } catch (_: Exception) {
                running = false
                restartWhenStopped = false
                start()
            }
        } else {
            start()
        }
    }

    private fun acquireLock() {
        val wifi = appContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager ?: return
        multicastLock = wifi.createMulticastLock("qevosagent-nsd").apply {
            setReferenceCounted(false)
            acquire()
        }
    }

    private fun releaseLock() {
        multicastLock?.let { if (it.isHeld) it.release() }
        multicastLock = null
    }

    companion object {
        const val SERVICE_TYPE = "_qevosagent._tcp."
    }
}
