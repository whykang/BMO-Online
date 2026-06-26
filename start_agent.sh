#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE_DIR"

"$BASE_DIR/install_desktop_launcher.sh" >/dev/null 2>&1 || true

export DISPLAY="${DISPLAY:-:0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [ -z "${WAYLAND_DISPLAY:-}" ]; then
    if [ -S "$XDG_RUNTIME_DIR/wayland-0" ]; then
        export WAYLAND_DISPLAY=wayland-0
    elif [ -S "$XDG_RUNTIME_DIR/wayland-1" ]; then
        export WAYLAND_DISPLAY=wayland-1
    fi
fi

if [ ! -d venv ]; then
    echo "❌ 没找到 venv，先跑：python3 -m venv venv && pip install -r requirements.txt"
    exit 1
fi

source venv/bin/activate
exec python agent.py
