package com.adbtoolkit.agent.services

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import com.adbtoolkit.agent.AgentApp
import com.adbtoolkit.agent.R
import com.adbtoolkit.agent.api.PeerApi
import com.adbtoolkit.agent.server.AgentServer
import com.adbtoolkit.agent.transfer.TransferServer
import com.adbtoolkit.agent.ui.MainActivity

/**
 * Foreground service that hosts:
 *  1. HTTP API server (NanoHTTPD on port 15555)
 *  2. High-speed TCP transfer server (port 15556)
 *  3. mDNS/NSD peer discovery registration
 *
 * Keeps a partial wake-lock to survive Doze mode.
 */
class AgentService : Service() {

    companion object {
        private const val TAG = "AgentService"
        private const val NOTIFICATION_ID = 1
        const val ACTION_START = "com.adbtoolkit.agent.START"
        const val ACTION_STOP = "com.adbtoolkit.agent.STOP"
        const val EXTRA_TOKEN = "auth_token"
    }

    private var httpServer: AgentServer? = null
    private var transferServer: TransferServer? = null
    private var peerApi: PeerApi? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        Log.i(TAG, "Service created")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopSelf()
                return START_NOT_STICKY
            }
            else -> {
                // Set auth token if provided
                intent?.getStringExtra(EXTRA_TOKEN)?.let { token ->
                    if (token.isNotEmpty()) {
                        AgentApp.authToken = token
                        Log.i(TAG, "Auth token set (${token.length} chars)")
                    }
                }
                startForeground(NOTIFICATION_ID, buildNotification())
                startServers()
                acquireWakeLock()
                return START_STICKY
            }
        }
    }

    override fun onDestroy() {
        stopServers()
        releaseWakeLock()
        Log.i(TAG, "Service destroyed")
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    // ═════════════════════════════════════════════════════════════════
    //  SERVERS
    // ═════════════════════════════════════════════════════════════════

    private fun startServers() {
        // HTTP API server
        if (httpServer == null) {
            try {
                httpServer = AgentServer(this, AgentApp.HTTP_PORT)
                httpServer?.start()
                Log.i(TAG, "HTTP server started on port ${AgentApp.HTTP_PORT}")
            } catch (e: Exception) {
                Log.e(TAG, "Failed to start HTTP server", e)
            }
        }

        // TCP transfer server
        if (transferServer == null) {
            try {
                transferServer = TransferServer(AgentApp.TRANSFER_PORT)
                transferServer?.start()
                Log.i(TAG, "TCP transfer server started on port ${AgentApp.TRANSFER_PORT}")
            } catch (e: Exception) {
                Log.e(TAG, "Failed to start transfer server", e)
            }
        }

        // Register mDNS for peer discovery
        try {
            peerApi = PeerApi(this)
            peerApi?.ensureNsdRegistered()
            Log.i(TAG, "mDNS/NSD registered")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to register NSD", e)
        }
    }

    private fun stopServers() {
        try {
            httpServer?.stop()
            httpServer = null
            Log.i(TAG, "HTTP server stopped")
        } catch (e: Exception) {
            Log.e(TAG, "Error stopping HTTP server", e)
        }

        try {
            transferServer?.stop()
            transferServer = null
            Log.i(TAG, "TCP transfer server stopped")
        } catch (e: Exception) {
            Log.e(TAG, "Error stopping transfer server", e)
        }

        try {
            peerApi?.unregisterNsd()
            peerApi = null
        } catch (e: Exception) {
            Log.e(TAG, "Error unregistering NSD", e)
        }
    }

    // ═════════════════════════════════════════════════════════════════
    //  WAKE LOCK
    // ═════════════════════════════════════════════════════════════════

    private fun acquireWakeLock() {
        if (wakeLock == null) {
            val pm = getSystemService(POWER_SERVICE) as PowerManager
            wakeLock = pm.newWakeLock(
                PowerManager.PARTIAL_WAKE_LOCK,
                "AgentService::WakeLock"
            ).apply {
                acquire(24 * 60 * 60 * 1000L) // 24h max
            }
            Log.d(TAG, "Wake lock acquired")
        }
    }

    private fun releaseWakeLock() {
        wakeLock?.let {
            if (it.isHeld) it.release()
            wakeLock = null
            Log.d(TAG, "Wake lock released")
        }
    }

    // ═════════════════════════════════════════════════════════════════
    //  NOTIFICATION
    // ═════════════════════════════════════════════════════════════════

    private fun buildNotification(): Notification {
        val openIntent = Intent(this, MainActivity::class.java)
        val pendingOpen = PendingIntent.getActivity(
            this, 0, openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val stopIntent = Intent(this, AgentService::class.java).apply {
            action = ACTION_STOP
        }
        val pendingStop = PendingIntent.getService(
            this, 1, stopIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        return NotificationCompat.Builder(this, AgentApp.CHANNEL_SERVICE)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(getString(R.string.status_running))
            .setSmallIcon(R.drawable.ic_agent)
            .setOngoing(true)
            .setContentIntent(pendingOpen)
            .addAction(android.R.drawable.ic_media_pause, getString(R.string.btn_stop), pendingStop)
            .build()
    }
}
