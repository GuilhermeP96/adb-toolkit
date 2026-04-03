import SwiftUI
import Network

// ─────────────────────────────────────────────────────────────────────
//  TransferView.swift — Peer-to-peer data transfer for iOS Agent
//
//  Features:
//    - Role selection: Source / Destination / Relay
//    - Bonjour/NWBrowser peer discovery (_adbtoolkit._tcp)
//    - Data type selection (contacts, photos, apps, etc.)
//    - Transfer orchestration via HTTP API
//
//  Relay mode allows a device (phone or notebook) to act as an
//  intermediary for backup/recovery between two other peers.
// ─────────────────────────────────────────────────────────────────────

struct TransferView: View {

    @EnvironmentObject var state: AgentState

    // Role
    @State private var selectedRole: TransferRole = .source

    // Peers
    @State private var peers: [PeerInfo] = []
    @State private var selectedPeer: PeerInfo?
    @State private var isDiscovering = false

    // Data types
    @State private var transferContacts = true
    @State private var transferPhotos = true
    @State private var transferApps = false
    @State private var transferSms = false
    @State private var transferFiles = false
    @State private var transferWifi = false

    // Transfer progress
    @State private var isTransferring = false
    @State private var transferStatus = ""
    @State private var transferProgress: Double = 0

    // NWBrowser for Bonjour discovery
    @State private var browser: NWBrowser?
    @State private var listener: NWListener?

    enum TransferRole: String, CaseIterable {
        case source = "source"
        case dest = "dest"
        case relay = "relay"

        var label: String {
            switch self {
            case .source: return "Origem"
            case .dest:   return "Destino"
            case .relay:  return "Relay"
            }
        }

        var description: String {
            switch self {
            case .source: return "Este dispositivo enviará dados para o par selecionado"
            case .dest:   return "Este dispositivo receberá dados do par selecionado"
            case .relay:  return "Este dispositivo será intermediário para backup/recovery entre dois outros pares"
            }
        }

        var icon: String {
            switch self {
            case .source: return "arrow.up.circle.fill"
            case .dest:   return "arrow.down.circle.fill"
            case .relay:  return "arrow.triangle.2.circlepath.circle.fill"
            }
        }
    }

    struct PeerInfo: Identifiable, Hashable {
        let id = UUID()
        let name: String
        let host: String
        let port: UInt16
        let role: String
        let platform: String

        func hash(into hasher: inout Hasher) {
            hasher.combine(host)
            hasher.combine(port)
        }

        static func == (lhs: PeerInfo, rhs: PeerInfo) -> Bool {
            lhs.host == rhs.host && lhs.port == rhs.port
        }
    }

