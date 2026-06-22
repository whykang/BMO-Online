#!/bin/bash
# Mac → 树莓派 一键同步脚本
# 用法：./sync.sh   （需要先配好 ssh pi@bmo.local 免密）

set -euo pipefail

REMOTE="${REMOTE:-pi@bmo.local}"
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
