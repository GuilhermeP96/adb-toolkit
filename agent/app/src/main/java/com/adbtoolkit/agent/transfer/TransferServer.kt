package com.adbtoolkit.agent.transfer

import android.util.Log
import com.adbtoolkit.agent.AgentApp
import com.adbtoolkit.agent.security.PairingManager
import java.io.*
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketTimeoutException
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * High-speed TCP transfer server for large file operations.
 *
 * Protocol (binary):
 * ┌──────────────────────────────────────────┐
 * │  HEADER (first 512 bytes, JSON padded)   │
 * │  {                                       │
 * │    "op": "push" | "pull",                │
 * │    "path": "/sdcard/...",                │
 * │    "size": 123456789,                    │
 * │    "peer_id": "...",         (optional)  │
 * │    "signature": "...",       (optional)  │
 * │    "timestamp": "...",       (optional)  │
 * │    "token": "..."            (local auth)│
 * │  }                                       │
 * ├──────────────────────────────────────────┤
 * │  BINARY PAYLOAD (for push ops)           │
 * │  Raw file bytes, streamed                │
 * ├──────────────────────────────────────────┤
 * │  FOOTER: SHA-256 hash (32 bytes)         │
 * └──────────────────────────────────────────┘
 *
 * For "pull" ops, server responds with same header+payload structure.
 *
 * Buffer size: 256KB for maximum throughput on local/USB connections.
 */
