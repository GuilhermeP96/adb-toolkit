package com.adbtoolkit.agent.api

import android.content.Context
import android.os.Environment
import android.os.StatFs
import fi.iki.elonen.NanoHTTPD
import fi.iki.elonen.NanoHTTPD.Response
import com.adbtoolkit.agent.server.AgentServer.Companion.jsonOk
import com.adbtoolkit.agent.server.AgentServer.Companion.jsonError
import com.adbtoolkit.agent.server.AgentServer.Companion.jsonResponse
import org.json.JSONArray
import org.json.JSONObject
import java.io.*
import java.security.MessageDigest

/**
 * File system API — replaces `adb pull`, `adb push`, `find`, `stat`.
 *
 * Endpoints:
 *   GET  /api/files/list?path=...&recursive=false
 *   GET  /api/files/read?path=...             → raw binary stream
 *   POST /api/files/write?path=...            → body = bytes
 *   GET  /api/files/stat?path=...
 *   GET  /api/files/hash?path=...&algo=sha256
 *   POST /api/files/mkdir?path=...
 *   POST /api/files/delete?path=...
 *   GET  /api/files/exists?path=...
 *   GET  /api/files/storage                   → storage stats
 *   GET  /api/files/search?path=...&pattern=...
 */
class FileApi(private val context: Context) {

    fun handle(
        method: NanoHTTPD.Method,
        parts: List<String>,
        session: NanoHTTPD.IHTTPSession,
    ): Response {
        val action = parts.getOrNull(0) ?: ""
        val params = session.parms

        return when (action) {
            "list"    -> list(params)
            "read"    -> read(params)
            "write"   -> write(params, session)
            "stat"    -> stat(params)
            "hash"    -> hash(params)
            "mkdir"   -> mkdir(params)
            "delete"  -> delete(params)
            "exists"  -> exists(params)
            "storage" -> storage()
            "search"  -> search(params)
            else      -> jsonError("Unknown file action: $action")
        }
    }

    // ── list ─────────────────────────────────────────────────────────────
    private fun list(params: Map<String, String>): Response {
        val path = params["path"] ?: return jsonError("Missing 'path'")
        val recursive = params["recursive"]?.toBoolean() ?: false
        val dir = File(path)

        if (!dir.exists()) return jsonError("Path not found: $path", Response.Status.NOT_FOUND)
        if (!dir.isDirectory) return jsonError("Not a directory: $path")

        val files = JSONArray()
        val items = if (recursive) dir.walkTopDown().filter { it != dir } else dir.listFiles()?.asSequence() ?: emptySequence()

        for (f in items) {
            files.put(JSONObject().apply {
                put("name", f.name)
                put("path", f.absolutePath)
                put("is_dir", f.isDirectory)
                put("size", if (f.isFile) f.length() else 0)
                put("modified", f.lastModified())
                put("readable", f.canRead())
                put("writable", f.canWrite())
            })
        }

        return jsonOk(JSONObject().apply {
            put("path", dir.absolutePath)
            put("count", files.length())
            put("files", files)
        })
    }

    // ── read (binary download) ───────────────────────────────────────────
    private fun read(params: Map<String, String>): Response {
        val path = params["path"] ?: return jsonError("Missing 'path'")
        val file = File(path)

        if (!file.exists()) return jsonError("Not found: $path", Response.Status.NOT_FOUND)
        if (!file.isFile)   return jsonError("Not a file: $path")

        val fis = FileInputStream(file)
        val mimeType = guessMime(file.name)
        return NanoHTTPD.newFixedLengthResponse(
            Response.Status.OK, mimeType, fis, file.length()
        ).apply {
            addHeader("Content-Disposition", "attachment; filename=\"${file.name}\"")
            addHeader("X-File-Size", file.length().toString())
            addHeader("X-File-Modified", file.lastModified().toString())
        }
    }

    // ── write (binary upload) ────────────────────────────────────────────
    private fun write(params: Map<String, String>, session: NanoHTTPD.IHTTPSession): Response {
        val path = params["path"] ?: return jsonError("Missing 'path'")
        val file = File(path)

        file.parentFile?.mkdirs()

        // Read body bytes
        val contentLength = session.headers["content-length"]?.toLongOrNull() ?: 0L
        val tmpFiles = mutableMapOf<String, String>()
        session.parseBody(tmpFiles)

        // NanoHTTPD puts the body in a temp file for large uploads
        val bodyFile = tmpFiles["content"]
        if (bodyFile != null) {
            File(bodyFile).copyTo(file, overwrite = true)
        } else {
            // Small body — read directly from input stream
            session.inputStream.use { input ->
                FileOutputStream(file).use { output ->
                    input.copyTo(output)
                }
            }
        }

        return jsonOk(JSONObject().apply {
            put("path", file.absolutePath)
            put("size", file.length())
            put("written", true)
        })
    }

