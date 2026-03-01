import Foundation
import CryptoKit

// ─────────────────────────────────────────────────────────────────────
//  PairingManager.swift — ECDH + HMAC P2P pairing (CryptoKit)
//
//  Compatible with the Android PairingManager — same protocol:
//  1. Exchange ECDH P-256 public keys
//  2. Derive shared secret via SHA-256(ECDH)
//  3. Display 6-digit confirm code (deterministic from both pubkeys)
//  4. User confirms visually on both devices
//  5. All subsequent requests signed via HMAC-SHA256
// ─────────────────────────────────────────────────────────────────────

final class PairingManager {

    // MARK: - Data types

    struct PairedDevice: Codable {
        let peerId: String
        let name: String
        let publicKeyBase64: String
        let sharedSecret: Data
        let pairedAt: Date
    }

    struct PendingPairing {
        let peerId: String
        let name: String
        let peerPublicKey: P256.KeyAgreement.PublicKey
        let sharedSecret: Data
        let confirmCode: String
        let createdAt: Date
    }

    // MARK: - Properties

    let deviceId: String
    private var keyPair: P256.KeyAgreement.PrivateKey
    private var pairedDevices: [String: PairedDevice] = [:]
    private var pendingPairing: PendingPairing?
    private let defaults = UserDefaults.standard
    private let keyPrefix = "agentios_pairing_"

    /// Number of currently paired devices.
    var pairedDeviceCount: Int { pairedDevices.count }

    /// Our public key in base64.
    var publicKeyBase64: String {
        keyPair.publicKey.rawRepresentation.base64EncodedString()
    }

    // MARK: - Init

    init() {
        // Load or generate device ID
        if let stored = defaults.string(forKey: "\(keyPrefix)device_id") {
            deviceId = stored
        } else {
            deviceId = UUID().uuidString
            defaults.set(deviceId, forKey: "\(keyPrefix)device_id")
        }

        // Load or generate ECDH keypair
        if let keyData = defaults.data(forKey: "\(keyPrefix)private_key"),
           let key = try? P256.KeyAgreement.PrivateKey(rawRepresentation: keyData) {
            keyPair = key
        } else {
            keyPair = P256.KeyAgreement.PrivateKey()
            defaults.set(keyPair.rawRepresentation, forKey: "\(keyPrefix)private_key")
        }

        // Load paired devices
        loadPairedDevices()
    }

    // MARK: - Pairing flow

    /// Step 1: Create a pending pairing from a peer's public key.
    func createPendingPairing(peerId: String, name: String, peerPublicKeyBase64: String) -> (confirmCode: String, ourPublicKey: String)? {
        guard let peerKeyData = Data(base64Encoded: peerPublicKeyBase64),
              let peerPublicKey = try? P256.KeyAgreement.PublicKey(rawRepresentation: peerKeyData) else {
            return nil
        }

        guard let sharedSecret = deriveSharedSecret(peerPublicKey: peerPublicKey) else {
            return nil
        }

        let confirmCode = generateConfirmCode(
            ourPublicKey: keyPair.publicKey,
            peerPublicKey: peerPublicKey
        )

        pendingPairing = PendingPairing(
            peerId: peerId,
            name: name,
            peerPublicKey: peerPublicKey,
            sharedSecret: sharedSecret,
            confirmCode: confirmCode,
            createdAt: Date()
        )

        return (confirmCode: confirmCode, ourPublicKey: publicKeyBase64)
    }

    /// Step 2: User confirms the 6-digit code matches → approve.
    func approvePairing() -> Bool {
        guard let pending = pendingPairing else { return false }

        let device = PairedDevice(
            peerId: pending.peerId,
            name: pending.name,
            publicKeyBase64: pending.peerPublicKey.rawRepresentation.base64EncodedString(),
            sharedSecret: pending.sharedSecret,
            pairedAt: Date()
        )

        pairedDevices[pending.peerId] = device
        savePairedDevices()
        pendingPairing = nil

        AgentState.shared.log("pairing", "Paired with \(device.name) (\(device.peerId))")
        AgentState.shared.pairedDevices = pairedDevices.count
        return true
    }

