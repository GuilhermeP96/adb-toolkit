package com.adbtoolkit.agent.api

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.ConnectivityManager
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.Build
import android.os.Environment
import android.os.StatFs
import android.app.ActivityManager
import fi.iki.elonen.NanoHTTPD
import fi.iki.elonen.NanoHTTPD.Response
import com.adbtoolkit.agent.server.AgentServer.Companion.jsonOk
import com.adbtoolkit.agent.server.AgentServer.Companion.jsonError
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.NetworkInterface

/**
 * Device info & control API.
 *
 * Endpoints:
 *   GET  /api/device/info          → full device details
 *   GET  /api/device/battery
 *   GET  /api/device/network
 *   GET  /api/device/storage
 *   GET  /api/device/props         → system properties
 *   GET  /api/device/permissions   → granted permissions list
 *   GET  /api/device/screen        → screenshot (PNG)
 */
class DeviceApi(private val context: Context) {

    fun handle(
        method: NanoHTTPD.Method,
        parts: List<String>,
        session: NanoHTTPD.IHTTPSession,
    ): Response {
        val action = parts.getOrNull(0) ?: "info"

        return when (action) {
            "info"        -> fullInfo()
            "battery"     -> battery()
            "network"     -> network()
            "storage"     -> storage()
            "props"       -> sysProps(session.parms)
            "permissions" -> permissions()
            "screen"      -> screenshot()
            else -> jsonError("Unknown device action: $action")
        }
    }

    // ── full info ────────────────────────────────────────────────────────
    private fun fullInfo(): Response {
        return jsonOk(JSONObject().apply {
            put("model", Build.MODEL)
            put("manufacturer", Build.MANUFACTURER)
            put("brand", Build.BRAND)
            put("product", Build.PRODUCT)
            put("device", Build.DEVICE)
            put("board", Build.BOARD)
            put("hardware", Build.HARDWARE)
            put("android_version", Build.VERSION.RELEASE)
            put("sdk_version", Build.VERSION.SDK_INT)
            put("build_id", Build.DISPLAY)
            put("fingerprint", Build.FINGERPRINT)
            put("serial", Build.getSerial().runCatching { this }.getOrDefault("unknown"))
            put("abis", JSONArray(Build.SUPPORTED_ABIS.toList()))
            put("battery", batteryJson())
            put("network", networkJson())
            put("storage", storageJson())
            put("agent_version", "1.0.0")
            put("ip_addresses", getIpAddresses())
        })
    }

    // ── battery ──────────────────────────────────────────────────────────
    private fun battery(): Response = jsonOk(batteryJson())

    private fun batteryJson(): JSONObject {
        val intent = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = intent?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = intent?.getIntExtra(BatteryManager.EXTRA_SCALE, 100) ?: 100
        val status = intent?.getIntExtra(BatteryManager.EXTRA_STATUS, -1) ?: -1
        val temp = (intent?.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, 0) ?: 0) / 10.0

