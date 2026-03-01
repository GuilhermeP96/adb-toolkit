import SwiftUI
import Contacts
import Photos

// ─────────────────────────────────────────────────────────────────────
//  ContentView.swift — Main UI for the iOS Agent app
//
//  Minimal functional UI:
//    - Server status & controls
//    - Connection info (IP, port, token)
//    - Paired devices list
//    - Permission requests
//    - Live log console
// ─────────────────────────────────────────────────────────────────────

struct ContentView: View {

    @EnvironmentObject var state: AgentState
    @State private var showToken = false

    var body: some View {
        NavigationView {
            List {
                // ── Status section ──
                Section("Server Status") {
                    HStack {
                        Circle()
                            .fill(state.isRunning ? Color.green : Color.red)
                            .frame(width: 12, height: 12)
                        Text(state.isRunning ? "Running" : "Stopped")
                            .font(.headline)
                        Spacer()
                    }

                    if state.isRunning {
                        LabeledContent("IP Address", value: state.localIPAddress)
                        LabeledContent("HTTP Port", value: "\(state.httpPort)")
                        LabeledContent("Transfer Port", value: "\(state.transferPort)")

                        HStack {
                            Text("Auth Token")
                            Spacer()
                            Text(showToken ? state.authToken : "••••••••••••")
                                .font(.system(.caption, design: .monospaced))
                                .foregroundColor(.secondary)
                            Button(showToken ? "Hide" : "Show") {
                                showToken.toggle()
                            }
                            .font(.caption)
                        }
                    }

                    // Start / Stop button
                    Button(action: toggleServer) {
                        HStack {
                            Image(systemName: state.isRunning ? "stop.fill" : "play.fill")
                            Text(state.isRunning ? "Stop Agent" : "Start Agent")
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(state.isRunning ? .red : .green)
                }

                // ── Connection info for PC ──
                if state.isRunning {
                    Section("Connect from PC") {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Via WiFi:")
                                .font(.caption.bold())
                            Text("http://\(state.localIPAddress):\(state.httpPort)")
                                .font(.system(.caption, design: .monospaced))
                                .textSelection(.enabled)

                            Divider()

                            Text("Via USB (libimobiledevice):")
                                .font(.caption.bold())
                            Text("iproxy 15555 15555")
                                .font(.system(.caption, design: .monospaced))
                                .textSelection(.enabled)
                        }
                        .padding(.vertical, 4)
                    }
                }

                // ── Paired devices ──
                Section("Paired Devices (\(state.pairedDevices))") {
                    if let pm = state.pairingManager {
                        let devices = pm.getPairedDevices()
                        if devices.isEmpty {
                            Text("No paired devices")
                                .foregroundColor(.secondary)
                        } else {
                            ForEach(devices, id: \.peerId) { device in
                                HStack {
                                    Image(systemName: "link.circle.fill")
                                        .foregroundColor(.blue)
                                    VStack(alignment: .leading) {
                                        Text(device.name).font(.body)
                                        Text(device.peerId)
                                            .font(.caption)
                                            .foregroundColor(.secondary)
                                    }
                                }
                            }
                        }
                    }
                }

                // ── Permissions ──
                Section("Permissions") {
                    NavigationLink("Manage Permissions") {
                        PermissionsView()
                    }
                }

                // ── Log console ──
                Section("Log (\(state.logLines.count) entries)") {
                    ScrollViewReader { proxy in
                        ScrollView {
                            LazyVStack(alignment: .leading, spacing: 2) {
                                ForEach(state.logLines) { entry in
                                    HStack(alignment: .top, spacing: 4) {
                                        Text(timeString(entry.timestamp))
                                            .font(.system(.caption2, design: .monospaced))
                                            .foregroundColor(.secondary)
                                        Text("[\(entry.source)]")
                                            .font(.system(.caption2, design: .monospaced))
                                            .foregroundColor(.blue)
                                        Text(entry.message)
                                            .font(.system(.caption2, design: .monospaced))
                                    }
                                    .id(entry.id)
                                }
                            }
                        }
                        .frame(height: 200)
                        .onChange(of: state.logLines.count) { _ in
                            if let last = state.logLines.last {
                                proxy.scrollTo(last.id, anchor: .bottom)
                            }
                        }
                    }
                }
            }
            .navigationTitle("ADB Toolkit Agent")
            .navigationBarTitleDisplayMode(.inline)
        }
    }

    private func toggleServer() {
        if state.isRunning {
            state.stopServer()
        } else {
            state.startServer()
        }
    }

    private func timeString(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter.string(from: date)
    }
}

// ─────────────────────────────────────────────────────────────────────
//  PermissionsView — Request & check permissions
// ─────────────────────────────────────────────────────────────────────

struct PermissionsView: View {

    @State private var contactsGranted = false
    @State private var photosGranted = false

    var body: some View {
        List {
            Section("Required Permissions") {
                PermissionRow(
                    name: "Contacts",
                    icon: "person.crop.circle",
                    granted: contactsGranted,
                    action: requestContacts
                )

                PermissionRow(
                    name: "Photos",
                    icon: "photo.on.rectangle",
                    granted: photosGranted,
                    action: requestPhotos
                )
            }

            Section {
                Button("Open Settings") {
                    if let url = URL(string: UIApplication.openSettingsURLString) {
                        UIApplication.shared.open(url)
                    }
                }
            }
        }
        .navigationTitle("Permissions")
        .onAppear { checkPermissions() }
    }

    private func checkPermissions() {
        contactsGranted = CNContactStore.authorizationStatus(for: .contacts) == .authorized

        let photoStatus = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        photosGranted = photoStatus == .authorized || photoStatus == .limited
    }

    private func requestContacts() {
        CNContactStore().requestAccess(for: .contacts) { granted, _ in
            DispatchQueue.main.async { contactsGranted = granted }
        }
    }

    private func requestPhotos() {
        PHPhotoLibrary.requestAuthorization(for: .readWrite) { status in
            DispatchQueue.main.async {
                photosGranted = status == .authorized || status == .limited
            }
        }
    }
}

struct PermissionRow: View {
    let name: String
    let icon: String
    let granted: Bool
    let action: () -> Void

    var body: some View {
        HStack {
            Image(systemName: icon)
                .foregroundColor(granted ? .green : .orange)
                .frame(width: 30)
            Text(name)
            Spacer()
            if granted {
                Image(systemName: "checkmark.circle.fill")
                    .foregroundColor(.green)
            } else {
                Button("Request") { action() }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
            }
        }
    }
}
