import Foundation
import CryptoKit

// ─────────────────────────────────────────────────────────────────────
//  FilesHandler.swift — File system API (app sandbox + shared docs)
//
//  Endpoints:
//    GET  /api/files/list?path=      — directory listing
//    GET  /api/files/read?path=      — download file (binary)
//    POST /api/files/write?path=     — upload file (binary body)
//    GET  /api/files/stat?path=      — file metadata
//    GET  /api/files/hash?path=      — SHA-256 hash
//    POST /api/files/mkdir?path=     — create directory
//    POST /api/files/delete?path=    — delete file/directory
//    GET  /api/files/exists?path=    — check existence
//    GET  /api/files/storage         — disk space statistics
//    GET  /api/files/search?name=    — search by filename
// ─────────────────────────────────────────────────────────────────────

final class FilesHandler {

    private let fileManager = FileManager.default

    /// Base directory: app's Documents folder (accessible via Files app)
    private var baseDir: URL {
        fileManager.urls(for: .documentDirectory, in: .userDomainMask)[0]
    }

    func handle(method: String, parts: [String], request: HTTPRequest) -> HTTPResponse {
        let action = parts.first ?? "list"

        switch action {
        case "list":    return listFiles(request: request)
        case "read":    return readFile(request: request)
        case "write":   return writeFile(request: request)
        case "stat":    return fileStat(request: request)
        case "hash":    return fileHash(request: request)
        case "mkdir":   return makeDirectory(request: request)
        case "delete":  return deleteFile(request: request)
        case "exists":  return fileExists(request: request)
        case "storage": return storageInfo()
        case "search":  return searchFiles(request: request)
        default:
            return .error("Unknown files action: \(action)", status: .notFound)
        }
    }

    // MARK: - Resolve path (sandbox-safe)

    private func resolvePath(_ relativePath: String?) -> URL? {
        guard let path = relativePath, !path.isEmpty else { return baseDir }
        let cleaned = path.hasPrefix("/") ? String(path.dropFirst()) : path

        // Prevent directory traversal
        if cleaned.contains("..") {
            return nil
        }

        return baseDir.appendingPathComponent(cleaned)
    }

    // MARK: - List directory

    private func listFiles(request: HTTPRequest) -> HTTPResponse {
        guard let url = resolvePath(request.queryParams["path"]) else {
            return .error("Invalid path")
        }

        do {
            let items = try fileManager.contentsOfDirectory(
                at: url, includingPropertiesForKeys: [
                    .fileSizeKey, .isDirectoryKey, .contentModificationDateKey,
                    .creationDateKey,
                ]
            )

            let files: [[String: Any]] = items.compactMap { itemURL in
                let values = try? itemURL.resourceValues(forKeys: [
                    .fileSizeKey, .isDirectoryKey, .contentModificationDateKey,
                    .creationDateKey,
                ])

                let isDir = values?.isDirectory ?? false
                return [
                    "name": itemURL.lastPathComponent,
                    "path": itemURL.path.replacingOccurrences(of: baseDir.path, with: ""),
                    "is_directory": isDir,
                    "size": values?.fileSize ?? 0,
                    "modified": ISO8601DateFormatter().string(
                        from: values?.contentModificationDate ?? Date()
                    ),
                    "created": ISO8601DateFormatter().string(
                        from: values?.creationDate ?? Date()
                    ),
                ]
            }

            return .ok(["files": files, "count": files.count, "path": url.path])
        } catch {
            return .error("Failed to list: \(error.localizedDescription)")
        }
    }

    // MARK: - Read file

    private func readFile(request: HTTPRequest) -> HTTPResponse {
        guard let url = resolvePath(request.queryParams["path"]) else {
            return .error("Invalid path")
        }

        guard let data = try? Data(contentsOf: url) else {
            return .error("File not found or unreadable")
        }

        let ext = url.pathExtension.lowercased()
        let contentType = Self.mimeType(for: ext)
        return .binary(data: data, contentType: contentType, filename: url.lastPathComponent)
    }

    // MARK: - Write file

    private func writeFile(request: HTTPRequest) -> HTTPResponse {
        guard let url = resolvePath(request.queryParams["path"]) else {
            return .error("Invalid path")
        }

        // Create parent directories
        try? fileManager.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )

