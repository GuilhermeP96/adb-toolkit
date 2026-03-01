package com.adbtoolkit.agent.ui

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.EditorInfo
import android.widget.ScrollView
import android.widget.Toast
import androidx.fragment.app.Fragment
import com.adbtoolkit.agent.databinding.FragmentTerminalBinding
import java.io.BufferedReader
import java.io.InputStreamReader
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.TimeUnit

/**
 * Terminal / Shell — execute commands directly on the device.
 * Mirrors the Shell / Toolbox features from the PC toolkit.
 */
class TerminalFragment : Fragment() {

    private var _binding: FragmentTerminalBinding? = null
    private val binding get() = _binding!!

    private val outputBuilder = StringBuilder()
    private val commandHistory = mutableListOf<String>()
    private var historyIndex = -1

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        _binding = FragmentTerminalBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)

        // Welcome message
        appendOutput("ADB Toolkit Agent Shell\n")
        appendOutput("Type commands below or tap quick commands.\n")
        appendOutput("─────────────────────────────────\n")

        // Send button
        binding.btnSend.setOnClickListener { executeCurrentCommand() }

        // Enter key sends
        binding.etCommand.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_SEND || actionId == EditorInfo.IME_ACTION_DONE) {
                executeCurrentCommand()
                true
            } else false
        }

        // Quick command chips
        binding.chipGetprop.setOnClickListener { runCommand("getprop") }
        binding.chipDf.setOnClickListener { runCommand("df -h") }
        binding.chipPs.setOnClickListener { runCommand("ps -A") }
        binding.chipTop.setOnClickListener { runCommand("top -n 1 -b") }
        binding.chipDumpsys.setOnClickListener { runCommand("dumpsys battery") }
        binding.chipLogcat.setOnClickListener { runCommand("logcat -d -t 50") }
    }

    override fun onDestroyView() {
        super.onDestroyView()
        _binding = null
    }

    private fun executeCurrentCommand() {
        val cmd = binding.etCommand.text?.toString()?.trim() ?: ""
        if (cmd.isEmpty()) return
        binding.etCommand.text?.clear()
        runCommand(cmd)
    }

    private fun runCommand(cmd: String) {
        commandHistory.add(cmd)
        historyIndex = commandHistory.size
        appendOutput("\n\$ $cmd\n")

        Thread {
            try {
                val process = Runtime.getRuntime().exec(arrayOf("sh", "-c", cmd))
                val stdout = BufferedReader(InputStreamReader(process.inputStream))
                val stderr = BufferedReader(InputStreamReader(process.errorStream))

                val output = StringBuilder()

                // Read stdout
                var line: String?
                while (stdout.readLine().also { line = it } != null) {
                    output.append(line).append('\n')
                }
                // Read stderr
                while (stderr.readLine().also { line = it } != null) {
                    output.append("E: ").append(line).append('\n')
                }

                val completed = process.waitFor(30, TimeUnit.SECONDS)
                if (!completed) {
                    process.destroyForcibly()
                    output.append("[Timeout after 30s]\n")
                }

                val exitCode = try { process.exitValue() } catch (_: Exception) { -1 }
                if (exitCode != 0 && output.isEmpty()) {
                    output.append("[Exit code: $exitCode]\n")
                }

                activity?.runOnUiThread {
                    appendOutput(output.toString())
                }
            } catch (e: Exception) {
                activity?.runOnUiThread {
                    appendOutput("Error: ${e.message}\n")
                }
            }
        }.start()
    }

    private fun appendOutput(text: String) {
        outputBuilder.append(text)
        // Keep buffer manageable
        if (outputBuilder.length > 50_000) {
            outputBuilder.delete(0, outputBuilder.length - 30_000)
        }
        _binding?.tvTerminalOutput?.text = outputBuilder.toString()
        // Scroll to bottom
        _binding?.svTerminal?.post {
            _binding?.svTerminal?.fullScroll(ScrollView.FOCUS_DOWN)
        }
    }
}
