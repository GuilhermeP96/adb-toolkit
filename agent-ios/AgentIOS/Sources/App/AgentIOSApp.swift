import SwiftUI

// ─────────────────────────────────────────────────────────────────────
//  AgentIOSApp.swift — Application entry point
// ─────────────────────────────────────────────────────────────────────

@main
struct AgentIOSApp: App {

    @StateObject private var agentState = AgentState.shared

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(agentState)
        }
    }
}

// ─────────────────────────────────────────────────────────────────────
//  Constants
// ─────────────────────────────────────────────────────────────────────

enum AgentConstants {
    static let httpPort: UInt16 = 15555
    static let transferPort: UInt16 = 15556
    static let appVersion = "1.0.0"
    static let serviceName = "_adbtoolkit._tcp."
    static let serviceType = "_adbtoolkit._tcp"
}
