import Foundation
import Photos
import UIKit

// ─────────────────────────────────────────────────────────────────────
//  PhotosHandler.swift — Photos & Videos API (Photos.framework)
//
//  Endpoints:
//    GET /api/photos/list           — list all photos/videos (metadata)
//    GET /api/photos/albums         — list albums
//    GET /api/photos/count          — total count
//    GET /api/photos/thumbnail?id=  — 200x200 JPEG thumbnail
//    GET /api/photos/full?id=       — full resolution image data
//    GET /api/photos/export?id=     — original file (HEIC/MOV/etc.)
// ─────────────────────────────────────────────────────────────────────

final class PhotosHandler {

    func handle(method: String, parts: [String], request: HTTPRequest) -> HTTPResponse {
        let action = parts.first ?? "list"

        switch action {
        case "list":      return listAssets(request: request)
        case "albums":    return listAlbums()
        case "count":     return assetCount()
        case "thumbnail": return getThumbnail(request: request)
        case "full":      return getFullImage(request: request)
        case "export":    return exportOriginal(request: request)
        default:
            return .error("Unknown photos action: \(action)", status: .notFound)
        }
    }

    // MARK: - List assets

    private func listAssets(request: HTTPRequest) -> HTTPResponse {
        let limit = Int(request.queryParams["limit"] ?? "100") ?? 100
        let offset = Int(request.queryParams["offset"] ?? "0") ?? 0
        let mediaType = request.queryParams["type"]  // "image", "video", or nil (all)

        let options = PHFetchOptions()
        options.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: false)]

        if let mediaType = mediaType {
            switch mediaType {
            case "image":
                options.predicate = NSPredicate(format: "mediaType == %d", PHAssetMediaType.image.rawValue)
            case "video":
                options.predicate = NSPredicate(format: "mediaType == %d", PHAssetMediaType.video.rawValue)
            default: break
            }
        }

        let results = PHAsset.fetchAssets(with: options)
        var assets: [[String: Any]] = []

        let end = min(offset + limit, results.count)
        guard offset < results.count else {
            return .ok(["assets": [], "total": results.count, "offset": offset, "limit": limit])
        }

        for i in offset..<end {
            let asset = results.object(at: i)
            assets.append([
                "id": asset.localIdentifier,
                "type": asset.mediaType == .image ? "image" : "video",
                "width": asset.pixelWidth,
                "height": asset.pixelHeight,
                "duration": asset.duration,
                "creation_date": ISO8601DateFormatter().string(from: asset.creationDate ?? Date()),
                "modification_date": ISO8601DateFormatter().string(from: asset.modificationDate ?? Date()),
                "is_favorite": asset.isFavorite,
                "filename": PHAssetResource.assetResources(for: asset).first?.originalFilename ?? "",
            ])
        }

        return .ok([
            "assets": assets,
            "total": results.count,
            "offset": offset,
            "limit": limit,
        ])
    }

    // MARK: - List albums

    private func listAlbums() -> HTTPResponse {
        var albums: [[String: Any]] = []

        // User albums
        let userAlbums = PHAssetCollection.fetchAssetCollections(
            with: .album, subtype: .any, options: nil
        )
        userAlbums.enumerateObjects { collection, _, _ in
            let count = PHAsset.fetchAssets(in: collection, options: nil).count
            albums.append([
                "id": collection.localIdentifier,
                "title": collection.localizedTitle ?? "Untitled",
                "count": count,
                "type": "user",
            ])
        }

        // Smart albums (Favorites, Screenshots, etc.)
        let smartAlbums = PHAssetCollection.fetchAssetCollections(
            with: .smartAlbum, subtype: .any, options: nil
        )
        smartAlbums.enumerateObjects { collection, _, _ in
            let count = PHAsset.fetchAssets(in: collection, options: nil).count
            if count > 0 {
                albums.append([
                    "id": collection.localIdentifier,
                    "title": collection.localizedTitle ?? "Untitled",
                    "count": count,
                    "type": "smart",
                ])
            }
        }

        return .ok(["albums": albums, "count": albums.count])
    }

    // MARK: - Count

    private func assetCount() -> HTTPResponse {
        let all = PHAsset.fetchAssets(with: nil)
        let images = PHAsset.fetchAssets(with: .image, options: nil)
        let videos = PHAsset.fetchAssets(with: .video, options: nil)

        return .ok([
            "total": all.count,
            "images": images.count,
            "videos": videos.count,
        ])
    }

    // MARK: - Thumbnail

    private func getThumbnail(request: HTTPRequest) -> HTTPResponse {
        guard let assetId = request.queryParams["id"] else {
            return .error("Missing 'id' parameter")
        }

        guard let asset = PHAsset.fetchAssets(
            withLocalIdentifiers: [assetId], options: nil
        ).firstObject else {
            return .error("Asset not found")
        }

        let size = CGSize(width: 200, height: 200)
        let options = PHImageRequestOptions()
        options.isSynchronous = true
        options.deliveryMode = .fastFormat

        var imageData: Data?
        PHImageManager.default().requestImage(
            for: asset, targetSize: size,
            contentMode: .aspectFill, options: options
        ) { image, _ in
            imageData = image?.jpegData(compressionQuality: 0.7)
        }

        guard let data = imageData else {
            return .error("Failed to generate thumbnail")
        }

        return .binary(data: data, contentType: "image/jpeg")
    }

    // MARK: - Full resolution image

    private func getFullImage(request: HTTPRequest) -> HTTPResponse {
        guard let assetId = request.queryParams["id"] else {
            return .error("Missing 'id' parameter")
        }

        guard let asset = PHAsset.fetchAssets(
            withLocalIdentifiers: [assetId], options: nil
        ).firstObject else {
            return .error("Asset not found")
        }

        let options = PHImageRequestOptions()
        options.isSynchronous = true
        options.deliveryMode = .highQualityFormat
        options.isNetworkAccessAllowed = true  // For iCloud photos

        var imageData: Data?
        PHImageManager.default().requestImageDataAndOrientation(
            for: asset, options: options
        ) { data, uti, _, _ in
            imageData = data
        }

        guard let data = imageData else {
            return .error("Failed to load image")
        }

        let filename = PHAssetResource.assetResources(for: asset).first?.originalFilename ?? "photo"
        let isHEIC = filename.lowercased().hasSuffix(".heic")
        let contentType = isHEIC ? "image/heic" : "image/jpeg"

        return .binary(data: data, contentType: contentType, filename: filename)
    }

    // MARK: - Export original

    private func exportOriginal(request: HTTPRequest) -> HTTPResponse {
        guard let assetId = request.queryParams["id"] else {
            return .error("Missing 'id' parameter")
        }

        guard let asset = PHAsset.fetchAssets(
            withLocalIdentifiers: [assetId], options: nil
        ).firstObject else {
            return .error("Asset not found")
        }

        guard let resource = PHAssetResource.assetResources(for: asset).first else {
            return .error("No resource available")
        }

        let semaphore = DispatchSemaphore(value: 0)
        var fileData = Data()
        var exportError: Error?

        let options = PHAssetResourceRequestOptions()
        options.isNetworkAccessAllowed = true

        PHAssetResourceManager.default().requestData(
            for: resource, options: options,
            dataReceivedHandler: { data in
                fileData.append(data)
            },
            completionHandler: { error in
                exportError = error
                semaphore.signal()
            }
        )

        semaphore.wait()

        if let error = exportError {
            return .error("Export failed: \(error.localizedDescription)")
        }

        let filename = resource.originalFilename
        let ext = (filename as NSString).pathExtension.lowercased()
        let contentType: String
        switch ext {
        case "heic": contentType = "image/heic"
        case "jpg", "jpeg": contentType = "image/jpeg"
        case "png": contentType = "image/png"
        case "mov": contentType = "video/quicktime"
        case "mp4": contentType = "video/mp4"
        default: contentType = "application/octet-stream"
        }

        return .binary(data: fileData, contentType: contentType, filename: filename)
    }
}
