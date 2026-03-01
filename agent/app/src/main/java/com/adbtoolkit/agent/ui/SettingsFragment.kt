package com.adbtoolkit.agent.ui

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.Settings
import android.view.Gravity
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import com.adbtoolkit.agent.AgentApp
import com.adbtoolkit.agent.R
import com.adbtoolkit.agent.databinding.FragmentSettingsBinding
import com.adbtoolkit.agent.python.PythonRuntime
import com.adbtoolkit.agent.security.BiometricGate
import com.adbtoolkit.agent.security.PairingManager
import java.security.SecureRandom
import java.util.Base64

/**
 * Settings — security/pairing, permissions, Python runtime, auth token, about.
 * Combines the Security, Permissions, Python and Token cards from the old single-page.
 */
class SettingsFragment : Fragment() {

    private var _binding: FragmentSettingsBinding? = null
    private val binding get() = _binding!!

    private lateinit var pairingManager: PairingManager
    private lateinit var biometricGate: BiometricGate

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { refreshPermissions() }

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        _binding = FragmentSettingsBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        pairingManager = PairingManager.getInstance(requireContext())
        biometricGate = BiometricGate(requireActivity())

        setupSecurity()
        setupToken()
        setupPython()
        setupAbout()
        refreshPermissions()
    }

    override fun onResume() {
        super.onResume()
        refreshPermissions()
    }

    override fun onDestroyView() {
        super.onDestroyView()
        _binding = null
    }

    // ═════════════════════════════════════════════════════════════════
    //  SECURITY / PAIRING
    // ═════════════════════════════════════════════════════════════════

    private fun setupSecurity() {
        val biometricAvailable = biometricGate.isAvailable()
        val deviceInsecure = biometricGate.isDeviceInsecure()
        binding.tvSecurityStatus.text = when {
            deviceInsecure -> "⚠️ DISPOSITIVO SEM BLOQUEIO — pareamento desabilitado"
            biometricAvailable -> "✅ Biometria/PIN disponível — pareamento seguro ativo"
            else -> "🔒 Bloqueio de tela disponível"
        }

        refreshPairedDevices()

        binding.btnRevokeAll.setOnClickListener {
            if (pairingManager.getPairedDevices().isEmpty()) {
                Toast.makeText(requireContext(), "Nenhum dispositivo pareado", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            biometricGate.authenticateAsync(
                activity = requireActivity(),
                title = getString(R.string.revoke_biometric_title),
                subtitle = "Revogar todos os pareamentos",
                description = "Esta ação desconectará todos os dispositivos pareados",
            ) { result ->
                if (result.success) {
                    pairingManager.revokeAll()
                    refreshPairedDevices()
                    Toast.makeText(requireContext(), "Todos os pareamentos revogados", Toast.LENGTH_SHORT).show()
                } else if (!result.cancelled) {
                    Toast.makeText(requireContext(), "Autenticação falhou", Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    private fun refreshPairedDevices() {
        val devices = pairingManager.getPairedDevices()
        binding.tvPairedCount.text = "Dispositivos pareados: ${devices.size}"
    }

    // ═════════════════════════════════════════════════════════════════
    //  AUTH TOKEN
    // ═════════════════════════════════════════════════════════════════

    private fun setupToken() {
        binding.tvToken.text = AgentApp.authToken
        binding.btnNewToken.setOnClickListener {
            AlertDialog.Builder(requireContext())
                .setTitle("Gerar novo token?")
                .setMessage("O token anterior será invalidado.")
                .setPositiveButton("Gerar") { _, _ ->
                    val newToken = generateToken()
                    val prefs = requireContext().getSharedPreferences("agent_prefs", android.content.Context.MODE_PRIVATE)
                    prefs.edit().putString("auth_token", newToken).apply()
                    AgentApp.authToken = newToken
                    binding.tvToken.text = newToken
                    Toast.makeText(requireContext(), "Token atualizado", Toast.LENGTH_SHORT).show()
                }
                .setNegativeButton("Cancelar", null)
                .show()
        }
    }

    private fun generateToken(): String {
        val bytes = ByteArray(32)
        SecureRandom().nextBytes(bytes)
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
        } else {
            android.util.Base64.encodeToString(bytes, android.util.Base64.URL_SAFE or android.util.Base64.NO_PADDING or android.util.Base64.NO_WRAP)
        }
    }

    // ═════════════════════════════════════════════════════════════════
    //  PYTHON
    // ═════════════════════════════════════════════════════════════════

    private fun setupPython() {
        val runtime = PythonRuntime.getInstance(requireContext())
        refreshPythonStatus(runtime)

        binding.btnSetupPython.setOnClickListener {
            binding.btnSetupPython.isEnabled = false
            binding.tvPythonStatus.text = "Instalando Python..."
            Thread {
                try {
                    runtime.bootstrap()
                    activity?.runOnUiThread {
                        refreshPythonStatus(runtime)
                        Toast.makeText(requireContext(), "Python instalado!", Toast.LENGTH_LONG).show()
                    }
                } catch (e: Exception) {
                    activity?.runOnUiThread {
                        binding.tvPythonStatus.text = "Erro: ${e.message}"
                        binding.btnSetupPython.isEnabled = true
                    }
                }
            }.start()
        }
    }

    private fun refreshPythonStatus(runtime: PythonRuntime) {
        if (runtime.isReady) {
            binding.tvPythonStatus.text = "Python ${runtime.pythonVersion ?: "?"} | pyaccelerate ${runtime.pyaccelerateVersion ?: "N/A"}"
            binding.btnSetupPython.text = "Atualizar"
        } else {
            binding.tvPythonStatus.text = "Não instalado"
            binding.btnSetupPython.text = "Instalar Python + pyaccelerate"
        }
        binding.btnSetupPython.isEnabled = true
    }

    // ═════════════════════════════════════════════════════════════════
    //  PERMISSIONS
    // ═════════════════════════════════════════════════════════════════

    private fun refreshPermissions() {
        val ctx = context ?: return
        val container = _binding?.llPermissions ?: return
        container.removeAllViews()

        data class Perm(val label: String, val granted: Boolean, val action: (() -> Unit)? = null)

        val perms = mutableListOf<Perm>()

        // Storage
        val hasStorage = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            Environment.isExternalStorageManager()
        } else {
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.WRITE_EXTERNAL_STORAGE) == PackageManager.PERMISSION_GRANTED
        }
        perms.add(Perm(getString(R.string.perm_storage), hasStorage) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                startActivity(Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION).apply {
                    data = Uri.parse("package:${ctx.packageName}")
                })
            } else {
                permissionLauncher.launch(arrayOf(Manifest.permission.READ_EXTERNAL_STORAGE, Manifest.permission.WRITE_EXTERNAL_STORAGE))
            }
        })

        // Contacts
        perms.add(Perm(getString(R.string.perm_contacts),
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.READ_CONTACTS) == PackageManager.PERMISSION_GRANTED
        ) { permissionLauncher.launch(arrayOf(Manifest.permission.READ_CONTACTS, Manifest.permission.WRITE_CONTACTS)) })

        // SMS
        perms.add(Perm(getString(R.string.perm_sms),
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.READ_SMS) == PackageManager.PERMISSION_GRANTED
        ) { permissionLauncher.launch(arrayOf(Manifest.permission.READ_SMS, Manifest.permission.READ_CALL_LOG)) })

        // Notifications (13+)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            perms.add(Perm(getString(R.string.perm_notifications),
                ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
            ) { permissionLauncher.launch(arrayOf(Manifest.permission.POST_NOTIFICATIONS)) })
        }

        // Battery
        val pm = ctx.getSystemService(android.content.Context.POWER_SERVICE) as android.os.PowerManager
        perms.add(Perm(getString(R.string.perm_battery), pm.isIgnoringBatteryOptimizations(ctx.packageName)) {
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                data = Uri.parse("package:${ctx.packageName}")
            })
        })

        // Biometric
        perms.add(Perm(getString(R.string.perm_biometric), !biometricGate.isDeviceInsecure()))

        // Build rows
        perms.forEach { perm ->
            val row = LinearLayout(ctx).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(0, 8, 0, 8)
            }
            val icon = TextView(ctx).apply {
                text = if (perm.granted) "✅" else "❌"
                textSize = 16f
                setPadding(0, 0, 12, 0)
            }
            val label = TextView(ctx).apply {
                text = perm.label
                textSize = 14f
                layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            }
            row.addView(icon)
            row.addView(label)

            if (!perm.granted && perm.action != null) {
                row.setOnClickListener { perm.action.invoke() }
                row.isClickable = true
                row.isFocusable = true
                val attrs = intArrayOf(android.R.attr.selectableItemBackground)
                val ta = ctx.obtainStyledAttributes(attrs)
                row.background = ta.getDrawable(0)
                ta.recycle()
            }
            container.addView(row)
        }
    }

    // ═════════════════════════════════════════════════════════════════
    //  ABOUT
    // ═════════════════════════════════════════════════════════════════

    private fun setupAbout() {
        try {
            val pi = requireContext().packageManager.getPackageInfo(requireContext().packageName, 0)
            binding.tvAppVersion.text = "ADB Toolkit Agent v${pi.versionName}"
        } catch (_: Exception) {
            binding.tvAppVersion.text = "ADB Toolkit Agent"
        }
        binding.tvAgentInfo.text = "HTTP API • TCP Transfer • P2P Pairing • Python Runtime"
    }
}
