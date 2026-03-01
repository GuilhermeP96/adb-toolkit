package com.adbtoolkit.agent.ui

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.fragment.app.Fragment
import com.adbtoolkit.agent.AgentApp
import com.adbtoolkit.agent.R
import com.adbtoolkit.agent.api.PeerApi
import com.adbtoolkit.agent.databinding.ActivityMainNavBinding
import com.adbtoolkit.agent.security.BiometricGate
import com.adbtoolkit.agent.security.PairingManager
import java.net.Inet4Address
import java.net.NetworkInterface
import java.security.SecureRandom
import java.util.Base64

/**
 * Main activity with bottom navigation across 5 tabs:
 *   Dashboard | Files | Apps | Terminal | Settings
 *
 * Hosts the top connection status bar and pairing dialog handler.
 */
class MainActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "MainActivity"
        private const val PREFS = "agent_prefs"
        private const val KEY_TOKEN = "auth_token"
    }

    private lateinit var binding: ActivityMainNavBinding
    private lateinit var pairingManager: PairingManager
    private lateinit var biometricGate: BiometricGate

    // Keep fragment instances to avoid recreation
    private val dashboardFragment by lazy { DashboardFragment() }
    private val filesFragment by lazy { FilesFragment() }
    private val appsFragment by lazy { AppsFragment() }
    private val terminalFragment by lazy { TerminalFragment() }
    private val settingsFragment by lazy { SettingsFragment() }
    private var activeFragment: Fragment? = null

    // Pairing broadcast receiver
    private val pairingReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action == PeerApi.ACTION_PAIRING_REQUEST) {
                val challengeId = intent.getStringExtra(PeerApi.EXTRA_CHALLENGE_ID) ?: return
                val peerLabel = intent.getStringExtra(PeerApi.EXTRA_PEER_LABEL) ?: "Desconhecido"
                val confirmCode = intent.getStringExtra(PeerApi.EXTRA_CONFIRM_CODE) ?: "------"
                showPairingDialog(challengeId, peerLabel, confirmCode)
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainNavBinding.inflate(layoutInflater)
        setContentView(binding.root)

        pairingManager = PairingManager.getInstance(this)
        biometricGate = BiometricGate(this)

        setupToken()
        setupNavigation()

        // Load default fragment on fresh start
        if (savedInstanceState == null) {
            switchFragment(dashboardFragment)
            binding.bottomNav.selectedItemId = R.id.nav_dashboard
        }

        updateTopBar(false)
    }

    override fun onResume() {
        super.onResume()
        val filter = IntentFilter(PeerApi.ACTION_PAIRING_REQUEST)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(pairingReceiver, filter, RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(pairingReceiver, filter)
        }
    }

    override fun onPause() {
        super.onPause()
        try { unregisterReceiver(pairingReceiver) } catch (_: Exception) {}
    }

    // ═════════════════════════════════════════════════════════════════
    //  TOKEN INITIALIZATION
    // ═════════════════════════════════════════════════════════════════

    private fun setupToken() {
        val prefs = getSharedPreferences(PREFS, MODE_PRIVATE)
        var token = prefs.getString(KEY_TOKEN, null)
        if (token.isNullOrEmpty()) {
            token = generateToken()
            prefs.edit().putString(KEY_TOKEN, token).apply()
        }
        AgentApp.authToken = token
    }

    private fun generateToken(): String {
        val bytes = ByteArray(32)
        SecureRandom().nextBytes(bytes)
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
        } else {
            android.util.Base64.encodeToString(
                bytes,
                android.util.Base64.URL_SAFE or android.util.Base64.NO_PADDING or android.util.Base64.NO_WRAP
            )
        }
    }

    // ═════════════════════════════════════════════════════════════════
    //  BOTTOM NAVIGATION
    // ═════════════════════════════════════════════════════════════════

    private fun setupNavigation() {
        binding.bottomNav.setOnItemSelectedListener { item ->
            val target = when (item.itemId) {
                R.id.nav_dashboard -> dashboardFragment
                R.id.nav_files     -> filesFragment
                R.id.nav_apps      -> appsFragment
                R.id.nav_terminal  -> terminalFragment
                R.id.nav_settings  -> settingsFragment
                else -> dashboardFragment
            }
            switchFragment(target)
            true
        }
    }

    private fun switchFragment(fragment: Fragment) {
        if (fragment === activeFragment) return
        supportFragmentManager.beginTransaction()
            .replace(R.id.fragmentContainer, fragment)
            .commit()
        activeFragment = fragment
    }

    // ═════════════════════════════════════════════════════════════════
    //  TOP STATUS BAR  (called by DashboardFragment)
    // ═════════════════════════════════════════════════════════════════

    fun updateTopBar(running: Boolean) {
        if (running) {
            binding.tvTopStatus.text = getString(R.string.status_running)
            binding.viewStatusDot.setBackgroundColor(getColor(android.R.color.holo_green_dark))
            binding.tvTopIp.text = "${getLocalIp()}:${AgentApp.HTTP_PORT}"
        } else {
            binding.tvTopStatus.text = getString(R.string.status_stopped)
            binding.viewStatusDot.setBackgroundColor(getColor(android.R.color.holo_red_dark))
            binding.tvTopIp.text = ""
        }
    }

    private fun getLocalIp(): String {
        return try {
            NetworkInterface.getNetworkInterfaces().asSequence()
                .flatMap { it.inetAddresses.asSequence() }
                .filter { !it.isLoopbackAddress && it is Inet4Address }
                .map { it.hostAddress }
                .firstOrNull() ?: "127.0.0.1"
        } catch (_: Exception) { "127.0.0.1" }
    }

    // ═════════════════════════════════════════════════════════════════
    //  PAIRING DIALOG
    // ═════════════════════════════════════════════════════════════════

    private fun showPairingDialog(challengeId: String, peerLabel: String, confirmCode: String) {
        if (biometricGate.isDeviceInsecure()) {
            AlertDialog.Builder(this)
                .setTitle("Pareamento bloqueado")
                .setMessage(getString(R.string.pairing_insecure_device))
                .setPositiveButton("OK", null)
                .show()
            pairingManager.rejectPairing(challengeId)
            return
        }

        AlertDialog.Builder(this)
            .setTitle(getString(R.string.pairing_title))
            .setMessage(
                "Dispositivo: $peerLabel\n\n" +
                "Código de confirmação:\n\n    $confirmCode\n\n" +
                "Verifique se este código é IGUAL no outro dispositivo.\n" +
                "Se não corresponder, REJEITE a solicitação."
            )
            .setPositiveButton("Confirmar com biometria") { _, _ ->
                biometricGate.authenticateAsync(
                    activity = this,
                    title = getString(R.string.biometric_title),
                    subtitle = String.format(getString(R.string.biometric_subtitle), peerLabel),
                    description = String.format(getString(R.string.biometric_description), confirmCode),
                ) { authResult ->
                    if (authResult.success) {
                        val device = pairingManager.approvePairing(challengeId)
                        if (device != null) {
                            Toast.makeText(this, getString(R.string.pairing_approved), Toast.LENGTH_LONG).show()
                        } else {
                            Toast.makeText(this, getString(R.string.pairing_expired), Toast.LENGTH_SHORT).show()
                        }
                    } else if (!authResult.cancelled) {
                        Toast.makeText(this, "Autenticação falhou", Toast.LENGTH_SHORT).show()
                        pairingManager.rejectPairing(challengeId)
                    } else {
                        pairingManager.rejectPairing(challengeId)
                    }
                }
            }
            .setNegativeButton("Rejeitar") { _, _ ->
                pairingManager.rejectPairing(challengeId)
                Toast.makeText(this, getString(R.string.pairing_rejected), Toast.LENGTH_SHORT).show()
            }
            .setCancelable(false)
            .show()
    }
}
