import Foundation
import UIKit
import Contacts
import Photos

// ─────────────────────────────────────────────────────────────────────
//  DeviceHandler.swift — Device info API
//
//  Endpoints:
//    GET /api/device/info       — full device details
//    GET /api/device/battery    — battery status
//    GET /api/device/network    — network info
//    GET /api/device/storage    — disk space
//    GET /api/device/permissions — permission status
// ─────────────────────────────────────────────────────────────────────

final class DeviceHandler {

    func handle(method: String, parts: [String], request: HTTPRequest) -> HTTPResponse {
        let action = parts.first ?? "info"

        switch action {
        case "info":        return deviceInfo()
        case "battery":     return batteryInfo()
        case "network":     return networkInfo()
        case "storage":     return storageInfo()
        case "permissions": return permissionStatus()
        default:
            return .error("Unknown device action: \(action)", status: .notFound)
        }
    }

    // MARK: - Full device info

    private func deviceInfo() -> HTTPResponse {
        let device = UIDevice.current
        let processInfo = ProcessInfo.processInfo

        return .ok([
            "platform": "ios",
            "model": device.model,
            "model_name": modelName(),
            "system_name": device.systemName,
            "system_version": device.systemVersion,
            "name": device.name,
            "identifier_for_vendor": device.identifierForVendor?.uuidString ?? "",
            "processor_count": processInfo.processorCount,
            "active_processor_count": processInfo.activeProcessorCount,
            "physical_memory_gb": String(format: "%.1f", Double(processInfo.physicalMemory) / 1_073_741_824),
            "physical_memory": processInfo.physicalMemory,
            "os_version": processInfo.operatingSystemVersionString,
            "uptime": processInfo.systemUptime,
            "is_multitasking_supported": device.isMultitaskingSupported,
            "agent_version": AgentConstants.appVersion,
        ])
    }

    // MARK: - Battery

    private func batteryInfo() -> HTTPResponse {
        UIDevice.current.isBatteryMonitoringEnabled = true
        let device = UIDevice.current

        let stateStr: String
        switch device.batteryState {
        case .unknown:    stateStr = "unknown"
        case .unplugged:  stateStr = "discharging"
        case .charging:   stateStr = "charging"
        case .full:       stateStr = "full"
        @unknown default: stateStr = "unknown"
        }

        return .ok([
            "level": device.batteryLevel,  // 0.0 – 1.0
            "level_percent": Int(device.batteryLevel * 100),
            "state": stateStr,
            "monitoring_enabled": device.isBatteryMonitoringEnabled,
        ])
    }

    // MARK: - Network

    private func networkInfo() -> HTTPResponse {
        let addresses = NetworkUtils.getAllIPAddresses()
        let wifiIP = NetworkUtils.getWiFiIPAddress() ?? ""

        return .ok([
            "wifi_ip": wifiIP,
            "all_addresses": addresses,
            "hostname": ProcessInfo.processInfo.hostName,
        ])
    }

    // MARK: - Storage

    private func storageInfo() -> HTTPResponse {
        do {
            let attrs = try FileManager.default.attributesOfFileSystem(
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
                "used_percent": total > 0 ? Int(Double(used) / Double(total) * 100) : 0,
            ])
        } catch {
            return .error("Storage query failed: \(error.localizedDescription)")
        }
    }

    // MARK: - Permissions

    private func permissionStatus() -> HTTPResponse {
        var permissions: [[String: Any]] = []

        // Contacts
        let contactStatus = CNContactStore.authorizationStatus(for: .contacts)
        permissions.append([
            "name": "Contacts",
            "status": authStatusString(contactStatus),
            "granted": contactStatus == .authorized,
        ])

        // Photos
        let photoStatus = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        permissions.append([
            "name": "Photos",
            "status": photoAuthString(photoStatus),
            "granted": photoStatus == .authorized || photoStatus == .limited,
        ])

        // Camera
        // NOTE: Requires adding NSCameraUsageDescription to Info.plist
        // AVCaptureDevice.authorizationStatus(for: .video)

        return .ok(["permissions": permissions])
    }

    // MARK: - Helpers

    private func authStatusString(_ status: CNAuthorizationStatus) -> String {
        switch status {
        case .notDetermined: return "not_determined"
        case .restricted:    return "restricted"
        case .denied:        return "denied"
        case .authorized:    return "authorized"
        @unknown default:    return "unknown"
        }
    }

    private func photoAuthString(_ status: PHAuthorizationStatus) -> String {
        switch status {
        case .notDetermined: return "not_determined"
        case .restricted:    return "restricted"
        case .denied:        return "denied"
        case .authorized:    return "authorized"
        case .limited:       return "limited"
        @unknown default:    return "unknown"
        }
    }

    /// Get the specific device model name (e.g., "iPhone 15 Pro Max").
    private func modelName() -> String {
        var systemInfo = utsname()
        uname(&systemInfo)
        let machine = withUnsafePointer(to: &systemInfo.machine) {
            $0.withMemoryRebound(to: CChar.self, capacity: 1) {
                String(validatingUTF8: $0) ?? "Unknown"
            }
        }

        // Common mappings
        let models: [String: String] = [
            "iPhone15,2": "iPhone 14 Pro",
            "iPhone15,3": "iPhone 14 Pro Max",
            "iPhone15,4": "iPhone 15",
            "iPhone15,5": "iPhone 15 Plus",
            "iPhone16,1": "iPhone 15 Pro",
            "iPhone16,2": "iPhone 15 Pro Max",
            "iPhone17,1": "iPhone 16 Pro",
            "iPhone17,2": "iPhone 16 Pro Max",
            "iPhone17,3": "iPhone 16",
            "iPhone17,4": "iPhone 16 Plus",
            "iPad14,1": "iPad mini (6th gen)",
            "iPad16,3": "iPad Pro 11-inch (M4)",
            "iPad16,6": "iPad Pro 13-inch (M4)",
        ]

        return models[machine] ?? machine
    }
}
