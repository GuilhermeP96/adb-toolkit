package com.adbtoolkit.agent.ui

import android.app.ActivityManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.StatFs
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Toast
import androidx.fragment.app.Fragment
import com.adbtoolkit.agent.AgentApp
import com.adbtoolkit.agent.R
import com.adbtoolkit.agent.databinding.FragmentDashboardBinding
import com.adbtoolkit.agent.security.PairingManager
import com.adbtoolkit.agent.services.AgentService
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Dashboard — service control, device info, quick actions, connection log.
 */
class DashboardFragment : Fragment() {

    private var _binding: FragmentDashboardBinding? = null
    private val binding get() = _binding!!

    private lateinit var pairingManager: PairingManager
    private val logLines = mutableListOf<String>()
    private val dateFormat = SimpleDateFormat("HH:mm:ss", Locale.getDefault())

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        _binding = FragmentDashboardBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        pairingManager = PairingManager.getInstance(requireContext())
        setupServiceControls()
        setupQuickActions()
        refreshDeviceInfo()
        refreshStatus()
    }

    override fun onResume() {
        super.onResume()
        refreshDeviceInfo()
        refreshStatus()
    }

    override fun onDestroyView() {
        super.onDestroyView()
        _binding = null
    }

    // ═════════════════════════════════════════════════════════════════
    //  SERVICE CONTROL
    // ═════════════════════════════════════════════════════════════════

    private fun setupServiceControls() {
        binding.tvDeviceId.text = "Device ID: ${pairingManager.deviceId}"
        binding.tvPort.text = "HTTP: ${AgentApp.HTTP_PORT} | TCP: ${AgentApp.TRANSFER_PORT}"

        binding.btnStart.setOnClickListener {
            val intent = Intent(requireContext(), AgentService::class.java).apply {
                action = AgentService.ACTION_START
                putExtra(AgentService.EXTRA_TOKEN, AgentApp.authToken)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                requireContext().startForegroundService(intent)
            } else {
                requireContext().startService(intent)
            }
            appendLog("Service started")
            refreshStatus()
            Toast.makeText(requireContext(), "Agent iniciado", Toast.LENGTH_SHORT).show()
        }

        binding.btnStop.setOnClickListener {
            val intent = Intent(requireContext(), AgentService::class.java).apply {
                action = AgentService.ACTION_STOP
            }
            requireContext().startService(intent)
            appendLog("Service stopped")
            refreshStatus()
            Toast.makeText(requireContext(), "Agent parado", Toast.LENGTH_SHORT).show()
        }
    }

    // ═════════════════════════════════════════════════════════════════
    //  DEVICE INFO
    // ═════════════════════════════════════════════════════════════════

    private fun refreshDeviceInfo() {
        val ctx = context ?: return

        // Model
        binding.tvDeviceModel.text = "${Build.MANUFACTURER} ${Build.MODEL}"
        binding.tvAndroidVersion.text = "Android ${Build.VERSION.RELEASE} (SDK ${Build.VERSION.SDK_INT})"

        // Battery
        val batteryIntent = ctx.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = batteryIntent?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = batteryIntent?.getIntExtra(BatteryManager.EXTRA_SCALE, 100) ?: 100
        val pct = if (scale > 0) (level * 100) / scale else 0
        val status = batteryIntent?.getIntExtra(BatteryManager.EXTRA_STATUS, -1) ?: -1
        val charging = status == BatteryManager.BATTERY_STATUS_CHARGING || status == BatteryManager.BATTERY_STATUS_FULL
        binding.tvBattery.text = "${pct}%${if (charging) " ⚡" else ""}"

        // Storage
        try {
            val stat = StatFs(Environment.getExternalStorageDirectory().path)
            val totalBytes = stat.totalBytes
            val freeBytes = stat.availableBytes
            val usedPct = ((totalBytes - freeBytes) * 100 / totalBytes).toInt()
            binding.tvStorageFree.text = formatSize(freeBytes)
            binding.progressStorage.progress = usedPct
        } catch (_: Exception) {
            binding.tvStorageFree.text = "?"
        }

        // RAM
        try {
            val am = ctx.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
            val memInfo = ActivityManager.MemoryInfo()
            am.getMemoryInfo(memInfo)
            binding.tvRamFree.text = formatSize(memInfo.availMem)
        } catch (_: Exception) {
            binding.tvRamFree.text = "?"
        }
    }

    // ═════════════════════════════════════════════════════════════════
    //  QUICK ACTIONS
    // ═════════════════════════════════════════════════════════════════

    private fun setupQuickActions() {
        binding.btnScreenshot.setOnClickListener {
            executeShellCommand("screencap -p /sdcard/screenshot_agent.png", "Screenshot salvo em /sdcard/screenshot_agent.png")
        }
        binding.btnScreenRecord.setOnClickListener {
            executeShellCommand("screenrecord --time-limit 30 /sdcard/screenrecord_agent.mp4 &", "Gravando tela por 30s...")
        }
        binding.btnExportContacts.setOnClickListener {
            appendLog("Export contacts → use via PC toolkit")
            Toast.makeText(requireContext(), "Use o toolkit no PC para exportar contatos via Agent API", Toast.LENGTH_LONG).show()
        }
        binding.btnExportSms.setOnClickListener {
            appendLog("Export SMS → use via PC toolkit")
            Toast.makeText(requireContext(), "Use o toolkit no PC para exportar SMS via Agent API", Toast.LENGTH_LONG).show()
        }
    }

    private fun executeShellCommand(cmd: String, successMsg: String) {
        Thread {
            try {
                val process = Runtime.getRuntime().exec(arrayOf("sh", "-c", cmd))
                process.waitFor()
                activity?.runOnUiThread {
                    appendLog(successMsg)
                    Toast.makeText(requireContext(), successMsg, Toast.LENGTH_SHORT).show()
                }
            } catch (e: Exception) {
                activity?.runOnUiThread {
                    appendLog("Error: ${e.message}")
                    Toast.makeText(requireContext(), "Erro: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }
        }.start()
    }

    // ═════════════════════════════════════════════════════════════════
    //  STATUS & LOG
    // ═════════════════════════════════════════════════════════════════

    fun refreshStatus() {
        val running = isServiceRunning()
        binding.tvStatus.text = if (running) getString(R.string.status_running) else getString(R.string.status_stopped)
        binding.btnStart.isEnabled = !running
        binding.btnStop.isEnabled = running

        // Update top bar via activity
        (activity as? MainActivity)?.updateTopBar(running)
    }

    private fun isServiceRunning(): Boolean {
        val am = requireContext().getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        @Suppress("DEPRECATION")
        return am.getRunningServices(Int.MAX_VALUE).any {
            it.service.className == AgentService::class.java.name
        }
    }

    private fun appendLog(msg: String) {
        val ts = dateFormat.format(Date())
        logLines.add("[$ts] $msg")
        if (logLines.size > 50) logLines.removeAt(0)
        _binding?.tvLog?.text = logLines.takeLast(8).joinToString("\n")
    }

    private fun formatSize(bytes: Long): String {
        return when {
            bytes >= 1L shl 30 -> String.format("%.1f GB", bytes.toFloat() / (1L shl 30))
            bytes >= 1L shl 20 -> String.format("%.1f MB", bytes.toFloat() / (1L shl 20))
            bytes >= 1L shl 10 -> String.format("%.1f KB", bytes.toFloat() / (1L shl 10))
            else -> "$bytes B"
        }
    }
}
