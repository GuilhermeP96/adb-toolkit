import Foundation

// ─────────────────────────────────────────────────────────────────────
//  PeerHandler.swift — P2P pairing and discovery API
//
//  Endpoints:
//    GET  /api/peer/info           — this device's peer info
//    GET  /api/peer/paired         — list paired devices
//    POST /api/peer/initiate       — start pairing (send our public key)
//    POST /api/peer/approve        — approve pending pairing
//    POST /api/peer/reject         — reject pending pairing
//    POST /api/peer/unpair?id=     — remove a paired device
// ─────────────────────────────────────────────────────────────────────

final class PeerHandler {

    private let pairingManager: PairingManager

    init(pairingManager: PairingManager) {
        self.pairingManager = pairingManager
    }

    func handle(method: String, parts: [String], request: HTTPRequest) -> HTTPResponse {
        let action = parts.first ?? "info"

        switch action {
        case "info":      return peerInfo()
        case "paired":    return listPaired()
        case "initiate":  return initiatePairing(request: request)
        case "approve":   return approvePairing()
        case "reject":    return rejectPairing()
        case "unpair":    return unpair(request: request)
        default:
            return .error("Unknown peer action: \(action)", status: .notFound)
        }
    }

    // MARK: - Peer info

    private func peerInfo() -> HTTPResponse {
        return .ok([
            "device_id": pairingManager.deviceId,
            "name": UIDevice.current.name,
            "platform": "ios",
            "public_key": pairingManager.publicKeyBase64,
            "paired_count": pairingManager.pairedDeviceCount,
        ])
    }

    // MARK: - List paired devices

    private func listPaired() -> HTTPResponse {
        let devices = pairingManager.getPairedDevices().map { device -> [String: Any] in
            return [
                "peer_id": device.peerId,
                "name": device.name,
                "paired_at": ISO8601DateFormatter().string(from: device.pairedAt),
            ]
        }
        return .ok(["devices": devices, "count": devices.count])
    }

    // MARK: - Initiate pairing

    private func initiatePairing(request: HTTPRequest) -> HTTPResponse {
        guard let json = try? JSONSerialization.jsonObject(with: request.body) as? [String: Any],
              let peerId = json["peer_id"] as? String,
              let name = json["name"] as? String,
              let publicKey = json["public_key"] as? String else {
            return .error("Missing peer_id, name, or public_key")
        }

        guard let result = pairingManager.createPendingPairing(
            peerId: peerId, name: name, peerPublicKeyBase64: publicKey
        ) else {
            return .error("Failed to create pairing")
        }

        return .ok([
            "status": "pending",
            "confirm_code": result.confirmCode,
            "our_public_key": result.ourPublicKey,
            "message": "Waiting for user to confirm the 6-digit code",
        ])
    }

    // MARK: - Approve

    private func approvePairing() -> HTTPResponse {
        if pairingManager.approvePairing() {
            return .ok(["status": "approved"])
        }
        return .error("No pending pairing to approve")
    }

    // MARK: - Reject

    private func rejectPairing() -> HTTPResponse {
        pairingManager.rejectPairing()
        return .ok(["status": "rejected"])
    }

    // MARK: - Unpair

    private func unpair(request: HTTPRequest) -> HTTPResponse {
        let peerId = request.queryParams["id"] ?? ""
        guard !peerId.isEmpty else {
            return .error("Missing 'id' parameter")
        }
        pairingManager.unpair(peerId: peerId)
        return .ok(["status": "unpaired", "peer_id": peerId])
    }
}

import UIKit  // For UIDevice.current.name
