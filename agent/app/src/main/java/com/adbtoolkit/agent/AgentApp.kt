package com.adbtoolkit.agent

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.os.Build
import android.util.Log

/**
 * Application entry point.
 *
 * Creates notification channels on startup so they're available before
 * the foreground services are started.
 */
class AgentApp : Application() {

    companion object {
        const val TAG = "AgentApp"
        const val CHANNEL_SERVICE = "agent_service"
        const val CHANNEL_TRANSFER = "agent_transfer"
        const val CHANNEL_SECURITY = "agent_security"

        /** Default HTTP API port (forwarded via `adb forward tcp:15555 tcp:15555`). */
        const val HTTP_PORT = 15555

        /** Default high-speed TCP transfer port. */
        const val TRANSFER_PORT = 15556

        /** Auth token — set at install time via ADB, shared with PC toolkit. */
        @Volatile
        var authToken: String = ""
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannels()
        Log.i(TAG, "ADB Toolkit Agent initialized")
    }

    private fun createNotificationChannels() {
        val nm = getSystemService(NotificationManager::class.java) ?: return

        nm.createNotificationChannel(
            NotificationChannel(
                CHANNEL_SERVICE,
                getString(R.string.notification_channel_name),
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = getString(R.string.notification_channel_desc)
            }
        )

        nm.createNotificationChannel(
            NotificationChannel(
                CHANNEL_TRANSFER,
                getString(R.string.transfer_channel_name),
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = getString(R.string.transfer_channel_desc)
            }
        )

        nm.createNotificationChannel(
            NotificationChannel(
                CHANNEL_SECURITY,
                getString(R.string.security_channel_name),
                NotificationManager.IMPORTANCE_HIGH
            ).apply {
                description = getString(R.string.security_channel_desc)
            }
        )
    }
}
