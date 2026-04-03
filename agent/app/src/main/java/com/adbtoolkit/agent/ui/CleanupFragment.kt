package com.adbtoolkit.agent.ui

import android.os.Bundle
import android.os.Environment
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.CheckBox
import android.widget.Toast
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.adbtoolkit.agent.R
import com.adbtoolkit.agent.databinding.FragmentCleanupBinding
import com.adbtoolkit.agent.databinding.ItemCleanupBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

/**
 * Cleanup screen — scans for junk files, caches and orphan data.
 * Modes mirror the desktop cleanup_manager.py:
 *   app_cache | junk_dirs | junk_files | known_junk | orphans | duplicates
 */
class CleanupFragment : Fragment() {

    private var _binding: FragmentCleanupBinding? = null
    private val binding get() = _binding!!
    private val adapter = CleanupAdapter()
    private val results = mutableListOf<CleanupItem>()

    data class CleanupItem(
        val path: String,
        val category: String,
        val size: Long,
        var selected: Boolean = true
    )

    // Well-known junk directories
    private val knownJunkDirs = listOf(
        "thumbnails", ".thumbnails", ".trash", ".Trash",
        "lost+found", ".cache", "temp", "tmp"
    )

    // Well-known junk file extensions
    private val junkExtensions = setOf(
        "tmp", "bak", "log", "old", "orig", "swp", "pyc"
    )

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        _binding = FragmentCleanupBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)

        binding.rvResults.layoutManager = LinearLayoutManager(requireContext())
        binding.rvResults.adapter = adapter

        binding.btnScan.setOnClickListener { performScan() }
        binding.btnSelectAll.setOnClickListener { toggleSelectAll() }
        binding.btnClean.setOnClickListener { performClean() }
    }

    override fun onDestroyView() {
        super.onDestroyView()
        _binding = null
    }

    // ═════════════════════════════════════════════════════════════════
    //  SCAN
    // ═════════════════════════════════════════════════════════════════

    private fun performScan() {
        val modes = buildList {
            if (binding.cbAppCache.isChecked)  add("app_cache")
            if (binding.cbJunkDirs.isChecked)  add("junk_dirs")
            if (binding.cbJunkFiles.isChecked) add("junk_files")
            if (binding.cbKnownJunk.isChecked) add("known_junk")
            if (binding.cbOrphans.isChecked)   add("orphans")
            if (binding.cbDuplicates.isChecked) add("duplicates")
        }

        if (modes.isEmpty()) {
            Toast.makeText(requireContext(), "Selecione ao menos um modo", Toast.LENGTH_SHORT).show()
            return
        }

        binding.btnScan.isEnabled = false
        binding.tvStatus.text = "Escaneando..."
        binding.cardResults.visibility = View.VISIBLE
        binding.progressScan.visibility = View.VISIBLE
        results.clear()

        lifecycleScope.launch {
            val found = withContext(Dispatchers.IO) { scanFiles(modes) }

            results.addAll(found)
            adapter.notifyDataSetChanged()

            val totalSize = results.sumOf { it.size }
            binding.tvTotalSize.text = "Encontrado: ${formatSize(totalSize)} em ${results.size} itens"
            binding.progressScan.visibility = View.GONE
            binding.btnScan.isEnabled = true
            binding.tvStatus.text = if (results.isEmpty()) "Nenhum item encontrado" else "Escaneamento concluído"
        }
    }

    private fun scanFiles(modes: List<String>): List<CleanupItem> {
        val items = mutableListOf<CleanupItem>()
        val root = Environment.getExternalStorageDirectory()

        if ("app_cache" in modes) {
            // App cache directories under Android/data/*/cache
            val dataDir = File(root, "Android/data")
            if (dataDir.exists() && dataDir.canRead()) {
                dataDir.listFiles()?.forEach { appDir ->
                    val cache = File(appDir, "cache")
                    if (cache.exists() && cache.canRead()) {
                        walkFiles(cache).forEach { f ->
                            items.add(CleanupItem(f.absolutePath, "Cache: ${appDir.name}", f.length()))
                        }
                    }
                }
            }
        }

        if ("junk_dirs" in modes) {
            walkDirs(root).filter { dir ->
                knownJunkDirs.any { dir.name.equals(it, ignoreCase = true) }
            }.forEach { dir ->
                walkFiles(dir).forEach { f ->
                    items.add(CleanupItem(f.absolutePath, "Diretório lixo: ${dir.name}", f.length()))
                }
            }
        }

        if ("junk_files" in modes) {
            walkFiles(root).filter { f ->
                val ext = f.extension.lowercase()
                ext in junkExtensions
            }.forEach { f ->
                items.add(CleanupItem(f.absolutePath, "Arquivo temporário", f.length()))
            }
        }

        if ("known_junk" in modes) {
            // .nomedia files, thumbdata, etc.
            walkFiles(root).filter { f ->
                f.name == ".nomedia" ||
                f.name.startsWith(".thumbdata") ||
                f.name == "Thumbs.db" ||
                f.name == "desktop.ini"
            }.forEach { f ->
                items.add(CleanupItem(f.absolutePath, "Lixo conhecido", f.length()))
            }
        }

        if ("orphans" in modes) {
            // Orphan OBB/data for uninstalled apps
            val pm = try { requireContext().packageManager } catch (_: Exception) { null }
            if (pm != null) {
                listOf("Android/data", "Android/obb").forEach { sub ->
                    val dir = File(root, sub)
                    if (dir.exists() && dir.canRead()) {
                        dir.listFiles()?.forEach { appDir ->
                            val pkg = appDir.name
                            try {
                                pm.getPackageInfo(pkg, 0)
                            } catch (_: Exception) {
                                // Package not installed — orphan
                                walkFiles(appDir).forEach { f ->
                                    items.add(CleanupItem(f.absolutePath, "Órfão: $pkg", f.length()))
                                }
                            }
                        }
                    }
                }
            }
        }

        if ("duplicates" in modes) {
            // Simple duplicate detection by size+name
            val sizeMap = mutableMapOf<Long, MutableList<File>>()
            walkFiles(root).filter { it.length() > 1024 }.forEach { f ->
                sizeMap.getOrPut(f.length()) { mutableListOf() }.add(f)
            }
            sizeMap.values.filter { it.size > 1 }.forEach { group ->
                // Group by name to find actual duplicates
                val byName = group.groupBy { it.name }
                byName.values.filter { it.size > 1 }.forEach { dups ->
                    // Keep the first, mark the rest
                    dups.drop(1).forEach { f ->
                        items.add(CleanupItem(f.absolutePath, "Duplicata de ${dups.first().parent}", f.length()))
                    }
                }
            }
        }

        return items.sortedByDescending { it.size }
    }

    // ═════════════════════════════════════════════════════════════════
    //  CLEAN
    // ═════════════════════════════════════════════════════════════════

    private fun performClean() {
        val selected = results.filter { it.selected }
        if (selected.isEmpty()) {
            Toast.makeText(requireContext(), "Nenhum item selecionado", Toast.LENGTH_SHORT).show()
            return
        }

        binding.btnClean.isEnabled = false
        binding.progressScan.visibility = View.VISIBLE

        lifecycleScope.launch {
            var deletedCount = 0
            var freedSize = 0L

            withContext(Dispatchers.IO) {
                selected.forEach { item ->
                    try {
                        val file = File(item.path)
                        if (file.exists() && file.delete()) {
                            deletedCount++
                            freedSize += item.size
                        }
                    } catch (_: Exception) {}
                }
            }

            results.removeAll { it.selected }
            adapter.notifyDataSetChanged()

            val remaining = results.sumOf { it.size }
            binding.tvTotalSize.text = "Restante: ${formatSize(remaining)} em ${results.size} itens"
            binding.progressScan.visibility = View.GONE
            binding.btnClean.isEnabled = true
            binding.tvStatus.text = "Removidos $deletedCount itens (${formatSize(freedSize)} liberados)"

            Toast.makeText(requireContext(), "${formatSize(freedSize)} liberados", Toast.LENGTH_LONG).show()
        }
    }

    // ═════════════════════════════════════════════════════════════════
    //  HELPERS
    // ═════════════════════════════════════════════════════════════════

    private fun toggleSelectAll() {
        val allSelected = results.all { it.selected }
        results.forEach { it.selected = !allSelected }
        adapter.notifyDataSetChanged()
    }

    private fun walkFiles(root: File): Sequence<File> = sequence {
        val stack = ArrayDeque<File>()
        stack.add(root)
        while (stack.isNotEmpty()) {
            val dir = stack.removeFirst()
            try {
                dir.listFiles()?.forEach { f ->
                    if (f.isDirectory) stack.add(f) else yield(f)
                }
            } catch (_: SecurityException) {}
        }
    }

    private fun walkDirs(root: File): Sequence<File> = sequence {
        val stack = ArrayDeque<File>()
        stack.add(root)
        while (stack.isNotEmpty()) {
            val dir = stack.removeFirst()
            yield(dir)
            try {
                dir.listFiles()?.filter { it.isDirectory }?.forEach { stack.add(it) }
            } catch (_: SecurityException) {}
        }
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

    inner class CleanupAdapter : RecyclerView.Adapter<CleanupAdapter.VH>() {

        inner class VH(val b: ItemCleanupBinding) : RecyclerView.ViewHolder(b.root)

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH {
            val b = ItemCleanupBinding.inflate(LayoutInflater.from(parent.context), parent, false)
            return VH(b)
        }

        override fun onBindViewHolder(holder: VH, position: Int) {
            val item = results[position]
            holder.b.cbItem.isChecked = item.selected
            holder.b.tvCategory.text = item.category
            holder.b.tvPath.text = item.path
            holder.b.tvSize.text = formatSize(item.size)

            holder.b.cbItem.setOnCheckedChangeListener { _, checked ->
                results[holder.adapterPosition].selected = checked
            }
            holder.itemView.setOnClickListener {
                val pos = holder.adapterPosition
                results[pos].selected = !results[pos].selected
                notifyItemChanged(pos)
            }
        }

        override fun getItemCount() = results.size
    }
}
