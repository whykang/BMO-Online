#!/bin/bash
# 树莓派一键安装脚本：第一次拉代码后跑这个
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}🤖 BMO 在线版 - 树莓派安装${NC}"

# 1. 系统依赖
echo -e "${YELLOW}[1/4] 装系统依赖（apt）...${NC}"
sudo apt update
sudo apt install -y \
    python3-venv python3-tk python3-dev \
    portaudio19-dev libasound2-dev \
    mpg123 ffmpeg \
    git

# 2. Python venv
echo -e "${YELLOW}[2/4] 建虚拟环境...${NC}"
if [ ! -d venv ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install --upgrade pip

# 3. Python 包
echo -e "${YELLOW}[3/4] 装 Python 包...${NC}"
pip install --force-reinstall --no-cache-dir sounddevice
pip install -r requirements.txt

# 4. 下载默认唤醒词
# 模型从 GitHub Releases 下载（仓库 main 分支不再保留 models 子目录）
echo -e "${YELLOW}[4/4] 下载默认唤醒词模型...${NC}"
mkdir -p wakewords
OWW_BASE="https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"

download_if_missing() {
    local out="$1"
    local url="$2"
    if [ ! -f "$out" ]; then
        echo "  ↓ $out"
        if ! curl -fL --retry 2 -o "$out" "$url"; then
            echo -e "${RED}  下载失败: $url${NC}"
            rm -f "$out"
        fi
    fi
}

download_if_missing wakewords/hey_jarvis.onnx   "$OWW_BASE/hey_jarvis_v0.1.onnx"
download_if_missing wakewords/alexa.onnx        "$OWW_BASE/alexa_v0.1.onnx"
download_if_missing wakewords/hey_mycroft.onnx  "$OWW_BASE/hey_mycroft_v0.1.onnx"
download_if_missing wakewords/hey_rhasspy.onnx  "$OWW_BASE/hey_rhasspy_v0.1.onnx"

# 5. Sherpa-ONNX 中文 KWS 模型（默认后端）
SHERPA_DIR="wakewords/sherpa-kws-zh"
SHERPA_TARBALL="sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2"
SHERPA_INNER="sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
SHERPA_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/$SHERPA_TARBALL"

if [ ! -d "$SHERPA_DIR" ]; then
    echo -e "${YELLOW}下载 Sherpa-ONNX 中文 KWS 模型（约 13MB）...${NC}"
    if curl -fL --retry 2 -o "wakewords/$SHERPA_TARBALL" "$SHERPA_URL"; then
        ( cd wakewords && tar xjf "$SHERPA_TARBALL" && mv "$SHERPA_INNER" sherpa-kws-zh && rm "$SHERPA_TARBALL" )
        echo -e "${GREEN}  ✓ Sherpa KWS 模型已就绪：$SHERPA_DIR${NC}"
    else
        echo -e "${RED}  ✗ Sherpa KWS 模型下载失败，中文唤醒词将不可用${NC}"
        rm -f "wakewords/$SHERPA_TARBALL"
    fi
fi

# 5. 创建 config.json（不在 git 里，从模板复制；保留用户已有的）
if [ ! -f config.json ]; then
    cp config.default.json config.json
    echo -e "${GREEN}  ✓ 已从 config.default.json 创建 config.json${NC}"
fi

# 6. .env 提醒
if [ ! -f .env ]; then
    cp .env.example .env
    echo -e "${YELLOW}⚠️  已创建 .env，请编辑填入 SILICONFLOW_API_KEY：${NC}"
    echo -e "${YELLOW}    nano .env${NC}"
fi

echo -e "${GREEN}✨ 安装完成！下一步：${NC}"
echo -e "${GREEN}    1. nano .env 填入 API key${NC}"
echo -e "${GREEN}    2. ./start_agent.sh   （会自动同时启动 Web 控制台）${NC}"
echo -e "${GREEN}    3. 浏览器打开 http://<树莓派IP>:8087${NC}"
