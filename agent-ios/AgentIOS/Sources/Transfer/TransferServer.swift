import Foundation
import Network
import CryptoKit

// ─────────────────────────────────────────────────────────────────────
//  TransferServer.swift — High-speed TCP binary transfer
//
//  Same protocol as Android agent:
//  ┌──────────────────────────────────────────┐
//  │  HEADER (512 bytes, JSON padded)         │
//  │  { "op": "push"|"pull",                 │
//  │    "path": "...",                        │
//  │    "size": N,                            │
//  │    "token": "..." }                      │
//  ├──────────────────────────────────────────┤
//  │  BINARY PAYLOAD (raw bytes)              │
//  ├──────────────────────────────────────────┤
//  │  FOOTER: SHA-256 hash (32 bytes)         │
//  └──────────────────────────────────────────┘
// ─────────────────────────────────────────────────────────────────────

final class TransferServer {

    private let port: UInt16
    private let authToken: String
    private let pairingManager: PairingManager
    private var listener: NWListener?
    private let queue = DispatchQueue(label: "com.adbtoolkit.ios.transfer", qos: .userInitiated)

    static let headerSize = 512
    static let bufferSize = 256 * 1024  // 256 KB

    private(set) var totalBytesTransferred: Int64 = 0
    private(set) var activeTransfers: Int = 0

    init(port: UInt16, authToken: String, pairingManager: PairingManager) {
        self.port = port
        self.authToken = authToken
        self.pairingManager = pairingManager
    }

    func start() {
        do {
            let params = NWParameters.tcp
            params.allowLocalEndpointReuse = true
            listener = try NWListener(using: params, on: NWEndpoint.Port(rawValue: port)!)
        } catch {
            AgentState.shared.log("transfer", "Failed to create listener: \(error)")
            return
        }

        listener?.newConnectionHandler = { [weak self] connection in
            self?.handleConnection(connection)
        }

        listener?.stateUpdateHandler = { state in
            switch state {
            case .ready:
                AgentState.shared.log("transfer", "Transfer server listening on port \(self.port)")
            case .failed(let error):
                AgentState.shared.log("transfer", "Listener failed: \(error)")
            default:
                break
            }
        }

        listener?.start(queue: queue)
    }

    func stop() {
        listener?.cancel()
        listener = nil
    }

    // MARK: - Connection handling

    private func handleConnection(_ connection: NWConnection) {
        connection.start(queue: queue)
        activeTransfers += 1

        // Read the 512-byte header first
        connection.receive(minimumIncompleteLength: Self.headerSize,
                           maximumLength: Self.headerSize) { [weak self] data, _, _, error in
            guard let self = self, let data = data, data.count == Self.headerSize else {
                connection.cancel()
                self?.activeTransfers -= 1
                return
            }

            self.processHeader(data, connection: connection)
        }
    }

    private func processHeader(_ headerData: Data, connection: NWConnection) {
        // Trim padding null bytes
        let trimmed = headerData.prefix(while: { $0 != 0 })
        guard let json = try? JSONSerialization.jsonObject(with: trimmed) as? [String: Any],
              let op = json["op"] as? String else {
            AgentState.shared.log("transfer", "Invalid header")
            connection.cancel()
            activeTransfers -= 1
            return
        }

        // Auth check
        let token = json["token"] as? String
        if !authToken.isEmpty && token != authToken {
            AgentState.shared.log("transfer", "Auth failed")
            connection.cancel()
            activeTransfers -= 1
            return
        }

        let path = json["path"] as? String ?? ""

        switch op {
        case "push":
            let size = json["size"] as? Int64 ?? 0
            handlePush(connection: connection, path: path, size: size)
        case "pull":
            handlePull(connection: connection, path: path)
        default:
            AgentState.shared.log("transfer", "Unknown op: \(op)")
            connection.cancel()
            activeTransfers -= 1
        }
    }

    // MARK: - Push (receive file)

    private func handlePush(connection: NWConnection, path: String, size: Int64) {
        AgentState.shared.log("transfer", "Receiving \(path) (\(size) bytes)")

        // For iOS, write to app's Documents directory
        let docsDir = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let destURL = docsDir.appendingPathComponent(
            path.hasPrefix("/") ? String(path.dropFirst()) : path
        )

        // Create parent directories
        try? FileManager.default.createDirectory(
            at: destURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )

        guard let fileHandle = try? FileHandle(forWritingTo: destURL) else {
            // Try creating the file first
            FileManager.default.createFile(atPath: destURL.path, contents: nil)
            guard let fh = try? FileHandle(forWritingTo: destURL) else {
                connection.cancel()
                activeTransfers -= 1
                return
            }
            receivePushData(connection: connection, fileHandle: fh, remaining: size, hash: SHA256())
            return
        }
        receivePushData(connection: connection, fileHandle: fileHandle, remaining: size, hash: SHA256())
    }

    private func receivePushData(connection: NWConnection, fileHandle: FileHandle,
                                  remaining: Int64, hash: SHA256) {
        var hash = hash

        if remaining <= 0 {
            // Read the 32-byte SHA-256 footer
            connection.receive(minimumIncompleteLength: 32, maximumLength: 32) {
                [weak self] data, _, _, _ in
                fileHandle.closeFile()
                if let data = data, data.count == 32 {
                    let computed = hash.finalize()
                    let expected = Data(data)
                    let actual = Data(computed)
                    if expected == actual {
                        AgentState.shared.log("transfer", "Push complete, hash verified ✅")
                    } else {
                        AgentState.shared.log("transfer", "Push complete, hash MISMATCH ⚠️")
                    }
                }
                connection.cancel()
                self?.activeTransfers -= 1
            }
            return
        }

        let readSize = min(Int(remaining), Self.bufferSize)
        connection.receive(minimumIncompleteLength: 1, maximumLength: readSize) {
            [weak self] data, _, _, error in
            guard let self = self, let data = data, !data.isEmpty else {
                fileHandle.closeFile()
                connection.cancel()
                self?.activeTransfers -= 1
                return
            }

            fileHandle.write(data)
            hash.update(data: data)
            self.totalBytesTransferred += Int64(data.count)

            self.receivePushData(
                connection: connection,
                fileHandle: fileHandle,
                remaining: remaining - Int64(data.count),
                hash: hash
            )
        }
    }

    // MARK: - Pull (send file)

    private func handlePull(connection: NWConnection, path: String) {
        let docsDir = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let fileURL = docsDir.appendingPathComponent(
            path.hasPrefix("/") ? String(path.dropFirst()) : path
        )

        guard let data = try? Data(contentsOf: fileURL) else {
            AgentState.shared.log("transfer", "File not found: \(path)")
            connection.cancel()
            activeTransfers -= 1
            return
        }

        AgentState.shared.log("transfer", "Sending \(path) (\(data.count) bytes)")

        // Build response header
        let headerJson: [String: Any] = [
            "op": "pull",
            "path": path,
            "size": data.count,
            "status": "ok"
        ]
        var headerData = (try? JSONSerialization.data(withJSONObject: headerJson)) ?? Data()
        headerData.append(Data(count: Self.headerSize - headerData.count))  // Pad to 512 bytes

        // Compute hash
        let hash = SHA256.hash(data: data)
        let hashData = Data(hash)

        // Send: header + payload + hash
        var fullResponse = headerData
        fullResponse.append(data)
        fullResponse.append(hashData)

        connection.send(content: fullResponse, completion: .contentProcessed { [weak self] _ in
            self?.totalBytesTransferred += Int64(data.count)
            self?.activeTransfers -= 1
            connection.cancel()
        })
    }
}