    /// Reject pending pairing.
    func rejectPairing() {
        if let pending = pendingPairing {
            AgentState.shared.log("pairing", "Rejected pairing from \(pending.name)")
        }
        pendingPairing = nil
    }

    /// Remove a paired device.
    func unpair(peerId: String) {
        pairedDevices.removeValue(forKey: peerId)
        savePairedDevices()
        AgentState.shared.pairedDevices = pairedDevices.count
    }

    /// Get all paired devices.
    func getPairedDevices() -> [PairedDevice] {
        Array(pairedDevices.values)
    }

    // MARK: - Request signing / verification

    /// Sign a request with HMAC-SHA256.
    func sign(peerId: String, method: String, uri: String, timestamp: String) -> String? {
        guard let device = pairedDevices[peerId] else { return nil }
        let key = SymmetricKey(data: device.sharedSecret)
        let message = "\(method):\(uri):\(timestamp)"
        let mac = HMAC<SHA256>.authenticationCode(for: Data(message.utf8), using: key)
        return Data(mac).base64EncodedString()
    }

    /// Validate a peer request's HMAC signature.
    func validatePeerRequest(method: String, uri: String, headers: [String: String]) -> Bool {
        guard let peerId = headers["x-peer-id"],
              let signature = headers["x-peer-signature"],
              let timestamp = headers["x-peer-timestamp"],
              let device = pairedDevices[peerId] else {
            return false
        }

        // Check timestamp freshness (5-minute window)
        if let ts = Double(timestamp) {
            let age = abs(Date().timeIntervalSince1970 - ts)
            if age > 300 {
                AgentState.shared.log("pairing", "Stale peer request: \(age)s old")
                return false
            }
        }

        let key = SymmetricKey(data: device.sharedSecret)
        let message = "\(method):\(uri):\(timestamp)"
        let expected = HMAC<SHA256>.authenticationCode(for: Data(message.utf8), using: key)
        let expectedBase64 = Data(expected).base64EncodedString()

        return signature == expectedBase64
    }

    // MARK: - Crypto helpers

    private func deriveSharedSecret(peerPublicKey: P256.KeyAgreement.PublicKey) -> Data? {
        guard let shared = try? keyPair.sharedSecretFromKeyAgreement(with: peerPublicKey) else {
            return nil
        }
        // Derive via SHA-256 (same as Android)
        let derived = shared.withUnsafeBytes { buffer -> Data in
            let hash = SHA256.hash(data: Data(buffer))
            return Data(hash)
        }
        return derived
    }

    /// Generate a deterministic 6-digit confirmation code from both public keys.
    private func generateConfirmCode(
        ourPublicKey: P256.KeyAgreement.PublicKey,
        peerPublicKey: P256.KeyAgreement.PublicKey
    ) -> String {
        // Sort by raw bytes to ensure same code on both sides
        let k1 = ourPublicKey.rawRepresentation
        let k2 = peerPublicKey.rawRepresentation
        let sorted = [k1, k2].sorted { $0.lexicographicallyPrecedes($1) }

        var data = Data()
        data.append(sorted[0])
        data.append(sorted[1])

        let hash = SHA256.hash(data: data)
        let bytes = Array(hash)
        let value = (Int(bytes[0]) << 16 | Int(bytes[1]) << 8 | Int(bytes[2])) % 1_000_000
        return String(format: "%06d", value)
    }

    // MARK: - Persistence

    private func savePairedDevices() {
        if let data = try? JSONEncoder().encode(Array(pairedDevices.values)) {
            defaults.set(data, forKey: "\(keyPrefix)paired_devices")
        }
    }

    private func loadPairedDevices() {
        guard let data = defaults.data(forKey: "\(keyPrefix)paired_devices"),
              let devices = try? JSONDecoder().decode([PairedDevice].self, from: data) else {
            return
        }
        pairedDevices = Dictionary(uniqueKeysWithValues: devices.map { ($0.peerId, $0) })
    }
}
