package com.adbtoolkit.agent.api

import android.content.Context
import android.util.Log
import fi.iki.elonen.NanoHTTPD
import fi.iki.elonen.NanoHTTPD.Response
import com.adbtoolkit.agent.security.PairingManager
import com.adbtoolkit.agent.server.AgentServer.Companion.jsonOk
import com.adbtoolkit.agent.server.AgentServer.Companion.jsonError
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.*
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * Orchestrator API — coordinate multi-device operations from any device
 * (phone or PC) acting as the command center.
 *
 * SECURITY: All operations against paired devices use HMAC-signed requests.
 * The orchestrator itself must be an already-paired and trusted device, or
 * the local PC toolkit using its auth token.
 *
 * Endpoints:
 *   GET  /api/orchestrator/topology         → Map of all connected devices
 *   POST /api/orchestrator/dispatch         → Send a command to one device
 *   POST /api/orchestrator/broadcast        → Send command to ALL devices
 *   POST /api/orchestrator/transfer         → Orchestrate D2D transfer
 *   POST /api/orchestrator/deploy-toolkit   → Push agent APK to a device
 *   GET  /api/orchestrator/status           → Orchestrator health + device connectivity
 *   POST /api/orchestrator/sync             → Sync data type across devices
 */
class OrchestratorApi(private val context: Context) {

    companion object {
        private const val TAG = "OrchestratorApi"
        private const val REQUEST_TIMEOUT_MS = 30_000
        private const val HMAC_ALGO = "HmacSHA256"
    }

    private val pairingManager = PairingManager.getInstance(context)
    private val executor: ExecutorService = Executors.newFixedThreadPool(4)

    fun handle(
        method: NanoHTTPD.Method,
        parts: List<String>,
        session: NanoHTTPD.IHTTPSession,
    ): Response {
        val action = parts.getOrNull(0) ?: ""

        return when (action) {
            "topology"       -> topology()
            "dispatch"       -> dispatch(session)
            "broadcast"      -> broadcast(session)
            "transfer"       -> orchestrateTransfer(session)
            "deploy-toolkit" -> deployToolkit(session)
            "status"         -> status()
            "sync"           -> syncData(session)
            else -> jsonError("Unknown orchestrator action: $action")
        }
    }

    // ═══════════════════════════════════════════════════════════════════
    //  TOPOLOGY — full mesh map
    // ═══════════════════════════════════════════════════════════════════

    private fun topology(): Response {
        val devices = pairingManager.getPairedDevices()
        val nodes = JSONArray()

        // This device
        nodes.put(JSONObject().apply {
            put("device_id", pairingManager.deviceId)
            put("label", pairingManager.getDeviceLabel())
            put("role", "self")
            put("reachable", true)
        })

        // Paired peers — probe connectivity in parallel
        val futures = devices.map { device ->
            executor.submit(Callable {
                val reachable = probeDevice(device.lastAddress)
                JSONObject().apply {
                    put("device_id", device.deviceId)
                    put("label", device.label)
                    put("last_address", device.lastAddress)
                    put("paired_at", device.pairedAt)
                    put("last_seen", device.lastSeen)
                    put("trusted", device.trusted)
                    put("reachable", reachable)
                    put("role", "peer")
                }
            })
        }

        futures.forEach { f ->
            try {
                nodes.put(f.get(REQUEST_TIMEOUT_MS.toLong(), TimeUnit.MILLISECONDS))
            } catch (e: Exception) {
                Log.w(TAG, "Topology probe timeout", e)
            }
        }

        return jsonOk(JSONObject().apply {
            put("nodes", nodes)
            put("total", nodes.length())
            put("orchestrator", pairingManager.deviceId)
        })
    }

    // ═══════════════════════════════════════════════════════════════════
    //  DISPATCH — send command to one device
    // ═══════════════════════════════════════════════════════════════════

