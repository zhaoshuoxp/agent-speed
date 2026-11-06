#!/usr/bin/env bash
set -euo pipefail
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
OUTPUT_DIR="${1:-/private/tmp/agent-maintenance-build}"
SDK_PATH="${AGENT_MAINTENANCE_SDK_PATH:-$(xcrun --sdk macosx --show-sdk-path)}"
mkdir -p "$OUTPUT_DIR"
mkdir -p "$SOURCE_DIR/.build-cache"
APP_DIR="$OUTPUT_DIR/Agent Maintenance.app"
if [[ -e "$APP_DIR" ]]; then
    echo 'Choose an output directory without an existing application.' >&2
    exit 1
fi
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources/CLI"
for arch in arm64 x86_64; do
    swiftc -parse-as-library -O -sdk "$SDK_PATH" -module-cache-path "$SOURCE_DIR/.build-cache" \
        -target "$arch-apple-macos14.0" "$SOURCE_DIR/desktop/AgentMaintenance.swift" \
        -o "$OUTPUT_DIR/AgentMaintenance-$arch"
done
lipo -create "$OUTPUT_DIR/AgentMaintenance-arm64" "$OUTPUT_DIR/AgentMaintenance-x86_64" \
    -output "$APP_DIR/Contents/MacOS/AgentMaintenance"
cat > "$APP_DIR/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>io.github.zhaoshuoxp.agent-maintenance</string>
<key>CFBundleName</key><string>Agent Maintenance</string>
<key>CFBundleExecutable</key><string>AgentMaintenance</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.1.0</string>
<key>CFBundleVersion</key><string>20261105.1</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
for item in agent_maintenance.py host.py README.txt; do
    cp "$SOURCE_DIR/$item" "$APP_DIR/Contents/Resources/CLI/$item"
done
xattr -cr "$APP_DIR"
codesign --force --options runtime --timestamp=none --sign - "$APP_DIR"
codesign --verify --deep --strict "$APP_DIR"
ditto -c -k --norsrc --keepParent "$APP_DIR" "$OUTPUT_DIR/AgentMaintenance-macOS.zip"
echo "$APP_DIR"
