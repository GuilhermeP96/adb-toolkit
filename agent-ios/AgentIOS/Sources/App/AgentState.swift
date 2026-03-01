import Foundation
import Combine

// ─────────────────────────────────────────────────────────────────────
//  AgentState.swift — Observable global state
// ─────────────────────────────────────────────────────────────────────

final class AgentState: ObservableObject {

    static let shared = AgentState()

    // Published state
    @Published var isRunning = false
    @Published var httpPort: UInt16 = AgentConstants.httpPort
    @Published var transferPort: UInt16 = AgentConstants.transferPort
    @Published var authToken: String = ""
    @Published var connectedClients: Int = 0
    @Published var pairedDevices: Int = 0
    @Published var logLines: [LogEntry] = []
    @Published var localIPAddress: String = "—"

    // Subsystems
    private(set) var httpServer: HTTPServer?
    private(set) var transferServer: TransferServer?
    private(set) var pairingManager: PairingManager?

    struct LogEntry: Identifiable {
        let id = UUID()
        let timestamp: Date
        let source: String
        let message: String
    }

    private init() {
        pairingManager = PairingManager()
        pairedDevices = pairingManager?.pairedDeviceCount ?? 0
        refreshIPAddress()
    }

    // MARK: - Server lifecycle

    func startServer(token: String = "") {
        guard !isRunning else { return }

        authToken = token.isEmpty ? Self.generateToken() : token

        let pairing = pairingManager ?? PairingManager()
        pairingManager = pairing

        httpServer = HTTPServer(
            port: httpPort,
            authToken: authToken,
            pairingManager: pairing
        )
        transferServer = TransferServer(
            port: transferPort,
            authToken: authToken,
            pairingManager: pairing
        )

        httpServer?.start()
        transferServer?.start()

        isRunning = true
        refreshIPAddress()
        log("server", "Agent started on port \(httpPort)")
    }

    func stopServer() {
        httpServer?.stop()
        transferServer?.stop()
        httpServer = nil
        transferServer = nil
        isRunning = false
        log("server", "Agent stopped")
    }

    // MARK: - Logging

    func log(_ source: String, _ message: String) {
        let entry = LogEntry(timestamp: Date(), source: source, message: message)
        DispatchQueue.main.async {
            self.logLines.append(entry)
            // Keep last 500 lines
            if self.logLines.count > 500 {
                self.logLines.removeFirst(self.logLines.count - 500)
            }
        }
    }

    // MARK: - Helpers

    func refreshIPAddress() {
        localIPAddress = NetworkUtils.getWiFiIPAddress() ?? "—"
    }

    private static func generateToken() -> String {
        let bytes = (0..<16).map { _ in UInt8.random(in: 0...255) }
        return bytes.map { String(format: "%02x", $0) }.joined()
    }
}
