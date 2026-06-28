# BMO (Online AI Agent) 🤖

> 一个跑在树莓派 5 上的在线 AI 语音助手，接入多种模型，可随意切换，带网页控制台。
> 基于 [brenpoly/be-more-agent](https://github.com/brenpoly/be-more-agent)（本地版）改写。

## ✨ 特性

- **语音对话**：硅基流动 SenseVoice 听话，DeepSeek-V3 思考，Edge-TTS 说话（**中文音色一流，免费**）
- **看图能力**：摄像头拍照 → Qwen2-VL 描述
- **画图能力**：说"画一只戴帽子的猫" → 硅基流动 / OpenRouter / OpenAI 文生图模型生成 → 屏幕展示
- **搜索问答**：调用博查搜索接口，回答需要网络搜索/新闻类问题
- **中文唤醒词**（Sherpa-ONNX KWS，零训练）+ **物理按钮 PTT** 两种触发，并存
- **网页控制台**（http://树莓派IP:8087）：在线切换音色 / 模型 / 性格 / 唤醒词，看日志、看历史、看画廊、当遥控器
- **保留 BMO 标志性脸部动画**

## 📦 硬件

| 硬件 | 推荐型号 |
|------|---------|
| 主机 | Raspberry Pi 5（4GB ） |
| 系统 | Raspberry Pi OS 64-bit Desktop（Bookworm） |
| 麦克风 | USB 麦 |
| 喇叭 | USB 喇叭 / 3.5mm 小喇叭 |
| 屏幕 | 5 英寸 DSI 或 HDMI（推荐 800×480） |
| 摄像头 | Raspberry Pi Camera Module v2 |
| PTT 按钮 | Arduino Pro Micro (32u4) + 7个 6x6x5 mm 微动开关 |

## 🚀 安装

### 1. 在树莓派上烧好系统、连好网络

参考 [Raspberry Pi 官方 Imager 教程](https://www.raspberrypi.com/software/)。
烧录时**勾上 Enable SSH** + **配好 WiFi**，省去插键鼠的麻烦。

### 2. 拉代码

SSH 登录到你的树莓派。

进去之后拉代码：

```bash
git clone https://github.com/whykang/BMO-Online.git
cd BMO-Online
```

### 3. 一键安装

```bash
chmod +x setup_pi.sh
./setup_pi.sh
```

这一步会：
- apt 装系统依赖（python3-tk / portaudio / mpg123 / ffmpeg）
- 建 venv + 装 Python 包
- 检查唤醒词模型（仓库自带 `hey_bmo.onnx` + Sherpa-ONNX 中文 KWS）
- 创建 `.env`（如果不存在）

> **🇨🇳 中国大陆环境**：脚本已内置兜底——pip 默认源失败会自动切清华镜像，
> GitHub 模型下载失败会自动套国内代理（ghfast.top 等）重试，无需手动配置。
> 想指定自己的 GitHub 代理：`GH_PROXY="https://你的代理" ./setup_pi.sh`。

### 4. 启动


```bash
./start_agent.sh
```

启动主程序时**会自动同时拉起 Web 控制台**（这个行为可在网页里关掉）。

> API key 直接在网页控制台的「API Key」里填即可（至少填一个，默认用[硅基流动](https://cloud.siliconflow.cn)）。

**浏览器打开** Web 控制台：

```
http://<树莓派 IP>:8087
```

不知道树莓派 IP？在ssh 终端跑：

```bash
hostname -I
```

> 如果你给树莓派设了 主机名（hostname）比如烧系统时填了 `bmo`，也可以用 `http://bmo.local:8087`。

## 🎮 使用

### 怎么唤醒 BMO

| 方式 | 操作 |
|------|------|
| 唤醒词 | 默认英文"hey bmo"(中文发音：“嘿，比目”），支持在网页里改成你想要的中文短语 |
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
| 聊天 | "讲个笑话" |
| 拍照看 | "看看这是什么？" / "你能看见什么？" |
| 画图 | "画一只戴耳机的橙色小猫" |
| 查系统状态 | "看看系统状态" / "CPU 和温度怎么样？" |
| 隐身 | "隐身" / "退出隐身" |
| 玩游戏 | "打开坦克大战"|
| 播放音乐 | "播放音乐花海" |
| 搜索 | “今天北京天气怎么样” / "今天有哪些新闻" |
| 打印 | "打印近10条的对话内容" / "画一只猫并打印出来"|
| 清记忆 | "忘记一切" / "清空记忆" |
| 以及等等等等。。。。。。。。。。|



## 🛠 网页控制台能做什么

| 标签 | 功能 |
|------|------|
| 仪表板 | 当前状态 + 快捷遥控 |
| 音色 | 切换 Edge-TTS 音色（中/英/日）+ 试听 + 兜底 TTS 设置 |
| 模型 | 切换 LLM / Vision / STT / 文生图模型 |
| 性格 | 编辑 system prompt + 调记忆轮数 |
| 唤醒词 | 中文关键词文本输入（任意短语，零训练）/ 切引擎 / 调灵敏度 |
| 对话历史 | 查看 + 清空 |
| 图片画廊 | 看 BMO 画过的图、删除 |
| 遥控器 | 录音/打断/拍照按钮 + 让 BMO 说一句话 |
| 日志 | 实时日志流（SSE） |
| API Key | 看哪几个 provider 已配置、改 key |
| 安全 | 设置/取消访问密码 |



## 🎙 自定义中文唤醒词


### 几个调参建议

| 现象 | 怎么改 |
|------|--------|
| 误唤醒太多（说话总被打断） | 阈值调高（0.30 → 0.40），或选更生僻的关键词 |
| 唤不醒（怎么喊都没反应） | 阈值调低（0.25 → 0.18） |
| 中间的字总被吞 | 关键词得分调高（1.5 → 2.0） |
| 想加英文唤醒词 | 中英文都能加，例：`["你好小明", "hello bmo"]` |

### 引擎切换

| 引擎 | 适用 | 配置 |
|------|------|------|
| **Sherpa-ONNX**） | 中文为主，任意短语零训练 | 在网页里直接打字 |
| **OpenWakeWord** | 英文，需训练 `.onnx` 模型 | 上传 .onnx，老办法 |


## 🐛 排错

| 现象 | 解决 |
|------|------|
| `❌ 缺少 SILICONFLOW_API_KEY` | 检查 `.env` 文件 |
| 麦克风没反应 | `python -c "import sounddevice as sd; print(sd.query_devices())"` 看设备列表，把名字填进 `config.json` 的 `input_device` |
| TTS 没声音 | 检查 mpg123 是否装好：`which mpg123`；测试：`echo "hi" \| espeak` |
| 唤醒词不响应 | 网页里调低 threshold；检查录音音量 |
| 鼠标箭头还在屏幕上 | `sudo apt install unclutter xdotool` 后重启 BMO；Wayland 下会继续用 Tk 透明光标兜底 |
| 开机自启没生效 | 在网页「仪表板」重新开一次自启；现在会同时写入 systemd user、labwc autostart 和传统 desktop autostart |
| Web 控制台连不上 | 检查端口（`sudo ss -tlnp \| grep 8087`）、防火墙 |
| ALSA 错误一堆 | 正常的，无影响（树莓派音频驱动的小毛病） |

## 📚 项目结构

```
BMO-Online/
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
├── wakewords/              # Sherpa KWS 模型目录 + OpenWakeWord .onnx 模型库
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
- [Sherpa-ONNX](https://github.com/k2-fsa/sherpa-onnx) — 中文唤醒词 KWS（默认引擎）
- [OpenWakeWord](https://github.com/dscripka/openWakeWord) — 英文唤醒词引擎（可选）
- [Edge-TTS](https://github.com/rany2/edge-tts) — 微软 TTS 的 Python 封装
- [硅基流动](https://siliconflow.cn) — 一站式 AI 模型 API
- BMO 角色版权归 Cartoon Network；本项目仅作非商业爱好

## 📄 许可证

MIT
