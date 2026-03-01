package com.adbtoolkit.agent.services

import android.accessibilityservice.AccessibilityService
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

/**
 * Accessibility service for UI automation tasks.
 *
 * Used for:
 * - Automating permission grants that require UI interaction
 * - Navigating settings screens (battery optimization, etc.)
 * - Automating app backup/restore flows that need UI interaction
 *
 * Only active when explicitly commanded by the toolkit.
 */
class AgentAccessibilityService : AccessibilityService() {

    companion object {
        private const val TAG = "AgentA11y"

        @Volatile
        var instance: AgentAccessibilityService? = null
            private set

        /** Pending UI automation commands from the API. */
        val pendingCommands = java.util.concurrent.ConcurrentLinkedQueue<A11yCommand>()
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        Log.i(TAG, "Accessibility service connected")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null) return

        val cmd = pendingCommands.peek() ?: return

        when (cmd) {
            is A11yCommand.ClickText -> {
                val node = findNodeByText(rootInActiveWindow, cmd.text)
                if (node != null) {
                    node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                    pendingCommands.poll()
                    Log.i(TAG, "Clicked: '${cmd.text}'")
                }
            }
            is A11yCommand.ClickId -> {
                val node = findNodeById(rootInActiveWindow, cmd.viewId)
                if (node != null) {
                    node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                    pendingCommands.poll()
                    Log.i(TAG, "Clicked ID: '${cmd.viewId}'")
                }
            }
            is A11yCommand.ScrollForward -> {
                rootInActiveWindow?.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD)
                pendingCommands.poll()
            }
            is A11yCommand.Back -> {
                performGlobalAction(GLOBAL_ACTION_BACK)
                pendingCommands.poll()
            }
            is A11yCommand.Home -> {
                performGlobalAction(GLOBAL_ACTION_HOME)
                pendingCommands.poll()
            }
        }
    }

    override fun onInterrupt() {
        Log.w(TAG, "Accessibility service interrupted")
    }

    override fun onDestroy() {
        instance = null
        pendingCommands.clear()
        Log.i(TAG, "Accessibility service destroyed")
        super.onDestroy()
    }

    // ── Node search helpers ──────────────────────────────────────────

    private fun findNodeByText(root: AccessibilityNodeInfo?, text: String): AccessibilityNodeInfo? {
        root ?: return null
        val nodes = root.findAccessibilityNodeInfosByText(text)
        return nodes?.firstOrNull { it.isClickable }
            ?: nodes?.firstOrNull()
    }

    private fun findNodeById(root: AccessibilityNodeInfo?, viewId: String): AccessibilityNodeInfo? {
        root ?: return null
        val nodes = root.findAccessibilityNodeInfosByViewId(viewId)
        return nodes?.firstOrNull()
    }
}

/** Commands the API can enqueue for the accessibility service. */
sealed class A11yCommand {
    data class ClickText(val text: String) : A11yCommand()
    data class ClickId(val viewId: String) : A11yCommand()
    data object ScrollForward : A11yCommand()
    data object Back : A11yCommand()
    data object Home : A11yCommand()
}
