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
echo -e "${YELLOW}[4/4] 下载默认唤醒词模型...${NC}"
mkdir -p wakewords
if [ ! -f wakewords/hey_jarvis.onnx ]; then
    curl -L -o wakewords/hey_jarvis.onnx \
        https://github.com/dscripka/openWakeWord/raw/main/openwakeword/resources/models/hey_jarvis_v0.1.onnx
fi
if [ ! -f wakewords/alexa.onnx ]; then
    curl -L -o wakewords/alexa.onnx \
        https://github.com/dscripka/openWakeWord/raw/main/openwakeword/resources/models/alexa_v0.1.onnx
fi
if [ ! -f wakewords/hey_mycroft.onnx ]; then
    curl -L -o wakewords/hey_mycroft.onnx \
        https://github.com/dscripka/openWakeWord/raw/main/openwakeword/resources/models/hey_mycroft_v0.1.onnx
fi

# 5. .env 提醒
if [ ! -f .env ]; then
    cp .env.example .env
    echo -e "${YELLOW}⚠️  已创建 .env，请编辑填入 SILICONFLOW_API_KEY：${NC}"
    echo -e "${YELLOW}    nano .env${NC}"
fi

echo -e "${GREEN}✨ 安装完成！下一步：${NC}"
echo -e "${GREEN}    1. nano .env 填入 API key${NC}"
echo -e "${GREEN}    2. ./start_agent.sh${NC}"
echo -e "${GREEN}    3. 另开终端：./start_webui.sh${NC}"
echo -e "${GREEN}    4. 浏览器打开 http://树莓派IP:8080${NC}"
