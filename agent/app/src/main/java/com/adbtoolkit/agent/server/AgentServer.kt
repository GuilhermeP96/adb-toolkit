package com.adbtoolkit.agent.server

import android.content.Context
import android.util.Log
import com.adbtoolkit.agent.AgentApp
import fi.iki.elonen.NanoHTTPD
import org.json.JSONObject

/**
 * Embedded HTTP server (NanoHTTPD).
 *
 * Listens on [AgentApp.HTTP_PORT] and dispatches requests to [ApiRouter].
 * All requests require the auth token header `X-Agent-Token` unless running
 * over `adb forward` on localhost.
 */
class AgentServer(
    private val context: Context,
    port: Int = AgentApp.HTTP_PORT
) : NanoHTTPD(port) {

    companion object {
        private const val TAG = "AgentServer"
        private const val AUTH_HEADER = "X-Agent-Token"

        fun jsonResponse(
            status: Response.Status,
            data: Any?,
        ): Response {
            val json = when (data) {
                is JSONObject -> data.toString()
                is Map<*, *> -> JSONObject(data as Map<String, Any?>).toString()
                is String -> data
                else -> JSONObject(mapOf("result" to data)).toString()
            }
            return newFixedLengthResponse(status, "application/json", json)
        }

        fun jsonOk(data: Any? = mapOf("status" to "ok")) = jsonResponse(Response.Status.OK, data)

        fun jsonError(msg: String, status: Response.Status = Response.Status.BAD_REQUEST) =
            jsonResponse(status, mapOf("error" to msg))
    }

    private val router = ApiRouter(context)

    override fun serve(session: IHTTPSession): Response {
        val uri = session.uri ?: "/"
        val method = session.method

        Log.d(TAG, "${method.name} $uri from ${session.remoteIpAddress}")

        // --- Auth check (skip for /api/ping — health check) -----------------
        if (uri != "/api/ping") {
            val token = session.headers[AUTH_HEADER.lowercase()]
                ?: session.parms["token"]
            val expected = AgentApp.authToken

            if (expected.isNotEmpty() && token != expected) {
                return jsonResponse(Response.Status.UNAUTHORIZED, mapOf(
                    "error" to "unauthorized",
                    "message" to "Missing or invalid X-Agent-Token"
                ))
            }
        }

        return try {
            router.route(method, uri, session)
        } catch (e: Exception) {
            Log.e(TAG, "Error handling $uri", e)
            jsonResponse(Response.Status.INTERNAL_ERROR, mapOf(
                "error" to "internal_error",
                "message" to e.message
            ))
        }
    }
}
