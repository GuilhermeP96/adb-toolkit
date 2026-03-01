package com.adbtoolkit.agent.api

import android.content.Context
import fi.iki.elonen.NanoHTTPD
import fi.iki.elonen.NanoHTTPD.Response
import com.adbtoolkit.agent.server.AgentServer.Companion.jsonOk
import com.adbtoolkit.agent.server.AgentServer.Companion.jsonError
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.util.concurrent.TimeUnit

/**
 * Shell execution API — runs commands directly on the device.
 *
 * Much faster than `adb shell` since there's no USB/protocol overhead.
 *
 * Endpoints:
 *   POST /api/shell/exec         → body: {"cmd": "...", "timeout": 30}
 *   POST /api/shell/exec-stream  → streaming stdout
 *   GET  /api/shell/getprop?key=...
 *   POST /api/shell/settings     → body: {"namespace": "global", "key": "...", "value": "..."}
 */
class ShellApi(private val context: Context) {

    fun handle(
        method: NanoHTTPD.Method,
        parts: List<String>,
        session: NanoHTTPD.IHTTPSession,
    ): Response {
        val action = parts.getOrNull(0) ?: ""

        return when (action) {
            "exec"        -> exec(session)
            "exec-stream" -> execStream(session)
            "getprop"     -> getprop(session.parms)
            "settings"    -> settings(session)
            else -> jsonError("Unknown shell action: $action")
        }
    }

    // ── exec ─────────────────────────────────────────────────────────────
    private fun exec(session: NanoHTTPD.IHTTPSession): Response {
        val body = parseJsonBody(session)
        val cmd = body.optString("cmd", "")
        val timeoutSec = body.optLong("timeout", 30)

        if (cmd.isEmpty()) return jsonError("Missing 'cmd'")

        return try {
            val proc = Runtime.getRuntime().exec(arrayOf("sh", "-c", cmd))
            val finished = proc.waitFor(timeoutSec, TimeUnit.SECONDS)

            val stdout = proc.inputStream.bufferedReader().readText()
            val stderr = proc.errorStream.bufferedReader().readText()
            val exitCode = if (finished) proc.exitValue() else -1

            if (!finished) proc.destroyForcibly()

            jsonOk(JSONObject().apply {
                put("exit_code", exitCode)
                put("stdout", stdout)
                put("stderr", stderr)
                put("timed_out", !finished)
            })
        } catch (e: Exception) {
            jsonError("Exec failed: ${e.message}")
        }
    }

    // ── exec-stream ──────────────────────────────────────────────────────
    private fun execStream(session: NanoHTTPD.IHTTPSession): Response {
        val body = parseJsonBody(session)
        val cmd = body.optString("cmd", "")
        if (cmd.isEmpty()) return jsonError("Missing 'cmd'")

        val proc = Runtime.getRuntime().exec(arrayOf("sh", "-c", cmd))
        val stream = proc.inputStream

        // Return as chunked stream so the client can read incrementally
        return NanoHTTPD.newChunkedResponse(
            Response.Status.OK,
            "text/plain",
            stream
        )
    }

    // ── getprop ──────────────────────────────────────────────────────────
    private fun getprop(params: Map<String, String>): Response {
        val key = params["key"] ?: return jsonError("Missing 'key'")

        val proc = Runtime.getRuntime().exec(arrayOf("getprop", key))
        proc.waitFor(5, TimeUnit.SECONDS)
        val value = proc.inputStream.bufferedReader().readText().trim()

        return jsonOk(mapOf("key" to key, "value" to value))
    }

    // ── settings ─────────────────────────────────────────────────────────
    private fun settings(session: NanoHTTPD.IHTTPSession): Response {
        val body = parseJsonBody(session)
        val namespace = body.optString("namespace", "global")
        val key = body.optString("key", "")
        val value = body.optString("value", "")

        if (key.isEmpty()) return jsonError("Missing 'key'")

        return if (value.isNotEmpty()) {
            // PUT
            val proc = Runtime.getRuntime().exec(
                arrayOf("settings", "put", namespace, key, value)
            )
            proc.waitFor(5, TimeUnit.SECONDS)
            val err = proc.errorStream.bufferedReader().readText()
            if (err.isNotEmpty()) jsonError(err)
            else jsonOk(mapOf("set" to true, "namespace" to namespace, "key" to key, "value" to value))
        } else {
            // GET
            val proc = Runtime.getRuntime().exec(
                arrayOf("settings", "get", namespace, key)
            )
            proc.waitFor(5, TimeUnit.SECONDS)
            val result = proc.inputStream.bufferedReader().readText().trim()
            jsonOk(mapOf("namespace" to namespace, "key" to key, "value" to result))
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────
    private fun parseJsonBody(session: NanoHTTPD.IHTTPSession): JSONObject {
        return try {
            val body = mutableMapOf<String, String>()
            session.parseBody(body)
            val raw = body["postData"] ?: body["content"] ?: "{}"
            JSONObject(raw)
        } catch (_: Exception) {
            JSONObject()
        }
    }
}
