package com.adbtoolkit.agent.api

import android.content.Context
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.os.Build
import fi.iki.elonen.NanoHTTPD
import fi.iki.elonen.NanoHTTPD.Response
import com.adbtoolkit.agent.server.AgentServer.Companion.jsonOk
import com.adbtoolkit.agent.server.AgentServer.Companion.jsonError
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileInputStream

/**
 * Package / App management API.
 *
 * Endpoints:
 *   GET  /api/apps/list?third_party=true
 *   GET  /api/apps/info/<package>
 *   GET  /api/apps/apk/<package>          → raw APK stream
 *   GET  /api/apps/data-paths/<package>   → accessible data dirs
 *   POST /api/apps/install                → body = APK bytes
 *   POST /api/apps/uninstall/<package>
 */
class AppApi(private val context: Context) {

    private val pm: PackageManager get() = context.packageManager

    fun handle(
        method: NanoHTTPD.Method,
        parts: List<String>,
        session: NanoHTTPD.IHTTPSession,
    ): Response {
        val action = parts.getOrNull(0) ?: ""
        val param = parts.getOrNull(1) ?: session.parms["package"] ?: ""

        return when (action) {
            "list"       -> listApps(session.parms)
            "info"       -> appInfo(param)
            "apk"        -> downloadApk(param)
            "data-paths" -> dataPaths(param)
            "install"    -> installApk(session)
            "uninstall"  -> uninstallApp(param)
            else -> jsonError("Unknown apps action: $action")
        }
    }

    // ── list ─────────────────────────────────────────────────────────────
    private fun listApps(params: Map<String, String>): Response {
        val thirdPartyOnly = params["third_party"]?.toBoolean() ?: true
        val flags = PackageManager.GET_META_DATA

        val packages = pm.getInstalledPackages(flags)
        val apps = JSONArray()

        for (pkg in packages) {
            val isSystem = (pkg.applicationInfo?.flags ?: 0) and ApplicationInfo.FLAG_SYSTEM != 0
            if (thirdPartyOnly && isSystem) continue

            val appInfo = pkg.applicationInfo
            apps.put(JSONObject().apply {
                put("package", pkg.packageName)
                put("name", appInfo?.let { pm.getApplicationLabel(it).toString() } ?: pkg.packageName)
                put("version_name", pkg.versionName ?: "")
                put("version_code", if (Build.VERSION.SDK_INT >= 28) pkg.longVersionCode else pkg.versionCode.toLong())
                put("target_sdk", appInfo?.targetSdkVersion ?: 0)
                put("min_sdk", appInfo?.minSdkVersion ?: 0)
                put("is_system", isSystem)
                put("apk_path", appInfo?.sourceDir ?: "")
                put("data_dir", appInfo?.dataDir ?: "")
                put("installed", pkg.firstInstallTime)
                put("updated", pkg.lastUpdateTime)
            })
        }

        return jsonOk(JSONObject().apply {
            put("count", apps.length())
            put("apps", apps)
        })
    }

    // ── info ─────────────────────────────────────────────────────────────
    private fun appInfo(pkg: String): Response {
        if (pkg.isEmpty()) return jsonError("Missing package name")

        val info = try {
            pm.getPackageInfo(pkg, PackageManager.GET_PERMISSIONS or PackageManager.GET_META_DATA)
        } catch (_: PackageManager.NameNotFoundException) {
            return jsonError("Package not found: $pkg", Response.Status.NOT_FOUND)
        }

        val appInfo = info.applicationInfo
        val permissions = info.requestedPermissions ?: emptyArray()

        // Accessible data directories
        val dataDirs = mutableListOf<String>()
        for (base in listOf("/sdcard/Android/data", "/sdcard/Android/media")) {
            val dir = File("$base/$pkg")
            if (dir.exists() && dir.canRead()) dataDirs.add(dir.absolutePath)
        }

        return jsonOk(JSONObject().apply {
            put("package", pkg)
            put("name", appInfo?.let { pm.getApplicationLabel(it).toString() } ?: pkg)
            put("version_name", info.versionName ?: "")
            put("version_code", if (Build.VERSION.SDK_INT >= 28) info.longVersionCode else info.versionCode.toLong())
            put("apk_path", appInfo?.sourceDir ?: "")
            put("split_apks", JSONArray((appInfo?.splitSourceDirs ?: emptyArray()).toList()))
            put("data_dir", appInfo?.dataDir ?: "")
            put("accessible_data_dirs", JSONArray(dataDirs))
            put("permissions", JSONArray(permissions.toList()))
            put("target_sdk", appInfo?.targetSdkVersion ?: 0)
            put("installed", info.firstInstallTime)
            put("updated", info.lastUpdateTime)
        })
    }

