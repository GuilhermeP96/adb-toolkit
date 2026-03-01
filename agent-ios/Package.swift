// swift-tools-version: 5.9
// Package.swift — SPM manifest (for non-Xcode builds / CI)
// The canonical project uses AgentIOS.xcodeproj

import PackageDescription

let package = Package(
    name: "AgentIOS",
    platforms: [.iOS(.v15)],
    products: [
        .library(name: "AgentIOS", targets: ["AgentIOS"]),
    ],
    targets: [
        .target(
            name: "AgentIOS",
            path: "AgentIOS/Sources"
        ),
    ]
)
