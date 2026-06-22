#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE_DIR"

if [ ! -d venv ]; then
    echo "❌ 没找到 venv，先跑：python3 -m venv venv && pip install -r requirements.txt"
    exit 1
fi

source venv/bin/activate
exec python agent.py
