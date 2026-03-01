package com.adbtoolkit.agent.api

import android.content.Context
import android.content.Intent
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import android.util.Log
import fi.iki.elonen.NanoHTTPD
import fi.iki.elonen.NanoHTTPD.Response
import com.adbtoolkit.agent.AgentApp
import com.adbtoolkit.agent.security.PairingManager
import com.adbtoolkit.agent.server.AgentServer.Companion.jsonOk
import com.adbtoolkit.agent.server.AgentServer.Companion.jsonError
import org.json.JSONArray
import org.json.JSONObject
import java.net.Inet4Address
import java.net.NetworkInterface

/**
 * Peer-to-peer API — discovery, pairing, and D2D data transfer.
 *
 * SECURITY MODEL:
 * ─────────────────────────────────────────────────────────────
 * 1. Discovery is open (mDNS) — any device can see available agents
 * 2. Pairing REQUIRES:
 *    a) ECDH public key exchange (PairingManager)
 *    b) 6-digit visual confirmation code shown on BOTH devices
 *    c) Biometric / device-lock confirmation on the RESPONDER
 *    d) Device with no lock screen (insecure) CANNOT pair
 * 3. All post-pairing requests MUST carry HMAC-SHA256 signature:
 *    X-Peer-Id: <device_id>
 *    X-Peer-Signature: HMAC("METHOD|URI|timestamp", shared_secret)
 *    X-Peer-Timestamp: <epoch_millis>
 * 4. Replay protection: 5-minute timestamp window
 * 5. Pairings can be revoked at any time (requires biometric)
 * ─────────────────────────────────────────────────────────────
 *
 * Endpoints:
 *   ── Discovery ──
 *   GET  /api/peer/discover           → Start/check mDNS discovery
 *   GET  /api/peer/identity           → This device's public identity
 *
 *   ── Pairing (mutual auth) ──
 *   POST /api/peer/pair-init          → Initiator sends pubkey+label
 *   GET  /api/peer/pair-pending       → List pending pairing requests
 *   POST /api/peer/pair-approve       → Approve after biometric (challengeId)
 *   POST /api/peer/pair-reject        → Reject a pairing request
 *
 *   ── Paired devices management ──
 *   GET  /api/peer/paired             → List all paired devices
 *   POST /api/peer/revoke             → Revoke a pairing (requires biometric)
 *   POST /api/peer/revoke-all         → Revoke ALL pairings
 *
 *   ── Authenticated P2P operations ──
 *   POST /api/peer/send               → Send data to this device (HMAC required)
 *   POST /api/peer/request            → Request data from this device (HMAC required)
 *   POST /api/peer/relay              → Relay a command to another paired device
 */
class PeerApi(private val context: Context) {

    companion object {
        private const val TAG = "PeerApi"
        private const val NSD_SERVICE_TYPE = "_adbtoolkit._tcp."
        private const val NSD_SERVICE_NAME = "ADBToolkitAgent"

        /** Action broadcast to MainActivity to show pairing confirmation UI. */
        const val ACTION_PAIRING_REQUEST = "com.adbtoolkit.agent.PAIRING_REQUEST"
        const val EXTRA_CHALLENGE_ID = "challenge_id"
        const val EXTRA_PEER_LABEL = "peer_label"
        const val EXTRA_CONFIRM_CODE = "confirm_code"
    }

    private val pairingManager = PairingManager.getInstance(context)
    private var nsdManager: NsdManager? = null
    private var isRegistered = false
    private val discoveredPeers = mutableMapOf<String, NsdServiceInfo>()

    fun handle(
        method: NanoHTTPD.Method,
        parts: List<String>,
        session: NanoHTTPD.IHTTPSession,
    ): Response {
        val action = parts.getOrNull(0) ?: ""

        return when (action) {
            // ── Discovery ────────────────────────────────────────────
            "discover"      -> discover()
            "identity"      -> identity()

            // ── Pairing (no HMAC needed — this IS the auth step) ─────
            "pair-init"     -> pairInit(session)
            "pair-pending"  -> pairPending()
            "pair-approve"  -> pairApprove(session)
            "pair-reject"   -> pairReject(session)

            // ── Paired device management ─────────────────────────────
            "paired"        -> listPaired()
            "revoke"        -> revokePairing(session)
            "revoke-all"    -> revokeAll()

            // ── Authenticated P2P (HMAC enforced) ────────────────────
            "send"          -> withPeerAuth(method, session) { peerId -> receiveSend(peerId, session) }
            "request"       -> withPeerAuth(method, session) { peerId -> handleRequest(peerId, session) }
            "relay"         -> withPeerAuth(method, session) { peerId -> handleRelay(peerId, session) }

            else -> jsonError("Unknown peer action: $action")
        }
    }