        do {
            try request.body.write(to: url)
            return .ok(["path": url.path, "size": request.body.count])
        } catch {
            return .error("Write failed: \(error.localizedDescription)")
        }
    }

    // MARK: - File stat

    private func fileStat(request: HTTPRequest) -> HTTPResponse {
        guard let url = resolvePath(request.queryParams["path"]),
              let attrs = try? fileManager.attributesOfItem(atPath: url.path) else {
            return .error("File not found")
        }

        return .ok([
            "path": url.path,
            "name": url.lastPathComponent,
            "size": (attrs[.size] as? Int) ?? 0,
            "is_directory": (attrs[.type] as? FileAttributeType) == .typeDirectory,
            "modified": ISO8601DateFormatter().string(
                from: (attrs[.modificationDate] as? Date) ?? Date()
            ),
            "created": ISO8601DateFormatter().string(
                from: (attrs[.creationDate] as? Date) ?? Date()
            ),
            "permissions": String(format: "%o", (attrs[.posixPermissions] as? Int) ?? 0),
        ])
    }

    // MARK: - File hash

    private func fileHash(request: HTTPRequest) -> HTTPResponse {
        guard let url = resolvePath(request.queryParams["path"]),
              let data = try? Data(contentsOf: url) else {
            return .error("File not found")
        }

        let hash = SHA256.hash(data: data)
        let hex = hash.map { String(format: "%02x", $0) }.joined()

        return .ok(["hash": hex, "algorithm": "sha256", "path": url.path])
    }

    // MARK: - Create directory

    private func makeDirectory(request: HTTPRequest) -> HTTPResponse {
        guard let url = resolvePath(request.queryParams["path"]) else {
            return .error("Invalid path")
        }

        do {
            try fileManager.createDirectory(at: url, withIntermediateDirectories: true)
            return .ok(["path": url.path])
        } catch {
            return .error("mkdir failed: \(error.localizedDescription)")
        }
    }

    // MARK: - Delete

    private func deleteFile(request: HTTPRequest) -> HTTPResponse {
        guard let url = resolvePath(request.queryParams["path"]) else {
            return .error("Invalid path")
        }

        do {
            try fileManager.removeItem(at: url)
            return .ok(["deleted": url.path])
        } catch {
            return .error("Delete failed: \(error.localizedDescription)")
        }
    }

    // MARK: - Exists check

    private func fileExists(request: HTTPRequest) -> HTTPResponse {
        guard let url = resolvePath(request.queryParams["path"]) else {
            return .ok(["exists": false])
        }
        var isDir: ObjCBool = false
        let exists = fileManager.fileExists(atPath: url.path, isDirectory: &isDir)
        return .ok(["exists": exists, "is_directory": isDir.boolValue])
    }

    // MARK: - Storage info

    private func storageInfo() -> HTTPResponse {
        do {
            let attrs = try fileManager.attributesOfFileSystem(
                forPath: NSHomeDirectory()
            )
            let total = (attrs[.systemSize] as? Int64) ?? 0
            let free = (attrs[.systemFreeSize] as? Int64) ?? 0
            let used = total - free

            return .ok([
                "total": total,
                "free": free,
                "used": used,
                "total_gb": String(format: "%.1f", Double(total) / 1_073_741_824),
                "free_gb": String(format: "%.1f", Double(free) / 1_073_741_824),
                "used_gb": String(format: "%.1f", Double(used) / 1_073_741_824),
            ])
        } catch {
            return .error("Storage query failed: \(error.localizedDescription)")
        }
    }

    // MARK: - Search

    private func searchFiles(request: HTTPRequest) -> HTTPResponse {
        guard let pattern = request.queryParams["name"] else {
            return .error("Missing 'name' parameter")
        }

        var results: [[String: Any]] = []
        let maxResults = Int(request.queryParams["limit"] ?? "50") ?? 50

        if let enumerator = fileManager.enumerator(at: baseDir, includingPropertiesForKeys: nil) {
            while let url = enumerator.nextObject() as? URL {
                if results.count >= maxResults { break }
                if url.lastPathComponent.localizedCaseInsensitiveContains(pattern) {
                    var isDir: ObjCBool = false
                    fileManager.fileExists(atPath: url.path, isDirectory: &isDir)
                    let size = (try? url.resourceValues(forKeys: [.fileSizeKey]))?.fileSize ?? 0
                    results.append([
                        "name": url.lastPathComponent,
                        "path": url.path.replacingOccurrences(of: baseDir.path, with: ""),
                        "is_directory": isDir.boolValue,
                        "size": size,
                    ])
                }
            }
        }

        return .ok(["results": results, "count": results.count, "pattern": pattern])
    }

    // MARK: - MIME type helper

    static func mimeType(for ext: String) -> String {
        switch ext {
        case "txt":  return "text/plain"
        case "html": return "text/html"
        case "json": return "application/json"
        case "xml":  return "application/xml"
        case "jpg", "jpeg": return "image/jpeg"
        case "png":  return "image/png"
        case "gif":  return "image/gif"
        case "heic": return "image/heic"
        case "pdf":  return "application/pdf"
        case "zip":  return "application/zip"
        case "mp3":  return "audio/mpeg"
        case "mp4":  return "video/mp4"
        case "mov":  return "video/quicktime"
        default:     return "application/octet-stream"
        }
    }
}
