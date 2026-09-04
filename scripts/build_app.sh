#!/usr/bin/env bash
# 打包 FleetMonitor.app（启动器模式：不复制代码，双击即引用项目内 desktop/app.py）
#
# 用法: ./scripts/build_app.sh
# 产物: dist/FleetMonitor.app（可拖入 ~/Applications 或 /Applications）

set -euo pipefail
cd "$(dirname "$0")/.."

APP_NAME="FleetMonitor"
PROJECT_DIR="$(pwd)"
APP_DIR="dist/${APP_NAME}.app"

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

# ---- 1. 可执行启动器（注入项目绝对路径）----
cat > "$APP_DIR/Contents/MacOS/$APP_NAME" <<EOF
#!/bin/bash
# FleetMonitor 桌面启动器（自动生成，勿手改）
PROJECT="$PROJECT_DIR"
if [ ! -d "\$PROJECT" ]; then
  osascript -e 'display alert "fleet-monitor" message "项目路径不存在：$PROJECT_DIR" as critical'
  exit 1
fi
cd "\$PROJECT" || exit 1
export NO_PROXY="127.0.0.1,localhost,::1"
export PYTHONPATH="src:\${PYTHONPATH:-}"
exec "\$PROJECT/.venv/bin/python" "\$PROJECT/desktop/app.py"
EOF
chmod +x "$APP_DIR/Contents/MacOS/$APP_NAME"

# ---- 2. Info.plist ----
cat > "$APP_DIR/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>FleetMonitor</string>
    <key>CFBundleDisplayName</key>
    <string>FleetMonitor</string>
    <key>CFBundleIdentifier</key>
    <string>com.fleetmonitor.app</string>
    <key>CFBundleVersion</key>
    <string>1.0.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>CFBundleExecutable</key>
    <string>FleetMonitor</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.developer-tools</string>
</dict>
</plist>
EOF

# ---- 3. 图标 ----
if "$PROJECT_DIR/.venv/bin/python" -c "import PIL" 2>/dev/null; then
  "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/make_icon.py" \
    "$APP_DIR/Contents/Resources/AppIcon.icns"
else
  echo "[跳过] 未安装 Pillow，使用默认图标"
fi

echo "已生成 $APP_DIR"
