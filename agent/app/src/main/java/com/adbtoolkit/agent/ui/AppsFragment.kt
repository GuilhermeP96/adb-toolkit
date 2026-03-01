package com.adbtoolkit.agent.ui

import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.graphics.drawable.Drawable
import android.os.Build
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.PopupMenu
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.core.widget.doAfterTextChanged
import androidx.fragment.app.Fragment
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.adbtoolkit.agent.R
import com.adbtoolkit.agent.databinding.FragmentAppsBinding
import com.adbtoolkit.agent.databinding.ItemAppBinding
import java.io.File

/**
 * App Manager — list/search/manage installed apps.
 * Mirrors the Cleanup (app list) tab from the PC toolkit.
 */
class AppsFragment : Fragment() {

    private var _binding: FragmentAppsBinding? = null
    private val binding get() = _binding!!

    private val allApps = mutableListOf<AppEntry>()
    private val filteredApps = mutableListOf<AppEntry>()
    private val adapter = AppAdapter()

    data class AppEntry(
        val name: String,
        val pkg: String,
        val versionName: String,
        val versionCode: Long,
        val apkSize: Long,
        val isSystem: Boolean,
        val icon: Drawable?,
    )

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        _binding = FragmentAppsBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)

        binding.rvApps.layoutManager = LinearLayoutManager(requireContext())
        binding.rvApps.adapter = adapter

        binding.swipeRefresh.setOnRefreshListener {
            loadApps()
            binding.swipeRefresh.isRefreshing = false
        }

        // Search
        binding.etSearch.doAfterTextChanged { filterApps() }

        // Filter chips
        binding.chipAll.setOnClickListener { filterApps() }
        binding.chipUser.setOnClickListener { filterApps() }
        binding.chipSystem.setOnClickListener { filterApps() }

        // Bottom actions
        binding.btnInstallApk.setOnClickListener {
            Toast.makeText(requireContext(), "Use o toolkit no PC para instalar APKs via Agent API", Toast.LENGTH_LONG).show()
        }
        binding.btnBatchUninstall.setOnClickListener {
            Toast.makeText(requireContext(), "Selecione apps na lista para desinstalar", Toast.LENGTH_SHORT).show()
        }

        loadApps()
    }

    override fun onDestroyView() {
        super.onDestroyView()
        _binding = null
    }

    private fun loadApps() {
        Thread {
            val pm = requireContext().packageManager
            val packages = pm.getInstalledPackages(PackageManager.GET_META_DATA)
            val entries = packages.mapNotNull { pkg ->
                try {
                    val appInfo = pkg.applicationInfo ?: return@mapNotNull null
                    val isSystem = (appInfo.flags and ApplicationInfo.FLAG_SYSTEM) != 0
                    val apkSize = try { File(appInfo.sourceDir).length() } catch (_: Exception) { 0L }
                    AppEntry(
                        name = pm.getApplicationLabel(appInfo).toString(),
                        pkg = pkg.packageName,
                        versionName = pkg.versionName ?: "",
                        versionCode = if (Build.VERSION.SDK_INT >= 28) pkg.longVersionCode else pkg.versionCode.toLong(),
                        apkSize = apkSize,
                        isSystem = isSystem,
                        icon = try { pm.getApplicationIcon(appInfo) } catch (_: Exception) { null },
                    )
                } catch (_: Exception) { null }
            }.sortedBy { it.name.lowercase() }

            activity?.runOnUiThread {
                allApps.clear()
                allApps.addAll(entries)
                filterApps()
            }
        }.start()
    }

    private fun filterApps() {
        val query = _binding?.etSearch?.text?.toString()?.lowercase() ?: ""
        val showSystem = _binding?.chipSystem?.isChecked == true
        val showUser = _binding?.chipUser?.isChecked == true
        val showAll = _binding?.chipAll?.isChecked == true || (!showSystem && !showUser)

        filteredApps.clear()
        filteredApps.addAll(allApps.filter { app ->
            val matchesFilter = when {
                showAll -> true
                showSystem -> app.isSystem
                showUser -> !app.isSystem
                else -> true
            }
            val matchesSearch = query.isEmpty() ||
                app.name.lowercase().contains(query) ||
                app.pkg.lowercase().contains(query)
            matchesFilter && matchesSearch
        })

        adapter.notifyDataSetChanged()
        _binding?.tvAppCount?.text = "${filteredApps.size} aplicativos"
    }

    private fun formatSize(bytes: Long): String {
        return when {
            bytes >= 1L shl 30 -> String.format("%.1f GB", bytes.toFloat() / (1L shl 30))
            bytes >= 1L shl 20 -> String.format("%.1f MB", bytes.toFloat() / (1L shl 20))
            bytes >= 1L shl 10 -> String.format("%.1f KB", bytes.toFloat() / (1L shl 10))
            else -> "$bytes B"
        }
    }

    // ═════════════════════════════════════════════════════════════════
    //  ADAPTER
    // ═════════════════════════════════════════════════════════════════

    inner class AppAdapter : RecyclerView.Adapter<AppAdapter.VH>() {
        inner class VH(val b: ItemAppBinding) : RecyclerView.ViewHolder(b.root)

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH {
            val b = ItemAppBinding.inflate(LayoutInflater.from(parent.context), parent, false)
            return VH(b)
        }

        override fun onBindViewHolder(holder: VH, position: Int) {
            val app = filteredApps[position]
            holder.b.tvAppName.text = app.name
            holder.b.tvAppPackage.text = app.pkg
            holder.b.tvAppVersion.text = "v${app.versionName} • ${formatSize(app.apkSize)}"

            if (app.icon != null) {
                holder.b.ivAppIcon.setImageDrawable(app.icon)
            } else {
                holder.b.ivAppIcon.setImageResource(R.drawable.ic_apps)
            }

            holder.b.ivAppMenu.setOnClickListener { v ->
                showAppMenu(v, app)
            }

            holder.itemView.setOnClickListener {
                showAppInfo(app)
            }
        }

        override fun getItemCount() = filteredApps.size
    }

    private fun showAppMenu(anchor: View, app: AppEntry) {
        val popup = PopupMenu(requireContext(), anchor)
        popup.menu.add(0, 1, 0, "Informações")
        popup.menu.add(0, 2, 0, "Abrir")
        popup.menu.add(0, 3, 0, "Extrair APK")
        if (!app.isSystem) {
            popup.menu.add(0, 4, 0, "Desinstalar")
        }
        popup.menu.add(0, 5, 0, "Forçar parada")
        popup.menu.add(0, 6, 0, "Limpar dados")

        popup.setOnMenuItemClickListener { item ->
            when (item.itemId) {
                1 -> showAppInfo(app)
                2 -> openApp(app.pkg)
                3 -> extractApk(app)
                4 -> uninstallApp(app)
                5 -> forceStop(app.pkg)
                6 -> clearData(app.pkg)
            }
            true
        }
        popup.show()
    }

    private fun showAppInfo(app: AppEntry) {
        AlertDialog.Builder(requireContext())
            .setTitle(app.name)
            .setMessage(
                "Pacote: ${app.pkg}\n" +
                "Versão: ${app.versionName} (${app.versionCode})\n" +
                "APK: ${formatSize(app.apkSize)}\n" +
                "Tipo: ${if (app.isSystem) "Sistema" else "Usuário"}"
            )
            .setPositiveButton("OK", null)
            .show()
    }

    private fun openApp(pkg: String) {
        val intent = requireContext().packageManager.getLaunchIntentForPackage(pkg)
        if (intent != null) {
            startActivity(intent)
        } else {
            Toast.makeText(requireContext(), "Não é possível abrir este app", Toast.LENGTH_SHORT).show()
        }
    }

    private fun extractApk(app: AppEntry) {
        Thread {
            try {
                val sourceApk = File(requireContext().packageManager.getApplicationInfo(app.pkg, 0).sourceDir)
                val dest = File(android.os.Environment.getExternalStoragePublicDirectory(android.os.Environment.DIRECTORY_DOWNLOADS), "${app.pkg}.apk")
                sourceApk.copyTo(dest, overwrite = true)
                activity?.runOnUiThread {
                    Toast.makeText(requireContext(), "APK salvo em Downloads/${app.pkg}.apk", Toast.LENGTH_LONG).show()
                }
            } catch (e: Exception) {
                activity?.runOnUiThread {
                    Toast.makeText(requireContext(), "Erro: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }
        }.start()
    }

    private fun uninstallApp(app: AppEntry) {
        AlertDialog.Builder(requireContext())
            .setTitle("Desinstalar ${app.name}?")
            .setPositiveButton("Desinstalar") { _, _ ->
                val intent = android.content.Intent(android.content.Intent.ACTION_DELETE).apply {
                    data = android.net.Uri.parse("package:${app.pkg}")
                }
                startActivity(intent)
            }
            .setNegativeButton("Cancelar", null)
            .show()
    }

    private fun forceStop(pkg: String) {
        executeShell("am force-stop $pkg", "App encerrado")
    }

    private fun clearData(pkg: String) {
        AlertDialog.Builder(requireContext())
            .setTitle("Limpar dados de $pkg?")
            .setMessage("Isso apagará todos os dados do app.")
            .setPositiveButton("Limpar") { _, _ ->
                executeShell("pm clear $pkg", "Dados limpos")
            }
            .setNegativeButton("Cancelar", null)
            .show()
    }

    private fun executeShell(cmd: String, successMsg: String) {
        Thread {
            try {
                val p = Runtime.getRuntime().exec(arrayOf("sh", "-c", cmd))
                p.waitFor()
                activity?.runOnUiThread {
                    Toast.makeText(requireContext(), successMsg, Toast.LENGTH_SHORT).show()
                }
            } catch (e: Exception) {
                activity?.runOnUiThread {
                    Toast.makeText(requireContext(), "Erro: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }
        }.start()
    }
}