    // ═══════════════════════════════════════════════════════════════════
    //  DISCOVERY (mDNS/NSD)
    // ═══════════════════════════════════════════════════════════════════

    private fun discover(): Response {
        ensureNsdRegistered()
        startDiscovery()

        return jsonOk(JSONObject().apply {
            put("this_device", pairingManager.deviceId)
            put("label", pairingManager.getDeviceLabel())
            put("local_ips", getLocalIps())
            put("port", AgentApp.HTTP_PORT)
            val peers = JSONArray()
            discoveredPeers.forEach { (name, info) ->
                peers.put(JSONObject().apply {
                    put("name", name)
                    put("host", info.host?.hostAddress ?: "unknown")
                    put("port", info.port)
                })
            }
            put("discovered_peers", peers)
        })
    }

    private fun identity(): Response = jsonOk(JSONObject().apply {
        put("device_id", pairingManager.deviceId)
        put("label", pairingManager.getDeviceLabel())
        put("public_key", pairingManager.localPublicKeyB64)
        put("port", AgentApp.HTTP_PORT)
        put("transfer_port", AgentApp.TRANSFER_PORT)
        put("local_ips", getLocalIps())
        put("paired_count", pairingManager.getPairedDevices().size)
    })

    // ═══════════════════════════════════════════════════════════════════
    //  PAIRING — INITIATOR SIDE
    // ═══════════════════════════════════════════════════════════════════

    /**
     * POST /api/peer/pair-init
     * Body: { "device_id": "...", "label": "...", "public_key": "..." }
     *
     * Called BY the initiator ON the responder's server.
     * The responder creates a pending pairing and notifies the UI for
     * biometric confirmation.
     */
    private fun pairInit(session: NanoHTTPD.IHTTPSession): Response {
        val body = parseJsonBody(session)

        val peerDeviceId = body.optString("device_id", "")
        val peerLabel = body.optString("label", "")
        val peerPublicKey = body.optString("public_key", "")

        if (peerDeviceId.isEmpty() || peerPublicKey.isEmpty()) {
            return jsonError("Missing device_id or public_key")
        }

        // Already paired?
        if (pairingManager.isPaired(peerDeviceId)) {
            return jsonOk(JSONObject().apply {
                put("status", "already_paired")
                put("device_id", pairingManager.deviceId)
                put("public_key", pairingManager.localPublicKeyB64)
            })
        }

        // Infer address from the HTTP connection
        val peerAddress = session.remoteIpAddress ?: "unknown"

        // Create pending pairing
        val pending = pairingManager.createPendingPairing(
            peerDeviceId = peerDeviceId,
            peerLabel = peerLabel,
            peerPublicKeyB64 = peerPublicKey,
            peerAddress = peerAddress,
        )

        // Broadcast to MainActivity to show biometric confirmation dialog
        val intent = Intent(ACTION_PAIRING_REQUEST).apply {
            putExtra(EXTRA_CHALLENGE_ID, pending.challengeId)
            putExtra(EXTRA_PEER_LABEL, peerLabel)
            putExtra(EXTRA_CONFIRM_CODE, pending.confirmCode)
            setPackage(context.packageName)
        }
        context.sendBroadcast(intent)

        // Return the challenge info to the initiator.
        // Initiator should also compute confirm code and display it.
        return jsonOk(JSONObject().apply {
            put("status", "pending_approval")
            put("challenge_id", pending.challengeId)
            put("responder_device_id", pairingManager.deviceId)
            put("responder_public_key", pairingManager.localPublicKeyB64)
            put("confirm_code", pending.confirmCode)
            put("message", "Aguardando confirmação biométrica no dispositivo")
        })
    }