    // ── download APK ─────────────────────────────────────────────────────
    private fun downloadApk(pkg: String): Response {
        if (pkg.isEmpty()) return jsonError("Missing package name")

        val appInfo = try {
            pm.getApplicationInfo(pkg, 0)
        } catch (_: PackageManager.NameNotFoundException) {
            return jsonError("Package not found: $pkg", Response.Status.NOT_FOUND)
        }

        val apkFile = File(appInfo.sourceDir)
        if (!apkFile.exists()) return jsonError("APK file not found")

        val fis = FileInputStream(apkFile)
        return NanoHTTPD.newFixedLengthResponse(
            Response.Status.OK,
            "application/vnd.android.package-archive",
            fis,
            apkFile.length()
        ).apply {
            addHeader("Content-Disposition", "attachment; filename=\"$pkg.apk\"")
            addHeader("X-File-Size", apkFile.length().toString())
        }
    }

    // ── data paths ───────────────────────────────────────────────────────
    private fun dataPaths(pkg: String): Response {
        if (pkg.isEmpty()) return jsonError("Missing package name")

        val paths = JSONArray()
        for (base in listOf(
            "/sdcard/Android/data",
            "/sdcard/Android/media",
            "/storage/emulated/0/Android/data",
            "/storage/emulated/0/Android/media",
        )) {
            val dir = File("$base/$pkg")
            if (dir.exists() && dir.canRead()) {
                var totalSize = 0L
                var fileCount = 0
                dir.walkTopDown().filter { it.isFile }.forEach {
                    totalSize += it.length()
                    fileCount++
                }
                paths.put(JSONObject().apply {
                    put("path", dir.absolutePath)
                    put("size", totalSize)
                    put("file_count", fileCount)
                    put("readable", dir.canRead())
                    put("writable", dir.canWrite())
                })
            }
        }

        return jsonOk(JSONObject().apply {
            put("package", pkg)
            put("paths", paths)
        })
    }

    // ── install APK ──────────────────────────────────────────────────────
    private fun installApk(session: NanoHTTPD.IHTTPSession): Response {
        // Save uploaded APK to temp
        val tmpFiles = mutableMapOf<String, String>()
        session.parseBody(tmpFiles)
        val bodyFile = tmpFiles["content"]
            ?: return jsonError("No APK data in request body")

        val apkFile = File(context.cacheDir, "install_tmp.apk")
        File(bodyFile).copyTo(apkFile, overwrite = true)

        // Use pm install via shell (we have shell access as app)
        val result = Runtime.getRuntime().exec(
            arrayOf("pm", "install", "-r", "-g", apkFile.absolutePath)
        ).apply { waitFor() }

        val output = result.inputStream.bufferedReader().readText()
        val success = result.exitValue() == 0
        apkFile.delete()

        return if (success) {
            jsonOk(mapOf("installed" to true, "output" to output.trim()))
        } else {
            val err = result.errorStream.bufferedReader().readText()
            jsonError("Install failed: ${output.trim()} $err")
        }
    }

    // ── uninstall ────────────────────────────────────────────────────────
    private fun uninstallApp(pkg: String): Response {
        if (pkg.isEmpty()) return jsonError("Missing package name")

        val result = Runtime.getRuntime().exec(
            arrayOf("pm", "uninstall", pkg)
        ).apply { waitFor() }

        val output = result.inputStream.bufferedReader().readText()
        return jsonOk(mapOf(
            "package" to pkg,
            "uninstalled" to (result.exitValue() == 0),
            "output" to output.trim()
        ))
    }
}