    /**
     * POST /api/orchestrator/dispatch
     * Body: {
     *   "target_device_id": "...",
     *   "method": "GET|POST",
     *   "endpoint": "/api/device/info",
     *   "body": { ... }         // optional for POST
     * }
     */
    private fun dispatch(session: NanoHTTPD.IHTTPSession): Response {
        val body = parseJsonBody(session)
        val targetId = body.optString("target_device_id", "")
        val httpMethod = body.optString("method", "GET")
        val endpoint = body.optString("endpoint", "")
        val payload = body.optJSONObject("body")

        if (targetId.isEmpty() || endpoint.isEmpty()) {
            return jsonError("Missing target_device_id or endpoint")
        }

        val device = pairingManager.getPairedDevice(targetId)
            ?: return jsonError("Device not paired: $targetId")

        if (device.lastAddress.isEmpty()) {
            return jsonError("No known address for device $targetId")
        }

        return try {
            val result = sendSignedRequest(
                device = device,
                method = httpMethod,
                endpoint = endpoint,
                payload = payload?.toString()
            )
            NanoHTTPD.newFixedLengthResponse(
                Response.Status.OK, "application/json", result
            )
        } catch (e: Exception) {
            jsonError("Dispatch failed: ${e.message}")
        }
    }

    // ═══════════════════════════════════════════════════════════════════
    //  BROADCAST — send to ALL paired devices
    // ═══════════════════════════════════════════════════════════════════

    /**
     * POST /api/orchestrator/broadcast
     * Body: { "method": "GET", "endpoint": "/api/device/info" }
     */
    private fun broadcast(session: NanoHTTPD.IHTTPSession): Response {
        val body = parseJsonBody(session)
        val httpMethod = body.optString("method", "GET")
        val endpoint = body.optString("endpoint", "")
        val payload = body.optJSONObject("body")

        if (endpoint.isEmpty()) return jsonError("Missing endpoint")

        val devices = pairingManager.getPairedDevices().filter { it.trusted }
        val results = JSONObject()

        val futures = devices.map { device ->
            device.deviceId to executor.submit(Callable {
                try {
                    val result = sendSignedRequest(
                        device = device,
                        method = httpMethod,
                        endpoint = endpoint,
                        payload = payload?.toString()
                    )
                    JSONObject(result)
                } catch (e: Exception) {
                    JSONObject().apply {
                        put("error", e.message)
                        put("device_id", device.deviceId)
                    }
                }
            })
        }

        futures.forEach { (deviceId, future) ->
            try {
                results.put(deviceId, future.get(REQUEST_TIMEOUT_MS.toLong(), TimeUnit.MILLISECONDS))
            } catch (e: Exception) {
                results.put(deviceId, JSONObject().put("error", "Timeout: ${e.message}"))
            }
        }

        return jsonOk(JSONObject().apply {
            put("results", results)
            put("device_count", devices.size)
        })
    }

    // ═══════════════════════════════════════════════════════════════════
    //  D2D TRANSFER ORCHESTRATION
    // ═══════════════════════════════════════════════════════════════════