    // ── stat ─────────────────────────────────────────────────────────────
    private fun stat(params: Map<String, String>): Response {
        val path = params["path"] ?: return jsonError("Missing 'path'")
        val file = File(path)

        if (!file.exists()) return jsonError("Not found: $path", Response.Status.NOT_FOUND)

        return jsonOk(JSONObject().apply {
            put("path", file.absolutePath)
            put("name", file.name)
            put("is_dir", file.isDirectory)
            put("is_file", file.isFile)
            put("size", if (file.isFile) file.length() else dirSize(file))
            put("modified", file.lastModified())
            put("readable", file.canRead())
            put("writable", file.canWrite())
            put("hidden", file.isHidden)
            put("parent", file.parent)
        })
    }

    // ── hash ─────────────────────────────────────────────────────────────
    private fun hash(params: Map<String, String>): Response {
        val path = params["path"] ?: return jsonError("Missing 'path'")
        val algo = params["algo"] ?: "sha256"
        val file = File(path)

        if (!file.isFile) return jsonError("Not a file: $path")

        val digest = MessageDigest.getInstance(algo.uppercase().replace("SHA", "SHA-"))
        val buffer = ByteArray(8192)
        FileInputStream(file).use { fis ->
            var n: Int
            while (fis.read(buffer).also { n = it } != -1) {
                digest.update(buffer, 0, n)
            }
        }
        val hex = digest.digest().joinToString("") { "%02x".format(it) }

        return jsonOk(JSONObject().apply {
            put("path", file.absolutePath)
            put("algorithm", algo)
            put("hash", hex)
            put("size", file.length())
        })
    }

    // ── mkdir ────────────────────────────────────────────────────────────
    private fun mkdir(params: Map<String, String>): Response {
        val path = params["path"] ?: return jsonError("Missing 'path'")
        val dir = File(path)
        val created = dir.mkdirs()
        return jsonOk(mapOf("path" to dir.absolutePath, "created" to created))
    }

    // ── delete ───────────────────────────────────────────────────────────
    private fun delete(params: Map<String, String>): Response {
        val path = params["path"] ?: return jsonError("Missing 'path'")
        val file = File(path)
        if (!file.exists()) return jsonError("Not found: $path", Response.Status.NOT_FOUND)

        val deleted = if (file.isDirectory) file.deleteRecursively() else file.delete()
        return jsonOk(mapOf("path" to file.absolutePath, "deleted" to deleted))
    }

    // ── exists ───────────────────────────────────────────────────────────
    private fun exists(params: Map<String, String>): Response {
        val path = params["path"] ?: return jsonError("Missing 'path'")
        val file = File(path)
        return jsonOk(JSONObject().apply {
            put("path", file.absolutePath)
            put("exists", file.exists())
            put("is_file", file.isFile)
            put("is_dir", file.isDirectory)
        })
    }

    // ── storage ──────────────────────────────────────────────────────────
    private fun storage(): Response {
        val stat = StatFs(Environment.getExternalStorageDirectory().path)
        val total = stat.totalBytes
        val free = stat.availableBytes

        val internal = StatFs(Environment.getDataDirectory().path)

        return jsonOk(JSONObject().apply {
            put("external_total", total)
            put("external_free", free)
            put("external_used", total - free)
            put("internal_total", internal.totalBytes)
            put("internal_free", internal.availableBytes)
            put("external_path", Environment.getExternalStorageDirectory().absolutePath)
        })
    }

    // ── search ───────────────────────────────────────────────────────────
    private fun search(params: Map<String, String>): Response {
        val basePath = params["path"] ?: "/sdcard"
        val pattern = params["pattern"] ?: return jsonError("Missing 'pattern'")
        val maxResults = params["max"]?.toIntOrNull() ?: 500
        val regex = Regex(pattern, RegexOption.IGNORE_CASE)

        val results = JSONArray()
        val baseDir = File(basePath)
        var count = 0

        if (baseDir.exists() && baseDir.isDirectory) {
            for (file in baseDir.walkTopDown()) {
                if (count >= maxResults) break
                if (regex.containsMatchIn(file.name)) {
                    results.put(JSONObject().apply {
                        put("path", file.absolutePath)
                        put("name", file.name)
                        put("is_dir", file.isDirectory)
                        put("size", if (file.isFile) file.length() else 0)
                    })
                    count++
                }
            }
        }

        return jsonOk(JSONObject().apply {
            put("base", basePath)
            put("pattern", pattern)
            put("count", results.length())
            put("results", results)
        })
    }

    // ── helpers ──────────────────────────────────────────────────────────
    private fun dirSize(dir: File): Long =
        dir.walkTopDown().filter { it.isFile }.sumOf { it.length() }

    private fun guessMime(name: String): String = when {
        name.endsWith(".json") -> "application/json"
        name.endsWith(".xml")  -> "application/xml"
        name.endsWith(".txt") || name.endsWith(".log") -> "text/plain"
        name.endsWith(".html") -> "text/html"
        name.endsWith(".jpg") || name.endsWith(".jpeg") -> "image/jpeg"
        name.endsWith(".png")  -> "image/png"
        name.endsWith(".mp4")  -> "video/mp4"
        name.endsWith(".apk")  -> "application/vnd.android.package-archive"
        name.endsWith(".db") || name.endsWith(".sqlite") -> "application/x-sqlite3"
        else -> "application/octet-stream"
    }
}
