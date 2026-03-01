package com.adbtoolkit.agent.services

import android.app.admin.DeviceAdminReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Device admin receiver — enables lock/wipe/password-reset capabilities.
 *
 * Enabled via:
 *   adb shell dpm set-active-admin com.adbtoolkit.agent/.services.AgentDeviceAdmin
 */
class AgentDeviceAdmin : DeviceAdminReceiver() {

    companion object {
        private const val TAG = "AgentDeviceAdmin"
    }

    override fun onEnabled(context: Context, intent: Intent) {
        Log.i(TAG, "Device admin enabled")
    }

    override fun onDisabled(context: Context, intent: Intent) {
        Log.i(TAG, "Device admin disabled")
    }

    override fun onPasswordFailed(context: Context, intent: Intent) {
        Log.w(TAG, "Password attempt failed")
    }

    override fun onPasswordSucceeded(context: Context, intent: Intent) {
        Log.d(TAG, "Password succeeded")
    }
}
