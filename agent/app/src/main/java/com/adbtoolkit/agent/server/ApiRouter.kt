package com.adbtoolkit.agent.server

import android.content.Context
import android.util.Log
import com.adbtoolkit.agent.api.*
import com.adbtoolkit.agent.security.PairingManager
import fi.iki.elonen.NanoHTTPD
import fi.iki.elonen.NanoHTTPD.Response

/**
 * Routes incoming HTTP requests to the appropriate API handler.
 *
 * URL scheme: `/api/<domain>/<action>[/<param>]`
 *
 * Security layers:
 * - Local/ADB requests: authenticated via X-Agent-Token
 * - Peer-to-peer requests: authenticated via X-Peer-Id + X-Peer-Signature (HMAC)
 * - Pairing endpoints: open (they ARE the authentication step)
 */
class ApiRouter(context: Context) {

    companion object {
        private const val TAG = "ApiRouter"
    }

    private val fileApi = FileApi(context)
    private val appApi = AppApi(context)
    private val contactsApi = ContactsApi(context)
    private val smsApi = SmsApi(context)
    private val deviceApi = DeviceApi(context)
    private val shellApi = ShellApi(context)
    private val pythonApi = PythonApi(context)
    private val peerApi = PeerApi(context)
    private val orchestratorApi = OrchestratorApi(context)
    private val pairingManager = PairingManager.getInstance(context)

    fun route(
        method: NanoHTTPD.Method,
        uri: String,
        session: NanoHTTPD.IHTTPSession,
    ): Response {
        // Normalise path
        val path = uri.trimEnd('/')
        val parts = path.removePrefix("/api/").split("/", limit = 3)
        val domain = parts.getOrNull(0)

        // ── Peer-authenticated requests ──────────────────────────
        // If request carries X-Peer-Id header, validate HMAC before
        // allowing access to data APIs (but NOT pairing endpoints).
        val peerId = session.headers["x-peer-id"]
        if (peerId != null && domain !in listOf("ping", "peer")) {
            val validation = pairingManager.validatePeerRequest(
                method = method.name,
                uri = uri,
                headers = session.headers,
            )
            if (!validation.valid) {
                Log.w(TAG, "Peer auth rejected: ${validation.reason}")
                return AgentServer.jsonError(
                    "Autenticação P2P falhou: ${validation.reason}",
                    Response.Status.FORBIDDEN
                )
            }
            Log.d(TAG, "Peer auth OK: $peerId → $path")
        }

        return when (domain) {
            "ping"         -> AgentServer.jsonOk(mapOf(
                "status" to "alive",
                "version" to "1.0.0",
                "python_ready" to pythonApi.isReady(),
                "device_id" to pairingManager.deviceId,
                "paired_count" to pairingManager.getPairedDevices().size,
            ))

            "files"        -> fileApi.handle(method, parts.drop(1), session)
            "apps"         -> appApi.handle(method, parts.drop(1), session)
            "contacts"     -> contactsApi.handle(method, parts.drop(1), session)
            "sms"          -> smsApi.handle(method, parts.drop(1), session)
            "device"       -> deviceApi.handle(method, parts.drop(1), session)
            "shell"        -> shellApi.handle(method, parts.drop(1), session)
            "python"       -> pythonApi.handle(method, parts.drop(1), session)
            "peer"         -> peerApi.handle(method, parts.drop(1), session)
            "orchestrator" -> orchestratorApi.handle(method, parts.drop(1), session)

            else -> AgentServer.jsonError("Unknown endpoint: $path",
                Response.Status.NOT_FOUND)
        }
    }
}