    var body: some View {
        NavigationView {
            List {
                // ── Role selection ──
                Section("Papel do Dispositivo") {
                    Picker("Papel", selection: $selectedRole) {
                        ForEach(TransferRole.allCases, id: \.self) { role in
                            Label(role.label, systemImage: role.icon)
                                .tag(role)
                        }
                    }
                    .pickerStyle(.segmented)
                    .onChange(of: selectedRole) { _ in
                        restartAdvertising()
                    }

                    Text(selectedRole.description)
                        .font(.caption)
                        .foregroundColor(.secondary)
                }

                // ── Peer discovery ──
                Section("Dispositivos na Rede") {
                    HStack {
                        if isDiscovering {
                            ProgressView()
                                .padding(.trailing, 8)
                        }
                        Text(isDiscovering ? "Procurando..." : "\(peers.count) dispositivo(s)")
                            .foregroundColor(.secondary)
                        Spacer()
                        Button(action: startDiscovery) {
                            Image(systemName: "arrow.clockwise")
                        }
                    }

                    if peers.isEmpty && !isDiscovering {
                        Text("Nenhum dispositivo encontrado na rede")
                            .foregroundColor(.secondary)
                            .font(.caption)
                    }

                    ForEach(peers) { peer in
                        HStack {
                            Image(systemName: peerIcon(for: peer.platform))
                                .foregroundColor(selectedPeer == peer ? .blue : .primary)
                                .frame(width: 30)

                            VStack(alignment: .leading, spacing: 2) {
                                Text(peer.name)
                                    .font(.body)
                                Text("\(peer.host):\(peer.port)")
                                    .font(.system(.caption2, design: .monospaced))
                                    .foregroundColor(.secondary)
                                Text(peerRoleLabel(peer))
                                    .font(.caption2)
                                    .foregroundColor(.blue)
                            }

                            Spacer()

                            Circle()
                                .fill(selectedPeer == peer ? Color.blue : Color.green)
                                .frame(width: 10, height: 10)
                        }
                        .contentShape(Rectangle())
                        .onTapGesture { selectedPeer = peer }
                        .listRowBackground(selectedPeer == peer ? Color.blue.opacity(0.1) : nil)
                    }
                }

                // ── Data types ──
                if selectedPeer != nil {
                    Section("Dados para Transferir") {
                        Toggle("Contatos", isOn: $transferContacts)
                        Toggle("Fotos e Vídeos", isOn: $transferPhotos)
                        Toggle("Apps Instalados", isOn: $transferApps)
                        Toggle("SMS / Mensagens", isOn: $transferSms)
                        Toggle("Arquivos", isOn: $transferFiles)
                        Toggle("Redes Wi-Fi", isOn: $transferWifi)
                    }

                    // ── Transfer action ──
                    Section {
                        if isTransferring {
                            VStack(alignment: .leading, spacing: 8) {
                                ProgressView(value: transferProgress)
                                Text(transferStatus)
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                            }
                        }

                        Button(action: startTransfer) {
                            HStack {
                                Image(systemName: "arrow.left.arrow.right")
                                Text("Iniciar Transferência")
                            }
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(isTransferring || !hasSelectedDataTypes)
                    }
                }
            }
            .navigationTitle("Transferir")
            .navigationBarTitleDisplayMode(.inline)
            .onAppear {
                startDiscovery()
                startAdvertising()
            }
            .onDisappear {
                stopDiscovery()
                stopAdvertising()
            }
        }
    }

    // MARK: - Bonjour Discovery

    private func startDiscovery() {
        stopDiscovery()
        peers = []
        selectedPeer = nil
        isDiscovering = true

        let params = NWParameters()
        params.includePeerToPeer = true
        let newBrowser = NWBrowser(for: .bonjour(type: "_adbtoolkit._tcp", domain: nil), using: params)

        newBrowser.browseResultsChangedHandler = { results, _ in
            DispatchQueue.main.async {
                self.handleBrowseResults(results)
            }
        }

        newBrowser.stateUpdateHandler = { newState in
            if case .failed = newState {
                DispatchQueue.main.async {
                    self.isDiscovering = false
                }
            }
        }

        newBrowser.start(queue: .main)
        browser = newBrowser

        // Stop loading indicator after 5 seconds
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
            self.isDiscovering = false
        }
    }

    private func stopDiscovery() {
        browser?.cancel()
        browser = nil
        isDiscovering = false
    }

    private func handleBrowseResults(_ results: Set<NWBrowser.Result>) {
        let localIP = state.localIPAddress
        let localPort = state.httpPort

        for result in results {
            if case .service(let name, _, _, _) = result.endpoint {
                // Resolve via NWConnection probe
                let conn = NWConnection(to: result.endpoint, using: .tcp)
                conn.stateUpdateHandler = { connState in
                    if case .ready = connState {
                        if let path = conn.currentPath,
                           let endpoint = path.remoteEndpoint,
                           case .hostPort(let host, let port) = endpoint {
                            let hostStr = "\(host)"
                                .replacingOccurrences(of: "%.*", with: "", options: .regularExpression)
                            let portVal = port.rawValue

                            // Skip self
                            if hostStr == localIP && portVal == localPort {
                                conn.cancel()
                                return
                            }

                            let peer = PeerInfo(
                                name: name,
                                host: hostStr,
                                port: portVal,
                                role: "",
                                platform: "unknown"
                            )

                            DispatchQueue.main.async {
                                if !self.peers.contains(where: { $0.host == hostStr && $0.port == portVal }) {
                                    self.peers.append(peer)
                                }
                            }
                        }
                        conn.cancel()
                    }
                }
                conn.start(queue: .global())
            }
        }
    }

    // MARK: - Bonjour Advertising

    private func startAdvertising() {
        stopAdvertising()

        do {
            let newListener = try NWListener(using: .tcp, on: NWEndpoint.Port(rawValue: state.httpPort + 100) ?? 15655)
            newListener.service = NWListener.Service(
                name: "\(UIDevice.current.name)-\(state.httpPort)",
                type: "_adbtoolkit._tcp"
            )
            newListener.start(queue: .main)
            listener = newListener
        } catch {
            state.log("transfer", "Failed to advertise: \(error)")
        }
    }

