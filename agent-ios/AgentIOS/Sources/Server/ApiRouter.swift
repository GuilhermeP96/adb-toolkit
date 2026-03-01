import Foundation

// ─────────────────────────────────────────────────────────────────────
//  ApiRouter.swift — Routes HTTP requests to API handlers
//
//  Same URL scheme as Android: /api/<domain>/<action>[/<param>]
// ─────────────────────────────────────────────────────────────────────

final class ApiRouter {

    private let contactsApi = ContactsHandler()
    private let photosApi = PhotosHandler()
    private let filesApi = FilesHandler()
    private let deviceApi = DeviceHandler()
    private let peerApi: PeerHandler
    private let pairingManager: PairingManager

    init(pairingManager: PairingManager) {
        self.pairingManager = pairingManager
        self.peerApi = PeerHandler(pairingManager: pairingManager)
    }

    func route(request: HTTPRequest) -> HTTPResponse {
        let path = request.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let parts = path
            .replacingOccurrences(of: "api/", with: "", options: .anchored)
            .components(separatedBy: "/")

        let domain = parts.first ?? ""
        let subParts = Array(parts.dropFirst())

        // P2P auth check (if X-Peer-Id header present)
        let peerId = request.headers["x-peer-id"]
        if let peerId = peerId, domain != "ping" && domain != "peer" {
            let isValid = pairingManager.validatePeerRequest(
                method: request.method,
                uri: request.path,
                headers: request.headers
            )
            if !isValid {
                AgentState.shared.log("router", "Peer auth rejected for \(peerId)")
                return .error("P2P authentication failed", status: .forbidden)
            }
        }

        switch domain {
        case "ping":
            return .ok([
                "status": "alive",
                "version": AgentConstants.appVersion,
                "platform": "ios",
                "device_id": pairingManager.deviceId,
                "paired_count": pairingManager.pairedDeviceCount,
            ])

        case "contacts":
            return contactsApi.handle(method: request.method, parts: subParts, request: request)

        case "photos":
            return photosApi.handle(method: request.method, parts: subParts, request: request)

        case "files":
            return filesApi.handle(method: request.method, parts: subParts, request: request)

        case "device":
            return deviceApi.handle(method: request.method, parts: subParts, request: request)

        case "peer":
            return peerApi.handle(method: request.method, parts: subParts, request: request)

        default:
            return .error("Unknown endpoint: /api/\(domain)", status: .notFound)
        }
    }
}