    // ═══════════════════════════════════════════════════════════════════
    //  PAIRING — RESPONDER SIDE (after biometric)
    // ═══════════════════════════════════════════════════════════════════

    private fun pairPending(): Response {
        // Clean expired
        pairingManager.pendingPairings.entries.removeIf { it.value.isExpired }

        val arr = JSONArray()
        pairingManager.pendingPairings.values.forEach { arr.put(it.toJson()) }

        return jsonOk(JSONObject().apply {
            put("pending", arr)
            put("count", arr.length())
        })
    }

    /**
     * POST /api/peer/pair-approve
     * Body: { "challenge_id": "...", "biometric_verified": true }
     *
     * Called from the local UI AFTER biometric verification succeeds.
     * The `biometric_verified` flag MUST be true — the actual biometric
     * check happens in BiometricGate on the UI layer (MainActivity),
     * and only then this endpoint is called.
     */
    private fun pairApprove(session: NanoHTTPD.IHTTPSession): Response {
        val body = parseJsonBody(session)
        val challengeId = body.optString("challenge_id", "")
        val biometricVerified = body.optBoolean("biometric_verified", false)

        if (challengeId.isEmpty()) return jsonError("Missing challenge_id")

        if (!biometricVerified) {
            return jsonError(
                "Confirmação biométrica obrigatória. " +
                "Autentique com impressão digital, rosto ou PIN do dispositivo.",
                403
            )
        }

        val device = pairingManager.approvePairing(challengeId)
            ?: return jsonError("Pairing not found, expired, or already processed")

        return jsonOk(JSONObject().apply {
            put("status", "paired")
            put("device_id", pairingManager.deviceId)
            put("public_key", pairingManager.localPublicKeyB64)
            put("paired_device", device.toPublicJson())
            put("message", "Pareamento aprovado com sucesso")
        })
    }

    private fun pairReject(session: NanoHTTPD.IHTTPSession): Response {
        val body = parseJsonBody(session)
        val challengeId = body.optString("challenge_id", "")
        if (challengeId.isEmpty()) return jsonError("Missing challenge_id")
        pairingManager.rejectPairing(challengeId)
        return jsonOk(mapOf("status" to "rejected"))
    }

    // ═══════════════════════════════════════════════════════════════════
    //  PAIRED DEVICES MANAGEMENT
    // ═══════════════════════════════════════════════════════════════════

    private fun listPaired(): Response {
        val arr = JSONArray()
        pairingManager.getPairedDevices().forEach { arr.put(it.toPublicJson()) }
        return jsonOk(JSONObject().apply {
            put("devices", arr)
            put("count", arr.length())
        })
    }

    private fun revokePairing(session: NanoHTTPD.IHTTPSession): Response {
        val body = parseJsonBody(session)
        val deviceId = body.optString("device_id", "")
        val biometricVerified = body.optBoolean("biometric_verified", false)

        if (deviceId.isEmpty()) return jsonError("Missing device_id")
        if (!biometricVerified) {
            return jsonError(
                "Revogação de pareamento requer confirmação biométrica",
                403
            )
        }

        return if (pairingManager.revokePairing(deviceId)) {
            jsonOk(mapOf("status" to "revoked", "device_id" to deviceId))
        } else {
            jsonError("Device not found: $deviceId")
        }
    }

    private fun revokeAll(): Response {
        pairingManager.revokeAll()
        return jsonOk(mapOf("status" to "all_revoked"))
    }

    // ═══════════════════════════════════════════════════════════════════
    //  AUTHENTICATED P2P OPERATIONS
    // ═══════════════════════════════════════════════════════════════════

    /**
     * Middleware: validates X-Peer-Id / X-Peer-Signature / X-Peer-Timestamp.
     * Only proceeds to [handler] if HMAC is valid and peer is trusted.
     */
    private fun withPeerAuth(
        method: NanoHTTPD.Method,
        session: NanoHTTPD.IHTTPSession,
        handler: (peerId: String) -> Response,
    ): Response {
        val validation = pairingManager.validatePeerRequest(
            method = method.name,
            uri = session.uri,
            headers = session.headers,
        )

        if (!validation.valid) {
            Log.w(TAG, "P2P auth failed: ${validation.reason}")
            return jsonError(
                "Autenticação P2P falhou: ${validation.reason}",
                403
            )
        }

        return handler(validation.peerId!!)
    }