    /**
     * POST /api/orchestrator/transfer
     * Body: {
     *   "source_device_id": "...",
     *   "target_device_id": "...",
     *   "data_type": "contacts|sms|files|apps",
     *   "params": { ... }
     * }
     *
     * The orchestrator tells source to push data directly to target.
     * Both must be paired with the orchestrator, and ideally with each other.
     */
    private fun orchestrateTransfer(session: NanoHTTPD.IHTTPSession): Response {
        val body = parseJsonBody(session)
        val sourceId = body.optString("source_device_id", "")
        val targetId = body.optString("target_device_id", "")
        val dataType = body.optString("data_type", "")
        val params = body.optJSONObject("params") ?: JSONObject()

        if (sourceId.isEmpty() || targetId.isEmpty() || dataType.isEmpty()) {
            return jsonError("Missing source_device_id, target_device_id, or data_type")
        }

        val source = pairingManager.getPairedDevice(sourceId)
            ?: return jsonError("Source not paired: $sourceId")
        val target = pairingManager.getPairedDevice(targetId)
            ?: return jsonError("Target not paired: $targetId")

        // Step 1: Check reachability
        val sourceReachable = probeDevice(source.lastAddress)
        val targetReachable = probeDevice(target.lastAddress)

        if (!sourceReachable) return jsonError("Source device unreachable: ${source.lastAddress}")
        if (!targetReachable) return jsonError("Target device unreachable: ${target.lastAddress}")

        // Step 2: Verify source and target are paired with each other
        //         (they need each other's shared secret for D2D HMAC)
        // If not, we act as relay

        // Step 3: Command the source to export + push data to target
        val transferCommand = JSONObject().apply {
            put("action", "export_and_push")
            put("data_type", dataType)
            put("target_address", target.lastAddress)
            put("target_device_id", target.deviceId)
            put("params", params)
        }

        return try {
            val result = sendSignedRequest(
                device = source,
                method = "POST",
                endpoint = "/api/peer/send",
                payload = transferCommand.toString()
            )

            jsonOk(JSONObject().apply {
                put("status", "transfer_initiated")
                put("source", source.toPublicJson())
                put("target", target.toPublicJson())
                put("data_type", dataType)
                put("result", JSONObject(result))
            })
        } catch (e: Exception) {
            jsonError("Transfer orchestration failed: ${e.message}")
        }
    }

    // ═══════════════════════════════════════════════════════════════════
    //  DEPLOY TOOLKIT
    // ═══════════════════════════════════════════════════════════════════

    /**
     * POST /api/orchestrator/deploy-toolkit
     * Body: { "target_device_id": "..." }
     *
     * Pushes the agent APK + Python scripts to a paired device.
     * The target device must already have the base agent installed
     * (installed initially via ADB sideload or Play Store).
     */
    private fun deployToolkit(session: NanoHTTPD.IHTTPSession): Response {
        val body = parseJsonBody(session)
        val targetId = body.optString("target_device_id", "")

        if (targetId.isEmpty()) return jsonError("Missing target_device_id")

        val target = pairingManager.getPairedDevice(targetId)
            ?: return jsonError("Device not paired: $targetId")

        // Get our own APK path
        val apkPath = context.applicationInfo.sourceDir

        return jsonOk(JSONObject().apply {
            put("status", "deploy_available")
            put("target", target.toPublicJson())
            put("apk_path", apkPath)
            put("apk_size", java.io.File(apkPath).length())
            put("instructions", JSONObject().apply {
                put("step1", "GET /api/apps/download?package=com.adbtoolkit.agent from this device")
                put("step2", "POST /api/peer/send?path=/sdcard/Download/agent.apk&type=raw to target")
                put("step3", "POST /api/apps/install?path=/sdcard/Download/agent.apk on target")
            })
        })
    }

    // ═══════════════════════════════════════════════════════════════════
    //  STATUS
    // ═══════════════════════════════════════════════════════════════════

    private fun status(): Response {
        val devices = pairingManager.getPairedDevices()
        val reachable = devices.count { probeDevice(it.lastAddress) }

        return jsonOk(JSONObject().apply {
            put("orchestrator_id", pairingManager.deviceId)
            put("orchestrator_label", pairingManager.getDeviceLabel())
            put("total_paired", devices.size)
            put("reachable", reachable)
            put("unreachable", devices.size - reachable)
            put("devices", JSONArray().apply {
                devices.forEach { d ->
                    put(JSONObject().apply {
                        put("device_id", d.deviceId)
                        put("label", d.label)
                        put("address", d.lastAddress)
                        put("reachable", probeDevice(d.lastAddress))
                    })
                }
            })
        })
    }

    // ═══════════════════════════════════════════════════════════════════
    //  SYNC — bidirectional data sync across devices
    // ═══════════════════════════════════════════════════════════════════

