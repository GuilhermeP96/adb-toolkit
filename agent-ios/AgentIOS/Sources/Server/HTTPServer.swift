import Foundation
import Network

// ─────────────────────────────────────────────────────────────────────
//  HTTPServer.swift — Lightweight HTTP server using Network.framework
//
//  Same protocol as the Android agent: JSON API on port 15555.
//  Routes requests to ApiRouter for handling.
// ─────────────────────────────────────────────────────────────────────

final class HTTPServer {

    private let port: UInt16
    private let authToken: String
    private let router: ApiRouter
    private var listener: NWListener?
    private let queue = DispatchQueue(label: "com.adbtoolkit.ios.http", qos: .userInitiated)

    init(port: UInt16, authToken: String, pairingManager: PairingManager) {
        self.port = port
        self.authToken = authToken
        self.router = ApiRouter(pairingManager: pairingManager)
    }

    func start() {
        do {
            let params = NWParameters.tcp
            params.allowLocalEndpointReuse = true
            listener = try NWListener(using: params, on: NWEndpoint.Port(rawValue: port)!)
        } catch {
            AgentState.shared.log("http", "Failed to create listener: \(error)")
            return
        }

        listener?.newConnectionHandler = { [weak self] connection in
            self?.handleConnection(connection)
        }

        listener?.stateUpdateHandler = { [weak self] state in
            switch state {
            case .ready:
                AgentState.shared.log("http", "HTTP server listening on port \(self?.port ?? 0)")
            case .failed(let error):
                AgentState.shared.log("http", "Listener failed: \(error)")
                self?.listener?.cancel()
            default:
                break
            }
        }

        listener?.start(queue: queue)
    }

    func stop() {
        listener?.cancel()
        listener = nil
        AgentState.shared.log("http", "HTTP server stopped")
    }

    // MARK: - Connection handling

    private func handleConnection(_ connection: NWConnection) {
        connection.start(queue: queue)
        receiveHTTPRequest(connection)
    }

    private func receiveHTTPRequest(_ connection: NWConnection) {
        // Read up to 1MB for the full HTTP request
        connection.receive(minimumIncompleteLength: 1, maximumLength: 1_048_576) {
            [weak self] data, _, isComplete, error in
            guard let self = self, let data = data, !data.isEmpty else {
                connection.cancel()
                return
            }

            let request = HTTPRequest.parse(data: data)

            // Auth check (skip /api/ping)
            if request.path != "/api/ping" {
                let token = request.headers["x-agent-token"] ?? request.queryParams["token"]
                if !self.authToken.isEmpty && token != self.authToken {
                    let resp = HTTPResponse.json(
                        status: .unauthorized,
                        body: ["error": "unauthorized", "message": "Missing or invalid X-Agent-Token"]
                    )
                    self.sendResponse(connection, response: resp)
                    return
                }
            }

            // Route to handler
            let response = self.router.route(request: request)
            self.sendResponse(connection, response: response)
        }
    }

    private func sendResponse(_ connection: NWConnection, response: HTTPResponse) {
        let data = response.serialize()
        connection.send(content: data, completion: .contentProcessed { _ in
            connection.cancel()
        })
    }
}

// ─────────────────────────────────────────────────────────────────────
//  HTTPRequest — Simple HTTP/1.1 parser
// ─────────────────────────────────────────────────────────────────────

struct HTTPRequest {
    let method: String       // GET, POST, PUT, DELETE
    let path: String         // /api/contacts/list
    let queryParams: [String: String]
    let headers: [String: String]
    let body: Data

