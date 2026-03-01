package com.adbtoolkit.agent.python

import android.content.Context
import android.util.Log
import java.io.File
import java.io.InputStream
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Manages an embedded Python runtime on the device.
 *
 * Bootstraps a minimal Python 3 installation under app-private storage:
 *   /data/data/com.adbtoolkit.agent/files/python/
 *
 * Approach:
 *  1. Extract prebuilt Python binaries from assets or download them
 *  2. Set up pip (ensurepip / get-pip.py)
 *  3. Install pyaccelerate from PyPI
 *  4. Provide exec/pip/runScript methods for the PythonApi
 *
 * The actual Python binaries come from the Termux bootstrap packages,
 * repackaged as .zip in assets/python/ (arch-specific: arm64, arm, x86_64).
 */
class PythonRuntime private constructor(private val context: Context) {

    companion object {
        private const val TAG = "PythonRuntime"
        private const val PYTHON_DIR = "python"
        private const val SCRIPTS_DIR = "scripts"

        @Volatile
        private var instance: PythonRuntime? = null

        fun getInstance(context: Context): PythonRuntime {
            return instance ?: synchronized(this) {
                instance ?: PythonRuntime(context.applicationContext).also { instance = it }
            }
        }
    }

    /** Root directory: /data/data/<pkg>/files/python */
    val homeDir: File = File(context.filesDir, PYTHON_DIR)

    /** Scripts directory: /data/data/<pkg>/files/python/scripts */
    private val scriptsDir = File(homeDir, SCRIPTS_DIR)

    /** Python binary path (null if not installed). */
    val pythonBin: String?
        get() {
            val bin = File(homeDir, "usr/bin/python3")
            return if (bin.exists() && bin.canExecute()) bin.absolutePath else null
        }

    /** pip binary path. */
    val pipBin: String?
        get() {
            val bin = File(homeDir, "usr/bin/pip3")
            return if (bin.exists()) bin.absolutePath else null
        }

    val isReady: Boolean get() = pythonBin != null
    val hasPip: Boolean get() = pipBin != null

    @Volatile
    var isInstalling: Boolean = false
        private set

    val pythonVersion: String?
        get() {
            val bin = pythonBin ?: return null
            return try {
                val result = execInternal(bin, listOf("--version"), 10)
                result.stdout.trim().removePrefix("Python ")
            } catch (_: Exception) { null }
        }

    val pyaccelerateVersion: String?
        get() {
            if (!isReady || !hasPip) return null
            return try {
                val result = pip("show", "pyaccelerate")
                if (result.exitCode == 0) {
                    result.stdout.lines().find { it.startsWith("Version:") }
                        ?.substringAfter("Version:")?.trim()
                } else null
            } catch (_: Exception) { null }
        }

    // ═════════════════════════════════════════════════════════════════
    //  BOOTSTRAP
    // ═════════════════════════════════════════════════════════════════

    /**
     * Extract and set up the Python environment.
     * Blocking — call from background thread.
     */
    fun bootstrap() {
        if (isReady) {
            Log.i(TAG, "Python already installed")
            return
        }
        if (isInstalling) {
            Log.w(TAG, "Bootstrap already in progress")
            return
        }

        isInstalling = true
        try {
            Log.i(TAG, "Starting Python bootstrap...")

            // Create directories
            homeDir.mkdirs()
            scriptsDir.mkdirs()

            // Extract Python from assets (arch-specific)
            val arch = System.getProperty("os.arch") ?: "aarch64"
            val archDir = when {
                arch.contains("aarch64") || arch.contains("arm64") -> "arm64"
                arch.contains("arm") -> "arm"
                arch.contains("x86_64") || arch.contains("amd64") -> "x86_64"
                arch.contains("x86") || arch.contains("i686") -> "x86"
                else -> "arm64"
            }

            Log.i(TAG, "Architecture: $arch → $archDir")

            // Try to extract from assets
            val bootstrapFile = "python/python-$archDir.zip"
            try {
                val input = context.assets.open(bootstrapFile)
                extractZip(input, homeDir)
                Log.i(TAG, "Extracted Python from assets")
            } catch (e: Exception) {
                Log.w(TAG, "No bundled Python for $archDir, attempting pkg install", e)
                // Fallback: use system pkg-compatible method
                installViaPkg()
            }

            // Set executable permissions
            File(homeDir, "usr/bin").listFiles()?.forEach {
                it.setExecutable(true, false)
            }

            // Extract bundled scripts from assets
            extractScripts()

            // Install pip if not present
            if (!hasPip) {
                installPip()
            }

            // Install pyaccelerate
            if (hasPip) {
                pip("install", "--upgrade", "pyaccelerate")
            }

            Log.i(TAG, "Python bootstrap complete: ${pythonVersion}")
        } catch (e: Exception) {
            Log.e(TAG, "Bootstrap failed", e)
            throw e
        } finally {
            isInstalling = false
        }
    }

