package com.adbtoolkit.agent.ui

import android.os.Bundle
import android.os.Environment
import android.os.StatFs
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Toast
import androidx.fragment.app.Fragment
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.adbtoolkit.agent.R
import com.adbtoolkit.agent.databinding.FragmentFilesBinding
import com.adbtoolkit.agent.databinding.ItemFileBinding
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * File browser — real-time file system navigation using direct Java File API.
 * Mirrors the Files tab from the PC toolkit, but operates locally.
 */
class FilesFragment : Fragment() {

    private var _binding: FragmentFilesBinding? = null
    private val binding get() = _binding!!

    private var currentPath: File = Environment.getExternalStorageDirectory()
    private val adapter = FileAdapter()
    private val dateFormat = SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.getDefault())

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        _binding = FragmentFilesBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)

        binding.rvFiles.layoutManager = LinearLayoutManager(requireContext())
        binding.rvFiles.adapter = adapter
        adapter.onItemClick = { file ->
            if (file.isDirectory) {
                navigateTo(file)
            } else {
                Toast.makeText(requireContext(), "${file.name} (${formatSize(file.length())})", Toast.LENGTH_SHORT).show()
            }
        }

        binding.btnUp.setOnClickListener {
            currentPath.parentFile?.let { navigateTo(it) }
        }
        binding.btnHome.setOnClickListener {
            navigateTo(Environment.getExternalStorageDirectory())
        }
        binding.btnRefreshFiles.setOnClickListener { loadFiles() }
        binding.swipeRefresh.setOnRefreshListener {
            loadFiles()
            binding.swipeRefresh.isRefreshing = false
        }

        // Quick path chips
        binding.chipSdcard.setOnClickListener { navigateTo(Environment.getExternalStorageDirectory()) }
        binding.chipDCIM.setOnClickListener { navigateTo(File(Environment.getExternalStorageDirectory(), "DCIM")) }
        binding.chipDownload.setOnClickListener { navigateTo(File(Environment.getExternalStorageDirectory(), "Download")) }
        binding.chipDocuments.setOnClickListener { navigateTo(File(Environment.getExternalStorageDirectory(), "Documents")) }
        binding.chipData.setOnClickListener { navigateTo(File(Environment.getExternalStorageDirectory(), "Android/data")) }

        // Actions
        binding.btnNewFolder.setOnClickListener {
            val editText = android.widget.EditText(requireContext()).apply {
                hint = "Nome da pasta"
                setPadding(48, 24, 48, 24)
            }
            android.app.AlertDialog.Builder(requireContext())
                .setTitle("Nova Pasta")
                .setView(editText)
                .setPositiveButton("Criar") { _, _ ->
                    val name = editText.text.toString().trim()
                    if (name.isNotEmpty()) {
                        val dir = File(currentPath, name)
                        if (dir.mkdirs()) {
                            loadFiles()
                            Toast.makeText(requireContext(), "Pasta criada", Toast.LENGTH_SHORT).show()
                        } else {
                            Toast.makeText(requireContext(), "Falha ao criar pasta", Toast.LENGTH_SHORT).show()
                        }
                    }
                }
                .setNegativeButton("Cancelar", null)
                .show()
        }

        binding.btnUpload.setOnClickListener {
            Toast.makeText(requireContext(), "Use o toolkit no PC para enviar arquivos via Agent API", Toast.LENGTH_LONG).show()
        }

        loadFiles()
    }

    override fun onDestroyView() {
        super.onDestroyView()
        _binding = null
    }

    private fun navigateTo(dir: File) {
        if (dir.exists() && dir.canRead()) {
            currentPath = dir
            loadFiles()
        } else {
            Toast.makeText(requireContext(), "Sem permissão: ${dir.path}", Toast.LENGTH_SHORT).show()
        }
    }

    private fun loadFiles() {
        binding.tvCurrentPath.text = currentPath.absolutePath

        val files = try {
            currentPath.listFiles()?.sortedWith(compareBy<File> { !it.isDirectory }.thenBy { it.name.lowercase() }) ?: emptyList()
        } catch (_: SecurityException) {
            Toast.makeText(requireContext(), "Acesso negado", Toast.LENGTH_SHORT).show()
            emptyList()
        }

        adapter.items = files
        adapter.notifyDataSetChanged()
        binding.tvFileCount.text = "${files.size} itens"

        // Storage info
        try {
            val stat = StatFs(currentPath.path)
            val total = stat.totalBytes
            val free = stat.availableBytes
            binding.tvStorageInfo.text = "${formatSize(free)} livres de ${formatSize(total)}"
        } catch (_: Exception) {
            binding.tvStorageInfo.text = ""
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

    inner class FileAdapter : RecyclerView.Adapter<FileAdapter.VH>() {
        var items: List<File> = emptyList()
        var onItemClick: ((File) -> Unit)? = null

        inner class VH(val b: ItemFileBinding) : RecyclerView.ViewHolder(b.root)

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH {
            val b = ItemFileBinding.inflate(LayoutInflater.from(parent.context), parent, false)
            return VH(b)
        }

        override fun onBindViewHolder(holder: VH, position: Int) {
            val file = items[position]
            holder.b.tvFileName.text = file.name

            if (file.isDirectory) {
                holder.b.ivFileIcon.setImageResource(R.drawable.ic_files)
                holder.b.tvFileInfo.text = try {
                    "${file.listFiles()?.size ?: 0} itens"
                } catch (_: Exception) { "---" }
                holder.b.tvFileSize.text = ""
            } else {
                holder.b.ivFileIcon.setImageResource(R.drawable.ic_dashboard)
                holder.b.tvFileInfo.text = dateFormat.format(Date(file.lastModified()))
                holder.b.tvFileSize.text = formatSize(file.length())
            }

            holder.itemView.setOnClickListener { onItemClick?.invoke(file) }
        }

        override fun getItemCount() = items.size
    }
}
