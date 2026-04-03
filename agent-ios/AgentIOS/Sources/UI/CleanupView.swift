import SwiftUI

// ─────────────────────────────────────────────────────────────────────
//  CleanupView.swift — Device cleanup / junk scanner for iOS Agent
//
//  Scans for:
//    - App caches (tmp/Caches directories)
//    - Junk/temporary files
//    - Known junk patterns
//
//  Note: iOS sandboxing limits cleanup to the app's own sandbox and
//  any directories the user explicitly grants access to.
// ─────────────────────────────────────────────────────────────────────

struct CleanupView: View {

    @EnvironmentObject var state: AgentState

    // Scan modes
    @State private var scanAppCache = true
    @State private var scanTempFiles = true
    @State private var scanKnownJunk = true

    // Results
    @State private var isScanning = false
    @State private var results: [CleanupItem] = []
    @State private var statusText = "Pronto para escanear"

    struct CleanupItem: Identifiable {
        let id = UUID()
        let path: String
        let category: String
        let size: Int64
        var selected: Bool = true

        var displayName: String {
            (path as NSString).lastPathComponent
        }
    }

    var body: some View {
        NavigationView {
            List {
                // ── Scan modes ──
                Section("Modos de Escaneamento") {
                    Toggle("Cache de apps", isOn: $scanAppCache)
                    Toggle("Arquivos temporários", isOn: $scanTempFiles)
                    Toggle("Lixo conhecido", isOn: $scanKnownJunk)
                }

                // ── Status & action ──
                Section {
                    HStack {
                        if isScanning {
                            ProgressView()
                                .padding(.trailing, 8)
                        }
                        Text(statusText)
                            .foregroundColor(.secondary)
                        Spacer()
                    }

                    Button(action: performScan) {
                        HStack {
                            Image(systemName: "magnifyingglass")
                            Text("Escanear")
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(isScanning || (!scanAppCache && !scanTempFiles && !scanKnownJunk))
                }

                // ── Results ──
                if !results.isEmpty {
                    Section("Resultados (\(results.count) itens — \(formatSize(results.filter(\.selected).reduce(0) { $0 + $1.size })))") {
                        ForEach($results) { $item in
                            HStack {
                                Image(systemName: item.selected ? "checkmark.circle.fill" : "circle")
                                    .foregroundColor(item.selected ? .blue : .gray)
                                    .onTapGesture { item.selected.toggle() }

                                VStack(alignment: .leading, spacing: 2) {
                                    Text(item.category)
                                        .font(.caption.bold())
                                    Text(item.displayName)
                                        .font(.caption2)
                                        .foregroundColor(.secondary)
                                        .lineLimit(1)
                                }

                                Spacer()

                                Text(formatSize(item.size))
                                    .font(.caption.bold())
                                    .foregroundColor(.blue)
                            }
                            .contentShape(Rectangle())
                            .onTapGesture { item.selected.toggle() }
                        }
                    }

                    Section {
                        HStack {
                            Button("Selecionar Tudo") {
                                let allSelected = results.allSatisfy(\.selected)
                                for i in results.indices { results[i].selected = !allSelected }
                            }
                            .buttonStyle(.bordered)

                            Spacer()

                            Button(action: performClean) {
                                HStack {
                                    Image(systemName: "trash")
                                    Text("Limpar")
                                }
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(.red)
                            .disabled(results.filter(\.selected).isEmpty)
                        }
                    }
                }
            }
            .navigationTitle("Limpeza")
            .navigationBarTitleDisplayMode(.inline)
        }
    }

    // MARK: - Scan

    private func performScan() {
        isScanning = true
        statusText = "Escaneando..."
        results = []

        DispatchQueue.global(qos: .userInitiated).async {
            var found: [CleanupItem] = []

            if scanAppCache {
                found += scanCacheDirectories()
            }
            if scanTempFiles {
                found += scanTempDirectories()
            }
            if scanKnownJunk {
                found += scanKnownJunkFiles()
            }

            found.sort { $0.size > $1.size }

            DispatchQueue.main.async {
                results = found
                isScanning = false
                let totalSize = found.reduce(0) { $0 + $1.size }
                statusText = found.isEmpty
                    ? "Nenhum item encontrado"
                    : "Encontrado: \(formatSize(totalSize)) em \(found.count) itens"
            }
        }
    }

    private func scanCacheDirectories() -> [CleanupItem] {
        var items: [CleanupItem] = []
        let fm = FileManager.default

        // App's Caches directory
        if let cachesDir = fm.urls(for: .cachesDirectory, in: .userDomainMask).first {
            items += scanDirectory(cachesDir, category: "Cache")
        }

        return items
    }

    private func scanTempDirectories() -> [CleanupItem] {
        var items: [CleanupItem] = []

        let tmpDir = URL(fileURLWithPath: NSTemporaryDirectory())
        items += scanDirectory(tmpDir, category: "Temporário")

        return items
    }

    private func scanKnownJunkFiles() -> [CleanupItem] {
        var items: [CleanupItem] = []
        let fm = FileManager.default
        let junkExtensions = Set(["tmp", "bak", "log", "old", "orig"])

        if let docsDir = fm.urls(for: .documentDirectory, in: .userDomainMask).first {
            if let enumerator = fm.enumerator(at: docsDir, includingPropertiesForKeys: [.fileSizeKey, .isDirectoryKey]) {
                for case let fileURL as URL in enumerator {
                    let ext = fileURL.pathExtension.lowercased()
                    if junkExtensions.contains(ext) {
                        if let size = try? fileURL.resourceValues(forKeys: [.fileSizeKey]).fileSize {
                            items.append(CleanupItem(path: fileURL.path, category: "Lixo conhecido", size: Int64(size)))
                        }
                    }
                }
            }
        }

        return items
    }

    private func scanDirectory(_ dir: URL, category: String) -> [CleanupItem] {
        var items: [CleanupItem] = []
        let fm = FileManager.default

        guard let enumerator = fm.enumerator(at: dir, includingPropertiesForKeys: [.fileSizeKey, .isDirectoryKey]) else {
            return items
        }

        for case let fileURL as URL in enumerator {
            let values = try? fileURL.resourceValues(forKeys: [.fileSizeKey, .isDirectoryKey])
            if values?.isDirectory == false, let size = values?.fileSize, size > 0 {
                items.append(CleanupItem(path: fileURL.path, category: category, size: Int64(size)))
            }
        }

        return items
    }

    // MARK: - Clean

    private func performClean() {
        let selected = results.filter(\.selected)
        guard !selected.isEmpty else { return }

        var deletedCount = 0
        var freedSize: Int64 = 0

        for item in selected {
            do {
                try FileManager.default.removeItem(atPath: item.path)
                deletedCount += 1
                freedSize += item.size
            } catch {
                state.log("cleanup", "Falha ao remover: \(item.path)")
            }
        }

        results.removeAll(where: \.selected)
        statusText = "Removidos \(deletedCount) itens (\(formatSize(freedSize)) liberados)"
        state.log("cleanup", statusText)
    }

    // MARK: - Helpers

    private func formatSize(_ bytes: Int64) -> String {
        let gb: Int64 = 1 << 30
        let mb: Int64 = 1 << 20
        let kb: Int64 = 1 << 10
        switch bytes {
        case gb...: return String(format: "%.1f GB", Double(bytes) / Double(gb))
        case mb...: return String(format: "%.1f MB", Double(bytes) / Double(mb))
        case kb...: return String(format: "%.1f KB", Double(bytes) / Double(kb))
        default:    return "\(bytes) B"
        }
    }
}