    private fun installPip() {
        val bin = pythonBin ?: return
        Log.i(TAG, "Installing pip via ensurepip...")
        val result = execInternal(bin, listOf("-m", "ensurepip", "--upgrade"), 120)
        if (result.exitCode != 0) {
            Log.w(TAG, "ensurepip failed (${result.exitCode}), trying get-pip.py")
            // Download get-pip.py as fallback
            try {
                val getPipFile = File(homeDir, "get-pip.py")
                val url = java.net.URL("https://bootstrap.pypa.io/get-pip.py")
                url.openStream().use { input ->
                    getPipFile.outputStream().use { output -> input.copyTo(output) }
                }
                execInternal(bin, listOf(getPipFile.absolutePath), 120)
            } catch (e: Exception) {
                Log.e(TAG, "get-pip.py also failed", e)
            }
        }
    }

    private fun installViaPkg() {
        // Try to use a Termux-compatible bootstrap package
        // This is a placeholder — in production, we'd download from our CDN
        Log.w(TAG, "pkg-based install not available yet")
    }

    private fun extractScripts() {
        try {
            context.assets.list("python/scripts")?.forEach { name ->
                val input = context.assets.open("python/scripts/$name")
                val dest = File(scriptsDir, name)
                input.use { inp ->
                    dest.outputStream().use { out -> inp.copyTo(out) }
                }
                dest.setExecutable(true, false)
            }
            Log.i(TAG, "Scripts extracted to ${scriptsDir.absolutePath}")
        } catch (e: Exception) {
            Log.d(TAG, "No bundled scripts: ${e.message}")
        }
    }

    @Suppress("DEPRECATION")
    private fun extractZip(input: InputStream, destDir: File) {
        val zip = java.util.zip.ZipInputStream(input)
        var entry = zip.nextEntry
        while (entry != null) {
            val file = File(destDir, entry.name)
            if (entry.isDirectory) {
                file.mkdirs()
            } else {
                file.parentFile?.mkdirs()
                file.outputStream().use { out ->
                    zip.copyTo(out)
                }
            }
            zip.closeEntry()
            entry = zip.nextEntry
        }
        zip.close()
    }

    // ═════════════════════════════════════════════════════════════════
    //  EXECUTION
    // ═════════════════════════════════════════════════════════════════

    /**
     * Execute arbitrary Python code.
     */
    fun exec(code: String, timeoutSeconds: Int = 60): ExecResult {
        val bin = pythonBin ?: return ExecResult(-1, "", "Python not installed", 0)
        return execInternal(bin, listOf("-c", code), timeoutSeconds)
    }

    /**
     * Run a Python script file.
     */
    fun runScript(script: File, args: List<String> = emptyList(), timeoutSeconds: Int = 300): ExecResult {
        val bin = pythonBin ?: return ExecResult(-1, "", "Python not installed", 0)
        return execInternal(bin, listOf(script.absolutePath) + args, timeoutSeconds)
    }

    /**
     * Run pip commands.
     */
    fun pip(vararg args: String): ExecResult {
        val bin = pythonBin ?: return ExecResult(-1, "", "Python not installed", 0)
        return execInternal(bin, listOf("-m", "pip") + args.toList(), 120)
    }

    /**
     * Get a bundled or user script by name.
     */
    fun getScript(name: String): File? {
        // Check user scripts first
        val userScript = File(scriptsDir, name)
        if (userScript.exists()) return userScript

        // Check with .py extension
        val withExt = File(scriptsDir, "$name.py")
        if (withExt.exists()) return withExt

        return null
    }

    private fun execInternal(binary: String, args: List<String>, timeoutSeconds: Int): ExecResult {
        val start = System.currentTimeMillis()

        val env = buildMap {
            put("HOME", homeDir.absolutePath)
            put("PYTHONHOME", File(homeDir, "usr").absolutePath)
            put("PYTHONPATH", File(homeDir, "usr/lib/python3.12").absolutePath)
            put("LD_LIBRARY_PATH", "${File(homeDir, "usr/lib").absolutePath}:/system/lib64")
            put("PATH", "${File(homeDir, "usr/bin").absolutePath}:${System.getenv("PATH")}")
            put("TMPDIR", context.cacheDir.absolutePath)
            put("LANG", "en_US.UTF-8")
        }

        val pb = ProcessBuilder(listOf(binary) + args)
            .directory(homeDir)
            .redirectErrorStream(false)

        pb.environment().putAll(env)

        val process = pb.start()

        val stdout = process.inputStream.bufferedReader().readText()
        val stderr = process.errorStream.bufferedReader().readText()

        val finished = process.waitFor(timeoutSeconds.toLong(), TimeUnit.SECONDS)
        val exitCode = if (finished) process.exitValue() else {
            process.destroyForcibly()
            -999 // timeout
        }

        val duration = System.currentTimeMillis() - start
        return ExecResult(exitCode, stdout, stderr, duration)
    }
}

/** Result of a Python execution. */
data class ExecResult(
    val exitCode: Int,
    val stdout: String,
    val stderr: String,
    val durationMs: Long,
)
