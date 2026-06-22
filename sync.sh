#!/bin/bash
# 一键 rsync 同步脚本：把当前目录推到树莓派
#
# 用法（请用环境变量指定你的树莓派 SSH 目标和远程路径）：
#     REMOTE=<用户名>@<hostname-or-ip> REMOTE_DIR=~/BMO-Online/ ./sync.sh
# 例：
#     REMOTE=pi@192.168.1.50 ./sync.sh
# 提示：先配好对应 SSH 免密登录（ssh-copy-id <用户名>@<hostname-or-ip>）。

set -euo pipefail

if [ -z "${REMOTE:-}" ]; then
    echo "❌ 缺少 REMOTE 环境变量。用法："
    echo "    REMOTE=<用户名>@<hostname-or-ip> ./sync.sh"
    exit 1
fi
REMOTE_DIR="${REMOTE_DIR:-~/BMO-Online/}"

echo "→ 同步到 $REMOTE:$REMOTE_DIR"

rsync -avz --delete \
    --exclude='venv/' \
    --exclude='__pycache__/' \
    --exclude='.env' \
    --exclude='logs/' \
    --exclude='generated/' \
    --exclude='chat_memory.json' \
    --exclude='state.json' \
    --exclude='commands.json' \
    --exclude='input.wav' \
    --exclude='current_image.jpg' \
    --exclude='auth.json' \
    --exclude='.git/' \
    ./ "$REMOTE:$REMOTE_DIR"

echo "✓ 同步完成"