    /// Parse raw HTTP data into a structured request.
    static func parse(data: Data) -> HTTPRequest {
        guard let str = String(data: data, encoding: .utf8) else {
            return HTTPRequest(method: "GET", path: "/", queryParams: [:], headers: [:], body: Data())
        }

        let parts = str.components(separatedBy: "\r\n\r\n")
        let headerSection = parts[0]
        let bodySection = parts.count > 1 ? parts[1...].joined(separator: "\r\n\r\n") : ""

        let lines = headerSection.components(separatedBy: "\r\n")
        guard let requestLine = lines.first else {
            return HTTPRequest(method: "GET", path: "/", queryParams: [:], headers: [:], body: Data())
        }

        let requestParts = requestLine.components(separatedBy: " ")
        let method = requestParts.count > 0 ? requestParts[0] : "GET"
        let fullPath = requestParts.count > 1 ? requestParts[1] : "/"

        // Parse path and query params
        let pathComponents = fullPath.components(separatedBy: "?")
        let path = pathComponents[0]
        var queryParams: [String: String] = [:]
        if pathComponents.count > 1 {
            let queryString = pathComponents[1]
            for param in queryString.components(separatedBy: "&") {
                let kv = param.components(separatedBy: "=")
                if kv.count == 2 {
                    queryParams[kv[0].lowercased()] = kv[1].removingPercentEncoding ?? kv[1]
                }
            }
        }

        // Parse headers
        var headers: [String: String] = [:]
        for line in lines.dropFirst() {
            let headerParts = line.components(separatedBy: ": ")
            if headerParts.count >= 2 {
                headers[headerParts[0].lowercased()] = headerParts[1...].joined(separator: ": ")
            }
        }

        let body = bodySection.data(using: .utf8) ?? Data()

        return HTTPRequest(
            method: method, path: path, queryParams: queryParams,
            headers: headers, body: body
        )
    }
}

// ─────────────────────────────────────────────────────────────────────
//  HTTPResponse — HTTP/1.1 response builder
// ─────────────────────────────────────────────────────────────────────

struct HTTPResponse {

    enum Status: Int {
        case ok = 200
        case badRequest = 400
        case unauthorized = 401
        case forbidden = 403
        case notFound = 404
        case serverError = 500

        var reason: String {
            switch self {
            case .ok: return "OK"
            case .badRequest: return "Bad Request"
            case .unauthorized: return "Unauthorized"
            case .forbidden: return "Forbidden"
            case .notFound: return "Not Found"
            case .serverError: return "Internal Server Error"
            }
        }
    }

    let status: Status
    let contentType: String
    let body: Data
    var extraHeaders: [String: String] = [:]

    /// Create a JSON response.
    static func json(status: Status = .ok, body: [String: Any]) -> HTTPResponse {
        let data = (try? JSONSerialization.data(withJSONObject: body)) ?? Data()
        return HTTPResponse(status: status, contentType: "application/json", body: data)
    }

    /// Create an OK JSON response with a result.
    static func ok(_ data: [String: Any] = ["status": "ok"]) -> HTTPResponse {
        return json(status: .ok, body: data)
    }

    /// Create an error response.
    static func error(_ message: String, status: Status = .badRequest) -> HTTPResponse {
        return json(status: status, body: ["error": message])
    }

    /// Raw binary response (for file downloads, etc.)
    static func binary(data: Data, contentType: String, filename: String? = nil) -> HTTPResponse {
        var resp = HTTPResponse(status: .ok, contentType: contentType, body: data)
        if let fname = filename {
            resp.extraHeaders["Content-Disposition"] = "attachment; filename=\"\(fname)\""
        }
        return resp
    }

    /// Serialize to raw HTTP bytes.
    func serialize() -> Data {
        var header = "HTTP/1.1 \(status.rawValue) \(status.reason)\r\n"
        header += "Content-Type: \(contentType)\r\n"
        header += "Content-Length: \(body.count)\r\n"
        header += "Connection: close\r\n"
        header += "Server: ADBToolkit-iOS/\(AgentConstants.appVersion)\r\n"
        for (key, value) in extraHeaders {
            header += "\(key): \(value)\r\n"
        }
        header += "\r\n"

        var data = header.data(using: .utf8) ?? Data()
        data.append(body)
        return data
    }
}
