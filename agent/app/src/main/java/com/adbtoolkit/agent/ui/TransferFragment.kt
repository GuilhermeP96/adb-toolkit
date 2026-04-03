package com.adbtoolkit.agent.ui

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Toast
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.adbtoolkit.agent.AgentApp
import com.adbtoolkit.agent.R
import com.adbtoolkit.agent.databinding.FragmentTransferBinding
import com.adbtoolkit.agent.databinding.ItemPeerBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.NetworkInterface
import java.net.URL

/**
 * Transfer screen — peer discovery via mDNS/NSD, role selection
 * (source / destination / relay), and data transfer orchestration.
 *
 * Phones and notebook instances on the network all register as
 * _adbtoolkit._tcp peers. The relay role acts as an intermediary
 * for backup/recovery between two other peers.
 */
class TransferFragment : Fragment() {

    companion object {
        private const val SERVICE_TYPE = "_adbtoolkit._tcp."
        private const val TAG = "TransferFragment"
    }

    private var _binding: FragmentTransferBinding? = null
    private val binding get() = _binding!!

    private val peers = mutableListOf<PeerInfo>()
    private val adapter = PeerAdapter()

    private var nsdManager: NsdManager? = null
    private var discoveryActive = false
    private var registeredService: NsdServiceInfo? = null
    private var selectedRole = "source" // source | dest | relay
    private var selectedPeer: PeerInfo? = null