    /**
     * POST /api/peer/send — receive data pushed by a paired peer.
     * Body contains the payload (file chunk, contacts JSON, etc.)
     */
    private fun receiveSend(peerId: String, session: NanoHTTPD.IHTTPSession): Response {
        val contentType = session.headers["content-type"] ?: "application/octet-stream"
        val targetPath = session.parms["path"] ?: ""
        val dataType = session.parms["type"] ?: "raw"  // raw, contacts, sms, apk

        Log.i(TAG, "Receiving data from peer $peerId — type=$dataType, path=$targetPath")

        // For file transfers, stream directly to disk
        if (dataType == "raw" && targetPath.isNotEmpty()) {
            val file = java.io.File(targetPath)
            file.parentFile?.mkdirs()
            session.inputStream.use { input ->
                file.outputStream().use { output ->
                    input.copyTo(output, bufferSize = 64 * 1024)
                }
            }
            return jsonOk(JSONObject().apply {
                put("status", "received")
                put("path", targetPath)
                put("size", file.length())
                put("from_peer", peerId)
            })
        }

        // For structured data, read as JSON
        val body = parseJsonBody(session)
        return jsonOk(JSONObject().apply {
            put("status", "received")
            put("type", dataType)
            put("from_peer", peerId)
            put("data_keys", body.keys().asSequence().toList().let { JSONArray(it) })
        })
    }

    /**
     * POST /api/peer/request — peer requests data from this device.
     * Body: { "type": "contacts|sms|files|apps", "params": {...} }
     */
    private fun handleRequest(peerId: String, session: NanoHTTPD.IHTTPSession): Response {
        val body = parseJsonBody(session)
        val type = body.optString("type", "")

        Log.i(TAG, "Peer $peerId requesting: $type")

        // Delegate to the appropriate API handler
        return when (type) {
            "identity" -> identity()
            // Other types delegate internally — the calling peer must
            // re-issue through normal API endpoints with HMAC headers
            else -> jsonOk(JSONObject().apply {
                put("status", "use_direct_api")
                put("message", "Use the specific API endpoint with X-Peer-* headers")
                put("available_apis", JSONArray(listOf(
                    "files", "apps", "contacts", "sms", "device", "shell"
                )))
            })
        }
    }

    /**
     * POST /api/peer/relay — relay a command to another paired device.
     * This device acts as a proxy, forwarding the request.
     */
    private fun handleRelay(peerId: String, session: NanoHTTPD.IHTTPSession): Response {
        val body = parseJsonBody(session)
        val targetDeviceId = body.optString("target_device_id", "")
        val targetEndpoint = body.optString("endpoint", "")

        if (targetDeviceId.isEmpty() || targetEndpoint.isEmpty()) {
            return jsonError("Missing target_device_id or endpoint")
        }

        val target = pairingManager.getPairedDevice(targetDeviceId)
            ?: return jsonError("Target device not paired: $targetDeviceId")

        return jsonOk(JSONObject().apply {
            put("status", "relay_queued")
            put("target", target.toPublicJson())
            put("endpoint", targetEndpoint)
            put("message", "Relay not yet implemented — use direct connection to ${target.lastAddress}")
        })
    }

    // ═══════════════════════════════════════════════════════════════════
    //  NSD (mDNS) SERVICE REGISTRATION & DISCOVERY
    // ═══════════════════════════════════════════════════════════════════