    private func restartAdvertising() {
        stopAdvertising()
        startAdvertising()
    }

    private func stopAdvertising() {
        listener?.cancel()
        listener = nil
    }

    // MARK: - Transfer

    private var hasSelectedDataTypes: Bool {
        transferContacts || transferPhotos || transferApps || transferSms || transferFiles || transferWifi
    }

    private func startTransfer() {
        guard let peer = selectedPeer else { return }

        isTransferring = true
        transferProgress = 0
        transferStatus = "Iniciando transferência..."

        var dataTypes: [String] = []
        if transferContacts { dataTypes.append("contacts") }
        if transferPhotos   { dataTypes.append("photos") }
        if transferApps     { dataTypes.append("apps") }
        if transferSms      { dataTypes.append("sms") }
        if transferFiles    { dataTypes.append("files") }
        if transferWifi     { dataTypes.append("wifi") }

        state.log("transfer", "Starting \(selectedRole.rawValue) transfer to \(peer.host):\(peer.port) [\(dataTypes.joined(separator: ","))]")

        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let result = try self.performTransfer(peer: peer, dataTypes: dataTypes)

                DispatchQueue.main.async {
                    self.transferProgress = 1.0
                    self.transferStatus = result
                    self.isTransferring = false
                    self.state.log("transfer", "Completed: \(result)")
                }
            } catch {
                DispatchQueue.main.async {
                    self.transferStatus = "Erro: \(error.localizedDescription)"
                    self.isTransferring = false
                    self.state.log("transfer", "Failed: \(error)")
                }
            }
        }
    }

    private func performTransfer(peer: PeerInfo, dataTypes: [String]) throws -> String {
        let localIP = state.localIPAddress
        let localPort = state.httpPort

        let payload: [String: Any] = [
            "source": selectedRole == .source ? "\(localIP):\(localPort)" : "\(peer.host):\(peer.port)",
            "destination": selectedRole == .dest ? "\(localIP):\(localPort)" : "\(peer.host):\(peer.port)",
            "relay": selectedRole == .relay ? "\(localIP):\(localPort)" : "",
            "data_types": dataTypes.joined(separator: ","),
            "role": selectedRole.rawValue
        ]

        let targetHost = selectedRole == .source ? "\(localIP):\(localPort)" : "\(peer.host):\(peer.port)"
        guard let url = URL(string: "http://\(targetHost)/api/orchestrator/transfer") else {
            throw URLError(.badURL)
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(state.authToken, forHTTPHeaderField: "X-Agent-Token")
        request.timeoutInterval = 60

        request.httpBody = try JSONSerialization.data(withJSONObject: payload)

        let semaphore = DispatchSemaphore(value: 0)
        var responseData: Data?
        var responseError: Error?

        URLSession.shared.dataTask(with: request) { data, response, error in
            responseData = data
            responseError = error
            if let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode >= 400 {
                responseError = NSError(domain: "Transfer", code: httpResponse.statusCode,
                                       userInfo: [NSLocalizedDescriptionKey: "HTTP \(httpResponse.statusCode)"])
            }
            semaphore.signal()
        }.resume()

        semaphore.wait()

        if let error = responseError {
            throw error
        }

        return "Transferência OK — \(dataTypes.count) tipos de dados"
    }

    // MARK: - Helpers

    private func peerIcon(for platform: String) -> String {
        switch platform {
        case "android": return "phone.fill"
        case "ios":     return "iphone"
        case "desktop": return "laptopcomputer"
        default:        return "desktopcomputer"
        }
    }

    private func peerRoleLabel(_ peer: PeerInfo) -> String {
        let roleText: String
        switch peer.role {
        case "source": roleText = "Origem"
        case "dest":   roleText = "Destino"
        case "relay":  roleText = "Relay"
        default:       roleText = ""
        }

        let platformText: String
        switch peer.platform {
        case "android": platformText = "Android"
        case "ios":     platformText = "iOS"
        case "desktop": platformText = "Desktop"
        default:        platformText = ""
        }

        if !roleText.isEmpty && !platformText.isEmpty {
            return "\(roleText) • \(platformText)"
        }
        return roleText.isEmpty ? platformText : roleText
    }
}