    data class PeerInfo(
        val name: String,
        val host: String,
        val port: Int,
        val role: String = "",      // reported role
        val platform: String = "",  // android | ios | desktop
        var selected: Boolean = false
    )

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        _binding = FragmentTransferBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)

        binding.rvPeers.layoutManager = LinearLayoutManager(requireContext())
        binding.rvPeers.adapter = adapter

        nsdManager = requireContext().getSystemService(Context.NSD_SERVICE) as NsdManager

        setupRoleToggle()
        binding.btnRefreshPeers.setOnClickListener { startDiscovery() }
        binding.btnStartTransfer.setOnClickListener { startTransfer() }

        // Default role: source
        binding.btnRoleSource.isChecked = true
        updateRoleDescription("source")
    }

    override fun onResume() {
        super.onResume()
        registerSelf()
        startDiscovery()
    }

    override fun onPause() {
        super.onPause()
        stopDiscovery()
        unregisterSelf()
    }

    override fun onDestroyView() {
        super.onDestroyView()
        _binding = null
    }

    // ═════════════════════════════════════════════════════════════════
    //  ROLE SELECTION
    // ═════════════════════════════════════════════════════════════════

    private fun setupRoleToggle() {
        binding.toggleRole.addOnButtonCheckedListener { _, checkedId, isChecked ->
            if (!isChecked) return@addOnButtonCheckedListener
            selectedRole = when (checkedId) {
                R.id.btnRoleSource -> "source"
                R.id.btnRoleDest   -> "dest"
                R.id.btnRoleRelay  -> "relay"
                else -> "source"
            }
            updateRoleDescription(selectedRole)
            // Re-register with new role
            unregisterSelf()
            registerSelf()
        }
    }

    private fun updateRoleDescription(role: String) {
        binding.tvRoleDescription.text = when (role) {
            "source" -> getString(R.string.transfer_role_source_desc)
            "dest"   -> getString(R.string.transfer_role_dest_desc)
            "relay"  -> getString(R.string.transfer_role_relay_desc)
            else -> ""
        }
    }

    // ═════════════════════════════════════════════════════════════════
    //  mDNS/NSD — REGISTER THIS DEVICE
    // ═════════════════════════════════════════════════════════════════

    private fun registerSelf() {
        val serviceInfo = NsdServiceInfo().apply {
            serviceName = "${android.os.Build.MODEL}-${AgentApp.HTTP_PORT}"
            serviceType = "_adbtoolkit._tcp"
            port = AgentApp.HTTP_PORT
            setAttribute("role", selectedRole)
            setAttribute("platform", "android")
            setAttribute("version", AgentApp.VERSION)
        }

        nsdManager?.registerService(serviceInfo, NsdManager.PROTOCOL_DNS_SD, object : NsdManager.RegistrationListener {
            override fun onServiceRegistered(info: NsdServiceInfo) {
                registeredService = info
            }
            override fun onRegistrationFailed(info: NsdServiceInfo, errorCode: Int) {}
            override fun onServiceUnregistered(info: NsdServiceInfo) {
                registeredService = null
            }
            override fun onUnregistrationFailed(info: NsdServiceInfo, errorCode: Int) {}
        })
    }

    private fun unregisterSelf() {
        if (registeredService != null) {
            try {
                nsdManager?.unregisterService(object : NsdManager.RegistrationListener {
                    override fun onServiceRegistered(info: NsdServiceInfo) {}
                    override fun onRegistrationFailed(info: NsdServiceInfo, errorCode: Int) {}
                    override fun onServiceUnregistered(info: NsdServiceInfo) {}
                    override fun onUnregistrationFailed(info: NsdServiceInfo, errorCode: Int) {}
                })
            } catch (_: Exception) {}
            registeredService = null
        }
    }

    // ═════════════════════════════════════════════════════════════════
    //  mDNS/NSD — DISCOVER PEERS
    // ═════════════════════════════════════════════════════════════════

    private fun startDiscovery() {
        stopDiscovery()
        peers.clear()
        adapter.notifyDataSetChanged()
        binding.progressDiscovery.visibility = View.VISIBLE
        binding.tvNoPeers.visibility = View.GONE

        val localIp = getLocalIp()

        nsdManager?.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {
                discoveryActive = true
            }

            override fun onServiceFound(serviceInfo: NsdServiceInfo) {
                nsdManager?.resolveService(serviceInfo, object : NsdManager.ResolveListener {
                    override fun onResolveFailed(info: NsdServiceInfo, errorCode: Int) {}
                    override fun onServiceResolved(info: NsdServiceInfo) {
                        val host = info.host?.hostAddress ?: return
                        // Skip self
                        if (host == localIp && info.port == AgentApp.HTTP_PORT) return

                        val role = info.attributes["role"]?.let { String(it) } ?: ""
                        val platform = info.attributes["platform"]?.let { String(it) } ?: "unknown"

                        val peer = PeerInfo(
                            name = info.serviceName,
                            host = host,
                            port = info.port,
                            role = role,
                            platform = platform
                        )

                        activity?.runOnUiThread {
                            if (peers.none { it.host == host && it.port == info.port }) {
                                peers.add(peer)
                                adapter.notifyItemInserted(peers.size - 1)
                                binding.tvNoPeers.visibility = View.GONE
                            }
                        }
                    }
                })
            }

            override fun onServiceLost(serviceInfo: NsdServiceInfo) {
                activity?.runOnUiThread {
                    val idx = peers.indexOfFirst { it.name == serviceInfo.serviceName }
                    if (idx >= 0) {
                        peers.removeAt(idx)
                        adapter.notifyItemRemoved(idx)
                        if (peers.isEmpty()) binding.tvNoPeers.visibility = View.VISIBLE
                    }
                }
            }

            override fun onDiscoveryStopped(serviceType: String) {
                discoveryActive = false
            }
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                discoveryActive = false
                activity?.runOnUiThread {
                    binding.progressDiscovery.visibility = View.GONE
                    binding.tvNoPeers.visibility = View.VISIBLE
                }
            }
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
        })

        // Hide progress after a delay (discovery is continuous)
        binding.root.postDelayed({
            _binding?.progressDiscovery?.visibility = View.GONE
            if (peers.isEmpty()) _binding?.tvNoPeers?.visibility = View.VISIBLE
        }, 5000)
    }

    private fun stopDiscovery() {
        if (discoveryActive) {
            try {
                nsdManager?.stopServiceDiscovery(object : NsdManager.DiscoveryListener {
                    override fun onDiscoveryStarted(serviceType: String) {}
                    override fun onServiceFound(serviceInfo: NsdServiceInfo) {}
                    override fun onServiceLost(serviceInfo: NsdServiceInfo) {}
                    override fun onDiscoveryStopped(serviceType: String) {}
                    override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {}
                    override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
                })
            } catch (_: Exception) {}
            discoveryActive = false
        }
    }

    // ═════════════════════════════════════════════════════════════════
    //  TRANSFER
    // ═════════════════════════════════════════════════════════════════

    private fun startTransfer() {
        val peer = selectedPeer
        if (peer == null) {
            Toast.makeText(requireContext(), "Selecione um par na lista", Toast.LENGTH_SHORT).show()
            return
        }

        val dataTypes = buildList {
            if (binding.cbContacts.isChecked)  add("contacts")
            if (binding.cbPhotos.isChecked)    add("photos")
            if (binding.cbApps.isChecked)      add("apps")
            if (binding.cbSms.isChecked)       add("sms")
            if (binding.cbFiles.isChecked)     add("files")
            if (binding.cbWifi.isChecked)      add("wifi")
        }

        if (dataTypes.isEmpty()) {
            Toast.makeText(requireContext(), "Selecione ao menos um tipo de dado", Toast.LENGTH_SHORT).show()
            return
        }

        binding.cardDataTypes.visibility = View.GONE
        binding.cardProgress.visibility = View.VISIBLE
        binding.btnStartTransfer.isEnabled = false
        binding.progressTransfer.isIndeterminate = true
        binding.tvTransferDetail.text = "Iniciando transferência..."
        binding.tvTransferSpeed.text = ""

        lifecycleScope.launch {
            try {
                val result = withContext(Dispatchers.IO) {
                    initiateTransfer(peer, dataTypes)
                }

                binding.progressTransfer.isIndeterminate = false
                binding.progressTransfer.progress = 100
                binding.tvTransferDetail.text = result
                binding.tvTransferSpeed.text = "Concluído"
                Toast.makeText(requireContext(), "Transferência concluída", Toast.LENGTH_LONG).show()
            } catch (e: Exception) {
                binding.tvTransferDetail.text = "Erro: ${e.message}"
                binding.tvTransferSpeed.text = ""
                Toast.makeText(requireContext(), "Falha na transferência", Toast.LENGTH_LONG).show()
            } finally {
                binding.btnStartTransfer.isEnabled = true
            }
        }
    }

    private fun initiateTransfer(peer: PeerInfo, dataTypes: List<String>): String {
        val localIp = getLocalIp()
        val payload = JSONObject().apply {
            put("source", if (selectedRole == "source") "$localIp:${AgentApp.HTTP_PORT}" else "${peer.host}:${peer.port}")
            put("destination", if (selectedRole == "dest") "$localIp:${AgentApp.HTTP_PORT}" else "${peer.host}:${peer.port}")
            if (selectedRole == "relay") {
                put("relay", "$localIp:${AgentApp.HTTP_PORT}")
            }
            put("data_types", dataTypes.joinToString(","))
            put("role", selectedRole)
        }

        // Call the peer's orchestrator transfer endpoint
        val targetHost = if (selectedRole == "source") "$localIp:${AgentApp.HTTP_PORT}" else "${peer.host}:${peer.port}"
        val url = URL("http://$targetHost/api/orchestrator/transfer")
        val conn = url.openConnection() as HttpURLConnection
        conn.requestMethod = "POST"
        conn.setRequestProperty("Content-Type", "application/json")
        conn.setRequestProperty("X-Agent-Token", AgentApp.authToken)
        conn.connectTimeout = 10000
        conn.readTimeout = 60000
        conn.doOutput = true

        conn.outputStream.use { os ->
            os.write(payload.toString().toByteArray(Charsets.UTF_8))
        }

        val responseCode = conn.responseCode
        val body = if (responseCode in 200..299) {
            BufferedReader(InputStreamReader(conn.inputStream)).use { it.readText() }
        } else {
            BufferedReader(InputStreamReader(conn.errorStream)).use { it.readText() }
        }
        conn.disconnect()

        if (responseCode !in 200..299) {
            throw RuntimeException("HTTP $responseCode: $body")
        }

        return "Transferência OK — ${dataTypes.size} tipos de dados"
    }

    // ═════════════════════════════════════════════════════════════════
    //  HELPERS
    // ═════════════════════════════════════════════════════════════════

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
    //  PEER ADAPTER
    // ═════════════════════════════════════════════════════════════════

    inner class PeerAdapter : RecyclerView.Adapter<PeerAdapter.VH>() {

        inner class VH(val b: ItemPeerBinding) : RecyclerView.ViewHolder(b.root)

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH {
            val b = ItemPeerBinding.inflate(LayoutInflater.from(parent.context), parent, false)
            return VH(b)
        }

        override fun onBindViewHolder(holder: VH, position: Int) {
            val peer = peers[position]
            holder.b.tvPeerName.text = peer.name
            holder.b.tvPeerAddress.text = "${peer.host}:${peer.port}"

            val roleLabel = when (peer.role) {
                "source" -> "Origem"
                "dest"   -> "Destino"
                "relay"  -> "Relay"
                else -> peer.platform.replaceFirstChar { it.uppercase() }
            }
            val platformLabel = when (peer.platform) {
                "android" -> "Android"
                "ios"     -> "iOS"
                "desktop" -> "Desktop"
                else -> ""
            }
            holder.b.tvPeerRole.text = if (platformLabel.isNotEmpty()) "$roleLabel • $platformLabel" else roleLabel

            // Status dot color
            holder.b.viewPeerStatus.setBackgroundColor(
                if (peer.selected)
                    requireContext().getColor(android.R.color.holo_blue_dark)
                else
                    requireContext().getColor(android.R.color.holo_green_dark)
            )

            holder.itemView.setOnClickListener {
                // Deselect previous
                val prevIdx = peers.indexOfFirst { it.selected }
                if (prevIdx >= 0) {
                    peers[prevIdx] = peers[prevIdx].copy(selected = false)
                    notifyItemChanged(prevIdx)
                }
                // Select this
                peers[position] = peers[position].copy(selected = true)
                notifyItemChanged(position)
                selectedPeer = peers[position]

                // Show data types card
                binding.cardDataTypes.visibility = View.VISIBLE
            }
        }

        override fun getItemCount() = peers.size
    }
}