        return JSONObject().apply {
            put("level", if (scale > 0) (level * 100) / scale else level)
            put("status", when (status) {
                BatteryManager.BATTERY_STATUS_CHARGING -> "charging"
                BatteryManager.BATTERY_STATUS_FULL -> "full"
                BatteryManager.BATTERY_STATUS_DISCHARGING -> "discharging"
                BatteryManager.BATTERY_STATUS_NOT_CHARGING -> "not_charging"
                else -> "unknown"
            })
            put("temperature", temp)
            put("plugged", intent?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0)
        }
    }

    // ── network ──────────────────────────────────────────────────────────
    private fun network(): Response = jsonOk(networkJson())

    private fun networkJson(): JSONObject {
        val wifi = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
        val wifiInfo = wifi?.connectionInfo

        return JSONObject().apply {
            put("wifi_ssid", wifiInfo?.ssid?.removeSurrounding("\"") ?: "")
            put("wifi_rssi", wifiInfo?.rssi ?: 0)
            put("wifi_link_speed", wifiInfo?.linkSpeed ?: 0)
            put("wifi_frequency", wifiInfo?.frequency ?: 0)
            put("ip_addresses", getIpAddresses())
        }
    }

    private fun getIpAddresses(): JSONArray {
        val addrs = JSONArray()
        try {
            for (intf in NetworkInterface.getNetworkInterfaces()) {
                if (!intf.isUp || intf.isLoopback) continue
                for (addr in intf.inetAddresses) {
                    if (addr.isLoopbackAddress) continue
                    val ip = addr.hostAddress ?: continue
                    // Skip IPv6 link-local
                    if (ip.contains("%")) continue
                    addrs.put(JSONObject().apply {
                        put("interface", intf.name)
                        put("address", ip)
                        put("is_ipv6", ip.contains(":"))
                    })
                }
            }
        } catch (_: Exception) {}
        return addrs
    }

    // ── storage ──────────────────────────────────────────────────────────
    private fun storage(): Response = jsonOk(storageJson())

    private fun storageJson(): JSONObject {
        val ext = StatFs(Environment.getExternalStorageDirectory().path)
        val data = StatFs(Environment.getDataDirectory().path)

        return JSONObject().apply {
            put("external_total", ext.totalBytes)
            put("external_free", ext.availableBytes)
            put("internal_total", data.totalBytes)
            put("internal_free", data.availableBytes)
        }
    }

    // ── system properties ────────────────────────────────────────────────
    private fun sysProps(params: Map<String, String>): Response {
        val filter = params["filter"] ?: ""

        val result = Runtime.getRuntime().exec(arrayOf("getprop")).let { proc ->
            proc.waitFor()
            proc.inputStream.bufferedReader().readText()
        }

        val props = JSONObject()
        for (line in result.lines()) {
            val match = Regex("""\[(.+?)]: \[(.*)]\s*""").matchEntire(line) ?: continue
            val key = match.groupValues[1]
            val value = match.groupValues[2]
            if (filter.isEmpty() || key.contains(filter, ignoreCase = true)) {
                props.put(key, value)
            }
        }

        return jsonOk(props)
    }

    // ── permissions ──────────────────────────────────────────────────────
    private fun permissions(): Response {
        val granted = JSONArray()
        val denied = JSONArray()

        val importantPerms = listOf(
            "android.permission.MANAGE_EXTERNAL_STORAGE",
            "android.permission.READ_CONTACTS",
            "android.permission.WRITE_CONTACTS",
            "android.permission.READ_SMS",
            "android.permission.READ_CALL_LOG",
            "android.permission.PACKAGE_USAGE_STATS",
            "android.permission.REQUEST_INSTALL_PACKAGES",
            "android.permission.POST_NOTIFICATIONS",
            "android.permission.READ_EXTERNAL_STORAGE",
            "android.permission.WRITE_EXTERNAL_STORAGE",
        )

        for (perm in importantPerms) {
            val status = context.checkSelfPermission(perm)
            if (status == android.content.pm.PackageManager.PERMISSION_GRANTED) {
                granted.put(perm)
            } else {
                denied.put(perm)
            }
        }

        // Check special permissions
        val isDeviceAdmin = try {
            val dpm = context.getSystemService(Context.DEVICE_POLICY_SERVICE)
                as? android.app.admin.DevicePolicyManager
            val adminComponent = android.content.ComponentName(
                context, "com.adbtoolkit.agent.services.AgentDeviceAdmin"
            )
            dpm?.isAdminActive(adminComponent) ?: false
        } catch (_: Exception) { false }

        val hasStorageAccess = if (Build.VERSION.SDK_INT >= 30) {
            Environment.isExternalStorageManager()
        } else true

        return jsonOk(JSONObject().apply {
            put("granted", granted)
            put("denied", denied)
            put("manage_storage", hasStorageAccess)
            put("device_admin", isDeviceAdmin)
            put("accessibility", isAccessibilityEnabled())
        })
    }

    private fun isAccessibilityEnabled(): Boolean {
        return try {
            val setting = android.provider.Settings.Secure.getString(
                context.contentResolver,
                android.provider.Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
            ) ?: ""
            setting.contains("com.adbtoolkit.agent")
        } catch (_: Exception) { false }
    }

    // ── screenshot ───────────────────────────────────────────────────────
    private fun screenshot(): Response {
        // Use screencap command
        val proc = Runtime.getRuntime().exec(arrayOf("screencap", "-p"))
        val bytes = proc.inputStream.readBytes()
        proc.waitFor()

        if (bytes.isEmpty()) return jsonError("Screenshot failed")

        return NanoHTTPD.newFixedLengthResponse(
            Response.Status.OK,
            "image/png",
            bytes.inputStream(),
            bytes.size.toLong()
        ).apply {
            addHeader("Content-Disposition", "inline; filename=\"screenshot.png\"")
        }
    }
}
