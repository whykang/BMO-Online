#!/bin/bash
# 树莓派一键安装脚本：第一次拉代码后跑这个
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# ===== 下载源策略 =====
# 本脚本是共用引擎，不直接运行；请用下面两个之一：
#   ./setup_pi_cn.sh      —— 纯代理：pip 走清华，GitHub 走代理（中国大陆）
#   ./setup_pi_direct.sh  —— 纯直连：不走任何代理（海外，或自带全局代理）
BMO_MIRROR="${BMO_MIRROR:-}"
if [ "$BMO_MIRROR" != "cn" ] && [ "$BMO_MIRROR" != "direct" ]; then
    echo -e "${RED}请不要直接运行 setup_pi.sh。${NC}"
    echo -e "${YELLOW}  中国大陆： ./setup_pi_cn.sh${NC}"
    echo -e "${YELLOW}  海外直连： ./setup_pi_direct.sh${NC}"
    exit 1
fi
PIP_CN_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
# GitHub 代理可用 GH_PROXY 环境变量覆盖（空格分隔多个）。
GH_PROXIES=(${GH_PROXY:-"https://ghfast.top" "https://gh-proxy.com" "https://ghproxy.net"})

pip_install() {
    if [ "$BMO_MIRROR" = "cn" ]; then
        pip install -i "$PIP_CN_MIRROR" "$@"      # 纯代理：清华镜像
    else
        pip install "$@"                           # 纯直连：默认 PyPI
    fi
}

gh_download() {
    # gh_download <github-url> <output-path>
    local url="$1" out="$2" proxy
    if [ "$BMO_MIRROR" = "cn" ]; then
        for proxy in "${GH_PROXIES[@]}"; do        # 纯代理：依次试代理
            echo -e "${YELLOW}    经 $proxy 下载...${NC}"
            if curl -fL --retry 2 --connect-timeout 15 -o "$out" "$proxy/$url"; then
                return 0
            fi
        done
        rm -f "$out"
        return 1
    fi
    # 纯直连
    if curl -fL --retry 2 --connect-timeout 15 -o "$out" "$url"; then
        return 0
    fi
    rm -f "$out"
    return 1
}

echo -e "${GREEN}🤖 BMO 在线版 - 树莓派安装${NC}"
case "$BMO_MIRROR" in
    cn)     echo -e "${YELLOW}下载模式：纯代理（pip 清华 + GitHub 代理，大陆加速）${NC}" ;;
    direct) echo -e "${YELLOW}下载模式：纯直连（不走任何代理）${NC}" ;;
esac

# 1. 系统依赖
echo -e "${YELLOW}[1/4] 装系统依赖（apt）...${NC}"
sudo apt update

# 必装项：运行时真正需要的，这些在 RPi OS（含 trixie）上都能正常装。
# 注意：sounddevice 运行时只需 libportaudio2（不需要 portaudio19-dev 头文件）；
#       aplay/amixer 来自 alsa-utils。
sudo apt install -y \
    python3-venv python3-tk python3-dev \
    mpg123 ffmpeg \
    unclutter xdotool grim \
    fceux git \
    libportaudio2 alsa-utils

# 可选项（best-effort）：开发头 / PipeWire-ALSA / PulseAudio 工具。
# RPi OS Desktop 一般已自带它们的 +rpt 版，而 Debian 源的版本会与 +rpt 版本号
# 精确冲突（trixie 上的经典报错）。所以逐个尝试，装不上就跳过——不影响运行：
#   pactl 缺失时代码会自动跳过；音频路由用系统已装的 PipeWire/ALSA。
for pkg in portaudio19-dev libasound2-dev pipewire-alsa pulseaudio-utils; do
    sudo apt install -y "$pkg" \
        || echo -e "${YELLOW}  跳过 $pkg（系统已有兼容版本或与 +rpt 版冲突，不影响运行）${NC}"
done

