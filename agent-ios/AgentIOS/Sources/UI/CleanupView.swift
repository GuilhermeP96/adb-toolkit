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
    @State private var scanWaSent = false
    @State private var waAgeDays = 365

    // WhatsApp Sent age options
    private let waAgeOptions: [(days: Int, label: String)] = [
        (90,  "Sent > 90 dias"),
        (180, "Sent > 6 meses"),
        (365, "Sent > 1 ano"),
        (730, "Sent > 2 anos"),
        (1095, "Sent > 3 anos"),
        (Int.max, "Toda pasta Sent"),
    ]

    // WhatsApp filename date pattern
    private let waDateRegex = try! NSRegularExpression(
        pattern: "(?:VID|IMG|AUD|DOC|STK|PTT)-?(\\d{4})(\\d{2})(\\d{2})-WA"
    )

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
                    Toggle("WhatsApp Sent Media", isOn: $scanWaSent)
                    if scanWaSent {
                        Picker("Idade mínima", selection: $waAgeDays) {
                            ForEach(waAgeOptions, id: \.days) { opt in
                                Text(opt.label).tag(opt.days)
                            }
                        }
                        .pickerStyle(.menu)
                        Text("Escaneia vídeos/imagens enviados pelo WhatsApp exportados para Arquivos")
                            .font(.caption2)
                            .foregroundColor(.secondary)
                    }
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
                    .disabled(isScanning || (!scanAppCache && !scanTempFiles && !scanKnownJunk && !scanWaSent))
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
            if scanWaSent {
                found += scanWaSentMedia()
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

    private func scanWaSentMedia() -> [CleanupItem] {
        var items: [CleanupItem] = []
        let fm = FileManager.default
        let calendar = Calendar.current
        let cutoffDate = waAgeDays == Int.max
            ? Date.distantFuture
            : calendar.date(byAdding: .day, value: -waAgeDays, to: Date())!

        // On iOS we can only scan user-accessible directories (Documents, Downloads)
        // where WhatsApp media may have been exported/saved
        let searchDirs: [URL] = [
            fm.urls(for: .documentDirectory, in: .userDomainMask),
            fm.urls(for: .downloadsDirectory, in: .userDomainMask),
        ].flatMap { $0 }

        for dir in searchDirs {
            guard let enumerator = fm.enumerator(
                at: dir,
                includingPropertiesForKeys: [.fileSizeKey, .isDirectoryKey]
            ) else { continue }

            for case let fileURL as URL in enumerator {
                let values = try? fileURL.resourceValues(forKeys: [.fileSizeKey, .isDirectoryKey])
                guard values?.isDirectory == false,
                      let size = values?.fileSize, size > 0 else { continue }

                let name = fileURL.lastPathComponent
                let nsName = name as NSString
                let range = NSRange(location: 0, length: nsName.length)

                guard let match = waDateRegex.firstMatch(in: name, range: range),
                      match.numberOfRanges >= 4 else { continue }

                guard let year = Int(nsName.substring(with: match.range(at: 1))),
                      let month = Int(nsName.substring(with: match.range(at: 2))),
                      let day = Int(nsName.substring(with: match.range(at: 3))) else { continue }

                var comps = DateComponents()
                comps.year = year; comps.month = month; comps.day = day
                guard let fileDate = calendar.date(from: comps) else { continue }

                let passesFilter = waAgeDays == Int.max || fileDate < cutoffDate
                guard passesFilter else { continue }

                let mediaType: String
                let lower = name.lowercased()
                if lower.hasPrefix("vid") { mediaType = "Vídeo" }
                else if lower.hasPrefix("img") { mediaType = "Imagem" }
                else if lower.hasPrefix("doc") { mediaType = "Documento" }
                else if lower.hasPrefix("aud") || lower.hasPrefix("ptt") { mediaType = "Áudio" }
                else { mediaType = "Mídia" }

                let dateStr = String(format: "%04d-%02d-%02d", year, month, day)
                items.append(CleanupItem(
                    path: fileURL.path,
                    category: "WA Sent \(mediaType) (\(dateStr))",
                    size: Int64(size)
                ))
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