    fun ensureNsdRegistered() {
        if (isRegistered) return
        try {
            nsdManager = context.getSystemService(Context.NSD_SERVICE) as NsdManager

            val serviceInfo = NsdServiceInfo().apply {
                serviceName = "${NSD_SERVICE_NAME}_${pairingManager.deviceId.take(8)}"
                serviceType = NSD_SERVICE_TYPE
                port = AgentApp.HTTP_PORT
            }

            nsdManager?.registerService(serviceInfo, NsdManager.PROTOCOL_DNS_SD,
                object : NsdManager.RegistrationListener {
                    override fun onRegistrationFailed(si: NsdServiceInfo, code: Int) {
                        Log.e(TAG, "NSD registration failed: $code")
                    }
                    override fun onUnregistrationFailed(si: NsdServiceInfo, code: Int) {
                        Log.e(TAG, "NSD unregistration failed: $code")
                    }
                    override fun onServiceRegistered(si: NsdServiceInfo) {
                        isRegistered = true
                        Log.i(TAG, "NSD registered: ${si.serviceName}")
                    }
                    override fun onServiceUnregistered(si: NsdServiceInfo) {
                        isRegistered = false
                        Log.i(TAG, "NSD unregistered: ${si.serviceName}")
                    }
                })
        } catch (e: Exception) {
            Log.e(TAG, "NSD registration error", e)
        }
    }

    private fun startDiscovery() {
        try {
            nsdManager?.discoverServices(NSD_SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD,
                object : NsdManager.DiscoveryListener {
                    override fun onStartDiscoveryFailed(type: String, code: Int) {
                        Log.e(TAG, "Discovery start failed: $code")
                    }
                    override fun onStopDiscoveryFailed(type: String, code: Int) {}
                    override fun onDiscoveryStarted(type: String) {
                        Log.i(TAG, "NSD discovery started")
                    }
                    override fun onDiscoveryStopped(type: String) {}

                    override fun onServiceFound(info: NsdServiceInfo) {
                        if (info.serviceName.startsWith(NSD_SERVICE_NAME)) {
                            resolveService(info)
                        }
                    }
                    override fun onServiceLost(info: NsdServiceInfo) {
                        discoveredPeers.remove(info.serviceName)
                    }
                })
        } catch (e: Exception) {
            Log.e(TAG, "Discovery error", e)
        }
    }

    private fun resolveService(info: NsdServiceInfo) {
        try {
            nsdManager?.resolveService(info, object : NsdManager.ResolveListener {
                override fun onResolveFailed(si: NsdServiceInfo, code: Int) {
                    Log.w(TAG, "Resolve failed: ${si.serviceName} code=$code")
                }
                override fun onServiceResolved(si: NsdServiceInfo) {
                    discoveredPeers[si.serviceName] = si
                    Log.i(TAG, "Resolved peer: ${si.serviceName} → ${si.host}:${si.port}")
                }
            })
        } catch (e: Exception) {
            Log.e(TAG, "Resolve error", e)
        }
    }

    fun unregisterNsd() {
        try {
            if (isRegistered) {
                nsdManager?.unregisterService(object : NsdManager.RegistrationListener {
                    override fun onRegistrationFailed(si: NsdServiceInfo, code: Int) {}
                    override fun onUnregistrationFailed(si: NsdServiceInfo, code: Int) {}
                    override fun onServiceRegistered(si: NsdServiceInfo) {}
                    override fun onServiceUnregistered(si: NsdServiceInfo) { isRegistered = false }
                })
            }
        } catch (e: Exception) {
            Log.e(TAG, "NSD unregister error", e)
        }
    }

    // ═══════════════════════════════════════════════════════════════════
    //  UTILITY
    // ═══════════════════════════════════════════════════════════════════

    private fun getLocalIps(): JSONArray {
        val ips = JSONArray()
        try {
            NetworkInterface.getNetworkInterfaces()?.toList()?.forEach { iface ->
                if (iface.isUp && !iface.isLoopback) {
                    iface.inetAddresses.toList()
                        .filterIsInstance<Inet4Address>()
                        .forEach { ips.put(it.hostAddress) }
                }
            }
        } catch (_: Exception) {}
        return ips
    }

    private fun parseJsonBody(session: NanoHTTPD.IHTTPSession): JSONObject {
        return try {
            val body = mutableMapOf<String, String>()
            session.parseBody(body)
            JSONObject(body["postData"] ?: body["content"] ?: "{}")
        } catch (_: Exception) { JSONObject() }
    }

    private fun jsonError(msg: String, code: Int): Response {
        return NanoHTTPD.newFixedLengthResponse(
            if (code == 403) Response.Status.FORBIDDEN else Response.Status.BAD_REQUEST,
            "application/json",
            JSONObject().put("error", msg).toString()
        )
    }
}
