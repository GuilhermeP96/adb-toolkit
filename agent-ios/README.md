# ADB Toolkit — iOS Agent

Companion app for iOS devices (iPhone / iPad).

## Features

| Feature               | Status |
|-----------------------|--------|
| HTTP API Server       | ✅     |
| TCP Transfer Server   | ✅     |
| ECDH + HMAC Pairing   | ✅     |
| Contacts (read/write) | ✅     |
| Photos & Videos       | ✅     |
| Files (app sandbox)   | ✅     |
| Device Info           | ✅     |
| D2D Peer Transfer     | ✅     |

## Architecture

Same pattern as the Android agent:

```
PC Toolkit (Python)
       │
       ▼
  AgentClient ──── HTTP (port 15555) ───► HTTPServer (Network.framework)
       │           TCP  (port 15556) ───► TransferServer
       │           WiFi direct
       ▼
  iOS Agent (Swift)
       ├── ContactsHandler   (Contacts.framework)
       ├── PhotosHandler     (Photos.framework)
       ├── FilesHandler      (FileManager — sandbox)
       ├── DeviceHandler     (UIDevice, ProcessInfo)
       └── PairingManager    (CryptoKit — ECDH P-256, HMAC-SHA256)
```

## Requirements

- iOS 15.0+
- Xcode 15+
- Swift 5.9+

## Building

### Via Xcode
1. Open `AgentIOS.xcodeproj` in Xcode
2. Select your target device
3. Build & Run (⌘R)

### Via Command Line
```bash
xcodebuild -project AgentIOS.xcodeproj \
  -scheme AgentIOS \
  -destination 'generic/platform=iOS' \
  -configuration Release \
  build
```

## Limitations vs Android Agent

- **No SMS access** — iOS sandbox prevents reading messages
- **No app listing** — private API, not allowed on App Store
- **No shell access** — iOS sandbox prevents arbitrary command execution
- **No background persistence** — app suspends after ~30s in background
- **Files limited to sandbox** — access to other files requires user Document Picker
- **Photos require explicit permission** — PHPhotoLibrary authorization

## Connection

The iOS Agent uses the same protocol as the Android agent:
- HTTP API on port 15555
- TCP binary transfer on port 15556
- Same ECDH + HMAC security for P2P pairing
- The PC toolkit's `companion_client.py` works unchanged

For USB connections via `libimobiledevice`:
```bash
iproxy 15555 15555 &   # Forward HTTP port
iproxy 15556 15556 &   # Forward transfer port
```
