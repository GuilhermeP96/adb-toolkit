package com.adbtoolkit.agent.api

import android.content.Context
import android.util.Log
import fi.iki.elonen.NanoHTTPD
import fi.iki.elonen.NanoHTTPD.Response
import com.adbtoolkit.agent.python.PythonRuntime
import com.adbtoolkit.agent.server.AgentServer.Companion.jsonOk
import com.adbtoolkit.agent.server.AgentServer.Companion.jsonError
import org.json.JSONObject
import java.io.File

/**
 * Python execution API — runs Python scripts on-device via embedded runtime.
 *
 * Endpoints:
 *   GET  /api/python/status               → runtime status, pyaccelerate version
 *   POST /api/python/setup                → bootstrap Python + pip + pyaccelerate
 *   POST /api/python/exec                 → run arbitrary Python code
 *   POST /api/python/run-script?name=...  → run a bundled script from assets
 *   POST /api/python/pip?package=...      → install pip package
 *   GET  /api/python/packages             → list installed packages
 */
class PythonApi(private val context: Context) {

    companion object {
        private const val TAG = "PythonApi"
    }

    private val runtime = PythonRuntime.getInstance(context)

    fun isReady(): Boolean = runtime.isReady

    fun handle(
        method: NanoHTTPD.Method,
        parts: List<String>,
        session: NanoHTTPD.IHTTPSession,
    ): Response {
        val action = parts.getOrNull(0) ?: ""

        return when (action) {
            "status"     -> status()
            "setup"      -> setup()
            "exec"       -> execPython(session)
            "run-script" -> runScript(session)
            "pip"        -> pip(session)
            "packages"   -> packages()
            else -> jsonError("Unknown python action: $action")
        }
    }

    // ── status ───────────────────────────────────────────────────────────
    private fun status(): Response {
        return jsonOk(JSONObject().apply {
            put("ready", runtime.isReady)
            put("installing", runtime.isInstalling)
            put("python_path", runtime.pythonBin ?: "")
            put("home_dir", runtime.homeDir?.absolutePath ?: "")
            put("version", runtime.pythonVersion ?: "not installed")
            put("pyaccelerate_version", runtime.pyaccelerateVersion ?: "not installed")
            put("pip_available", runtime.hasPip)
        })
    }

    // ── setup ────────────────────────────────────────────────────────────
    private fun setup(): Response {
        if (runtime.isReady) {
            return jsonOk(mapOf("status" to "already_ready", "version" to runtime.pythonVersion))
        }
        if (runtime.isInstalling) {
            return jsonOk(mapOf("status" to "installing"))
        }

        // Start async install
        Thread {
            try {
                runtime.bootstrap()
                Log.i(TAG, "Python runtime ready: ${runtime.pythonVersion}")
            } catch (e: Exception) {
                Log.e(TAG, "Python bootstrap failed", e)
            }
        }.start()

        return jsonOk(mapOf("status" to "setup_started"))
    }

    // ── exec ─────────────────────────────────────────────────────────────
    private fun execPython(session: NanoHTTPD.IHTTPSession): Response {
        if (!runtime.isReady) return jsonError("Python not ready — call /api/python/setup first")

        val body = parseJsonBody(session)
        val code = body.optString("code", "")
        val timeoutSec = body.optInt("timeout", 60)

        if (code.isEmpty()) return jsonError("Missing 'code'")

        val result = runtime.exec(code, timeoutSeconds = timeoutSec)

        return jsonOk(JSONObject().apply {
            put("exit_code", result.exitCode)
            put("stdout", result.stdout)
            put("stderr", result.stderr)
            put("duration_ms", result.durationMs)
        })
    }

    // ── run-script ───────────────────────────────────────────────────────
    private fun runScript(session: NanoHTTPD.IHTTPSession): Response {
        if (!runtime.isReady) return jsonError("Python not ready")

        val params = session.parms
        val name = params["name"] ?: return jsonError("Missing 'name'")
        val argsStr = params["args"] ?: ""

        // Look in bundled scripts first, then user scripts
        val scriptFile = runtime.getScript(name)
            ?: return jsonError("Script not found: $name")

        val args = if (argsStr.isNotEmpty()) argsStr.split(" ") else emptyList()
        val result = runtime.runScript(scriptFile, args, timeoutSeconds = 300)

        return jsonOk(JSONObject().apply {
            put("exit_code", result.exitCode)
            put("stdout", result.stdout)
            put("stderr", result.stderr)
            put("duration_ms", result.durationMs)
            put("script", name)
        })
    }

    // ── pip ──────────────────────────────────────────────────────────────
    private fun pip(session: NanoHTTPD.IHTTPSession): Response {
        if (!runtime.isReady) return jsonError("Python not ready")

        val pkg = session.parms["package"] ?: return jsonError("Missing 'package'")
        val result = runtime.pip("install", pkg)

        return jsonOk(JSONObject().apply {
            put("exit_code", result.exitCode)
            put("stdout", result.stdout)
            put("stderr", result.stderr)
            put("package", pkg)
        })
    }

    // ── packages ─────────────────────────────────────────────────────────
    private fun packages(): Response {
        if (!runtime.isReady) return jsonError("Python not ready")

        val result = runtime.pip("list", "--format=json")
        return if (result.exitCode == 0) {
            NanoHTTPD.newFixedLengthResponse(
                Response.Status.OK, "application/json", result.stdout
            )
        } else {
            jsonError("pip list failed: ${result.stderr}")
        }
    }

    private fun parseJsonBody(session: NanoHTTPD.IHTTPSession): JSONObject {
        return try {
            val body = mutableMapOf<String, String>()
            session.parseBody(body)
            JSONObject(body["postData"] ?: body["content"] ?: "{}")
        } catch (_: Exception) { JSONObject() }
    }
}
