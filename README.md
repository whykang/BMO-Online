# Be More Agent (Online Edition) 🤖

> 一个跑在树莓派 5 上的在线 AI 语音助手，使用硅基流动 API + Edge-TTS，带网页控制台。
> 基于 [brenpoly/be-more-agent](https://github.com/brenpoly/be-more-agent)（本地版）改写。

## ✨ 特性

- **语音对话**：硅基流动 SenseVoice 听话，DeepSeek-V3 思考，Edge-TTS 说话（**中文音色一流，免费**）
- **看图能力**：摄像头拍照 → Qwen2-VL 描述
- **画图能力**：说"画一只戴帽子的猫" → Kolors 生成 → 屏幕展示
- **网络搜索**：DuckDuckGo
- **唤醒词** + **物理按钮 PTT** 两种触发，并存
- **网页控制台**（http://树莓派IP:8080）：在线切换音色 / 模型 / 性格 / 唤醒词，看日志、看历史、看画廊、当遥控器
- **保留 BMO 标志性脸部动画**

## 📦 硬件

| 硬件 | 推荐型号 |
|------|---------|
| 主机 | Raspberry Pi 5（4GB 起步） |
| 系统 | Raspberry Pi OS 64-bit Desktop（Bookworm） |
| 麦克风 | USB 麦 |
| 喇叭 | USB 喇叭 / 3.5mm 小喇叭 |
| 屏幕 | DSI 或 HDMI（推荐 800×480） |
| 摄像头 | Raspberry Pi Camera Module |
| PTT 按钮 | Arduino Pro Micro (32u4) / Adafruit Feather 32u4 + 任意按钮开关 |

## 🚀 安装

### 1. 在树莓派上烧好系统、连好网络

参考 [Raspberry Pi 官方 Imager 教程](https://www.raspberrypi.com/software/)。
烧录时**勾上 Enable SSH** + **配好 WiFi**，省去插键鼠的麻烦。

### 2. 拉代码

```bash
ssh pi@bmo.local
cd ~
git clone https://github.com/你的用户名/be-more-agent-online.git
cd be-more-agent-online
```

### 3. 一键安装

```bash
chmod +x setup_pi.sh
./setup_pi.sh
```

这一步会：
- apt 装系统依赖（python3-tk / portaudio / mpg123 / ffmpeg）
- 建 venv + 装 Python 包
- 下载几个默认唤醒词模型
- 创建 `.env`（如果不存在）

### 4. 填 API key

```bash
nano .env
```

至少填一个：
```
SILICONFLOW_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

注册：<https://cloud.siliconflow.cn>

### 5. 启动

仓库已经包含了原版的 `faces/`（脸部动画）和 `sounds/`（音效），不用自己准备。

**第一个终端 — 启动 BMO 主程序**：
```bash
./start_agent.sh
```

**第二个终端 — 启动 Web 控制台**：
```bash
./start_webui.sh
```

**浏览器打开**：
```
http://bmo.local:8080
```
或 `http://树莓派IP:8080`

## 🎮 使用

### 怎么唤醒 BMO

| 方式 | 操作 |
|------|------|
| 唤醒词 | 默认 "Hey Jarvis"，可在网页换 |
| 物理按钮 | 短按 PTT 按钮（toggle 录音）|
| 网页遥控器 | 控制台 → 仪表板 → "开始录音" |

### 怎么打断 BMO

| 方式 | 操作 |
|------|------|
| 按住按钮 | 长按 PTT 按钮 ≥400ms（发空格）|
| 键盘 | 按空格键（GUI 窗口有焦点时） |
| 网页 | 控制台 → "打断说话" |

### BMO 能做什么（工具）

只要语音里隐含需求，LLM 会自己决定调用：

| 想做的事 | 怎么说 |
|---------|--------|
| 查时间 | "现在几点啦？" |
| 拍照看 | "看看这是什么？" / "你能看见什么？" |
| 搜网络 | "搜一下今天的新闻" |
| 画图 | "画一只戴耳机的橙色小猫" |
| 清记忆 | "忘记一切" / "清空记忆" |

## 🛠 网页控制台能做什么

| 标签 | 功能 |
|------|------|
| 仪表板 | 当前状态 + 快捷遥控 |
| 音色 | 切换 Edge-TTS 音色（中/英/日）+ 试听 + 兜底 TTS 设置 |
| 模型 | 切换 LLM / Vision / STT / 文生图模型 |
| 性格 | 编辑 system prompt + 调记忆轮数 |
| 唤醒词 | 启用/禁用、切换、上传 .onnx、调灵敏度 |
| 对话历史 | 查看 + 清空 |
| 图片画廊 | 看 BMO 画过的图、删除 |
| 遥控器 | 录音/打断/拍照按钮 + 让 BMO 说一句话 |
| 日志 | 实时日志流（SSE） |
| API Key | 看哪几个 provider 已配置、改 key |
| 安全 | 设置/取消访问密码 |

## ⚙️ 配置说明

所有可调参数在 `config.json`，关键字段：

```jsonc
{
  "llm":    { "provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V3" },
  "vision": { "provider": "siliconflow", "model": "Qwen/Qwen2-VL-7B-Instruct" },
  "stt":    { "provider": "siliconflow", "model": "FunAudioLLM/SenseVoiceSmall" },
  "tts": {
    "provider": "edge",
    "voice": "zh-CN-XiaoyiNeural",       // 默认晓伊
    "fallback_provider": "siliconflow",  // Edge 失败时兜底
    "fallback_model": "FunAudioLLM/CosyVoice2-0.5B"
  },
  "image_gen": { "provider": "siliconflow", "model": "Kwai-Kolors/Kolors" },
  "wake_word": { "enabled": true, "model": "wakewords/hey_jarvis.onnx", "threshold": 0.5 },
  "memory_max_turns": 30
}
```

`.env`：API key 和 Web 控制台端口。

## 🔧 物理按钮（可选）

Arduino Pro Micro / Feather 32u4 + 一个按钮开关，就能加物理 PTT。
见 [firmware/README.md](firmware/README.md)。

## 🌐 远程同步开发

在你 Mac 上改完代码：

```bash
./sync.sh                 # rsync 增量同步到 pi@bmo.local
```

需要先配好 `pi@bmo.local` SSH 免密登录。
变量 `REMOTE` / `REMOTE_DIR` 可以覆盖默认值：

```bash
REMOTE=pi@192.168.1.50 ./sync.sh
```

## 💰 费用估算

按一天 100 次对话估算（中度使用）：

| 模块 | 月费 |
|------|------|
| STT (SenseVoice) | ¥1 |
| LLM (DeepSeek-V3) | ¥10~20 |
| Vision (Qwen2-VL) | ¥3 |
| TTS (Edge-TTS) | **¥0**（免费） |
| 文生图 (Kolors) | 看使用量 |
| **合计** | **¥15~30/月** |

实时余额到 [硅基流动控制台](https://cloud.siliconflow.cn/account/billing) 查看。

## 🐛 排错

| 现象 | 解决 |
|------|------|
| `❌ 缺少 SILICONFLOW_API_KEY` | 检查 `.env` 文件 |
| 麦克风没反应 | `python -c "import sounddevice as sd; print(sd.query_devices())"` 看设备列表，把名字填进 `config.json` 的 `input_device` |
| TTS 没声音 | 检查 mpg123 是否装好：`which mpg123`；测试：`echo "hi" \| espeak` |
| 唤醒词不响应 | 网页里调低 threshold；检查录音音量 |
| Web 控制台连不上 | 检查端口（`sudo ss -tlnp \| grep 8080`）、防火墙 |
| ALSA 错误一堆 | 正常的，无影响（树莓派音频驱动的小毛病） |

## 📚 项目结构

```
be-more-agent-online/
├── agent.py                # BMO 主程序（GUI + 状态机 + 流水线）
├── webui.py                # FastAPI Web 控制台
├── providers/              # API provider 抽象层
│   ├── llm.py
│   ├── stt.py
│   ├── vision.py
│   ├── tts_edge.py
│   ├── tts_siliconflow.py
│   └── image_gen.py
├── static/                 # Web 控制台前端（单文件 HTML）
│   ├── index.html
│   └── login.html
├── wakewords/              # 唤醒词 .onnx 模型库
├── firmware/               # Pro Micro 按钮固件
├── faces/                  # BMO 脸部动画（PNG 序列，需自备）
├── sounds/                 # 音效（.wav，需自备）
├── generated/              # AI 画的图（运行时生成）
├── logs/                   # 日志按天分文件
├── config.json
├── .env.example
├── requirements.txt
├── setup_pi.sh
├── start_agent.sh
├── start_webui.sh
└── sync.sh
```

## 🙏 致谢

- 原版 [Be More Agent](https://github.com/brenpoly/be-more-agent) by **brenpoly** — 整个项目的灵感和骨架来源
- [OpenWakeWord](https://github.com/dscripka/openWakeWord) — 唤醒词检测
- [Edge-TTS](https://github.com/rany2/edge-tts) — 微软 TTS 的 Python 封装
- [硅基流动](https://siliconflow.cn) — 一站式 AI 模型 API
- BMO 角色版权归 Cartoon Network；本项目仅作非商业爱好

## 📄 许可证

MIT
