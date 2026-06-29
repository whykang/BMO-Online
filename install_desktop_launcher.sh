#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
# 优先用自定义程序图标 static/icon.png；没有就回落到脸部动画的一帧
ICON_SOURCE="$BASE_DIR/static/icon.png"
[ -f "$ICON_SOURCE" ] || ICON_SOURCE="$BASE_DIR/faces/idle/idle 01.png"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons"
ICON_FILE="$ICON_DIR/bmo-online.png"

desktop_dir=""
if command -v xdg-user-dir >/dev/null 2>&1; then
    desktop_dir="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
fi
if [ -z "$desktop_dir" ] || [ "$desktop_dir" = "$HOME" ]; then
    if [ -d "$HOME/Desktop" ]; then
        desktop_dir="$HOME/Desktop"
    elif [ -d "$HOME/桌面" ]; then
        desktop_dir="$HOME/桌面"
    else
        desktop_dir="$HOME/Desktop"
    fi
fi

mkdir -p "$desktop_dir" "$ICON_DIR"
if [ -f "$ICON_SOURCE" ]; then
    cp "$ICON_SOURCE" "$ICON_FILE"
fi

project_escaped="$(printf '%q' "$BASE_DIR")"
launcher="$desktop_dir/BMO Online.desktop"
tmp_launcher="$launcher.tmp"

cat > "$tmp_launcher" <<EOF
[Desktop Entry]
Type=Application
Name=BMO Online
Name[zh_CN]=启动 BMO
Comment=Start BMO Online
Comment[zh_CN]=启动 BMO 在线版
Exec=sh -lc 'cd $project_escaped && exec ./start_agent.sh'
Path=$BASE_DIR
Icon=$ICON_FILE
Terminal=false
Categories=Utility;
StartupNotify=false
EOF

if [ ! -f "$launcher" ] || ! cmp -s "$tmp_launcher" "$launcher"; then
    mv "$tmp_launcher" "$launcher"
    chmod +x "$launcher"
    if command -v gio >/dev/null 2>&1; then
        gio set "$launcher" metadata::trusted true >/dev/null 2>&1 || true
    fi
    echo "✓ 已创建桌面启动图标：$launcher"
else
    rm -f "$tmp_launcher"
fi
