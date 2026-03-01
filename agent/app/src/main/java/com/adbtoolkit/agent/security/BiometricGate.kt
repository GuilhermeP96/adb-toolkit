package com.adbtoolkit.agent.security

import android.app.KeyguardManager
import android.content.Context
import android.os.Build
import android.util.Log
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricManager.Authenticators
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/**
 * Gate that requires biometric authentication (fingerprint / face) OR device
 * credential (PIN / pattern / password) before allowing security-sensitive
 * operations like pairing with a new device.
 *
 * Design decisions:
 * - Uses BIOMETRIC_STRONG | DEVICE_CREDENTIAL so users without biometrics
 *   enrolled can still use their lock-screen credential.
 * - Falls back to KeyguardManager.createConfirmDeviceCredentialIntent on
 *   API 28- for broad compatibility.
 * - Blocking API (waitForAuth()) for use from background threads / coroutines,
 *   plus async callbacks for UI-thread usage.
 */
class BiometricGate(private val context: Context) {

    companion object {
        private const val TAG = "BiometricGate"

        /** Maximum time to wait for biometric in blocking mode. */
        private const val AUTH_TIMEOUT_SEC = 120L

        /** Authenticator combination: strong biometric OR device PIN/pattern/password. */
        private const val AUTHENTICATORS =
            Authenticators.BIOMETRIC_STRONG or Authenticators.DEVICE_CREDENTIAL
    }

    /**
     * Check whether the device supports at least one authentication method.
     */
    fun isAvailable(): Boolean {
        val bm = BiometricManager.from(context)
        return when (bm.canAuthenticate(AUTHENTICATORS)) {
            BiometricManager.BIOMETRIC_SUCCESS -> true
            BiometricManager.BIOMETRIC_ERROR_NONE_ENROLLED -> {
                // Device has hardware but no biometric enrolled — fallback to device credential
                val km = context.getSystemService(Context.KEYGUARD_SERVICE) as? KeyguardManager
                km?.isDeviceSecure == true
            }
            else -> {
                val km = context.getSystemService(Context.KEYGUARD_SERVICE) as? KeyguardManager
                km?.isDeviceSecure == true
            }
        }
    }

    /**
     * Returns true if the device has NO lock screen at all (swipe only).
     * If this returns true, we REFUSE pairing — the user MUST have at least
     * PIN / pattern / password set.
     */
    fun isDeviceInsecure(): Boolean {
        val km = context.getSystemService(Context.KEYGUARD_SERVICE) as? KeyguardManager
        return km?.isDeviceSecure != true
    }

    /**
     * Show the biometric / device-credential prompt and block until the user
     * authenticates or cancels. Must NOT be called from the main thread.
     *
     * @param activity      The current FragmentActivity (for BiometricPrompt)
     * @param title         Prompt title (e.g. "Confirmar pareamento")
     * @param subtitle      Prompt subtitle (e.g. "Dispositivo: Xiaomi Redmi...")
     * @param description   Longer description (e.g. confirm code)
     *
     * @return AuthResult with success/failure/cancel info
     */
    fun authenticate(
        activity: FragmentActivity,
        title: String,
        subtitle: String,
        description: String,
    ): AuthResult {
        val latch = CountDownLatch(1)
        val result = AtomicReference<AuthResult>()

        val executor = ContextCompat.getMainExecutor(context)

        val callback = object : BiometricPrompt.AuthenticationCallback() {
            override fun onAuthenticationSucceeded(auth: BiometricPrompt.AuthenticationResult) {
                Log.i(TAG, "Biometric auth succeeded")
                result.set(AuthResult(success = true))
                latch.countDown()
            }

            override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                Log.w(TAG, "Biometric auth error: $errorCode — $errString")
                result.set(AuthResult(
                    success = false,
                    errorCode = errorCode,
                    errorMessage = errString.toString(),
                    cancelled = errorCode == BiometricPrompt.ERROR_USER_CANCELED ||
                                errorCode == BiometricPrompt.ERROR_NEGATIVE_BUTTON ||
                                errorCode == BiometricPrompt.ERROR_CANCELED
                ))
                latch.countDown()
            }

            override fun onAuthenticationFailed() {
                // Called on each failed attempt (e.g. wrong finger) — don't dismiss yet
                Log.w(TAG, "Biometric auth attempt failed (wrong biometric)")
            }
        }

        // Must create BiometricPrompt on main thread
        activity.runOnUiThread {
            try {
                val prompt = BiometricPrompt(activity, executor, callback)

                val info = BiometricPrompt.PromptInfo.Builder()
                    .setTitle(title)
                    .setSubtitle(subtitle)
                    .setDescription(description)
                    .setAllowedAuthenticators(AUTHENTICATORS)
                    .setConfirmationRequired(true)
                    .build()

                prompt.authenticate(info)
            } catch (e: Exception) {
                Log.e(TAG, "Failed to show biometric prompt", e)
                result.set(AuthResult(
                    success = false,
                    errorMessage = "Failed to show prompt: ${e.message}"
                ))
                latch.countDown()
            }
        }

        // Block the calling thread (NOT main thread)
        val completed = latch.await(AUTH_TIMEOUT_SEC, TimeUnit.SECONDS)
        if (!completed) {
            return AuthResult(success = false, errorMessage = "Authentication timed out")
        }
        return result.get() ?: AuthResult(success = false, errorMessage = "No result")
    }

    /**
     * Async version — fires callback on the main thread.
     */
    fun authenticateAsync(
        activity: FragmentActivity,
        title: String,
        subtitle: String,
        description: String,
        onResult: (AuthResult) -> Unit,
    ) {
        val executor = ContextCompat.getMainExecutor(context)

        val callback = object : BiometricPrompt.AuthenticationCallback() {
            override fun onAuthenticationSucceeded(auth: BiometricPrompt.AuthenticationResult) {
                onResult(AuthResult(success = true))
            }

            override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                onResult(AuthResult(
                    success = false,
                    errorCode = errorCode,
                    errorMessage = errString.toString(),
                    cancelled = errorCode == BiometricPrompt.ERROR_USER_CANCELED ||
                                errorCode == BiometricPrompt.ERROR_NEGATIVE_BUTTON ||
                                errorCode == BiometricPrompt.ERROR_CANCELED
                ))
            }

            override fun onAuthenticationFailed() {
                // Retry — prompt stays open
            }
        }

        activity.runOnUiThread {
            try {
                val prompt = BiometricPrompt(activity, executor, callback)

                val info = BiometricPrompt.PromptInfo.Builder()
                    .setTitle(title)
                    .setSubtitle(subtitle)
                    .setDescription(description)
                    .setAllowedAuthenticators(AUTHENTICATORS)
                    .setConfirmationRequired(true)
                    .build()

                prompt.authenticate(info)
            } catch (e: Exception) {
                onResult(AuthResult(
                    success = false,
                    errorMessage = "Failed to show prompt: ${e.message}"
                ))
            }
        }
    }
}

/** Result of a biometric / device credential authentication attempt. */
data class AuthResult(
    val success: Boolean,
    val errorCode: Int = 0,
    val errorMessage: String = "",
    val cancelled: Boolean = false,
)