    /**
     * POST /api/orchestrator/sync
     * Body: {
     *   "device_ids": ["id1", "id2"],   // or "*" for all
     *   "data_type": "contacts|sms",
     *   "direction": "bidirectional|source_to_targets",
     *   "source_device_id": "..." // only if direction=source_to_targets
     * }
     */
    private fun syncData(session: NanoHTTPD.IHTTPSession): Response {
        val body = parseJsonBody(session)
        val dataType = body.optString("data_type", "")
        val direction = body.optString("direction", "source_to_targets")

        if (dataType.isEmpty()) return jsonError("Missing data_type")

        return jsonOk(JSONObject().apply {
            put("status", "sync_planned")
            put("data_type", dataType)
            put("direction", direction)
            put("message", "Sync orchestration will coordinate export/import across paired devices")
            put("security_note", "Todos os dispositivos devem estar pareados e autenticados via HMAC")
        })
    }

    // ═══════════════════════════════════════════════════════════════════
    //  HTTP CLIENT — signed requests to paired devices
    // ═══════════════════════════════════════════════════════════════════

    /**
     * Send an HMAC-signed HTTP request to a paired device.
     * Signs: "METHOD|endpoint|timestamp" with the shared secret.
     */
    private fun sendSignedRequest(
        device: com.adbtoolkit.agent.security.PairedDevice,
        method: String,
        endpoint: String,
        payload: String? = null,
    ): String {
        val address = device.lastAddress
        val baseUrl = if (':' in address) "http://$address" else "http://$address:${com.adbtoolkit.agent.AgentApp.HTTP_PORT}"
        val url = URL("$baseUrl$endpoint")

        val timestamp = System.currentTimeMillis().toString()
        val message = "$method|$endpoint|$timestamp"
        val signature = hmacSign(device.sharedSecret, message)

        val conn = url.openConnection() as HttpURLConnection
        conn.requestMethod = method
        conn.connectTimeout = REQUEST_TIMEOUT_MS
        conn.readTimeout = REQUEST_TIMEOUT_MS
        conn.setRequestProperty("Content-Type", "application/json")
        conn.setRequestProperty("X-Peer-Id", pairingManager.deviceId)
        conn.setRequestProperty("X-Peer-Signature", signature)
        conn.setRequestProperty("X-Peer-Timestamp", timestamp)

        if (payload != null && method == "POST") {
            conn.doOutput = true
            conn.outputStream.use { it.write(payload.toByteArray(Charsets.UTF_8)) }
        }

        return try {
            val responseCode = conn.responseCode
            val body = if (responseCode in 200..299) {
                conn.inputStream.bufferedReader().readText()
            } else {
                conn.errorStream?.bufferedReader()?.readText() ?: """{"error":"HTTP $responseCode"}"""
            }
            body
        } finally {
            conn.disconnect()
        }
    }

    private fun probeDevice(address: String): Boolean {
        if (address.isEmpty()) return false
        return try {
            val baseUrl = if (':' in address) "http://$address" else "http://$address:${com.adbtoolkit.agent.AgentApp.HTTP_PORT}"
            val conn = URL("$baseUrl/api/ping").openConnection() as HttpURLConnection
            conn.connectTimeout = 3000
            conn.readTimeout = 3000
            val code = conn.responseCode
            conn.disconnect()
            code == 200
        } catch (_: Exception) { false }
    }

    private fun hmacSign(secretHex: String, message: String): String {
        val keyBytes = secretHex.chunked(2).map { it.toInt(16).toByte() }.toByteArray()
        val mac = Mac.getInstance(HMAC_ALGO)
        mac.init(SecretKeySpec(keyBytes, HMAC_ALGO))
        val sig = mac.doFinal(message.toByteArray(Charsets.UTF_8))
        return sig.joinToString("") { "%02x".format(it) }
    }

    private fun parseJsonBody(session: NanoHTTPD.IHTTPSession): JSONObject {
        return try {
            val body = mutableMapOf<String, String>()
            session.parseBody(body)
            JSONObject(body["postData"] ?: body["content"] ?: "{}")
        } catch (_: Exception) { JSONObject() }
    }
}
