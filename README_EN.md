# BMO (Online AI Agent) 🤖

<p align="center">
  <a href="README.md">简体中文</a> | <strong>English</strong>
</p>

> An online AI voice assistant running on Raspberry Pi 5, with support for multiple switchable models and a web control panel.
> Adapted from [brenpoly/be-more-agent](https://github.com/brenpoly/be-more-agent), the original local version.

## ✨ Features

- **Voice conversations:** Supports popular model providers including OpenAI, DeepSeek, OpenRouter, and SiliconFlow.
- **Vision:** Take a photo with the camera and ask a vision model to describe it.
- **Image generation:** Say “Draw a cat wearing a hat,” generate the image, and display it on screen.
- **Web search:** Uses Bocha Search for current information, news, and other web-dependent questions.
- **Wake-up options:** Local wake-word detection and a physical PTT button can be used together.
- **Web control panel** (`http://<Raspberry-Pi-IP>:8087`): Switch voices, models, personalities, and wake words; inspect logs, history, and the gallery; or use it as a remote control.
- **BMO's signature animated facial expressions.**

## 📦 Hardware

| Component | Recommended option |
|---|---|
| Computer | Raspberry Pi 5 (4 GB) |
| Operating system | Raspberry Pi OS 64-bit Desktop (Bookworm) |
| Microphone | USB microphone |
| Speaker | USB speaker or 3.5 mm speaker |
| Display | 5-inch DSI or HDMI display (800×480 recommended) |
| Camera | Raspberry Pi Camera Module v2 |
| PTT buttons | Arduino Pro Micro (ATmega32U4) + seven 6×6×5 mm tactile switches |

## 🚀 Installation

### 1. Install Raspberry Pi OS and connect to the network

Follow the official [Raspberry Pi Imager guide](https://www.raspberrypi.com/software/).
Enable **SSH** and configure **Wi-Fi** while flashing the image to avoid needing a keyboard and mouse during setup.

### 2. Clone the repository

Connect to your Raspberry Pi through SSH, then run:

```bash
git clone https://github.com/whykang/BMO-Online.git
cd BMO-Online
```

> **Network acceleration for mainland China:**
>
> ```bash
> git clone https://ghfast.top/https://github.com/whykang/BMO-Online.git
> cd BMO-Online
> ```

### 3. Run the installer

Make the installation scripts executable:

```bash
chmod +x setup_pi_cn.sh setup_pi_direct.sh
```

For **mainland China** (Tsinghua PyPI mirror and GitHub proxy):

```bash
./setup_pi_cn.sh
```

For **other regions** (direct connections without a proxy):

```bash
./setup_pi_direct.sh
```

The installer will:

- Install system dependencies with `apt` (`python3-tk`, PortAudio, `mpg123`, and FFmpeg).
- Create a virtual environment and install the required Python packages.
- Check the wake-word models (`hey_bmo.onnx` and the Sherpa-ONNX Chinese KWS model).
- Create `.env` if it does not already exist.

> To use a custom GitHub proxy, run: `GH_PROXY="https://your-proxy" ./setup_pi_cn.sh`.

### 4. Optional: enable UART for a thermal printer

> This step is required only when a thermal printer is connected through the GPIO UART pins. If you do not use a printer, skip to Step 5.

Raspberry Pi OS does not enable the hardware serial port by default. Without it, printing may fail with `could not open port /dev/serial0 / No such file or directory`.

Run:

```bash
sudo raspi-config
```

Open **Interface Options → Serial Port**, then answer:

- **Would you like a login shell to be accessible over serial?** → **No**. This prevents the login console from occupying the printer's serial port.
- **Would you like the serial port hardware to be enabled?** → **Yes**.

Select **Finish** and reboot:

```bash
sudo reboot
```

After rebooting, send a test line directly to the serial port. If the printer feeds paper, the connection is working:

```bash
printf 'BMO printer test\n\n\n' > /dev/ttyAMA0
```

> **Raspberry Pi 5 note:** The UART on GPIO pins 8 and 10 is normally **`/dev/ttyAMA0`**. On Pi 5, `/dev/serial0` may point to the `ttyAMA10` debug port instead of the GPIO header. The default `printer.device` is therefore `/dev/ttyAMA0`. If the test does not print, try `/dev/serial0` and use whichever device works in `config.json`.

Connect the printer RX, TX, and GND pins to Raspberry Pi TXD (GPIO14, physical pin 8), RXD (GPIO15, physical pin 10), and GND. TX and RX must be crossed, and both devices must share ground. Use a separate power supply for the printer because thermal printing can draw 1.5–2 A. The default baud rate is 9600; if necessary, try 19200 or 115200 in the `printer` section of `config.json`. Use `width=384` for 58 mm paper or `width=576` for 80 mm paper.

### 5. Start BMO

```bash
./start_agent.sh
```

Starting the main application also starts the web control panel automatically.

After the first launch, open the web control panel, enter your credentials under **API Key**, and select the corresponding model.

Open the control panel in a browser:

```text
http://<Raspberry-Pi-IP>:8087
```

To find the Raspberry Pi's IP address, run:

```bash
hostname -I
```

If your Raspberry Pi has a hostname such as `bmo`, you can also try `http://bmo.local:8087`.

## 🎮 Usage

### Waking BMO

| Method | Action |
|---|---|
| Wake word | The default is “hey bmo” (Chinese pronunciation hint: “嘿，比目”). You can configure a custom Chinese phrase in the web panel. |
| Physical button | Short-press the PTT button to toggle recording. |
| Web remote | Open **Dashboard → Start Recording**. |

### Interrupting BMO

| Method | Action |
|---|---|
| Physical button | Hold the PTT button for at least 400 ms to send Space. |
| Keyboard | Press Space while the GUI window has focus. |
| Web panel | Select **Interrupt Speech**. |

### Available tools

The LLM automatically selects a tool when your request implies that one is needed.

| Task | Example phrase |
|---|---|
| Check the time | “What time is it?” |
| Chat | “Tell me a joke.” |
| Use the camera | “What is this?” / “What can you see?” |
| Generate an image | “Draw an orange cat wearing headphones.” |
| Check system status | “How are the CPU and temperature?” |
| Enter stealth mode | “Go invisible.” / “Exit stealth mode.” |
| Play a game | “Open Battle City.” |
| Play music | “Play some music.” |
| Search the web | “What is the weather in Beijing today?” / “What is in the news today?” |
| Print | “Print the last ten conversations.” / “Draw a cat and print it.” |
| Clear memory | “Forget everything.” / “Clear your memory.” |
| More | See the tool prompts for additional capabilities. |

## 🛠 Web Control Panel

| Tab | Functions |
|---|---|
| Dashboard | Current status, quick remote controls, and one-click printing |
| Voice | Switch and preview Edge-TTS voices (Chinese, English, and Japanese), or configure a custom TTS provider |
| Models | Switch LLM, vision, STT, and image-generation models |
| Personality | Edit the system prompt and memory length |
| Wake Word | Enter any Chinese keyword without training, switch engines, and adjust sensitivity |
| Conversation History | View and clear conversations |
| Image Gallery | View and delete images generated by BMO |
| Games | Upload, launch, and exit games |
| Media | Upload music and video files that BMO can control |
| Remote Control | Record, interrupt, take a photo, or make BMO speak a sentence |
| Logs | Real-time log stream through SSE |
| API Key | View configured providers and update API keys |
| Security | Set or remove the web administration password |

## 🎙 Custom Chinese Wake Words

### Tuning tips

| Symptom | Adjustment |
|---|---|
| Too many false activations | Increase the threshold, for example from 0.30 to 0.40, or use a less common phrase. |
| BMO does not wake up | Lower the threshold, for example from 0.25 to 0.18. |
| Middle characters are frequently missed | Increase the keyword score, for example from 1.5 to 2.0. |
| Add an English wake word | Chinese and English phrases can be combined, for example `['你好小明', 'hello bmo']`. |

### Wake-word engines

| Engine | Best for | Configuration |
|---|---|---|
| **OpenWakeWord** | English (default); requires a trained `.onnx` model | Upload an ONNX model; one model is included. |
| **Sherpa** | Primarily Chinese; phrases can be changed freely | No custom model training required. |

## 🐛 Troubleshooting

| Symptom | Solution |
|---|---|
| `❌ Missing SILICONFLOW_API_KEY` | Check the `.env` file. |
| Microphone does not respond | Run `python -c "import sounddevice as sd; print(sd.query_devices())"`, then set the device name as `input_device` in `config.json`. |
| No TTS audio | Confirm that `mpg123` is installed with `which mpg123`; test audio with `echo "hi" \| espeak`. |
| Wake word does not respond | Lower the threshold in the web panel and check the recording volume. |
| Mouse cursor remains visible | Run `sudo apt install unclutter xdotool`, then restart BMO. Under Wayland, the Tk transparent-cursor fallback is used. |
| Autostart does not work | Toggle autostart again from the web dashboard. It writes systemd user, labwc autostart, and legacy desktop-autostart entries. |
| Cannot connect to the web panel | Check the port with `sudo ss -tlnp \| grep 8087` and inspect the firewall. |
| Many ALSA warnings | These are normal Raspberry Pi audio-driver warnings and usually do not affect operation. |

## 📚 Project Structure

```text
BMO-Online/
├── agent.py                # Main application: GUI, state machine, and pipeline
├── webui.py                # FastAPI web control panel
├── providers/              # API provider abstraction layer
│   ├── llm.py
│   ├── stt.py
│   ├── vision.py
│   ├── tts_edge.py
│   ├── tts_siliconflow.py
│   └── image_gen.py
├── static/                 # Web control panel frontend
│   ├── index.html
│   └── login.html
├── wakewords/              # Sherpa KWS and OpenWakeWord ONNX models
├── firmware/               # Pro Micro button firmware
├── faces/                  # BMO facial-animation PNG sequences
├── sounds/                 # WAV sound effects
├── generated/              # Images generated at runtime
├── logs/                   # Daily log files
├── config.json
├── .env.example
├── requirements.txt
├── setup_pi.sh
├── start_agent.sh
├── start_webui.sh
└── sync.sh
```

## 🙏 Credits

- Original project: [Be More Agent](https://github.com/brenpoly/be-more-agent) by **brenpoly**, which provided the inspiration and foundation for this project.
- [Sherpa-ONNX](https://github.com/k2-fsa/sherpa-onnx) — Chinese wake-word KWS.
- [OpenWakeWord](https://github.com/dscripka/openWakeWord) — English wake-word engine.
- [Edge-TTS](https://github.com/rany2/edge-tts) — Python wrapper for Microsoft TTS.
- BMO is © Cartoon Network. This project is a non-commercial hobby project.

## 📄 License

MIT