# 2. Python venv
echo -e "${YELLOW}[2/4] 建虚拟环境...${NC}"
if [ ! -d venv ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip_install --upgrade pip

# 3. Python 包
echo -e "${YELLOW}[3/4] 装 Python 包...${NC}"
pip_install --force-reinstall --no-cache-dir sounddevice
pip_install -r requirements.txt

# 4. 唤醒词模型（已随仓库自带，无需下载；缺失时才从外部下载兜底）
echo -e "${YELLOW}[4/4] 检查唤醒词模型...${NC}"
mkdir -p wakewords

# OpenWakeWord 模型 hey_bmo.onnx 仓库自带，无需下载；只检查在不在
if [ ! -f wakewords/hey_bmo.onnx ]; then
    echo -e "${RED}  ⚠️ 缺少 wakewords/hey_bmo.onnx（OpenWakeWord 英文唤醒词）${NC}"
fi

# Sherpa-ONNX 中文 KWS 模型 —— 正常情况下仓库已自带（int8 精简版）
SHERPA_DIR="wakewords/sherpa-kws-zh"
if [ ! -f "$SHERPA_DIR/tokens.txt" ]; then
    echo -e "${YELLOW}  仓库缺 Sherpa 中文模型，尝试下载兜底...${NC}"
    SHERPA_TARBALL="sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2"
    SHERPA_INNER="sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
    SHERPA_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/$SHERPA_TARBALL"
    if gh_download "$SHERPA_URL" "wakewords/$SHERPA_TARBALL"; then
        ( cd wakewords && tar xjf "$SHERPA_TARBALL" && mv "$SHERPA_INNER" sherpa-kws-zh && rm "$SHERPA_TARBALL" )
        echo -e "${GREEN}  ✓ Sherpa KWS 模型已就绪${NC}"
    else
        echo -e "${RED}  ✗ Sherpa KWS 模型下载失败，中文唤醒词将不可用${NC}"
    fi
else
    echo -e "${GREEN}  ✓ 唤醒词模型已就绪（仓库自带）${NC}"
fi

# 4b. 本地 STT 模型（SenseVoice，可选）。
#     官方打成一个 .tar.bz2（下载约 1GB，改不了），但解压时排除非 int8 大模型和
#     测试音频，只落地 int8 版（约 230MB），省磁盘、省后续清理。
#     只有 config.stt.provider = local_sherpa 时才需要；不想要可注释掉这段。
SV_DIR="models/sense-voice"
SV_NEWLY_INSTALLED=0
if [ ! -f "$SV_DIR/tokens.txt" ]; then
    echo -e "${YELLOW}  下载本地 STT 模型 SenseVoice（下载约 1GB，较慢；只保留 int8 约230MB）...${NC}"
    mkdir -p models
    SV_TARBALL="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2"
    SV_INNER="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
    SV_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$SV_TARBALL"
    if gh_download "$SV_URL" "models/$SV_TARBALL"; then
        ( cd models \
            && rm -rf "$SV_INNER" sense-voice \
            && tar xjf "$SV_TARBALL" \
                 --exclude='*/model.onnx' \
                 --exclude='*/test_wavs' \
            && mv "$SV_INNER" sense-voice \
            && rm "$SV_TARBALL" )
        if [ -f "$SV_DIR/tokens.txt" ]; then
            SV_NEWLY_INSTALLED=1
            echo -e "${GREEN}  ✓ 本地 STT 模型已就绪：$SV_DIR（仅 int8）${NC}"
        fi
    else
        echo -e "${RED}  ✗ 本地 STT 模型下载失败（不影响云端 STT）${NC}"
    fi
else
    echo -e "${GREEN}  ✓ 本地 STT 模型已就绪${NC}"
fi

# 4c. 本地 TTS 模型（Piper 中文 huayan，可选；约 60MB）
PIPER_DIR="models/piper-zh"
if [ ! -f "$PIPER_DIR/tokens.txt" ]; then
    echo -e "${YELLOW}  下载本地 TTS 模型 Piper 中文（约 60MB）...${NC}"
    mkdir -p models
    PIPER_TARBALL="vits-piper-zh_CN-huayan-medium.tar.bz2"
    PIPER_INNER="vits-piper-zh_CN-huayan-medium"
    PIPER_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/$PIPER_TARBALL"
    if gh_download "$PIPER_URL" "models/$PIPER_TARBALL"; then
        ( cd models && rm -rf "$PIPER_INNER" piper-zh \
            && tar xjf "$PIPER_TARBALL" && mv "$PIPER_INNER" piper-zh && rm "$PIPER_TARBALL" )
        echo -e "${GREEN}  ✓ 本地 TTS 模型已就绪：$PIPER_DIR${NC}"
    else
        echo -e "${RED}  ✗ 本地 TTS 模型下载失败（不影响 Edge/云端 TTS）${NC}"
    fi
else
    echo -e "${GREEN}  ✓ 本地 TTS 模型已就绪${NC}"
fi

# 5. 创建 config.json（不在 git 里，从模板复制；保留用户已有的）
if [ ! -f config.json ]; then
    cp config.default.json config.json
    echo -e "${GREEN}  ✓ 已从 config.default.json 创建 config.json${NC}"
fi

# 5b. 本次新装好本地 STT → 默认就用它（只在'本次新下载'时设，不覆盖你之后手动切回云端）
if [ "$SV_NEWLY_INSTALLED" = "1" ]; then
    python3 - <<'PYEOF'
import json
try:
    cfg = json.load(open("config.json", encoding="utf-8"))
    cfg.setdefault("stt", {})
    cfg["stt"]["provider"] = "local_sherpa"
    cfg["stt"]["model"] = "models/sense-voice"
    json.dump(cfg, open("config.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("  ✓ 已默认启用本地 STT（local_sherpa）")
except Exception as e:
    print(f"  ⚠️ 设置默认 STT 失败: {e}")
PYEOF
fi

# 6. .env 提醒
if [ ! -f .env ]; then
    cp .env.example .env
    echo -e "${YELLOW}⚠️  已创建 .env，请编辑填入 SILICONFLOW_API_KEY：${NC}"
    echo -e "${YELLOW}    nano .env${NC}"
fi

chmod +x start_agent.sh start_webui.sh install_desktop_launcher.sh
./install_desktop_launcher.sh || true

# 7. 默认开启开机自启（XDG autostart，和网页「仪表板」里那个开关同一份文件）。
#    已存在就不覆盖，尊重用户之后在网页里的开/关选择。
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
AUTOSTART_FILE="$AUTOSTART_DIR/bmo.desktop"
if [ ! -f "$AUTOSTART_FILE" ]; then
    mkdir -p "$AUTOSTART_DIR"
    cat > "$AUTOSTART_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=BMO Agent
Comment=Be More Agent (Online) auto-starter
Exec=sh -c 'sleep 8; exec $PROJECT_DIR/start_agent.sh'
Path=$PROJECT_DIR
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
    echo -e "${GREEN}  ✓ 已默认开启开机自启（开机进桌面后自动启动 BMO；可在网页「仪表板」关闭）${NC}"
fi

# 取本机 IP 和 Web 控制台端口，直接拼出可点的网址
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
WEB_PORT=$(grep -oE '"webui_port"[[:space:]]*:[[:space:]]*[0-9]+' config.json 2>/dev/null | grep -oE '[0-9]+$')
WEB_PORT=${WEB_PORT:-8087}

echo -e "${GREEN}✨ 安装完成！下一步：${NC}"
echo -e "${GREEN}    1. ./start_agent.sh   （会自动同时启动 Web 控制台）${NC}"
if [ -n "$LOCAL_IP" ]; then
    echo -e "${GREEN}    2. 浏览器打开 http://${LOCAL_IP}:${WEB_PORT}${NC}"
else
    echo -e "${GREEN}    2. 浏览器打开 http://<本机IP>:${WEB_PORT}（用 hostname -I 查看 IP）${NC}"
fi