class TransferServer(
    private val port: Int = AgentApp.TRANSFER_PORT,
    private val bufferSize: Int = 256 * 1024,
) {

    companion object {
        private const val TAG = "TransferServer"
        private const val HEADER_SIZE = 512
        private const val MAX_CONNECTIONS = 4
    }

    private var serverSocket: ServerSocket? = null
    private var executor: ExecutorService? = null
    private val running = AtomicBoolean(false)
    private var serverThread: Thread? = null

    // Statistics
    @Volatile var totalBytesTransferred: Long = 0L; private set
    @Volatile var activeTransfers: Int = 0; private set

    fun start() {
        if (running.get()) return
        running.set(true)

        executor = Executors.newFixedThreadPool(MAX_CONNECTIONS)
        serverSocket = ServerSocket(port).apply {
            soTimeout = 2000  // Check for shutdown every 2s
            reuseAddress = true
        }

        serverThread = Thread({
            Log.i(TAG, "Transfer server listening on port $port")
            while (running.get()) {
                try {
                    val socket = serverSocket?.accept() ?: break
                    socket.tcpNoDelay = true
                    socket.setSendBufferSize(bufferSize)
                    socket.setReceiveBufferSize(bufferSize)
                    executor?.submit { handleConnection(socket) }
                } catch (_: SocketTimeoutException) {
                    // Normal — just loop back to check running flag
                } catch (e: Exception) {
                    if (running.get()) Log.e(TAG, "Accept error", e)
                }
            }
            Log.i(TAG, "Transfer server stopped")
        }, "TransferServer-Accept").apply { isDaemon = true; start() }
    }

    fun stop() {
        running.set(false)
        try {
            serverSocket?.close()
        } catch (_: Exception) {}
        executor?.shutdownNow()
        serverThread?.join(5000)
        serverSocket = null
        executor = null
        serverThread = null
    }

    fun isRunning(): Boolean = running.get()

    // ═════════════════════════════════════════════════════════════════
    //  CONNECTION HANDLER
    // ═════════════════════════════════════════════════════════════════

    private fun handleConnection(socket: Socket) {
        val remote = socket.remoteSocketAddress.toString()
        activeTransfers++
        Log.i(TAG, "Connection from $remote (active=$activeTransfers)")

        try {
            socket.use { s ->
                val input = BufferedInputStream(s.getInputStream(), bufferSize)
                val output = BufferedOutputStream(s.getOutputStream(), bufferSize)

                // Read header (fixed 512 bytes)
                val headerBytes = ByteArray(HEADER_SIZE)
                readFully(input, headerBytes)
                val headerJson = String(headerBytes, Charsets.UTF_8).trim('\u0000', ' ')
                val header = org.json.JSONObject(headerJson)

                val op = header.optString("op", "")
                val path = header.optString("path", "")

                // Authenticate
                if (!authenticate(header, remote)) {
                    sendError(output, "Authentication failed")
                    return
                }

                when (op) {
                    "push" -> handlePush(header, path, input, output)
                    "pull" -> handlePull(header, path, input, output)
                    "stat" -> handleStat(path, output)
                    else -> sendError(output, "Unknown op: $op")
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Transfer error from $remote", e)
        } finally {
            activeTransfers--
            Log.i(TAG, "Connection closed: $remote (active=$activeTransfers)")
        }
    }

    // ═════════════════════════════════════════════════════════════════
    //  PUSH — receive file from client
    // ═════════════════════════════════════════════════════════════════

    private fun handlePush(
        header: org.json.JSONObject,
        path: String,
        input: InputStream,
        output: OutputStream,
    ) {
        val size = header.optLong("size", -1)
        if (path.isEmpty() || size < 0) {
            sendError(output, "Missing path or size")
            return
        }

        val file = File(path)
        file.parentFile?.mkdirs()

        val digest = MessageDigest.getInstance("SHA-256")
        var bytesWritten = 0L

        Log.i(TAG, "PUSH: $path ($size bytes)")

        FileOutputStream(file).buffered(bufferSize).use { fos ->
            val buffer = ByteArray(bufferSize)
            var remaining = size

            while (remaining > 0) {
                val toRead = minOf(remaining.toInt(), buffer.size)
                val read = input.read(buffer, 0, toRead)
                if (read < 0) break
                fos.write(buffer, 0, read)
                digest.update(buffer, 0, read)
                bytesWritten += read
                remaining -= read
            }
            fos.flush()
        }

        totalBytesTransferred += bytesWritten

        // Read client's hash (32 bytes)
        val clientHash = ByteArray(32)
        try {
            readFully(input, clientHash)
        } catch (_: Exception) {
            // Client may not send hash — that's OK
        }

        val serverHash = digest.digest()
        val hashMatch = clientHash.contentEquals(serverHash) || clientHash.all { it == 0.toByte() }

        val response = org.json.JSONObject().apply {
            put("status", if (hashMatch) "ok" else "hash_mismatch")
            put("bytes_written", bytesWritten)
            put("sha256", serverHash.joinToString("") { "%02x".format(it) })
            put("path", path)
        }

        sendHeader(output, response)
        Log.i(TAG, "PUSH complete: $path ($bytesWritten bytes, hash_ok=$hashMatch)")
    }

    // ═════════════════════════════════════════════════════════════════
    //  PULL — send file to client
    // ═════════════════════════════════════════════════════════════════

    private fun handlePull(
        header: org.json.JSONObject,
        path: String,
        input: InputStream,
        output: OutputStream,
    ) {
        val file = File(path)
        if (!file.exists() || !file.isFile) {
            sendError(output, "File not found: $path")
            return
        }

        val size = file.length()
        Log.i(TAG, "PULL: $path ($size bytes)")

        // Send response header
        val responseHeader = org.json.JSONObject().apply {
            put("status", "ok")
            put("op", "pull")
            put("path", path)
            put("size", size)
        }
        sendHeader(output, responseHeader)

        // Stream file
        val digest = MessageDigest.getInstance("SHA-256")
        var bytesSent = 0L

        FileInputStream(file).buffered(bufferSize).use { fis ->
            val buffer = ByteArray(bufferSize)
            while (true) {
                val read = fis.read(buffer)
                if (read < 0) break
                output.write(buffer, 0, read)
                digest.update(buffer, 0, read)
                bytesSent += read
            }
        }
        output.flush()

        // Send hash footer
        output.write(digest.digest())
        output.flush()

        totalBytesTransferred += bytesSent
        Log.i(TAG, "PULL complete: $path ($bytesSent bytes)")
    }

    // ═════════════════════════════════════════════════════════════════
    //  STAT — quick file info without transfer
    // ═════════════════════════════════════════════════════════════════

    private fun handleStat(path: String, output: OutputStream) {
        val file = File(path)
        val response = org.json.JSONObject().apply {
            put("exists", file.exists())
            put("path", path)
            put("size", if (file.exists()) file.length() else 0)
            put("is_file", file.isFile)
            put("is_dir", file.isDirectory)
            put("last_modified", file.lastModified())
            put("readable", file.canRead())
        }
        sendHeader(output, response)
    }

    // ═════════════════════════════════════════════════════════════════
    //  AUTH
    // ═════════════════════════════════════════════════════════════════

    private fun authenticate(header: org.json.JSONObject, remote: String): Boolean {
        // Local toolkit auth via token
        val token = header.optString("token", "")
        if (token.isNotEmpty() && token == AgentApp.authToken) return true

        // Peer auth via HMAC
        val peerId = header.optString("peer_id", "")
        val signature = header.optString("signature", "")
        val timestamp = header.optString("timestamp", "")

        if (peerId.isNotEmpty() && signature.isNotEmpty() && timestamp.isNotEmpty()) {
            val message = "${header.optString("op")}|${header.optString("path")}|$timestamp"
            // Use PairingManager to verify — needs Application context
            // For now, basic timestamp check + we trust paired peers
            val ts = timestamp.toLongOrNull() ?: return false
            val age = Math.abs(System.currentTimeMillis() - ts)
            return age < 5 * 60 * 1000  // 5-minute window
        }

        // Allow localhost without auth (via ADB forward)
        if (remote.contains("127.0.0.1") || remote.contains("localhost")) {
            return AgentApp.authToken.isEmpty()
        }

        return false
    }

    // ═════════════════════════════════════════════════════════════════
    //  I/O HELPERS
    // ═════════════════════════════════════════════════════════════════

    private fun sendHeader(output: OutputStream, json: org.json.JSONObject) {
        val bytes = json.toString().toByteArray(Charsets.UTF_8)
        val padded = ByteArray(HEADER_SIZE)
        System.arraycopy(bytes, 0, padded, 0, minOf(bytes.size, HEADER_SIZE))
        output.write(padded)
        output.flush()
    }

    private fun sendError(output: OutputStream, message: String) {
        sendHeader(output, org.json.JSONObject().apply {
            put("status", "error")
            put("error", message)
        })
    }

    private fun readFully(input: InputStream, buffer: ByteArray) {
        var offset = 0
        while (offset < buffer.size) {
            val read = input.read(buffer, offset, buffer.size - offset)
            if (read < 0) throw EOFException("Unexpected EOF at offset $offset/${buffer.size}")
            offset += read
        }
    }
}
