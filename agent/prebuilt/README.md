# Agent Prebuilt APKs

Place pre-built agent APK files in this directory for easy installation
via the **🤖 Agent** tab in the toolkit GUI.

## How to use

1. Build the agent APK (see `agent/` directory) or download a release
2. Copy the `.apk` file into this `prebuilt/` folder
3. Open the toolkit GUI → **Agent** tab → click **📲 Install Agent**

The toolkit will automatically find the newest APK in this directory.

## Building from source

```bash
cd agent/
# Linux / macOS
./gradlew :app:assembleDebug

# Windows
gradlew.bat :app:assembleDebug
```

The built APK will be at: `agent/app/build/outputs/apk/debug/app-debug.apk`

## Play Store

For Play Store publishing, use the release build:

```bash
./gradlew :app:assembleRelease
# or via the GUI: Agent tab → Build Release
```

Configure signing in `agent/app/build.gradle.kts` under `signingConfigs`.
