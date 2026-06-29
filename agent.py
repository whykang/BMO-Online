# =========================================================================
#  Be More Agent (Online Edition) 🤖
#  在线版：树莓派 + 硅基流动 + Edge-TTS
#
#  基于 brenpoly/be-more-agent 改写（MIT License）
#  原版地址：https://github.com/brenpoly/be-more-agent
# =========================================================================

import os
import sys
import json
import time
import wave
import random
import re
import select
import threading
import queue
import traceback
import datetime
import warnings
import atexit
import subprocess
import shutil
import signal
import shlex
import glob
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk
from dotenv import load_dotenv

import sounddevice as sd
import numpy as np
import scipy.signal

# 加载 .env
load_dotenv()

warnings.filterwarnings("ignore", category=RuntimeWarning)

# --- Providers ---
from providers.llm import LLMProvider
from providers.stt import STTProvider, create_stt
from providers.vision import VisionProvider
from providers.image_gen import ImageGenProvider
from providers.search_bocha import BochaSearchProvider
from providers.tts_edge import EdgeTTSProvider
from providers.tts_siliconflow import SiliconFlowTTSProvider
from providers.wakeword_sherpa import SherpaWakeWord

# --- 唤醒词后端（都可选）---
try:
    from openwakeword.model import Model as OWWModel
    OWW_AVAILABLE = True
except Exception:
    OWW_AVAILABLE = False

# =========================================================================
# 配置 & 常量
# =========================================================================

CONFIG_FILE = "config.json"
MEMORY_FILE = "chat_memory.json"
STATE_FILE = "state.json"            # agent → webui 状态
COMMANDS_FILE = "commands.json"      # webui → agent 命令
BMO_IMAGE_FILE = "current_image.jpg"
CAPTURES_DIR = "captures"
LOG_DIR = "logs"
ROMS_DIR = "roms"
ROM_EXTS = (".nes", ".zip", ".fds", ".unf")
MEDIA_DIR = "media"
MUSIC_DIR = os.path.join(MEDIA_DIR, "music")
VIDEOS_DIR = os.path.join(MEDIA_DIR, "videos")
MUSIC_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus")
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v")

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(CAPTURES_DIR, exist_ok=True)
os.makedirs(ROMS_DIR, exist_ok=True)
os.makedirs(MUSIC_DIR, exist_ok=True)
os.makedirs(VIDEOS_DIR, exist_ok=True)


def log(msg: str):
    """同时打印到终端和写入日志文件。日志文件按当天日期命名，跨午夜自动滚动到新文件。"""
    now = datetime.datetime.now()
    line = f"[{now.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        log_file = os.path.join(LOG_DIR, f"{now.date().isoformat()}.log")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _atomic_write_json(path: str, data, indent=None):
    """原子写 JSON：先写同目录临时文件 + fsync，再 os.replace 覆盖。
    避免树莓派掉电 / 两个进程并发时读到半截或损坏的文件。"""
    import tempfile
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def load_config() -> dict:
    # config.json 不在 git 里；首次运行从 config.default.json 复制
    if not os.path.exists(CONFIG_FILE) and os.path.exists("config.default.json"):
        import shutil
        shutil.copy("config.default.json", CONFIG_FILE)
        print("[INIT] 已从 config.default.json 创建 config.json", flush=True)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    rec = cfg.setdefault("recording", {})
    # 旧版默认 1.0/12.0 容易把长句截断；已有配置若还是旧默认，自动迁移到更稳的值。
    if float(rec.get("silence_duration", 1.0)) <= 1.0:
        rec["silence_duration"] = 1.8
    if float(rec.get("max_record_seconds", 12.0)) <= 12.0:
        rec["max_record_seconds"] = 25.0
    return cfg


def save_config(cfg: dict):
    _atomic_write_json(CONFIG_FILE, cfg, indent=2)


# 工具调用说明：默认值在 prompts.py（agent/webui 共用），用户可在网页里覆盖。
from prompts import DEFAULT_TOOLS_PROMPT
TOOLS_PROMPT = DEFAULT_TOOLS_PROMPT


# =========================================================================
# 状态机
# =========================================================================
class BotStates:
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ERROR = "error"
    CAPTURING = "capturing"
    WARMUP = "warmup"
    WAITING_FACE = "waiting"


# =========================================================================
# 音频工具
# =========================================================================

def _auto_pick_input(devices):
    """自动挑一个像样的麦输入设备：先按麦克风相关关键词打分，分相同再比输入通道数；
    避开坏掉的 ALSA 默认（capture slave 未定义那种）。
    多个 USB 设备时（如 USB 扬声器 + USB 麦），偏向'更像麦克风/带录音'的那个：
    'usb audio'/'audio device'/'usb pnp' 等麦克风常见名权重高于裸 'usb'。"""
    prefer = ("dji", "mic mini", "microphone", "usb audio", "audio device",
              "usb pnp", "pnp", "mic", "usb", "webcam", "camera")
    ranked = []
    for idx, dev in enumerate(devices):
        ch = int(dev.get("max_input_channels", 0) or 0)
        if ch <= 0:
            continue
        name = dev.get("name", "").lower()
        score = sum((len(prefer) - pos) for pos, token in enumerate(prefer) if token in name)
        if score:
            ranked.append((score, ch, idx))
    if ranked:
        # 关键词分优先；分相同时输入通道多的赢（真麦克风通常 >= 扬声器附带的采集端）
        return max(ranked, key=lambda item: (item[0], item[1]))[2]
    # 兜底：第一个有输入通道、且不是 default/sysdefault 的设备
    for idx, dev in enumerate(devices):
        name = dev.get("name", "").lower()
        if dev.get("max_input_channels", 0) > 0 and "default" not in name:
            return idx
    return None


def resolve_input_device(requested):
    """requested 可为：None/""/"auto"（自动检测）、序号、或设备名子串。匹配不到也回落到自动。"""
    try:
        devices = sd.query_devices()
    except Exception as e:
        log(f"[AUDIO] 设备查询失败: {e}")
        return None
    # 显式序号
    if isinstance(requested, int) or (isinstance(requested, str) and str(requested).strip().isdigit()):
        idx = int(requested)
        if 0 <= idx < len(devices) and devices[idx].get("max_input_channels", 0) > 0:
            return idx
        return _auto_pick_input(devices)
    # 显式名字（非空、非 auto/default）→ 模糊匹配
    req = str(requested).strip().lower() if requested is not None else ""
    if req and req not in ("auto", "default"):
        for idx, dev in enumerate(devices):
            if dev.get("max_input_channels", 0) > 0 and req in dev.get("name", "").lower():
                return idx
    # auto / 空 / 匹配不到 → 自动检测
    return _auto_pick_input(devices)


def choose_input_settings(device, preferred=None) -> tuple[int, int]:
    """协商设备实际支持的采样率和声道数；部分 USB 无线麦只接受双声道。"""
    candidates = []
    if preferred:
        candidates.append(preferred)
    info = {}
    try:
        info = sd.query_devices(device)
        if "default_samplerate" in info:
            candidates.append(int(info["default_samplerate"]))
    except Exception:
        pass
    candidates.extend([48000, 44100, 32000, 16000])
    max_channels = 1
    max_channels = max(1, int(info.get("max_input_channels", 1) or 1))
    channel_candidates = [1]
    if max_channels >= 2:
        channel_candidates.append(2)

    seen = set()
    for rate in candidates:
        if not rate or rate in seen:
            continue
        seen.add(rate)
        for channels in channel_candidates:
            try:
                sd.check_input_settings(
                    device=device, samplerate=rate, channels=channels, dtype="int16",
                )
                return int(rate), channels
            except Exception:
                continue
    fallback_rate = int(info.get("default_samplerate", 0) or 0)
    if not fallback_rate:
        fallback_rate = int(candidates[0]) if candidates else 44100
    fallback_channels = 2 if max_channels >= 2 else 1
    log(f"[AUDIO] 无法预检输入参数，尝试设备默认值: "
        f"sr={fallback_rate} channels={fallback_channels}")
    return fallback_rate, fallback_channels


def choose_input_samplerate(device, preferred=None) -> int:
    """兼容旧调用方；新录音流应使用 choose_input_settings。"""
    return choose_input_settings(device, preferred)[0]


def select_active_input_channel(data):
    """将多声道麦克风转为单声道，保留当前实际有声音的轨道。"""
    audio = np.asarray(data)
    if audio.ndim <= 1:
        return audio.flatten()
    if audio.shape[1] == 1:
        return audio[:, 0]
    work = audio.astype(np.float32)
    energy = np.mean(work * work, axis=0)
    return audio[:, int(np.argmax(energy))]


# =========================================================================
# 主 GUI 类
# =========================================================================
class BotGUI:
    BG_WIDTH, BG_HEIGHT = 800, 480
    OVERLAY_WIDTH, OVERLAY_HEIGHT = 400, 300

    def __init__(self, master):
        self.master = master
        master.title("Be More Agent (Online)")
        self._cursor_hider_proc = None
        self._cursor_should_hide = True
        self._blank_cursor = self._create_blank_cursor()
        try:
            master.attributes('-fullscreen', True)
        except Exception:
            pass
        self._exit_btn_timer = None
        master.bind('<Escape>', self.exit_fullscreen)
        # 回车：短按=唤醒，长按(≥0.6s)=回到待唤醒。靠 press/release 计时区分。
        master.bind('<KeyPress-Return>', self.handle_return_press)
        master.bind('<KeyRelease-Return>', self.handle_return_release)
        master.bind('<space>', self.handle_speaking_interrupt)
        self._return_pressed_at = None
        self._return_release_timer = None
        atexit.register(self.safe_exit)

        self.config = load_config()
        self.input_device = resolve_input_device(self.config.get("input_device"))
        self._log_input_device()

        # 状态
        self.current_state = BotStates.WARMUP
        self.current_status = "启动中..."
        self.animations = {}
        self.current_frame_index = 0
        self.current_animation_key = None
        self.current_overlay_image = None
        self.exiting = False
        self.wait_wake_started_at = None
        self.waiting_face_active = False

        # 记忆
        self.permanent_memory = self.load_chat_history()
        self.session_memory = []

        # 同步原语
        self.last_ptt_time = 0
        self.ptt_event = threading.Event()
        self._wake_stream = None   # 唤醒监听的输入流；按钮按下时 abort 它，强制立刻恢复
        self.recording_active = threading.Event()
        self.interrupted = threading.Event()
        self.abort_to_wake = threading.Event()   # 长按回车：中止当前对话，回到待唤醒
        self.rewake_event = threading.Event()    # 说话/等待开口时再次听到唤醒词

        # TTS 队列
        self.tts_queue = []
        self.tts_queue_lock = threading.Lock()
        self.tts_active = threading.Event()
        self.current_tts_proc = None
        self.thinking_cue_lock = threading.Lock()
        self.thinking_cue_token = 0

        # Providers
        endpoints = self.config["providers_endpoints"]
        self.llm = LLMProvider(self.config["llm"], endpoints)
        self.stt = create_stt(self.config["stt"], endpoints)
        self.vision = VisionProvider(self.config["vision"], endpoints)
        self.image_gen = ImageGenProvider(
            self.config["image_gen"], endpoints,
            output_dir=self.config.get("generated_dir", "generated"),
        )
        self.search = BochaSearchProvider(self.config.get("search", {}))
        self.tts_edge = EdgeTTSProvider(self.config["tts"])
        self.tts_sf = SiliconFlowTTSProvider(
            self.config["tts"], endpoints,
            sample_rate=self.config.get("tts_sample_rate", 24000),
        )
        self.tts_piper = None   # 本地 Piper TTS，懒加载（用到才加载模型）
        self.tts_doubao = None  # 豆包/火山引擎 TTS，懒加载（用到才校验凭证）
        self.printer = None     # 热敏打印机，懒加载（用到才开串口）
        self.last_image_for_print = None  # 最近生成的图片路径（供"打印刚画的图"用）
        self.pending_print = None          # 刚画完图、正等用户回答"要不要打印"
        self._suppress_action_memory = False
        self._action_sequence_spoken = []
        self._resolved_output = None   # 自动检测的音响输出设备缓存
        self.game_proc = None
        self.game_rom = None
        self.game_paused = False
        self.game_lock = threading.Lock()
        self.media_proc = None
        self.media_kind = None
        self.media_name = None
        self.media_paused_for_bmo = False
        self.media_lock = threading.Lock()
        self.hidden = False              # 隐身：窗口最小化但程序继续运行
        self.screen_shot = "/tmp/bmo_screen.png"   # 识别屏幕的截图路径

        # 唤醒词
        self.oww_model = None
        self.wake_reload_pending = False
        self._load_wake_word()

        # GUI 组件
        self.background_label = tk.Label(master)
        self.background_label.place(x=0, y=0, width=self.BG_WIDTH, height=self.BG_HEIGHT)
        # 单击不再弹文字框；双击屏幕才显示/隐藏退出按钮
        self.background_label.bind('<Double-Button-1>', self.toggle_exit_button)

        self.overlay_label = tk.Label(master, bg='black')
        self.overlay_label.bind('<Double-Button-1>', self.toggle_exit_button)

        self.response_text = tk.Text(master, height=6, width=60, wrap=tk.WORD,
                                     state=tk.DISABLED, bg="#ffffff", fg="#000000",
                                     font=('Arial', 12))

        self.status_var = tk.StringVar(value=self.current_status)
        self.status_label = ttk.Label(master, textvariable=self.status_var,
                                      background="#2e2e2e", foreground="white")
        self.exit_button = ttk.Button(master, text="退出", command=self.safe_exit)
        self._set_cursor_visible(False)

        self.load_animations()
        self.update_animation()
        self._safe_after(500, self._force_hide_cursor)
        # 默认只显示脸；show_hud=true 才显示状态栏+识别文字
        if self.config.get("show_hud", False):
            self.response_text.place(relx=0.5, rely=0.82, anchor=tk.S)
            self.status_label.place(relx=0.5, rely=1.0, anchor=tk.S, relwidth=1)
        self.poll_commands_file()  # 启动后台 webui 命令轮询
        self.update_state_file()   # 启动状态写入

        threading.Thread(target=self.safe_main_execution, daemon=True).start()

    # -------------------------------------------------------------------
    # 唤醒词（双后端：openwakeword | sherpa_onnx）
    # -------------------------------------------------------------------
    def _load_wake_word(self):
        """根据 config.wake_word.backend 加载对应后端。"""
        self.oww_model = None
        self.sherpa_kws = None
        self.wake_backend = None

        ww_cfg = self.config.get("wake_word", {})
        if not ww_cfg.get("enabled"):
            log("[INIT] 唤醒词未启用（纯 PTT 模式）")
            return

        backend = ww_cfg.get("backend", "sherpa_onnx")

        if backend == "sherpa_onnx":
            model_dir = ww_cfg.get("model_dir", "wakewords/sherpa-kws-zh")
            keywords = ww_cfg.get("keywords", [])
            threshold = float(ww_cfg.get("threshold", 0.25))
            score = float(ww_cfg.get("score", 1.5))
            if not SherpaWakeWord.is_available():
                log("[INIT] sherpa-onnx 未安装，唤醒词关闭")
                return
            try:
                self.sherpa_kws = SherpaWakeWord(
                    model_dir=model_dir, keywords=keywords,
                    threshold=threshold, score=score,
                )
                self.wake_backend = "sherpa_onnx"
                log(f"[INIT] Sherpa-KWS 已加载，生效关键词: {self.sherpa_kws.accepted}")
                if self.sherpa_kws.rejected:
                    log(f"[INIT] ⚠️ 这些关键词无法识别已跳过: {self.sherpa_kws.rejected}")
            except Exception as e:
                log(f"[INIT] Sherpa-KWS 加载失败，回落 PTT 模式: {e}")
                self.sherpa_kws = None
                self.wake_backend = None

        elif backend == "openwakeword":
            if not OWW_AVAILABLE:
                log("[INIT] openwakeword 未安装，唤醒词关闭")
                return
            model_path = ww_cfg.get("model", "")
            if not model_path or not os.path.exists(model_path):
                log(f"[INIT] 唤醒词模型不存在: {model_path}")
                return
            try:
                self.oww_model = OWWModel(wakeword_models=[model_path])
            except TypeError:
                try:
                    self.oww_model = OWWModel(wakeword_model_paths=[model_path])
                except Exception as e:
                    log(f"[INIT] 唤醒词加载失败: {e}")
                    return
            except Exception as e:
                log(f"[INIT] 唤醒词加载失败: {e}")
                return
            self.wake_backend = "openwakeword"
            log(f"[INIT] OpenWakeWord 已加载: {os.path.basename(model_path)}")
        else:
            log(f"[INIT] 未知唤醒词后端: {backend}")

    def _reload_wake_word_if_needed(self):
        if not self.wake_reload_pending:
            return False
        self.wake_reload_pending = False
        log("[CMD] 重载唤醒词后端...")
        try:
            self._load_wake_word()
        except Exception as e:
            log(f"[CMD] 唤醒词重载失败: {e}")
        return True

    # -------------------------------------------------------------------
    # 退出 & UI 工具
    # -------------------------------------------------------------------
    def safe_exit(self):
        if self.exiting:
            return
        self.exiting = True
        log("--- 退出 ---")
        try:
            if self.current_tts_proc:
                self.current_tts_proc.terminate()
        except Exception:
            pass
        try:
            if self._cursor_hider_proc and self._cursor_hider_proc.poll() is None:
                self._cursor_hider_proc.terminate()
        except Exception:
            pass
        try:
            self.stop_game(restore_bmo=False)
        except Exception:
            pass
        try:
            self.stop_media(restore_bmo=False)
        except Exception:
            pass
        self.recording_active.clear()
        self.tts_active.clear()
        self.save_chat_history()
        try:
            sd.stop()
        except Exception:
            pass
        try:
            self.master.quit()
        except Exception:
            pass

    def exit_fullscreen(self, event=None):
        try:
            self.master.attributes('-fullscreen', False)
        except Exception:
            pass
        self.safe_exit()

    def _create_blank_cursor(self):
        """Tk 的 cursor=none 在部分 Pi 桌面环境不生效，XBM 空光标更稳。"""
        try:
            cursor_dir = os.path.join("/tmp", "bmo_cursor")
            os.makedirs(cursor_dir, exist_ok=True)
            cursor_path = os.path.join(cursor_dir, "blank.xbm")
            mask_path = os.path.join(cursor_dir, "blank_mask.xbm")
            xbm = (
                "#define blank_width 16\n"
                "#define blank_height 16\n"
                "#define blank_x_hot 0\n"
                "#define blank_y_hot 0\n"
                "static unsigned char blank_bits[] = {\n"
                "  0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,\n"
                "  0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,\n"
                "  0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,\n"
                "  0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00};\n"
            )
            for path in (cursor_path, mask_path):
                with open(path, "w", encoding="ascii") as f:
                    f.write(xbm)
            return f"@{cursor_path} {mask_path} black white"
        except Exception as e:
            log(f"[CURSOR] 创建透明光标失败: {e}")
            return "none"

    def _start_system_cursor_hider(self):
        """系统级隐藏鼠标；没有安装也不影响 Tk 透明光标兜底。"""
        if self._cursor_hider_proc and self._cursor_hider_proc.poll() is None:
            return
        cmd = None
        if shutil.which("unclutter"):
            cmd = ["unclutter", "-idle", "0.1", "-root"]
        elif shutil.which("unclutter-xfixes"):
            cmd = ["unclutter-xfixes", "--timeout", "0", "--hide-on-touch"]
        if not cmd:
            log("[CURSOR] 未找到 unclutter，使用 Tk 透明光标")
            return
        try:
            self._cursor_hider_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log(f"[CURSOR] 已启动系统级隐藏鼠标: {cmd[0]}")
        except Exception as e:
            log(f"[CURSOR] 启动 {cmd[0]} 失败: {e}")

    def _stop_system_cursor_hider(self):
        try:
            if self._cursor_hider_proc and self._cursor_hider_proc.poll() is None:
                self._cursor_hider_proc.terminate()
        except Exception:
            pass
        self._cursor_hider_proc = None

    def _move_pointer_away(self):
        try:
            self.master.event_generate(
                "<Motion>",
                warp=True,
                x=self.BG_WIDTH - 2,
                y=self.BG_HEIGHT - 2,
            )
        except Exception:
            pass
        xdotool = shutil.which("xdotool")
        if xdotool:
            try:
                subprocess.Popen(
                    [xdotool, "mousemove", str(self.BG_WIDTH - 2), str(self.BG_HEIGHT - 2)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

    def _force_hide_cursor(self):
        if self.exiting or not self._cursor_should_hide:
            return
        self._set_cursor_visible(False)
        self._safe_after(1000, self._force_hide_cursor)

    def _set_cursor_visible(self, visible: bool):
        self._cursor_should_hide = not visible
        cursor = "" if visible else self._blank_cursor

        def apply(widget):
            nonlocal cursor
            try:
                widget.config(cursor=cursor)
            except tk.TclError:
                if not visible:
                    if cursor != "none":
                        log("[CURSOR] 透明 XBM 光标不可用，回退 cursor=none")
                        self._blank_cursor = "none"
                        cursor = "none"
                    try:
                        widget.config(cursor="none")
                    except tk.TclError:
                        pass
            for child in widget.winfo_children():
                apply(child)

        apply(self.master)
        if not visible:
            self._move_pointer_away()
            self._start_system_cursor_hider()
        else:
            self._stop_system_cursor_hider()

    def toggle_exit_button(self, event=None):
        """双击屏幕 → 显示退出按钮（恢复鼠标），6 秒后自动隐藏。"""
        try:
            if self.exit_button.winfo_ismapped():
                self._hide_exit_button()
            else:
                self.exit_button.place(x=10, y=10)
                self._set_cursor_visible(True)   # 恢复鼠标好点按钮
                if self._exit_btn_timer:
                    self.master.after_cancel(self._exit_btn_timer)
                self._exit_btn_timer = self.master.after(6000, self._hide_exit_button)
        except tk.TclError:
            pass

    def _hide_exit_button(self):
        try:
            self.exit_button.place_forget()
            self._set_cursor_visible(False)
            self._safe_after(1000, self._force_hide_cursor)
            self._exit_btn_timer = None
        except tk.TclError:
            pass

    # -------------------------------------------------------------------
    # 游戏控制（FCEUX）
    # -------------------------------------------------------------------
    def _game_cfg(self):
        return self.config.get("game", {})

    def _game_rom_dir(self):
        return os.path.abspath(self._game_cfg().get("rom_dir") or ROMS_DIR)

    def _list_rom_files(self):
        rom_dir = self._game_rom_dir()
        if not os.path.isdir(rom_dir):
            return []
        return sorted(
            f for f in os.listdir(rom_dir)
            if f.lower().endswith(ROM_EXTS) and "/" not in f
        )

    def _norm_game_name(self, name: str) -> str:
        base = os.path.splitext(name or "")[0].lower()
        return re.sub(r"[\s_\-()\[\]【】（）]+", "", base)

    def _speakable_game_name(self, filename: str) -> str:
        """念给用户听的游戏名：去掉后缀、去掉括号里的区域/版本标记、把 _-—. 换成空格。"""
        name = os.path.splitext(filename or "")[0]
        name = re.sub(r"[\(\[（【].*?[\)\]）】]", "", name)      # 去掉 (JP)[!]【…】这类
        name = re.sub(r"[_\-—–·.]+", " ", name)                # 分隔符 → 空格
        name = re.sub(r"\s+", " ", name).strip()
        return name or os.path.splitext(filename or "")[0]

    def _find_rom(self, query=None):
        roms = self._list_rom_files()
        if not roms:
            return None, "还没有 ROM。先在后台“游戏”页上传一个 .nes 文件吧。"
        q = (query or "").strip()
        if not q:
            if len(roms) == 1:
                return os.path.join(self._game_rom_dir(), roms[0]), ""
            return None, "你想打开哪个游戏呀？现在有：" + "、".join(self._speakable_game_name(r) for r in roms[:8])
        q_norm = self._norm_game_name(q)
        for name in roms:
            if q == name or q.lower() == name.lower():
                return os.path.join(self._game_rom_dir(), name), ""
        for name in roms:
            n_norm = self._norm_game_name(name)
            if q_norm and (q_norm in n_norm or n_norm in q_norm):
                return os.path.join(self._game_rom_dir(), name), ""
        return None, f"没找到“{q}”这个游戏。现在有：" + "、".join(self._speakable_game_name(r) for r in roms[:8])

    def _split_game_args(self, value):
        if not value:
            return []
        if isinstance(value, list):
            return [str(x) for x in value if str(x).strip()]
        if isinstance(value, str):
            try:
                return shlex.split(value)
            except ValueError:
                return value.split()
        return []

    def _game_emulator(self):
        emulator = self._game_cfg().get("emulator", "fceux")
        if os.path.sep in emulator:
            return emulator if os.path.exists(emulator) else ""
        return shutil.which(emulator) or ""

    def _build_game_command(self, rom_path):
        cfg = self._game_cfg()
        emulator = self._game_emulator()
        if not emulator:
            return None, "没找到 FCEUX。请先安装 fceux，或在 config.json 的 game.emulator 里写完整路径。"
        args = []
        if cfg.get("fullscreen", True):
            args.extend(self._split_game_args(cfg.get("fullscreen_args", ["--fullscreen", "1"])))
        args.extend(self._split_game_args(cfg.get("extra_args", [])))
        if any("{rom}" in arg for arg in args):
            args = [arg.replace("{rom}", rom_path) for arg in args]
            return [emulator, *args], ""
        return [emulator, *args, rom_path], ""

    def _game_env(self):
        """固定 FCEUX 的用户配置目录，避免从 autostart/nohup 启动时读到另一套空映射。"""
        env = os.environ.copy()
        configured_home = (self._game_cfg().get("home") or "").strip()
        configured_config_dir = (self._game_cfg().get("config_dir") or "").strip()
        sudo_user = os.getenv("SUDO_USER", "")
        if configured_home:
            home = configured_home
        elif hasattr(os, "geteuid") and os.geteuid() == 0 and sudo_user and sudo_user != "root":
            home = os.path.expanduser(f"~{sudo_user}")
        else:
            home = os.path.expanduser("~")
        if home:
            home = os.path.abspath(os.path.expanduser(home))
            env["HOME"] = home
            env.setdefault("USER", os.path.basename(home))
            env.setdefault("LOGNAME", os.path.basename(home))
            env.setdefault("XDG_CONFIG_HOME", os.path.join(home, ".config"))
            env.setdefault("XDG_DATA_HOME", os.path.join(home, ".local", "share"))
            env.setdefault("XDG_CACHE_HOME", os.path.join(home, ".cache"))
            config_dir = configured_config_dir or os.path.join(home, ".fceux")
            config_dir = os.path.abspath(os.path.expanduser(config_dir))
            env["FCEUX_CONFIG_DIR"] = config_dir
            self._ensure_fceux_input_profiles(config_dir, home)
        return env

    def _candidate_fceux_config_dirs(self, home, target):
        candidates = [
            os.getenv("FCEUX_CONFIG_DIR", ""),
            target,
            os.path.join(home, ".fceux"),
            os.path.join(home, ".config", "fceux"),
            os.path.join(home, ".config", "FCEUX"),
            os.path.join(home, ".local", "share", "fceux"),
            os.path.join(home, ".local", "share", "FCEUX"),
            os.path.join(home, ".var", "app", "org.fceux.FCEUX", "config", "fceux"),
            os.path.join(home, ".var", "app", "org.fceux.FCEUX", "data", "fceux"),
            os.path.join(home, ".var", "app", "net.sourceforge.fceux", "config", "fceux"),
            os.path.join(home, ".var", "app", "net.sourceforge.fceux", "data", "fceux"),
        ]
        seen = set()
        out = []
        for path in candidates:
            if not path:
                continue
            path = os.path.abspath(os.path.expanduser(path))
            if path in seen:
                continue
            seen.add(path)
            out.append(path)
        return out

    def _keyboard_profile_files(self, config_dir):
        return glob.glob(os.path.join(config_dir, "input", "keyboard", "*.txt"))

    def _keyboard_profile_is_valid(self, path):
        """FCEUX profile 存在但可能全空；至少主配置要有几个核心按键才算有效。"""
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except Exception:
            return False
        if "config:0" not in text:
            return False
        required = ("a:", "b:", "back:", "start:", "dpup:", "dpdown:", "dpleft:", "dpright:")
        for line in text.splitlines():
            if "config:0" not in line:
                continue
            if not all(k in line for k in required):
                continue
            values = {}
            for part in line.split(","):
                if ":" not in part:
                    continue
                key, value = part.split(":", 1)
                values[key.strip()] = value.strip()
            return all(values.get(k[:-1]) for k in required)
        return False

    def _write_fceux_default_keyboard_profile(self, default_path):
        os.makedirs(os.path.dirname(default_path), exist_ok=True)
        default_map = (
            "keyboard,default,config:0,a:kF,b:kD,back:kS,start:kReturn,"
            "dpup:kUp,dpdown:kDown,dpleft:kLeft,dpright:kRight,turboA:,turboB:,\n"
            "keyboard,default,config:1,a:,b:,back:,start:,dpup:,dpdown:,dpleft:,dpright:,turboA:,turboB:,\n"
            "keyboard,default,config:2,a:,b:,back:,start:,dpup:,dpdown:,dpleft:,dpright:,turboA:,turboB:,\n"
            "keyboard,default,config:3,a:,b:,back:,start:,dpup:,dpdown:,dpleft:,dpright:,turboA:,turboB:,\n"
        )
        with open(default_path, "w", encoding="utf-8") as f:
            f.write(default_map)

    def _backup_file(self, path):
        if not os.path.exists(path):
            return
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(path, f"{path}.bak-{stamp}")
        except Exception:
            pass

    def _write_keymap_profile(self, default_path, km):
        """按 config.game.keymap 写 FCEUX 主键盘 profile（config:0）。键值是 FCEUX token，
        如 kSpace/kShift/kReturn/kUp。其它三套(config:1/2/3)留空。"""
        def g(k):
            return (km.get(k) or "").strip()
        line0 = ("keyboard,default,config:0,"
                 f"a:{g('a')},b:{g('b')},back:{g('select')},start:{g('start')},"
                 f"dpup:{g('up')},dpdown:{g('down')},dpleft:{g('left')},dpright:{g('right')},"
                 "turboA:,turboB:,\n")
        empty = "keyboard,default,config:{n},a:,b:,back:,start:,dpup:,dpdown:,dpleft:,dpright:,turboA:,turboB:,\n"
        os.makedirs(os.path.dirname(default_path), exist_ok=True)
        self._backup_file(default_path)
        with open(default_path, "w", encoding="utf-8") as f:
            f.write(line0)
            for n in (1, 2, 3):
                f.write(empty.format(n=n))
        log(f"[GAME] 已按后台键位写入 FCEUX: {default_path}")

    def _ensure_fceux_input_profiles(self, target_dir, home):
        """FCEUX 只看 FCEUX_CONFIG_DIR/input/keyboard/*.txt；缺失时尝试从旧位置迁移。"""
        os.makedirs(target_dir, exist_ok=True)
        default_path = os.path.join(target_dir, "input", "keyboard", "default.txt")
        # 后台设了键位 → 以它为准，每次开游戏都按它重写
        km = self._game_cfg().get("keymap")
        if km and any((km.get(k) or "").strip() for k in
                      ("a", "b", "select", "start", "up", "down", "left", "right")):
            try:
                self._write_keymap_profile(default_path, km)
                return
            except Exception as e:
                log(f"[GAME] 写入后台键位失败，回退默认: {e}")
        if self._keyboard_profile_is_valid(default_path):
            return
        if os.path.exists(default_path):
            log(f"[GAME] FCEUX default.txt 存在但无有效主按键，准备修复: {default_path}")
        for src_dir in self._candidate_fceux_config_dirs(home, target_dir):
            if src_dir == target_dir:
                continue
            src_profiles = [p for p in self._keyboard_profile_files(src_dir)
                            if self._keyboard_profile_is_valid(p)]
            if not src_profiles:
                continue
            src_input = os.path.join(src_dir, "input")
            dst_input = os.path.join(target_dir, "input")
            try:
                self._backup_file(default_path)
                shutil.copytree(src_input, dst_input, dirs_exist_ok=True)
                cfg_src = os.path.join(src_dir, "fceux.cfg")
                cfg_dst = os.path.join(target_dir, "fceux.cfg")
                if os.path.exists(cfg_src) and not os.path.exists(cfg_dst):
                    shutil.copy2(cfg_src, cfg_dst)
                log(f"[GAME] 已迁移 FCEUX 输入映射: {src_dir} -> {target_dir}")
                if self._keyboard_profile_is_valid(default_path):
                    return
                shutil.copy2(src_profiles[0], default_path)
                log(f"[GAME] 已将有效 profile 设为 default: {src_profiles[0]}")
                if self._keyboard_profile_is_valid(default_path):
                    return
            except Exception as e:
                log(f"[GAME] 迁移 FCEUX 输入映射失败: {e}")
        try:
            self._backup_file(default_path)
            self._write_fceux_default_keyboard_profile(default_path)
            log(f"[GAME] 已重建 FCEUX 默认键盘映射: {default_path}")
        except Exception as e:
            log(f"[GAME] 创建 FCEUX 默认键盘映射失败: {e}")

    def _log_game_config_paths(self, env):
        home = env.get("HOME", "")
        config_dir = env.get("FCEUX_CONFIG_DIR", "")
        existing = [p for p in self._candidate_fceux_config_dirs(home, config_dir) if os.path.exists(p)]
        profiles = self._keyboard_profile_files(config_dir) if config_dir else []
        log(f"[GAME] FCEUX HOME={home} FCEUX_CONFIG_DIR={config_dir}")
        log("[GAME] FCEUX 候选配置目录: " + ("; ".join(existing) if existing else "未发现"))
        log("[GAME] FCEUX 键盘映射: " + ("; ".join(profiles) if profiles else "未发现 input/keyboard/*.txt"))

    def _game_is_running(self):
        proc = self.game_proc
        return bool(proc and proc.poll() is None)

    def _signal_game(self, sig):
        proc = self.game_proc
        if not proc or proc.poll() is not None:
            return False
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except Exception:
            try:
                proc.send_signal(sig)
            except Exception:
                return False
        return True

    def _enter_game_display_mode(self):
        def _update():
            try:
                self.exit_button.place_forget()
            except Exception:
                pass
            try:
                self.master.attributes('-fullscreen', False)
            except Exception:
                pass
            try:
                if self._game_cfg().get("hide_bmo_while_playing", True):
                    self.master.withdraw()
                else:
                    self.master.lower()
            except Exception:
                pass
            self._set_cursor_visible(False)
        self._safe_after(0, _update)

    def _restore_bmo_display_mode(self):
        if self.hidden:
            return   # 隐身中：不恢复显示，保持最小化直到“取消隐身”
        def _update():
            try:
                self.master.deiconify()
                self.master.attributes('-fullscreen', True)
                self.master.lift()
                self.master.focus_force()
            except Exception:
                pass
            self._set_cursor_visible(False)
        self._safe_after(0, _update)

    def _keep_game_pause_display(self):
        """游戏被 BMO 唤醒暂停时，不把 BMO 全屏盖上来，保留模拟器暂停画面。"""
        def _update():
            try:
                self.exit_button.place_forget()
            except Exception:
                pass
            try:
                self.master.attributes('-fullscreen', False)
            except Exception:
                pass
            try:
                if self._game_cfg().get("hide_bmo_while_playing", True):
                    self.master.withdraw()
                else:
                    self.master.lower()
            except Exception:
                pass
            self._set_cursor_visible(False)
        self._safe_after(0, _update)

    def _activate_game_window_later(self):
        xdotool = shutil.which("xdotool")
        proc = self.game_proc
        if not xdotool or not proc:
            return

        def _xdotool_search(*args):
            try:
                out = subprocess.check_output(
                    [xdotool, "search", *args],
                    text=True, stderr=subprocess.DEVNULL, timeout=2,
                ).strip().splitlines()
                return [w for w in out if w.strip()]
            except Exception:
                return []

        def _find_windows():
            windows = []
            seen = set()
            searches = [
                ("--pid", str(proc.pid)),
                ("--onlyvisible", "--class", "fceux"),
                ("--onlyvisible", "--class", "FCEUX"),
                ("--onlyvisible", "--name", "fceux"),
                ("--onlyvisible", "--name", "FCEUX"),
            ]
            for args in searches:
                for win in _xdotool_search(*args):
                    if win not in seen:
                        seen.add(win)
                        windows.append(win)
            return windows

        def _activate(win):
            for cmd in (
                [xdotool, "windowraise", win],
                [xdotool, "windowactivate", "--sync", win],
                [xdotool, "windowfocus", win],
            ):
                try:
                    subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=2)
                except Exception:
                    pass

        def _focus():
            cfg = self._game_cfg()
            time.sleep(float(cfg.get("focus_delay_seconds", 1.2)))
            attempts = int(cfg.get("focus_attempts", 10))
            interval = float(cfg.get("focus_retry_seconds", 0.5))
            for _ in range(max(1, attempts)):
                if not self._game_is_running() or self.game_paused:
                    return
                wins = _find_windows()
                if wins:
                    _activate(wins[-1])
                    log(f"[GAME] 已切换键盘焦点到 FCEUX 窗口: {wins[-1]}")
                    return
                time.sleep(interval)
            log("[GAME] 未找到 FCEUX 窗口，键盘焦点可能没切过去")

        threading.Thread(target=_focus, daemon=True).start()

    def _monitor_game_proc(self, proc, rom_name):
        rc = proc.wait()
        with self.game_lock:
            if self.game_proc is not proc:
                return
            self.game_proc = None
            self.game_rom = None
            self.game_paused = False
        log(f"[GAME] 已退出: {rom_name} rc={rc}")
        self._restore_bmo_display_mode()
        self.set_state(BotStates.IDLE, "游戏已退出")

    def list_games_text(self):
        roms = self._list_rom_files()
        if not roms:
            return "还没有游戏 ROM。"
        return "现在有：" + "、".join(self._speakable_game_name(r) for r in roms[:12])

    def start_game(self, query=None):
        if not self._game_cfg().get("enabled", True):
            return False, "游戏功能现在是关闭的。"
        rom_path, err = self._find_rom(query)
        if err:
            return False, err
        cmd, err = self._build_game_command(rom_path)
        if err:
            return False, err
        if self._game_is_running():
            self.stop_game(restore_bmo=False)
        rom_name = os.path.basename(rom_path)
        try:
            log(f"[GAME] 启动: {' '.join(cmd)}")
            env = self._game_env()
            self._log_game_config_paths(env)
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                cwd=env.get("HOME") or None,
                start_new_session=True,
            )
        except Exception as e:
            return False, f"游戏启动失败：{e}"
        with self.game_lock:
            self.game_proc = proc
            self.game_rom = rom_name
            self.game_paused = False
        self._enter_game_display_mode()
        self._activate_game_window_later()
        threading.Thread(target=self._monitor_game_proc, args=(proc, rom_name), daemon=True).start()
        self.abort_to_wake.set()
        return True, f"开始游戏：{rom_name}"

    def pause_game(self, for_bmo=False, show_bmo=False):
        if not self._game_is_running():
            return False, "现在没有正在运行的游戏。"
        if self.game_paused:
            if show_bmo:
                self._restore_bmo_display_mode()
            elif for_bmo:
                self._keep_game_pause_display()
            return True, "游戏已经暂停了。"
        ok = self._signal_game(signal.SIGSTOP)
        if not ok:
            return False, "暂停游戏失败。"
        self.game_paused = True
        log(f"[GAME] 已暂停: {self.game_rom}")
        if show_bmo:
            self._restore_bmo_display_mode()
        else:
            self._keep_game_pause_display()
        return True, "游戏已暂停。"

    def resume_game(self, auto=False):
        """auto=True：回到待唤醒时自动继续（已经在等唤醒了，不要再 set abort_to_wake，
        否则会吞掉下一次唤醒）。auto=False：语音“继续游戏”用，需打断当前对话回到游戏。"""
        if not self._game_is_running():
            return False, "现在没有正在运行的游戏。"
        if not self.game_paused:
            self._enter_game_display_mode()
            self._activate_game_window_later()
            if not auto:
                self.abort_to_wake.set()
            return True, "游戏已经在运行。"
        ok = self._signal_game(signal.SIGCONT)
        if not ok:
            return False, "继续游戏失败。"
        self.game_paused = False
        log(f"[GAME] 继续: {self.game_rom}" + ("（自动）" if auto else ""))
        self._enter_game_display_mode()
        self._activate_game_window_later()
        if not auto:
            self.abort_to_wake.set()
        return True, "继续游戏。"

    def stop_game(self, restore_bmo=True):
        proc = self.game_proc
        if not proc or proc.poll() is not None:
            self.game_proc = None
            self.game_rom = None
            self.game_paused = False
            if restore_bmo:
                self._restore_bmo_display_mode()
            return False, "现在没有正在运行的游戏。"
        rom_name = self.game_rom or "游戏"
        try:
            if self.game_paused:
                self._signal_game(signal.SIGCONT)
                time.sleep(0.1)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
        except Exception as e:
            log(f"[GAME] 退出失败: {e}")
            return False, "退出游戏失败。"
        finally:
            with self.game_lock:
                if self.game_proc is proc:
                    self.game_proc = None
                    self.game_rom = None
                    self.game_paused = False
            if restore_bmo:
                self._restore_bmo_display_mode()
        log(f"[GAME] 已关闭: {rom_name}")
        return True, f"已退出 {rom_name}。"

    def _game_state(self):
        if not self._game_is_running():
            return {"running": False, "paused": False, "rom": None}
        return {
            "running": True,
            "paused": bool(self.game_paused),
            "rom": self.game_rom,
        }

    # -------------------------------------------------------------------
    # 媒体播放（音乐 / 视频）
    # -------------------------------------------------------------------
    def _media_cfg(self):
        return self.config.get("media", {})

    def _media_dir(self, kind):
        cfg = self._media_cfg()
        if kind == "music":
            return os.path.abspath(cfg.get("music_dir") or MUSIC_DIR)
        return os.path.abspath(cfg.get("video_dir") or VIDEOS_DIR)

    def _media_exts(self, kind):
        return MUSIC_EXTS if kind == "music" else VIDEO_EXTS

    def _list_media_files(self, kind):
        folder = self._media_dir(kind)
        if not os.path.isdir(folder):
            return []
        exts = self._media_exts(kind)
        return sorted(f for f in os.listdir(folder) if f.lower().endswith(exts) and "/" not in f)

    def _norm_media_name(self, name: str) -> str:
        base = os.path.splitext(name or "")[0].lower()
        return re.sub(r"[\s_\-()\[\]【】（）·.]+", "", base)

    def _speakable_media_name(self, filename: str) -> str:
        name = os.path.splitext(filename or "")[0]
        name = re.sub(r"[\(\[（【].*?[\)\]）】]", "", name)
        name = re.sub(r"[_\-—–·.]+", " ", name)
        name = re.sub(r"\s+", " ", name).strip()
        return name or os.path.splitext(filename or "")[0]

    def _find_media(self, kind, query=None):
        files = self._list_media_files(kind)
        label = "音乐" if kind == "music" else "视频"
        if not files:
            return None, f"还没有{label}文件。先在后台“媒体”页上传吧。"
        q = (query or "").strip()
        if not q:
            if len(files) == 1:
                return os.path.join(self._media_dir(kind), files[0]), ""
            return None, f"你想播放哪个{label}呀？现在有：" + "、".join(self._speakable_media_name(f) for f in files[:8])
        q_norm = self._norm_media_name(q)
        for name in files:
            if q == name or q.lower() == name.lower():
                return os.path.join(self._media_dir(kind), name), ""
        for name in files:
            n_norm = self._norm_media_name(name)
            if q_norm and (q_norm in n_norm or n_norm in q_norm):
                return os.path.join(self._media_dir(kind), name), ""
        return None, f"没找到“{q}”这个{label}。现在有：" + "、".join(self._speakable_media_name(f) for f in files[:8])

    def list_media_text(self, kind):
        files = self._list_media_files(kind)
        label = "音乐" if kind == "music" else "视频"
        if not files:
            return f"还没有{label}文件。"
        return f"现在有这些{label}：" + "、".join(self._speakable_media_name(f) for f in files[:12])

    def _media_is_running(self):
        proc = self.media_proc
        return bool(proc and proc.poll() is None)

    def _signal_media(self, sig):
        proc = self.media_proc
        if not proc or proc.poll() is not None:
            return False
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except Exception:
            try:
                proc.send_signal(sig)
            except Exception:
                return False
        return True

    def _configured_player(self, key):
        value = (self._media_cfg().get(key) or "").strip()
        if not value:
            return []
        try:
            return shlex.split(value)
        except ValueError:
            return value.split()

    def _media_command_candidates(self, kind, path):
        cfg = self._media_cfg()
        if kind == "music":
            custom = self._configured_player("music_player")
            if custom:
                return [custom + [path]]
            out = []
            mpv = shutil.which("mpv")
            if mpv:
                out.append([mpv, "--no-video", "--really-quiet", path])
            cvlc = shutil.which("cvlc")
            if cvlc:
                out.append([cvlc, "--intf", "dummy", "--play-and-exit", path])
            ffplay = shutil.which("ffplay")
            if ffplay:
                out.append([ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", path])
            mpg123 = shutil.which("mpg123")
            if mpg123 and path.lower().endswith(".mp3"):
                cmd = [mpg123, "-q"]
                dev = self._resolve_output_device()
                if dev:
                    cmd += ["-a", dev]
                out.append(cmd + [path])
            return out
        custom = self._configured_player("video_player")
        if custom:
            return [custom + [path]]
        out = []
        mpv = shutil.which("mpv")
        if mpv:
            out.append([mpv, "--fs", "--really-quiet", path])
        vlc = shutil.which("vlc")
        if vlc:
            out.append([vlc, "--fullscreen", "--play-and-exit", path])
        cvlc = shutil.which("cvlc")
        if cvlc:
            out.append([cvlc, "--fullscreen", "--play-and-exit", path])
        ffplay = shutil.which("ffplay")
        if ffplay:
            out.append([ffplay, "-fs", "-autoexit", "-loglevel", "quiet", path])
        return out

    def _monitor_media_proc(self, proc, kind, name):
        rc = proc.wait()
        with self.media_lock:
            if self.media_proc is not proc:
                return
            self.media_proc = None
            self.media_kind = None
            self.media_name = None
            self.media_paused_for_bmo = False
        log(f"[MEDIA] 已退出: {name} rc={rc}")
        if kind == "video":
            self._restore_bmo_display_mode()
        self.set_state(BotStates.IDLE, "媒体播放已结束")

    def play_media(self, kind, query=None):
        kind = "video" if kind == "video" else "music"
        path, err = self._find_media(kind, query)
        if err:
            return False, err
        name = os.path.basename(path)
        candidates = self._media_command_candidates(kind, path)
        if not candidates:
            return False, "没找到可用播放器。请安装 mpv、vlc 或 ffmpeg。"
        if self._media_is_running():
            self.stop_media(restore_bmo=False)
        if kind == "video":
            self._enter_game_display_mode()
        else:
            self._ensure_pipewire_usb_sink()
            self._pin_hw_volume()
        last_err = ""
        for cmd in candidates:
            try:
                log(f"[MEDIA] 启动: {' '.join(cmd)}")
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                with self.media_lock:
                    self.media_proc = proc
                    self.media_kind = kind
                    self.media_name = name
                    self.media_paused_for_bmo = False
                threading.Thread(target=self._monitor_media_proc, args=(proc, kind, name), daemon=True).start()
                self.abort_to_wake.set()
                label = "视频" if kind == "video" else "音乐"
                return True, f"开始播放{label}：{self._speakable_media_name(name)}"
            except Exception as e:
                last_err = str(e)
                log(f"[MEDIA] 播放器失败: {e}")
        if kind == "video":
            self._restore_bmo_display_mode()
        return False, f"播放失败：{last_err or '没有可用播放器'}"

    def pause_media_for_bmo(self):
        if not self._media_is_running():
            return False, "现在没有正在播放的媒体。"
        if self.media_paused_for_bmo:
            return True, "媒体已经暂停了。"
        ok = self._signal_media(signal.SIGSTOP)
        if not ok:
            return False, "暂停媒体失败。"
        self.media_paused_for_bmo = True
        log(f"[MEDIA] 已暂停: {self.media_name}")
        if self.media_kind == "video":
            self._keep_game_pause_display()
        return True, f"已暂停播放 {self._speakable_media_name(self.media_name or '媒体')}。"

    def resume_media_for_bmo(self):
        if not self._media_is_running():
            self.media_paused_for_bmo = False
            return False, "现在没有正在播放的媒体。"
        if not self.media_paused_for_bmo:
            return True, "媒体已经在播放。"
        ok = self._signal_media(signal.SIGCONT)
        if not ok:
            return False, "继续媒体失败。"
        self.media_paused_for_bmo = False
        log(f"[MEDIA] 继续: {self.media_name}（自动）")
        if self.media_kind == "video":
            self._enter_game_display_mode()
        return True, f"继续播放 {self._speakable_media_name(self.media_name or '媒体')}。"

    def stop_media(self, restore_bmo=True):
        proc = self.media_proc
        kind = self.media_kind
        name = self.media_name or "媒体"
        if not proc or proc.poll() is not None:
            with self.media_lock:
                self.media_proc = None
                self.media_kind = None
                self.media_name = None
                self.media_paused_for_bmo = False
            if restore_bmo and kind == "video":
                self._restore_bmo_display_mode()
            return False, "现在没有正在播放的媒体。"
        try:
            if self.media_paused_for_bmo:
                self._signal_media(signal.SIGCONT)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
        except Exception as e:
            log(f"[MEDIA] 停止失败: {e}")
            return False, "停止播放失败。"
        finally:
            with self.media_lock:
                if self.media_proc is proc:
                    self.media_proc = None
                    self.media_kind = None
                    self.media_name = None
                    self.media_paused_for_bmo = False
            if restore_bmo and kind == "video":
                self._restore_bmo_display_mode()
        log(f"[MEDIA] 已停止: {name}")
        return True, f"已停止播放 {self._speakable_media_name(name)}。"

    def _media_state(self):
        if not self._media_is_running():
            return {"running": False, "kind": None, "name": None, "paused": False}
        return {
            "running": True,
            "kind": self.media_kind,
            "name": self.media_name,
            "paused": self.media_paused_for_bmo,
        }

    # -------------------------------------------------------------------
    # 识别屏幕（截屏 + 视觉模型描述）
    # -------------------------------------------------------------------
    def _capture_screen(self):
        """grim 截当前屏幕到 screen_shot，返回路径或 None。"""
        tool = shutil.which("grim")
        if not tool:
            log("[SCREEN] 未安装 grim（sudo apt install grim）")
            return None
        try:
            subprocess.run([tool, self.screen_shot], env=os.environ.copy(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        except Exception as e:
            log(f"[SCREEN] 截屏失败: {e}")
            return None
        return self.screen_shot if os.path.exists(self.screen_shot) else None

    def _read_screen_flow(self, original_text):
        self.set_state(BotStates.CAPTURING, "看一下屏幕...")
        shot = self._capture_screen()
        if not shot:
            self._say("我截不到屏幕，得先装 grim 截屏工具哦。", remember=original_text)
            return
        try:
            desc = self.vision.describe(shot, original_text or "请描述这张屏幕截图上的内容。")
        except Exception as e:
            log(f"[SCREEN] 视觉识别失败: {e}")
            self._say("我看到屏幕了，但看不太懂呢。", remember=original_text)
            return
        if not desc or len(desc.strip()) < 2:
            self._say("屏幕上好像没什么能认出来的东西。", remember=original_text)
            return
        log(f"[SCREEN] {desc}")
        try:
            final = self.llm.chat_once([
                {"role": "system", "content":
                    "你是 BMO，可爱的小机器人。根据屏幕截图的描述，用一两句活泼的话告诉用户屏幕上有什么。"
                    "绝不要输出 JSON、不要调用工具。"},
                {"role": "user", "content": f"屏幕内容：{desc}"},
            ])
        except Exception:
            final = f"屏幕上是：{desc}"
        self._say(final, remember=original_text)

    # -------------------------------------------------------------------
    # 隐身（最小化但继续运行；只有取消隐身才恢复）
    # -------------------------------------------------------------------
    def _hide_bmo(self):
        if self.hidden:
            self._say("我已经躲起来啦，叫我“取消隐身”就回来。")
            return
        self.hidden = True
        self._say("好的，我先躲起来啦，叫我“取消隐身”再回来！")
        self.wait_for_tts()

        def _do():
            try:
                self.exit_button.place_forget()
            except Exception:
                pass
            try:
                self.master.withdraw()
            except Exception:
                pass
        self._safe_after(0, _do)
        log("[HIDE] 已隐身（窗口最小化，程序继续运行）")

    def _unhide_bmo(self):
        if not self.hidden:
            self._say("我没有隐身呀，我一直都在。")
            return
        self.hidden = False

        def _do():
            try:
                self.master.deiconify()
                self.master.attributes('-fullscreen', True)
                self.master.lift()
                self.master.focus_force()
            except Exception:
                pass
            self._set_cursor_visible(False)
        self._safe_after(0, _do)
        log("[HIDE] 已取消隐身")
        self._say("我回来啦！")

    LONG_PRESS_SECONDS = 0.6   # 回车长按阈值

    def handle_return_press(self, event=None):
        # 系统按键自动重复会连发 press：刚才若有"待定的松开"，取消它（说明还按着）
        if self._return_release_timer is not None:
            try:
                self.master.after_cancel(self._return_release_timer)
            except Exception:
                pass
            self._return_release_timer = None
        if self._return_pressed_at is None:
            self._return_pressed_at = time.time()

    def handle_return_release(self, event=None):
        # 延后一点确认是不是真松开（自动重复会立刻又来一个 press 把它取消）
        if self._return_release_timer is not None:
            try:
                self.master.after_cancel(self._return_release_timer)
            except Exception:
                pass
        # 80ms > 系统按键自动重复间隔(~40ms)：长按时后续重复 press 会取消它，只有真松开才会落定
        self._return_release_timer = self.master.after(80, self._finalize_return)

    def _finalize_return(self):
        self._return_release_timer = None
        if self._return_pressed_at is None:
            return
        dur = time.time() - self._return_pressed_at
        self._return_pressed_at = None
        if dur >= self.LONG_PRESS_SECONDS:
            log(f"[BTN] 回车长按 {dur:.1f}s → 回到待唤醒")
            self.go_to_wait_wake()
        else:
            self.handle_ptt_toggle()

    def handle_ptt_toggle(self, event=None):
        # 短按 = 唤醒（和语音唤醒一致）：应答 + 自适应录音 + 连续对话
        now = time.time()
        if now - self.last_ptt_time < 0.5:
            return
        self.last_ptt_time = now
        if self.current_state == BotStates.IDLE or "等" in self.current_status or "Wait" in self.current_status:
            log("[BTN] 按钮唤醒")
            self.ptt_event.set()

    def go_to_wait_wake(self, event=None):
        """中止当前对话/录音/说话，强制回到"等待唤醒"状态。已空闲则忽略。"""
        if self.current_state in (BotStates.IDLE, BotStates.WARMUP):
            return
        self.abort_to_wake.set()
        self.interrupted.set()
        self.recording_active.clear()
        self.ptt_event.clear()
        self.pending_print = None
        with self.tts_queue_lock:
            self.tts_queue.clear()
        if self.current_tts_proc:
            try:
                self.current_tts_proc.terminate()
            except Exception:
                pass
        self.set_state(BotStates.IDLE, "等待唤醒...")

    def handle_speaking_interrupt(self, event=None):
        if self.current_state in (BotStates.SPEAKING, BotStates.THINKING):
            log("[打断] 用户按了空格")
            self.interrupted.set()
            with self.tts_queue_lock:
                self.tts_queue.clear()
            if self.current_tts_proc:
                try:
                    self.current_tts_proc.terminate()
                except Exception:
                    pass
            self.set_state(BotStates.IDLE, "已打断")

    # -------------------------------------------------------------------
    # 动画 / 状态显示
    # -------------------------------------------------------------------
    def _waiting_animation_files(self, files):
        closed_to_open = [
            "waiting 04.png",
            "waiting 05.png",
            "waiting 03.png",
            "waiting 02.png",
            "waiting 01.png",
            "waiting 06.png",
        ]
        present = {f: f for f in files}
        ordered = [present[f] for f in closed_to_open if f in present]
        ordered.extend(f for f in files if f not in set(ordered))
        return ordered

    def load_animations(self):
        base = "faces"
        states = ["idle", "listening", "thinking", "speaking", "error", "capturing", "warmup", "waiting"]
        for s in states:
            folder = os.path.join(base, s)
            self.animations[s] = []
            if os.path.isdir(folder):
                files = sorted(f for f in os.listdir(folder) if f.lower().endswith(".png"))
                if s == BotStates.WAITING_FACE:
                    files = self._waiting_animation_files(files)
                for f in files:
                    try:
                        img = Image.open(os.path.join(folder, f)).resize((self.BG_WIDTH, self.BG_HEIGHT))
                        self.animations[s].append(ImageTk.PhotoImage(img))
                    except Exception:
                        pass
            if not self.animations[s]:
                blank = Image.new('RGB', (self.BG_WIDTH, self.BG_HEIGHT), color='#0000FF')
                self.animations[s].append(ImageTk.PhotoImage(blank))

    def _begin_wait_wake_display(self, reset=False):
        if self.wait_wake_started_at and not reset:
            return
        self.wait_wake_started_at = time.time()
        self.waiting_face_active = False

    def _end_wait_wake_display(self):
        self.wait_wake_started_at = None
        self.waiting_face_active = False

    def _current_animation_key(self):
        if self.current_state == BotStates.IDLE and self.wait_wake_started_at:
            if time.time() - self.wait_wake_started_at >= 300:
                if not self.waiting_face_active:
                    self.waiting_face_active = True
                    self.current_frame_index = 0
                return BotStates.WAITING_FACE
        return self.current_state

    def update_animation(self):
        animation_key = self._current_animation_key()
        frames = self.animations.get(animation_key) or self.animations.get(BotStates.IDLE)
        if not frames:
            self.master.after(500, self.update_animation)
            return
        if animation_key != self.current_animation_key:
            self.current_animation_key = animation_key
            self.current_frame_index = 0
        elif self.current_state == BotStates.SPEAKING and len(frames) > 1:
            self.current_frame_index = random.randint(1, len(frames) - 1)
        else:
            self.current_frame_index = (self.current_frame_index + 1) % len(frames)
        self.background_label.config(image=frames[self.current_frame_index])
        speed = 50 if self.current_state == BotStates.SPEAKING else 500
        self.master.after(speed, self.update_animation)

    def _safe_after(self, delay, fn):
        """退出后 tkinter 主循环已停，after 会抛 RuntimeError，这里吞掉。"""
        if self.exiting:
            return
        try:
            self.master.after(delay, fn)
        except (RuntimeError, tk.TclError):
            pass

    def set_state(self, state, msg="", overlay_path=None):
        if state == BotStates.ERROR:
            threading.Thread(target=self._play_status_cue, args=("error",),
                             daemon=True).start()
        def _update():
            if msg:
                log(f"[STATE] {state.upper()}: {msg}")
            if self.current_state != state:
                self.current_state = state
                self.current_frame_index = 0
                self.current_animation_key = None
            if state == BotStates.IDLE and msg and "等待唤醒" in msg:
                self._begin_wait_wake_display()
            elif state != BotStates.IDLE or msg:
                self._end_wait_wake_display()
            if msg:
                self.current_status = msg
                self.status_var.set(msg)
            if overlay_path and os.path.exists(overlay_path) and state in (BotStates.THINKING, BotStates.SPEAKING):
                try:
                    img = Image.open(overlay_path).resize((self.OVERLAY_WIDTH, self.OVERLAY_HEIGHT))
                    self.current_overlay_image = ImageTk.PhotoImage(img)
                    self.overlay_label.config(image=self.current_overlay_image)
                    self.overlay_label.place(x=200, y=90)
                except Exception:
                    pass
            else:
                self.overlay_label.place_forget()
        self._safe_after(0, _update)

    def append_to_text(self, text, newline=True):
        def _update():
            self.response_text.config(state=tk.NORMAL)
            if newline:
                self.response_text.insert(tk.END, text + "\n")
            else:
                self.response_text.insert(tk.END, text)
            self.response_text.see(tk.END)
            self.response_text.config(state=tk.DISABLED)
        self._safe_after(0, _update)

    def _stream_to_text(self, chunk):
        def _update():
            self.response_text.config(state=tk.NORMAL)
            self.response_text.insert(tk.END, chunk)
            self.response_text.see(tk.END)
            self.response_text.config(state=tk.DISABLED)
        self._safe_after(0, _update)

    # -------------------------------------------------------------------
    # 主循环
    # -------------------------------------------------------------------
    def safe_main_execution(self):
        try:
            self.set_state(BotStates.WARMUP, "暖机中...")
            # 走 PipeWire 输出时，先把默认 sink 指到 USB 音箱（免得跑到 HDMI）
            threading.Thread(target=self._ensure_pipewire_usb_sink, daemon=True).start()
            # 启动 TTS 后台线程
            self.tts_active.set()
            threading.Thread(target=self._tts_worker, daemon=True).start()
            self.set_state(BotStates.IDLE, "准备好啦")
            # 开机问候：读出自定义状态词（默认"你好，我叫 BMO"）
            threading.Thread(target=self._play_status_cue, args=("greeting",),
                             daemon=True).start()

            while not self.exiting:
                trigger = self.detect_wake_word_or_ptt()
                if self.exiting:
                    return
                if self.interrupted.is_set():
                    self.interrupted.clear()
                    self.set_state(BotStates.IDLE, "重置")
                    continue
                if self._media_is_running() and not self.media_paused_for_bmo:
                    ok, msg = self.pause_media_for_bmo()
                    log(f"[MEDIA] 唤醒暂停 -> {'成功' if ok else '失败'}: {msg}")
                if self._game_is_running() and not self.game_paused:
                    self.pause_game(for_bmo=True, show_bmo=False)
                # 先把动画切到"在听"，立刻给视觉反馈；否则要等应答音(TTS ~2s)念完动画才变
                self.set_state(BotStates.LISTENING, "在听...")
                # 被唤醒/触发 → 读出应答词（等播完再录，避免录进自己的提示音）
                self._play_status_cue("ack")

                # 每轮都重新读 config，网页改了立即生效
                conv_cfg = self.config.get("conversation", {})
                follow_up = conv_cfg.get("follow_up", True)
                awake_secs = float(conv_cfg.get("awake_seconds", 15))
                followup_secs = float(conv_cfg.get("follow_up_seconds", 15))
                post_delay = float(conv_cfg.get("post_response_delay", 1.0))

                # 一轮唤醒（语音 or 按钮）后，统一走自适应录音 + 连续对话
                first = True
                # 监听截止时间：噪音不重置它，只有有效交互(真说话/再唤醒)才续期；
                # 这样无意义识别只消耗剩余时间，到点就回待唤醒，不会被噪音无限拖住。
                deadline = time.time() + awake_secs
                while not self.exiting and not self.abort_to_wake.is_set():
                    self.set_state(BotStates.LISTENING, "在听..." if first else "请说...")

                    remaining = deadline - time.time()
                    if remaining <= 0:
                        self.set_state(BotStates.IDLE, "没听到")
                        break
                    audio_file = self.record_voice_adaptive(onset_timeout=remaining)

                    if self._acknowledge_rewake():
                        first = True
                        deadline = time.time() + awake_secs   # 再唤醒：重置窗口
                        continue
                    if self.abort_to_wake.is_set():
                        break
                    if not audio_file:
                        self.set_state(BotStates.IDLE, "没听到")
                        break

                    user_text = self.transcribe_audio(audio_file)
                    if user_text and self._is_noise_text(user_text):
                        # 环境噪音常被误识成 '.' / '。' / 孤立韩文假名等无意义内容，
                        # 别喂给 LLM。不重置 deadline：继续听但剩余时间持续递减，
                        # 到点(remaining<=0)自然回待唤醒，避免被周期性噪音无限拖住。
                        log(f"[REC] 忽略无意义识别: {user_text!r}，继续听")
                        continue
                    if not user_text:
                        self.set_state(BotStates.IDLE, "没听清")
                        break

                    log(f"[USER] {user_text}")
                    self.append_to_text(f"你: {user_text}")
                    self.interrupted.clear()
                    self.chat_and_respond(user_text, img_path=None)

                    if self._acknowledge_rewake():
                        first = True
                        deadline = time.time() + awake_secs   # 再唤醒：重置窗口
                        continue
                    first = False
                    if not follow_up:
                        break
                    deadline = time.time() + followup_secs   # 有效一轮后：续期连续对话窗口
                    # 等回声散掉再录，避免把喇叭余音当成噪音地板（导致听不清）
                    time.sleep(post_delay)

        except Exception as e:
            traceback.print_exc()
            self.set_state(BotStates.ERROR, f"出错: {str(e)[:40]}")

    # -------------------------------------------------------------------
    # 唤醒词 / PTT 检测
    # -------------------------------------------------------------------
    def detect_wake_word_or_ptt(self):
        self.set_state(BotStates.IDLE, "等待唤醒...")
        self.pending_print = None   # 回到等唤醒就清掉"要不要打印"，避免下次唤醒被误当成回答
        self.abort_to_wake.clear()  # 已经回到待唤醒，清掉长按中止标志
        self.interrupted.clear()
        self.rewake_event.clear()
        self.ptt_event.clear()

        # 回到待唤醒：之前为了跟你说话把游戏暂停了的话，现在自动继续游戏（别一直暂停）
        if self._game_is_running() and self.game_paused:
            self.resume_game(auto=True)
        if self._media_is_running() and self.media_paused_for_bmo:
            self.resume_media_for_bmo()

        # 配置变更后在本线程安全重载唤醒词（避免和监听循环跨线程竞争）
        self._reload_wake_word_if_needed()

        # 没装任何唤醒词后端 → 纯 PTT 模式
        if self.wake_backend is None:
            while not self.ptt_event.is_set():
                if self.exiting:
                    return "PTT"
                if self._reload_wake_word_if_needed():
                    if self.wake_backend is not None:
                        break
                time.sleep(0.1)
            if self.ptt_event.is_set():
                self.ptt_event.clear()
                return "PTT"

        # 重置后端状态
        if self.sherpa_kws:
            self.sherpa_kws.reset()
        if self.oww_model:
            self.oww_model.reset()

        # 两个后端都用 16kHz 输入
        TARGET_SR = 16000
        # OpenWakeWord 要求 1280 样本/chunk；sherpa 不挑，但 0.1s = 1600 样本一块比较合适
        target_chunk = 1280 if self.wake_backend == "openwakeword" else 1600

        def build_stream_args():
            input_rate, input_channels = choose_input_settings(
                self.input_device, self.config.get("input_sample_rate"),
            )
            chunk = int(target_chunk * (input_rate / TARGET_SR)) if input_rate != TARGET_SR else target_chunk
            args = dict(
                samplerate=input_rate, channels=input_channels, dtype='int16',
                blocksize=chunk, device=self.input_device,
                latency=self.config.get("audio_latency", "high"),
            )
            return args, chunk

        stream_args, in_chunk = build_stream_args()
        refresh_secs = float(self.config.get("wake_word", {}).get("stream_refresh_seconds", 180))

        # 自动重开：定期刷新音频流 + 读取出错时重开，避免长时间空闲后唤不醒
        fail_count = 0
        while not self.exiting:
            try:
                result = self._listen_loop(stream_args, in_chunk, target_chunk, refresh_secs)
            except StopIteration as si:
                return str(si)
            except Exception as e:
                fail_count += 1
                log(f"[AUDIO] 唤醒词音频流出错({fail_count})，重开: {e}")
                if self.ptt_event.is_set():
                    self.ptt_event.clear()
                    return "PTT"
                # 连续失败 → 彻底重置 PortAudio，强制重新枚举设备（清掉卡死的 ALSA 句柄）
                if fail_count in (2, 4, 6):
                    self._reset_portaudio()
                    stream_args, in_chunk = build_stream_args()
                if fail_count >= 12:
                    log("[AUDIO] 连续重开失败，回落 PTT")
                    while not self.ptt_event.is_set() and not self.exiting:
                        time.sleep(0.1)
                    self.ptt_event.clear()
                    return "PTT"
                # 卡死后多等一会，让 USB/ALSA 完全释放再重开
                try:
                    sd.stop()
                except Exception:
                    pass
                time.sleep(1.5)
                continue
            fail_count = 0
            if result == "REFRESH":
                continue          # 定期刷新，重开音频流继续听
            if result == "RELOAD":
                self._reload_wake_word_if_needed()
                return self.detect_wake_word_or_ptt()  # 配置变更，按新后端重算音频参数
            return result          # "WAKE"
        return "PTT"

    def _reset_wake_detector(self):
        if self.sherpa_kws:
            self.sherpa_kws.reset()
        if self.oww_model:
            self.oww_model.reset()

    def _feed_wake_frame(self, audio, debug=False):
        """向当前唤醒后端送一帧 16kHz mono int16；命中时返回关键词。"""
        if self.sherpa_kws:
            return self.sherpa_kws.feed(audio) or None
        if not self.oww_model:
            return None

        self.oww_model.predict(audio)
        best_k, best_score = None, 0.0
        for key in self.oww_model.prediction_buffer.keys():
            score = list(self.oww_model.prediction_buffer[key])[-1]
            if score > best_score:
                best_k, best_score = key, score
        threshold = float(self.config.get("wake_word", {}).get("legacy_threshold", 0.3))
        if best_score >= threshold:
            self.oww_model.reset()
            return best_k or "openwakeword"
        if debug:
            near = max(0.2, threshold * 0.5)
            if best_score >= near:
                log(f"[WAKE?] '{best_k}' score={best_score:.2f}（阈值 {threshold:.2f}，没到）")
        return None

    @staticmethod
    def _fit_wake_frame(data, target_samples):
        audio = select_active_input_channel(data)
        audio = np.asarray(audio, dtype=np.float32).flatten()
        if len(audio) != target_samples:
            audio = scipy.signal.resample_poly(audio, target_samples, max(1, len(audio)))
        if len(audio) > target_samples:
            audio = audio[:target_samples]
        elif len(audio) < target_samples:
            audio = np.pad(audio, (0, target_samples - len(audio)))
        return np.clip(audio, -32768, 32767).astype(np.int16)

    def _trigger_rewake(self, keyword, source):
        if self.rewake_event.is_set():
            return
        log(f"[WAKE] {source}再次触发 '{keyword}'")
        self.rewake_event.set()
        self.interrupted.set()
        self._cancel_thinking_cue()
        with self.tts_queue_lock:
            self.tts_queue.clear()
        proc = self.current_tts_proc
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass
        self.set_state(BotStates.LISTENING, "重新唤醒...")

    def _acknowledge_rewake(self):
        """消费再次唤醒事件；调用方随后把 first=True 以重置首次唤醒计时。"""
        if not self.rewake_event.is_set():
            return False
        self.rewake_event.clear()
        self.interrupted.clear()
        with self.tts_queue_lock:
            self.tts_queue.clear()
        self.set_state(BotStates.LISTENING, "在听...")
        self._play_status_cue("ack")
        return True

    def _start_speaking_wake_monitor(self, speaking_text=""):
        """TTS 播放期间监听唤醒词；返回 (stop_event, thread)。"""
        if self.wake_backend is None:
            return None
        normalized_text = re.sub(r"\s+", "", speaking_text or "").lower()
        keywords = self.config.get("wake_word", {}).get("keywords", [])
        if any(re.sub(r"\s+", "", str(word)).lower() in normalized_text
               for word in keywords if str(word).strip()):
            log("[WAKE] 当前朗读内容含唤醒词，本句暂停打断监听以避免自唤醒")
            return None
        stop_event = threading.Event()

        def _monitor():
            target_sr = 16000
            target_chunk = 1280 if self.wake_backend == "openwakeword" else 1600
            try:
                self._reset_wake_detector()
                input_rate, input_channels = choose_input_settings(
                    self.input_device, self.config.get("input_sample_rate"),
                )
                in_chunk = max(1, int(target_chunk * input_rate / target_sr))
                with sd.InputStream(
                    samplerate=input_rate, channels=input_channels, dtype='int16',
                    blocksize=in_chunk, device=self.input_device,
                    latency=self.config.get("audio_latency", "high"),
                ) as stream:
                    while not stop_event.is_set() and not self.exiting:
                        data, _ = stream.read(in_chunk)
                        frame = self._fit_wake_frame(data, target_chunk)
                        hit = self._feed_wake_frame(frame)
                        if hit:
                            self._trigger_rewake(hit, "说话中")
                            return
            except Exception as e:
                if not stop_event.is_set():
                    log(f"[WAKE] 说话中监听失败: {e}")

        thread = threading.Thread(target=_monitor, daemon=True)
        thread.start()
        return stop_event, thread

    @staticmethod
    def _stop_speaking_wake_monitor(monitor):
        if not monitor:
            return
        stop_event, thread = monitor
        stop_event.set()
        thread.join(timeout=0.5)

    def _log_input_device(self):
        try:
            if self.input_device is not None:
                name = sd.query_devices(self.input_device).get("name", "?")
                log(f"[AUDIO] 麦克风输入设备: [{self.input_device}] {name}")
            else:
                log("[AUDIO] 麦克风输入设备: 系统默认（未自动选到 USB 麦）")
        except Exception:
            pass

    def _reset_portaudio(self):
        """彻底重启 PortAudio，强制重新枚举音频设备（清掉卡死的 ALSA 句柄）。"""
        try:
            log("[AUDIO] 重置 PortAudio（重新枚举设备）...")
            sd.stop()
            sd._terminate()
            time.sleep(0.8)
            sd._initialize()
            time.sleep(0.4)
            # USB 设备重连/PortAudio 重建后序号可能变化，必须按配置名称重新定位。
            self.input_device = resolve_input_device(self.config.get("input_device"))
            self._log_input_device()
        except Exception as e:
            log(f"[AUDIO] 重置 PortAudio 失败: {e}")

    def _listen_loop(self, args, in_chunk, target_chunk, refresh_secs=180):
        ww_cfg = self.config.get("wake_word", {})
        oww_debug = ww_cfg.get("legacy_debug", True)
        loop_start = time.time()
        last_audio_time = time.time()
        audio_timeout = float(ww_cfg.get("audio_callback_timeout_seconds", 10.0))
        audio_q = queue.Queue(maxsize=8)
        if self.sherpa_kws:
            self.sherpa_kws.reset()
        if self.oww_model:
            self.oww_model.reset()

        def _audio_callback(indata, frames, time_info, status):
            try:
                audio_q.put_nowait(indata.copy())
            except queue.Full:
                try:
                    audio_q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    audio_q.put_nowait(indata.copy())
                except queue.Full:
                    pass

        with sd.InputStream(**args, callback=_audio_callback):
            log(f"[AUDIO] 听唤醒词 backend={self.wake_backend} "
                f"sr={args['samplerate']} channels={args['channels']}")
            while True:
                if self.exiting:
                    raise StopIteration("EXIT")
                if self.wake_reload_pending:
                    return "RELOAD"
                if self.ptt_event.is_set():
                    self.ptt_event.clear()
                    raise StopIteration("PTT")
                # 定期刷新音频流（防止长时间运行后 ALSA/USB 进入坏状态）
                if time.time() - loop_start > refresh_secs:
                    log(f"[AUDIO] 定时刷新唤醒词音频流 ({refresh_secs:.0f}s)")
                    return "REFRESH"
                try:
                    data = audio_q.get(timeout=0.5)
                    last_audio_time = time.time()
                except queue.Empty:
                    if time.time() - last_audio_time > audio_timeout:
                        raise RuntimeError(f"audio callback timeout > {audio_timeout:.1f}s")
                    continue

                audio = self._fit_wake_frame(data, target_chunk)
                hit = self._feed_wake_frame(audio, debug=oww_debug)
                if hit:
                    log(f"[WAKE] 触发 '{hit}'")
                    return "WAKE"

    # -------------------------------------------------------------------
    # 录音
    # -------------------------------------------------------------------
    def record_voice_adaptive(self, filename="input.wav", onset_timeout=15.0):
        """录音：先等用户开口（最多 onset_timeout 秒），开口后录到静音为止。
        用阻塞式 stream.read（和唤醒词监听一样可靠，避免 Pi 上回调不触发）。
        在 onset_timeout 内没开口 → 返回 None（结束本次对话）。"""
        log(f"录音（自适应，等待开口≤{onset_timeout:.0f}s）...")
        time.sleep(0.2)
        sr, input_channels = choose_input_settings(
            self.input_device, self.config.get("input_sample_rate"),
        )

        rec_cfg = self.config.get("recording", {})
        silence_duration = float(rec_cfg.get("silence_duration", 1.8))
        max_speech = float(rec_cfg.get("max_record_seconds", 25.0))
        margin = float(rec_cfg.get("silence_margin", 0.05))
        floor_min = float(rec_cfg.get("silence_floor_min", 0.02))

        chunk_dur = 0.05
        chunk_size = int(sr * chunk_dur)
        num_silent = int(silence_duration / chunk_dur)
        calib_chunks = 12
        onset_chunks = int(onset_timeout / chunk_dur)
        max_speech_chunks = int(max_speech / chunk_dur)

        buf, calib = [], []
        noise = 0.0
        thr = floor_min
        peak = 0.0
        speaking = False
        speech_start = 0
        silent = 0
        n = 0
        timeout = False

        # 回调+队列：避免阻塞 read 在 USB 卡住时把整个录音冻死
        stall_timeout = float(rec_cfg.get("stall_timeout_seconds", 8.0))
        audio_q = queue.Queue(maxsize=16)
        stalled = False
        wake_target_chunk = 1280 if self.wake_backend == "openwakeword" else 1600
        wake_pending = np.empty(0, dtype=np.int16)
        if self.wake_backend is not None:
            self._reset_wake_detector()

        def _cb(indata, frames, time_info, status):
            try:
                chunk = select_active_input_channel(indata)
                audio_q.put_nowait(chunk.copy())
            except queue.Full:
                try:
                    audio_q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    audio_q.put_nowait(chunk.copy())
                except queue.Full:
                    pass

        try:
            last_audio = time.time()
            with sd.InputStream(samplerate=sr, channels=input_channels, dtype='int16',
                                blocksize=chunk_size, device=self.input_device,
                                latency=self.config.get("audio_latency", "high"),
                                callback=_cb) as stream:
                while not self.exiting:
                    if self.abort_to_wake.is_set():   # 长按回车：立刻停止录音
                        try:
                            stream.abort(ignore_errors=True)
                        except Exception:
                            pass
                        return None
                    try:
                        indata = audio_q.get(timeout=0.5)
                        last_audio = time.time()
                    except queue.Empty:
                        if time.time() - last_audio > stall_timeout:
                            log("[AUDIO] 录音音频流卡住，放弃本次录音")
                            stalled = True
                            # 立即 abort 卡死的流：坏流上 close 可能挂住（表现为整个程序冻住）
                            try:
                                stream.abort(ignore_errors=True)
                            except Exception:
                                pass
                            break
                        continue

                    audio = np.asarray(indata, dtype=np.float32).flatten()

                    # 已经处于唤醒状态时再次说唤醒词：丢弃当前录音、重播应答，
                    # 由主循环把等待时长恢复为 awake_seconds。
                    if self.wake_backend is not None:
                        wake_piece = audio
                        if sr != 16000:
                            wake_piece = scipy.signal.resample_poly(wake_piece, 16000, sr)
                        wake_piece = np.clip(wake_piece, -32768, 32767).astype(np.int16)
                        wake_pending = np.concatenate((wake_pending, wake_piece))
                        while len(wake_pending) >= wake_target_chunk:
                            wake_frame = wake_pending[:wake_target_chunk]
                            wake_pending = wake_pending[wake_target_chunk:]
                            hit = self._feed_wake_frame(wake_frame)
                            if hit:
                                self._trigger_rewake(hit, "等待说话时")
                                return None

                    buf.append(audio.copy())
                    n += 1
                    vn = float(np.sqrt(np.mean(audio ** 2)) / 32768.0)

                    # 校准噪音地板（中位数）
                    if n <= calib_chunks:
                        calib.append(vn)
                        if n == calib_chunks:
                            noise = float(np.median(calib))
                            thr = max(floor_min, noise + margin)
                        continue

                    peak = max(peak, vn)

                    if not speaking:
                        if vn >= thr:
                            speaking = True
                            speech_start = n
                            silent = 0
                        elif n - calib_chunks >= onset_chunks:
                            timeout = True
                            break
                        continue

                    # 已开口
                    if vn < thr:
                        silent += 1
                        if silent >= num_silent:
                            break
                    else:
                        silent = 0
                    if n - speech_start >= max_speech_chunks:
                        break
        except Exception as e:
            log(f"[AUDIO ERROR] 自适应录音失败: {e}")
            return None

        if stalled:
            # 录音流卡死多半是 USB 声卡进了坏状态（常发生在刚播完 TTS 又马上录音）：
            # 重置 PortAudio 重新枚举设备，让下一轮唤醒/录音能恢复，而不是一直冻住。
            self._reset_portaudio()
            return None
        if timeout or not speaking:
            log(f"[REC] {onset_timeout:.0f}s 没开口 噪音地板={noise:.4f} 阈值={thr:.4f} 峰值={peak:.4f}（峰值没过阈值=没听到你说话）")
            return None

        dur = (n - speech_start) * chunk_dur
        log(f"[REC] 语音 {dur:.1f}s 噪音地板={noise:.4f} 阈值={thr:.4f} 峰值={peak:.4f}")
        speech_buf = buf[max(0, speech_start - 4):]
        # 阻塞式读出来已经是 float32（-32768~32767 范围）；保存时归一化
        return self._save_int_buffer(speech_buf, filename, sr)

    def record_voice_ptt(self, filename="input.wav"):
        log("录音（PTT）...")
        time.sleep(0.2)
        sr, input_channels = choose_input_settings(
            self.input_device, self.config.get("input_sample_rate"),
        )
        chunk_size = int(sr * 0.05)
        buf = []
        try:
            with sd.InputStream(samplerate=sr, channels=input_channels, dtype='int16',
                                blocksize=chunk_size, device=self.input_device,
                                latency=self.config.get("audio_latency", "high")) as stream:
                while self.recording_active.is_set() and not self.exiting:
                    try:
                        data, _ = stream.read(chunk_size)
                    except Exception as e:
                        log(f"[AUDIO] PTT 读取失败: {e}")
                        break
                    chunk = select_active_input_channel(data)
                    buf.append(chunk.astype(np.float32).flatten())
        except Exception as e:
            log(f"[AUDIO ERROR] PTT 录音失败: {e}")
            return None
        return self._save_int_buffer(buf, filename, sr)

    def save_audio_buffer(self, buf, filename, sr=16000):
        """callback 录音用：indata 是 -1.0~1.0 浮点，乘 32767 转 int16。"""
        if not buf:
            return None
        audio = np.concatenate(buf, axis=0).flatten()
        audio = np.nan_to_num(audio, nan=0.0, posinf=0.0, neginf=0.0)
        audio = (audio * 32767).astype(np.int16)
        return self._write_wav(audio, filename, sr)

    def _save_int_buffer(self, buf, filename, sr=16000):
        """阻塞 read 录音用：已经是 int16 数值范围的 float32，直接取整。"""
        if not buf:
            return None
        audio = np.concatenate(buf, axis=0).flatten()
        audio = np.nan_to_num(audio, nan=0.0, posinf=0.0, neginf=0.0)
        audio = np.clip(audio, -32768, 32767).astype(np.int16)
        return self._write_wav(audio, filename, sr)

    def _write_wav(self, audio_int16, filename, sr):
        with wave.open(filename, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(audio_int16.tobytes())
        return filename

    # -------------------------------------------------------------------
    # STT
    # -------------------------------------------------------------------
    def transcribe_audio(self, filename):
        try:
            text = self.stt.transcribe(filename)
            log(f"听到: '{text}'")
            return text
        except Exception as e:
            log(f"[STT ERROR] {e}")
            return ""

    @staticmethod
    def _is_noise_text(text: str) -> bool:
        """判断识别结果是不是噪音误识（无实际语义）。
        噪音常被 SenseVoice 转成 '.'、'。'、'…' 或孤立的韩文/假名等。
        只保留中文/英文字母/数字作为'有意义字符'，一个都没有就当噪音。
        单个中文字（如'好'/'是'/'要'）仍保留，因为可能是有效的简短回答。"""
        if not text or not text.strip():
            return True
        meaningful = re.findall(r"[一-鿿A-Za-z0-9]", text)
        return len(meaningful) == 0

    # -------------------------------------------------------------------
    # 摄像头
    # -------------------------------------------------------------------
    def capture_image(self):
        self.set_state(BotStates.CAPTURING, "看一下...")
        try:
            subprocess.run(
                ["rpicam-still", "-t", "500", "-n", "--width", "640",
                 "--height", "480", "-o", BMO_IMAGE_FILE],
                check=True, timeout=10,
            )
            rot = self.config.get("camera_rotation", 0)
            if rot:
                img = Image.open(BMO_IMAGE_FILE).rotate(rot, expand=True)
                img.save(BMO_IMAGE_FILE)
            try:
                name = datetime.datetime.now().strftime("capture_%Y%m%d_%H%M%S.jpg")
                shutil.copy2(BMO_IMAGE_FILE, os.path.join(CAPTURES_DIR, name))
            except Exception as e:
                log(f"[CAM] 保存拍照历史失败: {e}")
            return BMO_IMAGE_FILE
        except Exception as e:
            log(f"[CAM ERROR] {e}")
            return None

    # -------------------------------------------------------------------
    # 工具执行
    # -------------------------------------------------------------------
    def extract_json_from_text(self, text):
        if not text:
            return None
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
        starts = [i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0]
        if not starts:
            return None
        decoder = json.JSONDecoder()
        for start in sorted(starts):
            try:
                data, _ = decoder.raw_decode(cleaned[start:])
                if isinstance(data, (dict, list)):
                    return data
            except Exception:
                continue
        return None

    def execute_actions(self, action_data):
        if isinstance(action_data, list):
            results = []
            for item in action_data:
                if isinstance(item, dict):
                    results.append(self.execute_action(item))
                else:
                    results.append("INVALID_ACTION")
            return results
        return self.execute_action(action_data)

    def _read_cpu_times(self):
        try:
            with open("/proc/stat", "r", encoding="utf-8") as f:
                parts = f.readline().split()[1:]
            values = [int(x) for x in parts[:8]]
            idle = values[3] + values[4]
            total = sum(values)
            return idle, total
        except Exception:
            return None

    def _get_cpu_usage_percent(self):
        first = self._read_cpu_times()
        if not first:
            return None
        time.sleep(0.15)
        second = self._read_cpu_times()
        if not second:
            return None
        idle_delta = second[0] - first[0]
        total_delta = second[1] - first[1]
        if total_delta <= 0:
            return None
        return max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0))

    def _get_cpu_temp_c(self):
        paths = [
            "/sys/class/thermal/thermal_zone0/temp",
            "/sys/class/hwmon/hwmon0/temp1_input",
        ]
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = f.read().strip()
                if raw:
                    val = float(raw)
                    return val / 1000.0 if val > 200 else val
            except Exception:
                pass
        try:
            out = subprocess.check_output(["vcgencmd", "measure_temp"], timeout=2).decode()
            m = re.search(r"temp=([\d.]+)", out)
            if m:
                return float(m.group(1))
        except Exception:
            pass
        return None

    def _get_memory_info(self):
        try:
            data = {}
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    key, val = line.split(":", 1)
                    data[key] = int(val.strip().split()[0]) * 1024
            total = data.get("MemTotal", 0)
            available = data.get("MemAvailable", 0)
            used = max(0, total - available)
            percent = (used / total * 100.0) if total else None
            return total, used, percent
        except Exception:
            return None

    def _get_uptime_text(self):
        try:
            with open("/proc/uptime", "r", encoding="utf-8") as f:
                seconds = int(float(f.read().split()[0]))
            days, rem = divmod(seconds, 86400)
            hours, rem = divmod(rem, 3600)
            minutes = rem // 60
            parts = []
            if days:
                parts.append(f"{days}天")
            if hours:
                parts.append(f"{hours}小时")
            parts.append(f"{minutes}分钟")
            return "".join(parts)
        except Exception:
            return "未知"

    def _fmt_gib(self, value):
        return f"{value / (1024 ** 3):.1f}GB"

    def get_system_status(self):
        cpu = self._get_cpu_usage_percent()
        temp = self._get_cpu_temp_c()
        mem = self._get_memory_info()
        disk = shutil.disk_usage("/")
        load = None
        try:
            load = os.getloadavg()
        except Exception:
            pass

        items = []
        if temp is not None:
            items.append(f"温度 {temp:.1f}℃")
        if cpu is not None:
            items.append(f"CPU 使用率 {cpu:.0f}%")
        if mem:
            total, used, percent = mem
            items.append(f"内存 {self._fmt_gib(used)}/{self._fmt_gib(total)}（{percent:.0f}%）")
        items.append(
            f"磁盘 {self._fmt_gib(disk.used)}/{self._fmt_gib(disk.total)}"
            f"（{disk.used / disk.total * 100:.0f}%）"
        )
        if load:
            items.append(f"负载 {load[0]:.2f}, {load[1]:.2f}, {load[2]:.2f}")
        items.append(f"已运行 {self._get_uptime_text()}")
        return "系统状态：" + "；".join(items) + "。"

    def execute_action(self, action_data):
        if not isinstance(action_data, dict):
            return "INVALID_ACTION"
        raw = (action_data.get("action") or "").lower().strip()
        value = action_data.get("value") or action_data.get("query") or action_data.get("prompt")

        ALIASES = {
            "look": "capture_image", "see": "capture_image",
            "check_time": "get_time", "draw": "generate_image", "paint": "generate_image",
            "status": "get_system_status", "system_status": "get_system_status",
            "system": "get_system_status", "health": "get_system_status",
            "speak": "say", "talk": "say", "reply": "say",
            "volume": "set_volume", "adjust_volume": "set_volume",
            "set_vol": "set_volume", "change_volume": "set_volume",
            "print_photo": "print", "print_text": "print", "print_history": "print",
            "open_game": "start_game", "play_game": "start_game", "launch_game": "start_game",
            "game_start": "start_game", "list_roms": "list_games", "games": "list_games",
            "game_list": "list_games", "pause": "pause_game", "game_pause": "pause_game",
            "resume": "resume_game", "continue_game": "resume_game", "game_resume": "resume_game",
            "quit_game": "stop_game", "exit_game": "stop_game", "close_game": "stop_game",
            "game_stop": "stop_game",
            "music": "play_music", "play_song": "play_music", "song": "play_music",
            "audio": "play_music", "play_audio": "play_music", "list_songs": "list_music",
            "songs": "list_music", "music_list": "list_music",
            "video": "play_video", "movie": "play_video", "play_movie": "play_video",
            "media": "play_media",
            "list_video": "list_videos", "video_list": "list_videos", "movies": "list_videos",
            "stop_music": "stop_media", "stop_video": "stop_media", "stop_playback": "stop_media",
            "media_stop": "stop_media",
            "打印": "print",
            "read_screen": "read_screen", "screen": "read_screen", "screenshot": "read_screen",
            "recognize_screen": "read_screen", "看屏幕": "read_screen", "识别屏幕": "read_screen",
            "hide": "hide", "minimize": "hide", "隐身": "hide",
            "unhide": "unhide", "show": "unhide", "restore": "unhide",
            "取消隐身": "unhide", "现身": "unhide",
            "wait_wake": "enter_wait_wake", "go_idle": "enter_wait_wake",
            "standby": "enter_wait_wake", "sleep": "enter_wait_wake",
            "退下": "enter_wait_wake", "待命": "enter_wait_wake",
        }
        action = ALIASES.get(raw, raw)
        log(f"[ACTION] {raw} -> {action}")

        if action == "get_time":
            now = datetime.datetime.now()
            weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
            wd = weekdays[now.weekday()]
            return f"现在是 {now.year}年{now.month}月{now.day}日 {wd} {now.strftime('%H:%M')}。"

        if action == "get_system_status":
            return self.get_system_status()

        if action == "say":
            text = (action_data.get("text") or action_data.get("content")
                    or action_data.get("message") or action_data.get("joke")
                    or value or "")
            return f"SAY_TEXT::{text}"

        if action == "set_volume":
            return self._set_volume_action(action_data)

        if action == "list_games":
            return "GAME_LIST"

        if action == "start_game":
            game = (action_data.get("game") or action_data.get("name")
                    or action_data.get("rom") or value or "")
            return f"GAME_START::{game}"

        if action == "pause_game":
            return "GAME_PAUSE"

        if action == "resume_game":
            return "GAME_RESUME"

        if action == "stop_game":
            return "GAME_STOP"

        if action == "list_music":
            return "MUSIC_LIST"

        if action == "list_videos":
            return "VIDEO_LIST"

        if action == "play_music":
            name = (action_data.get("name") or action_data.get("music")
                    or action_data.get("song") or action_data.get("file") or value or "")
            return f"MEDIA_PLAY::music::{name}"

        if action == "play_video":
            name = (action_data.get("name") or action_data.get("video")
                    or action_data.get("movie") or action_data.get("file") or value or "")
            return f"MEDIA_PLAY::video::{name}"

        if action == "play_media":
            kind_raw = str(action_data.get("kind") or action_data.get("type") or "").lower()
            kind = "video" if any(w in kind_raw for w in ("video", "movie", "视频")) else "music"
            name = (action_data.get("name") or action_data.get("file")
                    or action_data.get("media") or value or "")
            return f"MEDIA_PLAY::{kind}::{name}"

        if action == "stop_media":
            return "MEDIA_STOP"

        if action == "read_screen":
            return "READ_SCREEN"

        if action == "hide":
            return "HIDE_BMO"

        if action == "unhide":
            return "UNHIDE_BMO"

        if action == "enter_wait_wake":
            return "ENTER_WAIT_WAKE"

        if action == "print":
            content = action_data.get("content") or action_data.get("text")
            target = (action_data.get("target") or action_data.get("what")
                      or action_data.get("type") or "")
            target = str(target).lower()
            if content:
                return f"PRINT_TEXT::{content}"
            if any(w in target for w in ("time", "时间", "几点", "日期", "date")):
                return "PRINT_TIME"
            if any(w in target for w in ("status", "状态", "系统", "温度", "cpu")):
                return "PRINT_STATUS"
            if any(w in target for w in ("generated", "last", "刚", "画", "生成", "上一", "图")):
                return "PRINT_LAST_IMAGE"
            if any(w in target for w in ("photo", "camera", "照", "拍", "相")):
                return "PRINT_PHOTO"
            if any(w in target for w in ("history", "chat", "对话", "历史", "记录", "聊天")):
                cnt = (action_data.get("count") or action_data.get("n")
                       or action_data.get("num") or action_data.get("lines")
                       or action_data.get("turns") or action_data.get("value"))
                try:
                    cnt = int(cnt)
                except (TypeError, ValueError):
                    cnt = 0
                return f"PRINT_HISTORY::{cnt}"
            val = action_data.get("value")
            if val:
                return f"PRINT_TEXT::{val}"
            return "PRINT_EMPTY"

        if action == "search_web":
            return f"SEARCH_WEB::{value or ''}"

        if action in ("google", "browser", "news", "search"):
            return f"SEARCH_WEB::{value or ''}"

        if action == "capture_image":
            return "IMAGE_CAPTURE_TRIGGERED"

        if action == "generate_image":
            if not value:
                return "IMAGE_GEN_EMPTY"
            return f"IMAGE_GEN_TRIGGERED::{value}"

        return "INVALID_ACTION"

    MIN_VOLUME = 10   # 语音调音量的下限：不支持静音

    def _set_volume_action(self, data: dict) -> str:
        """语音调音量。支持：绝对值(value/percent/level=数字)、相对("+20"/"-20"/delta/
        direction)、关键词(max/大一点/小一点)。改 config.volume_percent(软件增益)。
        不支持静音，最低 MIN_VOLUME%。"""
        cur = int(float(self.config.get("volume_percent", 100)))
        step = 20
        target = None

        def kw(s: str):
            low = s.strip().lower()
            if any(w in low for w in ("max", "最大", "最响", "最高")):
                return 200
            if any(w in low for w in ("+", "up", "louder", "increase", "大", "高", "响")):
                return cur + step
            if any(w in low for w in ("-", "down", "quieter", "decrease", "小", "低", "轻")):
                return cur - step
            return None

        # 1) value / percent / level / volume：可能是数字、"50"、"+20"、"max"、"大一点"
        for k in ("percent", "level", "value", "volume"):
            v = data.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                target = int(v)
                break
            if isinstance(v, str) and v.strip():
                s = v.strip().replace("%", "").replace("％", "")
                if s.lstrip("+-").isdigit():
                    target = cur + int(s) if s[0] in "+-" else int(s)
                else:
                    target = kw(v)
                if target is not None:
                    break
        # 2) delta 相对增量
        if target is None and isinstance(data.get("delta"), (int, float)):
            target = cur + int(data["delta"])
        # 3) direction 方向词
        if target is None and data.get("direction"):
            target = kw(str(data["direction"]))

        if target is None:
            return "VOLUME_SET::你想把音量调到多大呀？比如说“大一点”，或者“音量调到 50”。"

        target = max(self.MIN_VOLUME, min(200, int(target)))
        self.config["volume_percent"] = target
        save_config(self.config)
        log(f"[VOLUME] {cur}% -> {target}%")
        return f"VOLUME_SET::好的，音量调到 {target}% 啦。"

    # -------------------------------------------------------------------
    # 打印（热敏打印机）
    # -------------------------------------------------------------------
    def _get_printer(self):
        if self.printer is None:
            from providers.printer import ThermalPrinter
            self.printer = ThermalPrinter(self.config.get("printer", {}))
        return self.printer

    def _print_text(self, text: str) -> bool:
        try:
            p = self._get_printer()
            p.text(text)
            p.feed(3)
            log(f"[PRINT] 文字 {len(text)} 字")
            return True
        except Exception as e:
            log(f"[PRINT ERROR] {e}")
            return False

    def _print_image(self, path: str) -> bool:
        try:
            p = self._get_printer()
            p.image(path)
            p.feed(3)
            log(f"[PRINT] 图片 {path}")
            return True
        except Exception as e:
            log(f"[PRINT ERROR] {e}")
            return False

    def _print_history(self, count=None) -> bool:
        """count = 要打印的轮数（一轮 = 一问一答 = 2 行）。省略则按 config.history_turns。"""
        msgs = [m for m in self.session_memory
                if m.get("role") in ("user", "assistant") and m.get("content")]
        turns = count if (count and count > 0) else int(
            self.config.get("printer", {}).get("history_turns", 10))
        msgs = msgs[-turns * 2:]                        # 按轮数取（每轮 2 条消息）
        if not msgs:
            return self._print_text("（还没有对话记录）")
        lines = ["==== BMO 对话记录 ====",
                 datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), ""]
        for m in msgs:
            who = "你" if m["role"] == "user" else "BMO"
            lines.append(f"{who}: {m['content']}")
        return self._print_text("\n".join(lines))

    # -------------------------------------------------------------------
    # 主对话
    # -------------------------------------------------------------------
    def chat_and_respond(self, text, img_path=None):
        # 刚画完图问过"要不要打印"，这一句优先当作回答处理（一次性）
        if self.pending_print and not img_path:
            img = self.pending_print
            self.pending_print = None
            low = text.strip().lower()
            if any(k in low for k in ("不", "别", "算了", "no", "先不", "暂时")):
                self._say("好的，那就先不打印啦～")
                return
            if any(k in low for k in ("要", "好", "打印", "是", "嗯", "可以", "行",
                                      "print", "yes", "ok", "打")):
                self.set_state(BotStates.THINKING, "打印图片中...")
                ok = bool(img) and os.path.exists(img) and self._print_image(img)
                self._say("图片打印好啦！" if ok else "打印图片没成功，检查下打印机哦。")
                return
            # 既不像"要"也不像"不要" → 当普通对话继续（图仍可之后让它打印）

        # 清空记忆指令
        if any(kw in text for kw in ["忘记一切", "清空记忆", "forget everything", "reset memory"]):
            self.session_memory = []
            self.permanent_memory = [{"role": "system", "content": self.build_system_prompt()}]
            self.save_chat_history()
            with self.tts_queue_lock:
                self.tts_queue.append("好的，记忆清空啦。")
            self.set_state(BotStates.IDLE, "记忆已清空")
            return

        # 看图(拍照)走独立干净流程：不进工具检测，绝不念 JSON
        if img_path:
            self._respond_with_image(text, img_path)
            return

        self.set_state(BotStates.THINKING, "思考中...")
        # 思考状态词：超过 10 秒还没有回复时才读，快速回复时保持安静。
        self._schedule_thinking_cue()
        messages = self.permanent_memory + self.session_memory + [
            {"role": "user", "content": text}
        ]

        full_buf = ""
        sentence_buf = ""
        sentence_re = re.compile(r'[。！？.!?\n]')
        is_action = False
        allow_tools = True

        try:
            for chunk in self.llm.chat_stream(messages):
                if self.interrupted.is_set():
                    break
                full_buf += chunk
                # 工具调用检测：对象或数组 JSON 都立刻停止朗读
                if allow_tools and not is_action and ("{" in full_buf or full_buf.lstrip().startswith("[")):
                    is_action = True
                    continue
                if is_action:
                    continue

                if self.current_state != BotStates.SPEAKING:
                    self._cancel_thinking_cue()
                    self.set_state(BotStates.SPEAKING, "说话中...", overlay_path=img_path)
                    self.append_to_text("BMO: ", newline=False)
                self._stream_to_text(chunk)

                sentence_buf += chunk
                if sentence_re.search(chunk):
                    clean = sentence_buf.strip()
                    if clean:
                        with self.tts_queue_lock:
                            self.tts_queue.append(clean)
                    sentence_buf = ""

            if self.rewake_event.is_set():
                with self.tts_queue_lock:
                    self.tts_queue.clear()
                return

            # 收尾：剩余的最后一段
            if not is_action and sentence_buf.strip():
                with self.tts_queue_lock:
                    self.tts_queue.append(sentence_buf.strip())

            # 工具分支
            if is_action:
                action_data = self.extract_json_from_text(full_buf)
                if action_data:
                    result = self.execute_actions(action_data)
                    self.handle_action_results(result, text, img_path)
                    return

            # 保存对话历史
            if not is_action and full_buf.strip():
                log(f"[BMO] {full_buf.strip()}")
            self.session_memory.append({"role": "user", "content": text})
            self.session_memory.append({"role": "assistant", "content": full_buf})
            self.trim_memory()
            self.save_chat_history()

            self.wait_for_tts()
            if not self.rewake_event.is_set():
                self.set_state(BotStates.IDLE, "准备好啦")

        except Exception as e:
            log(f"[LLM ERROR] {e}")
            err = f"出错了：{str(e)[:60]}"
            self.append_to_text(err)
            with self.tts_queue_lock:
                self.tts_queue.append("呃，我连不上服务器。")
            self.set_state(BotStates.ERROR, err)

    def _respond_with_image(self, text, img_path):
        """看图：Vision 描述 → 干净的 LLM 转述（不带工具，绝不出 JSON）。"""
        self.set_state(BotStates.THINKING, "看看是什么...", overlay_path=img_path)
        try:
            old_model = self.config.get("vision", {}).get("model")
            desc = self.vision.describe(img_path, text or "请描述你看到的画面。")
            if self.vision.model and self.vision.model != old_model:
                self.config.setdefault("vision", {})["model"] = self.vision.model
                save_config(self.config)
                log(f"[VISION] 已保存可用模型: {self.vision.model}")
            log(f"[VISION] {desc}")
        except Exception as e:
            log(f"[VISION ERROR] {e}")
            self._say("我拍好照片了，但是看不太清呢。", overlay_path=img_path)
            return

        # 视觉返回太短/像乱码 = 多半是黑图/拍糊了
        if not desc or len(desc.strip()) < 2:
            self._say("照片好像黑漆漆的，是不是镜头盖没揭、或者光线太暗啦？",
                      overlay_path=img_path)
            return

        msgs = [
            {"role": "system", "content":
                "你是 BMO，可爱的小机器人。根据下面的画面描述，用一两句活泼的话告诉用户你看到了什么。"
                "绝对不要输出 JSON，不要调用任何工具。"},
            {"role": "user", "content": f"用户说：{text}"},
            {"role": "user", "content": f"画面内容：{desc}"},
        ]
        try:
            final = self.llm.chat_once(msgs)
        except Exception as e:
            log(f"[LLM ERROR] {e}")
            final = f"我看到了：{desc}"
        self._say(final, remember=text, overlay_path=img_path)

    def handle_action_results(self, result, original_text, img_path):
        """处理单个或多个工具结果；数组按顺序执行。"""
        if not isinstance(result, list):
            self.handle_action_result(result, original_text, img_path)
            return
        if not result:
            self.handle_action_result("INVALID_ACTION", original_text, img_path)
            return
        log(f"[ACTION] 多项执行: {len(result)}")
        old_suppress = self._suppress_action_memory
        old_spoken = self._action_sequence_spoken
        self._suppress_action_memory = True
        self._action_sequence_spoken = []
        try:
            for item in result:
                if self.exiting or self.rewake_event.is_set():
                    return
                self.handle_action_result(item, original_text, img_path)
        finally:
            spoken = list(self._action_sequence_spoken)
            self._suppress_action_memory = old_suppress
            self._action_sequence_spoken = old_spoken
        if spoken:
            self.session_memory.append({"role": "user", "content": original_text})
            self.session_memory.append({"role": "assistant", "content": "\n".join(spoken)})
            self.trim_memory()
            self.save_chat_history()

    def _handle_search(self, query, original_text):
        """联网搜索：配了博查 key 就真搜并让 LLM 转述；否则用已有知识回答。"""
        if not BochaSearchProvider.is_configured():
            self._answer_from_knowledge(query, original_text)
            return
        self.set_state(BotStates.THINKING, "联网搜索中...")
        try:
            digest = self.search.search_digest(query)
        except Exception as e:
            log(f"[SEARCH ERROR] {e}")
            self._answer_from_knowledge(query, original_text)
            return
        if not digest:
            self._say("我搜了一下，没找到什么有用的信息呢。", remember=original_text)
            return
        log(f"[SEARCH] {query} -> {len(digest)} 字摘要")
        msgs = [
            {"role": "system", "content":
                "你是 BMO，可爱的小机器人。根据下面的联网搜索结果，用一两句话简短、口语化地"
                "回答用户的问题。绝对不要输出 JSON、不要调用工具、不要念出网址。"
                "如果搜索结果不足以回答，就如实说没查到。"},
            {"role": "user", "content": f"用户问：{original_text}"},
            {"role": "user", "content": f"搜索结果：\n{digest}"},
        ]
        try:
            final = self.llm.chat_once(msgs)
        except Exception as e:
            log(f"[LLM ERROR] {e}")
            final = digest.split("\n", 1)[0]
        self._say(final, remember=original_text)

    def _answer_from_knowledge(self, query, original_text):
        """没配搜索 key / 搜索失败时的回落：用模型已有知识回答。"""
        fallback_messages = self.permanent_memory + self.session_memory + [
            {"role": "user", "content": original_text},
            {"role": "user", "content": f"用户想查询：{query}。不要调用任何工具，不要输出 JSON。"
                                        "请直接用你已有的知识回答；如果涉及最新内容，就说明可能不是最新信息。"
                                        "一两句话即可。"}
        ]
        try:
            final_text = self.llm.chat_once(fallback_messages)
        except Exception as e:
            log(f"[LLM ERROR] {e}")
            final_text = "这个我可以按已有知识回答，但可能不是最新信息。"
        self._say(final_text, remember=original_text)

    def handle_action_result(self, result, original_text, img_path):
        """工具执行结果处理。"""
        if isinstance(result, str) and result.startswith("VOLUME_SET::"):
            # 音量已在 execute_action 里改好，这里直接念确认（新音量立即生效）
            self._say(result.split("::", 1)[1], remember=original_text)
            return

        if isinstance(result, str) and result.startswith("SAY_TEXT::"):
            text = result.split("::", 1)[1].strip()
            self._say(text or "好的~", remember=original_text)
            return

        if result == "READ_SCREEN":
            self._read_screen_flow(original_text)
            return

        if result == "HIDE_BMO":
            self._hide_bmo()
            return

        if result == "UNHIDE_BMO":
            self._unhide_bmo()
            return

        if result == "ENTER_WAIT_WAKE":
            self._say("好的，我先退下了。", remember=original_text)
            self.abort_to_wake.set()
            self.ptt_event.clear()
            self.recording_active.clear()
            self.set_state(BotStates.IDLE, "等待唤醒...")
            return

        if result == "GAME_LIST":
            self._say(self.list_games_text(), remember=original_text)
            return

        if isinstance(result, str) and result.startswith("GAME_START::"):
            game = result.split("::", 1)[1].strip()
            rom_path, err = self._find_rom(game)
            if err:
                self._say(err, remember=original_text)
                return
            _, err = self._build_game_command(rom_path)
            if err:
                self._say(err, remember=original_text)
                return
            self._say(f"好的，打开 {self._speakable_game_name(os.path.basename(rom_path))}。", remember=original_text)
            ok, msg = self.start_game(game)
            if not ok:
                self._say(msg)
            return

        if result == "GAME_PAUSE":
            _, msg = self.pause_game(for_bmo=True, show_bmo=False)
            self._say(msg, remember=original_text)
            return

        if result == "GAME_RESUME":
            if not self._game_is_running():
                self._say("现在没有正在运行的游戏。", remember=original_text)
                return
            self._say("好的，继续游戏。", remember=original_text)
            ok, msg = self.resume_game()
            if not ok:
                self._say(msg)
            return

        if result == "GAME_STOP":
            _, msg = self.stop_game(restore_bmo=True)
            self._say(msg, remember=original_text)
            return

        if result == "MUSIC_LIST":
            self._say(self.list_media_text("music"), remember=original_text)
            return

        if result == "VIDEO_LIST":
            self._say(self.list_media_text("video"), remember=original_text)
            return

        if isinstance(result, str) and result.startswith("MEDIA_PLAY::"):
            _, kind, name = result.split("::", 2)
            log(f"[MEDIA] 请求: kind={kind} name={name!r}")
            path, err = self._find_media(kind, name)
            if err:
                self._say(err, remember=original_text)
                return
            if not self._media_command_candidates(kind, path):
                self._say("没找到可用播放器。请安装 mpv、vlc 或 ffmpeg。", remember=original_text)
                return
            label = "视频" if kind == "video" else "音乐"
            self._say(f"好的，播放{label} {self._speakable_media_name(os.path.basename(path))}。", remember=original_text)
            ok, msg = self.play_media(kind, name)
            if not ok:
                self._say(msg)
            return

        if result == "MEDIA_STOP":
            _, msg = self.stop_media(restore_bmo=True)
            self._say(msg, remember=original_text)
            return

        if result == "PRINT_PHOTO":
            self.set_state(BotStates.CAPTURING, "拍照打印中...")
            img = self.capture_image()
            ok = bool(img) and self._print_image(img)
            self._say("照片打印好啦！" if ok else "打印照片没成功，检查下打印机连接哦。",
                      remember=original_text)
            return

        if result in ("PRINT_TIME", "PRINT_STATUS"):
            txt = (self.execute_action({"action": "get_time"})
                   if result == "PRINT_TIME" else self.get_system_status())
            self.set_state(BotStates.THINKING, "打印中...")
            ok = self._print_text(txt)
            self._say("好的，打印好啦！" if ok else "打印没成功，检查下打印机哦。",
                      remember=original_text)
            return

        if result == "PRINT_LAST_IMAGE":
            img = self.last_image_for_print
            if not img or not os.path.exists(img):
                self._say("我还没有刚画好的图片呢，先让我画一个吧～", remember=original_text)
                return
            self.set_state(BotStates.THINKING, "打印图片中...")
            ok = self._print_image(img)
            self._say("图片打印好啦！" if ok else "打印图片没成功，检查下打印机哦。",
                      remember=original_text)
            return

        if isinstance(result, str) and result.startswith("PRINT_HISTORY"):
            cnt = 0
            if "::" in result:
                try:
                    cnt = int(result.split("::", 1)[1])
                except ValueError:
                    cnt = 0
            self.set_state(BotStates.THINKING, "打印对话中...")
            ok = self._print_history(cnt if cnt > 0 else None)
            self._say("对话记录打印好啦！" if ok else "打印对话没成功，检查下打印机哦。",
                      remember=original_text)
            return

        if isinstance(result, str) and result.startswith("PRINT_TEXT::"):
            content = result.split("::", 1)[1]
            self.set_state(BotStates.THINKING, "打印中...")
            ok = self._print_text(content)
            self._say("打印好啦！" if ok else "打印没成功，检查下打印机连接哦。",
                      remember=original_text)
            return

        if result == "PRINT_EMPTY":
            self._say("你要打印什么呀？照片、对话记录，还是一段文字？", remember=original_text)
            return

        if result == "IMAGE_CAPTURE_TRIGGERED":
            new_img = self.capture_image()
            if new_img:
                # 拍完照重新跟 LLM 对话
                self.chat_and_respond(original_text, img_path=new_img)
            else:
                self._say("拍照失败了。")
            return

        if isinstance(result, str) and result.startswith("IMAGE_GEN_TRIGGERED::"):
            prompt = result.split("::", 1)[1]
            self.set_state(BotStates.THINKING, f"画画中: {prompt[:20]}")
            try:
                path = self.image_gen.generate(prompt)
                if path:
                    log(f"[IMAGE GEN] 保存到 {path}")
                    self.last_image_for_print = path   # 记住，供"打印刚画的图"用
                    self.set_state(BotStates.SPEAKING, "看我画的~", overlay_path=path)
                    printer_on = self.config.get("printer", {}).get("enabled", True)
                    wants_print = any(k in original_text for k in
                                      ("打印", "打出来", "打一份", "打出", "print"))
                    if printer_on and wants_print:
                        # 原话已经说了"画完打印" → 不问，直接打
                        self.speak_text("画好啦，这就打印出来！")
                        self.wait_for_tts()
                        self.set_state(BotStates.THINKING, "打印图片中...")
                        ok = self._print_image(path)
                        self._say("打印好啦！" if ok else "打印没成功，检查下打印机哦。")
                        return
                    if printer_on:
                        # 没明说要打印 → 问一句，下一句"要/不要"由 chat_and_respond 处理
                        self.pending_print = path
                        self.speak_text("画好啦！要不要我打印出来呀？")
                        self.wait_for_tts()
                    else:
                        self.speak_text("画好啦！")
                        self.wait_for_tts()
                        time.sleep(self.config.get("image_display_seconds", 10))
                else:
                    self._say("画图失败了。")
                    return
            except Exception as e:
                log(f"[IMAGE GEN ERROR] {e}")
                self._say("画图时出错了。")
                return
            self.set_state(BotStates.IDLE, "准备好啦")
            return

        if result == "IMAGE_GEN_EMPTY":
            self._say("画什么呀？再告诉我一次。")
            return

        if result == "INVALID_ACTION":
            self._say("我不太确定怎么做这件事。")
            return

        # 联网搜索：配了博查 key 就真搜，否则回落到用模型已有知识回答。
        # SEARCH_DISABLED 是旧哨兵，一并兼容。
        if isinstance(result, str) and (
                result.startswith("SEARCH_WEB::") or result.startswith("SEARCH_DISABLED::")):
            query = result.split("::", 1)[1].strip() or original_text
            self._handle_search(query, original_text)
            return

        # get_time / get_system_status 返回了字符串：
        # 若用户本意是"把它打印出来"，就直接打印（支持"把时间打印出来"这类一句里连用两个能力）
        if isinstance(result, str) and result and any(
                k in original_text for k in ("打印", "打出来", "打一份", "打出", "print")):
            self.set_state(BotStates.THINKING, "打印中...")
            ok = self._print_text(result)
            self._say("好的，打印好啦！" if ok else "打印没成功，检查下打印机哦。",
                      remember=original_text)
            return

        # 否则让 LLM 把工具结果总结成一句话念出来
        if isinstance(result, str) and result:
            # 用干净的 system prompt，避免又触发工具调用
            summary_messages = [
                {"role": "system", "content":
                    "你是 BMO，可爱的小机器人。根据下面的工具结果，用一两句话自然、"
                    "简短地回答用户。绝对不要输出 JSON，不要再调用任何工具。"},
                {"role": "user", "content": f"我问的是：{original_text}"},
                {"role": "user", "content": f"工具结果：{result}"},
            ]
            try:
                final_text = self.llm.chat_once(summary_messages)
            except Exception as e:
                log(f"[LLM ERROR] {e}")
                final_text = result
            # 安全网：万一还是冒出 JSON，剥掉
            final_text = re.sub(r'\{.*?\}', '', final_text, flags=re.DOTALL).strip()
            if not final_text:
                final_text = result
            self.set_state(BotStates.SPEAKING, "说话中...", overlay_path=img_path)
            self.append_to_text(f"BMO: {final_text}")
            log(f"[BMO] {final_text}")
            self.speak_text(final_text)
            self.session_memory.append({"role": "user", "content": original_text})
            self.session_memory.append({"role": "assistant", "content": final_text})
            self.trim_memory()
            self.save_chat_history()
            self.wait_for_tts()
            self.set_state(BotStates.IDLE, "准备好啦")
            return

    def _say(self, text, remember=None, overlay_path=None):
        """说一句话并等播放完，再回 IDLE。remember 不为空时把这轮对话存进记忆。"""
        # 安全网：绝不朗读 JSON
        text = re.sub(r'\{.*?\}', '', text, flags=re.DOTALL).strip()
        if not text:
            text = "好的~"
        self.set_state(BotStates.SPEAKING, "说话中...", overlay_path=overlay_path)
        self.append_to_text(f"BMO: {text}")
        log(f"[BMO] {text}")
        self.speak_text(text)
        if remember:
            if self._suppress_action_memory:
                self._action_sequence_spoken.append(text)
            else:
                self.session_memory.append({"role": "user", "content": remember})
                self.session_memory.append({"role": "assistant", "content": text})
                self.trim_memory()
                self.save_chat_history()
        self.wait_for_tts()
        if not self.rewake_event.is_set():
            self.set_state(BotStates.IDLE, "准备好啦")

    def speak_text(self, text):
        with self.tts_queue_lock:
            self.tts_queue.append(text)

    def wait_for_tts(self):
        while True:
            with self.tts_queue_lock:
                empty = not self.tts_queue
            if empty and not self.tts_active.is_set():
                return
            if self.interrupted.is_set() or self.exiting:
                return
            time.sleep(0.1)

    # -------------------------------------------------------------------
    # TTS 后台 worker
    # -------------------------------------------------------------------
    def _tts_worker(self):
        while not self.exiting:
            text = None
            with self.tts_queue_lock:
                if self.tts_queue:
                    text = self.tts_queue.pop(0)
                    self.tts_active.set()
            if text:
                monitor = self._start_speaking_wake_monitor(text)
                try:
                    self.speak(text)
                finally:
                    self._stop_speaking_wake_monitor(monitor)
                with self.tts_queue_lock:
                    if not self.tts_queue:
                        self.tts_active.clear()
            else:
                time.sleep(0.05)

    # -------------------------------------------------------------------
    # 状态语音提示（greeting / ack / thinking / error）。
    # 字段没写时用默认词；字段写成空串时，该状态静默。
    # -------------------------------------------------------------------
    # 状态词默认值：config 里没写该字段时用这些（写了空串 = 该状态静默）
    _STATUS_DEFAULTS = {
        "greeting": "你好，我叫 BMO",
        "ack": "我在",
        "thinking": "让我想想",
        "error": "哎呀，我出错了",
    }

    def _status_text(self, kind: str) -> str:
        ss = self.config.get("status_speech", {})
        if kind in ss:
            return (ss.get(kind) or "").strip()
        return self._STATUS_DEFAULTS.get(kind, "")

    def _play_status_cue(self, kind):
        """同步读出状态词（greeting/ack/error）。greeting/error 由调用方放到线程里。"""
        text = self._status_text(kind)
        if text:
            self.speak(text)

    def _schedule_thinking_cue(self, delay=10.0):
        """超过 delay 秒还没开始回复时，才播放"思考中"状态词。"""
        text = self._status_text("thinking")
        if not text:
            return
        with self.thinking_cue_lock:
            self.thinking_cue_token += 1
            token = self.thinking_cue_token

        def _delayed():
            time.sleep(delay)
            if self.exiting or self.interrupted.is_set():
                return
            with self.thinking_cue_lock:
                if token != self.thinking_cue_token:
                    return
            if self.current_state != BotStates.THINKING:
                return
            with self.tts_queue_lock:
                self.tts_queue.append(text)

        threading.Thread(target=_delayed, daemon=True).start()

    def _cancel_thinking_cue(self):
        with self.thinking_cue_lock:
            self.thinking_cue_token += 1

    def speak(self, text):
        clean = text.strip()
        if not clean:
            return
        # 没有可朗读字符（字母/数字/汉字）就跳过，避免对 "{" 这种合成报错
        if not re.search(r'[0-9A-Za-z一-鿿]', clean):
            return
        log(f"[说] {clean}")
        provider = self.config["tts"].get("provider", "edge")
        try:
            if provider in ("piper", "local"):
                self._speak_piper(clean)
            elif provider in ("doubao", "volcengine", "seed"):
                self._speak_doubao(clean)
            elif provider == "edge":
                self._speak_edge(clean)
            else:
                self._speak_siliconflow(clean)
        except Exception as e:
            log(f"[TTS] {provider} 失败: {e}, 尝试兜底")
            try:
                self._speak_edge(clean)
            except Exception as e2:
                log(f"[TTS] 兜底也失败: {e2}")

    def _detect_usb_playback(self):
        """解析 aplay -l，找 USB 播放设备，返回 plughw:卡号,0。找不到返回 ''。"""
        try:
            out = subprocess.check_output(["aplay", "-l"], text=True,
                                          stderr=subprocess.DEVNULL)
        except Exception:
            return ""
        non_hdmi = ""
        for line in out.splitlines():
            m = re.match(r"\s*card (\d+): (\S+) \[(.*?)\]", line)
            if not m:
                continue
            card = int(m.group(1))
            tag = (m.group(2) + " " + m.group(3)).lower()
            is_hdmi = ("hdmi" in tag) or ("vc4" in tag)
            if "usb" in tag:
                return f"plughw:{card},0"
            if not is_hdmi and not non_hdmi:
                non_hdmi = f"plughw:{card},0"
        return non_hdmi

    def _ensure_pipewire_usb_sink(self):
        """把 PipeWire 默认输出 sink 设成 USB 音箱，让游戏/媒体等'走系统默认'的外部
        程序也从 USB 出声。trixie 上一般没 pactl，优先用 wpctl(新版 PipeWire 自带)，
        pactl 兜底。best-effort，失败静默。"""
        if self._set_default_sink_wpctl():
            return
        self._set_default_sink_pactl()

    @staticmethod
    def _parse_wpctl_usb_sink_id(status_out: str):
        """从 `wpctl status` 输出里找名字含 USB 的 sink id（只在 Sinks 段内找，
        遇到 Sources 段即停，避免误选 USB 麦克风）。找不到返回 None。"""
        in_sinks = False
        for line in status_out.splitlines():
            if re.search(r"\bSinks:", line):
                in_sinks = True
                continue
            if in_sinks:
                if re.search(r"\b(Sources|Filters|Streams|Devices)\b\s*:", line):
                    break
                m = re.search(r"(\d+)\.\s+(.+)", line)
                if m and "usb" in m.group(2).lower():
                    return m.group(1)
        return None

    def _set_default_sink_wpctl(self) -> bool:
        wpctl = shutil.which("wpctl")
        if not wpctl:
            return False
        try:
            out = subprocess.check_output([wpctl, "status"], text=True,
                                          timeout=5, stderr=subprocess.DEVNULL)
        except Exception:
            return False
        usb_id = self._parse_wpctl_usb_sink_id(out)
        if not usb_id:
            return False
        try:
            subprocess.run([wpctl, "set-default", usb_id],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            log(f"[AUDIO] wpctl 已把默认输出设为 USB sink (id={usb_id})")
            return True
        except Exception:
            return False

    def _set_default_sink_pactl(self):
        pactl = shutil.which("pactl")
        if not pactl:
            return
        try:
            out = subprocess.check_output([pactl, "list", "short", "sinks"],
                                          text=True, timeout=5, stderr=subprocess.DEVNULL)
        except Exception:
            return
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and "usb" in parts[1].lower():
                try:
                    subprocess.run([pactl, "set-default-sink", parts[1]],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                    log(f"[AUDIO] pactl 已把默认输出设为 USB 音箱: {parts[1]}")
                except Exception:
                    pass
                return

    def _resolve_output_device(self):
        """default/空/pipewire → 走系统默认(PipeWire)，不加 -D，能和游戏共用音箱；
        auto → 自动检测 USB plughw；显式值 → 直接用。结果缓存。"""
        raw = self.config.get("audio_output_device")
        dev = (raw if raw is not None else "auto").strip()
        if dev.lower() in ("", "default", "pipewire", "pulse", "system"):
            return ""   # aplay/mpg123 不带 -D，交给系统默认（PipeWire 混音，不抢设备）
        if dev.lower() != "auto":
            return dev
        if getattr(self, "_resolved_output", None) is None:
            self._resolved_output = self._detect_usb_playback()
            if self._resolved_output:
                log(f"[AUDIO] 自动选用输出设备: {self._resolved_output}")
            else:
                log("[AUDIO] 未检测到 USB 音响，用系统默认输出")
        return self._resolved_output

    def _gain(self):
        """软件音量增益（来自 config.volume_percent，默认100）。
        100 = 原始音量(直通)；<100 衰减；>100 放大（USB 音箱本身偏小时用来提音量）。
        上限 3.0(=300%) 防止过度削波；超过后多半失真。"""
        try:
            return max(0.0, min(3.0, float(self.config.get("volume_percent", 100)) / 100.0))
        except Exception:
            return 1.0

    def _amixer_ctrl_for(self, card):
        """找声卡的播放音量控件名，结果缓存（避免每句都扫一次）。"""
        cache = getattr(self, "_amixer_ctrl_cache", None)
        if cache is None:
            cache = self._amixer_ctrl_cache = {}
        if card in cache:
            return cache[card]
        ctrl = None
        try:
            out = subprocess.check_output(["amixer", "-c", str(card), "scontrols"],
                                          text=True, stderr=subprocess.DEVNULL, timeout=5)
            names = re.findall(r"'([^']+)'", out)
            for pref in ("Master", "PCM", "Speaker", "Headphone", "Playback"):
                if pref in names:
                    ctrl = pref
                    break
            if not ctrl and names:
                ctrl = names[0]
        except Exception:
            ctrl = None
        cache[card] = ctrl
        return ctrl

    def _pin_hw_volume(self):
        """每次播放前把输出声卡硬件音量顶到 100% 并取消静音。
        PipeWire/WirePlumber 常在每段播放结束后把硬件音量复位（表现为"这句大、
        下一句又变小"），所以每句都重新顶一次；用户音量统一交给软件增益 _gain()。
        config.hw_volume_pin=false 可关掉这个行为。"""
        if not self.config.get("hw_volume_pin", True):
            return
        dev = self._resolve_output_device()  # "plughw:N,0" 或 ""
        m = re.search(r"hw:(\d+)", dev or "")
        if not m:
            return
        card = m.group(1)
        ctrl = self._amixer_ctrl_for(card)
        if not ctrl:
            return
        try:
            subprocess.run(["amixer", "-c", card, "sset", ctrl, "100%", "unmute"],
                           capture_output=True, timeout=5)
        except Exception:
            pass

    @staticmethod
    def _wav_header(sr: int, data_size: int, channels: int = 1, bits: int = 16) -> bytes:
        """生成 WAV 头。流式(长度未知)时 data_size 传一个很大的占位值，播到 stdin 关闭为止。"""
        import struct
        byte_rate = sr * channels * bits // 8
        block_align = channels * bits // 8
        riff_size = 0xFFFFFFFF if data_size >= 0xFFFFFFFF - 44 else data_size + 36
        return (b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
                + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sr, byte_rate, block_align, bits)
                + b"data" + struct.pack("<I", data_size))

    def _spawn_pcm_proc(self, sr: int):
        """开一个吃 16bit 单声道音频(stdin)的播放进程，所有 TTS 引擎统一用它。
        默认/PipeWire 模式 → pw-play 播 WAV（走 PipeWire，和游戏/媒体混音、共用 USB）；
        指定了具体 ALSA 设备(plughw) → aplay -D 吃裸 PCM。
        返回 (proc, wav_mode)；wav_mode=True 时调用方需先写一段 WAV 头。"""
        dev = self._resolve_output_device()
        if not dev and shutil.which("pw-play"):
            proc = subprocess.Popen(["pw-play", "-"], stdin=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            return proc, True
        cmd = ["aplay", "-q", "-f", "S16_LE", "-c", "1", "-r", str(sr)]
        if dev:
            cmd += ["-D", dev]
        cmd += ["-"]
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL), False

    def _play_pcm_aplay(self, pcm: bytes, sr: int):
        """播 16-bit 单声道 PCM（默认走 PipeWire/pw-play，指定设备走 aplay）。应用软件音量。"""
        if not pcm:
            return
        self._pin_hw_volume()
        gain = self._gain()
        if gain != 1.0:
            arr = np.frombuffer(pcm, dtype='<i2').astype(np.float32) * gain
            pcm = np.clip(arr, -32768, 32767).astype('<i2').tobytes()
        proc, wav_mode = self._spawn_pcm_proc(sr)
        try:
            self.current_tts_proc = proc
            if wav_mode:
                proc.stdin.write(self._wav_header(sr, len(pcm)))
            proc.stdin.write(pcm)
            proc.stdin.close()
            while proc.poll() is None:
                if self.interrupted.is_set() or self.exiting:
                    proc.terminate()
                    break
                time.sleep(0.05)
        finally:
            if self.current_tts_proc is proc:
                self.current_tts_proc = None

    def _speak_piper(self, text):
        if self.tts_piper is None:
            from providers.tts_piper import PiperTTSProvider
            self.tts_piper = PiperTTSProvider(self.config["tts"])
        pcm, sr = self.tts_piper.synthesize_pcm16(text)
        self._play_pcm_aplay(pcm, sr)

    def _speak_doubao(self, text):
        if self.tts_doubao is None:
            from providers.tts_doubao import DoubaoTTSProvider
            self.tts_doubao = DoubaoTTSProvider(self.config["tts"])
        # 流式：WS 边收边播，走 USB 输出 + 软件增益 + 硬件顶满
        self._play_pcm_stream_aplay(
            self.tts_doubao.synthesize_pcm_stream(text), self.tts_doubao.rate,
        )

    def _play_pcm_stream_aplay(self, chunks, sr: int):
        """把一串 PCM 帧边收边写进播放器（流式）。应用软件音量、路由到音箱。"""
        self._pin_hw_volume()
        gain = self._gain()
        proc, wav_mode = self._spawn_pcm_proc(sr)
        carry = b""   # 应用增益时需要偶数字节对齐，落单的半个采样留到下一帧
        try:
            self.current_tts_proc = proc
            if wav_mode:
                # 流式长度未知，用占位大小的 WAV 头，播到 stdin 关闭为止
                proc.stdin.write(self._wav_header(sr, 0xFFFFFFFF - 44))
            for chunk in chunks:
                if self.interrupted.is_set() or self.exiting:
                    break
                if not chunk:
                    continue
                if gain != 1.0:
                    buf = carry + chunk
                    if len(buf) % 2:
                        carry = buf[-1:]
                        buf = buf[:-1]
                    else:
                        carry = b""
                    if buf:
                        arr = np.frombuffer(buf, dtype='<i2').astype(np.float32) * gain
                        chunk = np.clip(arr, -32768, 32767).astype('<i2').tobytes()
                    else:
                        continue
                try:
                    proc.stdin.write(chunk)
                except (BrokenPipeError, OSError):
                    break
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            while proc.poll() is None:
                if self.interrupted.is_set() or self.exiting:
                    proc.terminate()
                    break
                time.sleep(0.05)
        finally:
            if self.current_tts_proc is proc:
                self.current_tts_proc = None

    def _speak_edge(self, text):
        # Edge-TTS 输出 MP3 → 用 mpg123 解码播放（可指定 USB 音箱）
        mp3 = self.tts_edge.synthesize_mp3(text)
        if not mp3:
            return
        self._pin_hw_volume()
        dev = self._resolve_output_device()
        cmd = ["mpg123", "-q", "-f", str(int(32768 * self._gain()))]  # -f 软件音量
        if dev:
            # 指定了具体 ALSA 设备(plughw:N,0)时，强制用 alsa 输出模块，否则 mpg123
            # 默认模块可能忽略 -a，声音跑回系统默认。
            cmd += ["-o", "alsa", "-a", dev]
        else:
            # 默认模式：走 PipeWire 的 pulse 接口，和游戏/媒体混音、共用 USB 音箱。
            cmd += ["-o", "pulse"]
        cmd += ["-"]
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.current_tts_proc = proc
            proc.stdin.write(mp3)
            proc.stdin.close()
            while proc.poll() is None:
                if self.interrupted.is_set() or self.exiting:
                    proc.terminate()
                    break
                time.sleep(0.05)
        finally:
            if self.current_tts_proc is proc:
                self.current_tts_proc = None

    def _speak_siliconflow(self, text):
        # 走统一的流式播放（默认 pw-play→PipeWire；指定设备→aplay），和 Edge/豆包一致，
        # 避免 PortAudio 在默认模式下绕过 PipeWire 没声。
        sr = self.config.get("tts_sample_rate", 24000)
        self._play_pcm_stream_aplay(self.tts_sf.synthesize_pcm_stream(text), sr)

    # -------------------------------------------------------------------
    # 记忆
    # -------------------------------------------------------------------
    def build_system_prompt(self) -> str:
        # 顺序：性格 → 工具说明 → 附加说明。
        # 工具说明默认用 DEFAULT_TOOLS_PROMPT；用户在网页改过(config.tools_prompt 非空)就用自定义的。
        personality = (self.config.get("system_prompt", "") or "").strip()
        extras = (self.config.get("system_prompt_extras", "") or "").strip()
        tools = (self.config.get("tools_prompt") or "").strip() or DEFAULT_TOOLS_PROMPT
        parts = [personality, tools]
        if extras:
            parts.append(extras)
        return "\n\n".join(p for p in parts if p)

    def load_chat_history(self):
        sysp = {"role": "system", "content": self.build_system_prompt() if hasattr(self, "config") else ""}
        if os.path.exists(MEMORY_FILE):
            try:
                with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data and data[0].get("role") == "system":
                    data[0] = sysp
                    return data
                return [sysp] + data
            except Exception:
                pass
        return [sysp]

    def trim_memory(self):
        if not self.config.get("chat_memory", True):
            self.session_memory = []
            return
        max_turns = self.config.get("memory_max_turns", 30)
        # 1 turn = user + assistant
        max_msgs = max_turns * 2
        if len(self.session_memory) > max_msgs:
            self.session_memory = self.session_memory[-max_msgs:]

    def save_chat_history(self):
        full = self.permanent_memory + self.session_memory
        # 第 0 条是 system；其余的留最近 N 条
        if not full:
            return
        sysp = full[0]
        rest = full[1:]
        max_turns = self.config.get("memory_max_turns", 30)
        if len(rest) > max_turns * 2:
            rest = rest[-max_turns * 2:]
        try:
            _atomic_write_json(MEMORY_FILE, [sysp] + rest, indent=2)
        except Exception as e:
            log(f"[MEM] 保存失败: {e}")

    # -------------------------------------------------------------------
    # Webui 通信：commands.json + state.json
    # -------------------------------------------------------------------
    def poll_commands_file(self):
        """每 500ms 检查一次 commands.json，处理来自 webui 的命令。"""
        try:
            if os.path.exists(COMMANDS_FILE):
                with open(COMMANDS_FILE, "r", encoding="utf-8") as f:
                    cmds = json.load(f)
                os.remove(COMMANDS_FILE)
                for c in cmds:
                    self._handle_webui_command(c)
        except Exception as e:
            log(f"[CMD] 读取命令失败: {e}")
        self.master.after(500, self.poll_commands_file)

    def _handle_webui_command(self, cmd: dict):
        action = cmd.get("action")
        log(f"[CMD] 收到: {action}")
        if action == "ptt_start":
            # 网页"开始录音" = 唤醒（和按钮/语音一致）：按一次即应答+自适应录音
            if self.current_state == BotStates.IDLE or "等" in self.current_status:
                self.ptt_event.set()
        elif action == "ptt_stop":
            self.recording_active.clear()
        elif action == "interrupt":
            self.handle_speaking_interrupt()
        elif action == "capture":
            threading.Thread(target=self._webui_capture_flow, daemon=True).start()
        elif action == "clear_memory":
            self.session_memory = []
            self.permanent_memory = [{"role": "system", "content": self.build_system_prompt()}]
            self.save_chat_history()
            log("[CMD] 记忆已清空")
        elif action == "reload_config":
            try:
                # 重新读 .env，让网页新填的 key/凭证（如火山 appid/token）立即生效，无需重启
                load_dotenv(override=True)
                self.config = load_config()
                # 网页可能改了麦克风输入设备 → 重新解析（唤醒监听下一轮重开流时生效）
                self.input_device = resolve_input_device(self.config.get("input_device"))
                self._log_input_device()
                self.wake_reload_pending = True
                # 重建相关 provider
                endpoints = self.config["providers_endpoints"]
                self.llm = LLMProvider(self.config["llm"], endpoints)
                try:
                    self.stt = create_stt(self.config["stt"], endpoints)
                except Exception as e:
                    log(f"[CMD] STT 重建失败（沿用旧的）: {e}")
                self.tts_edge = EdgeTTSProvider(self.config["tts"])
                self.tts_sf = SiliconFlowTTSProvider(
                    self.config["tts"], endpoints,
                    sample_rate=self.config.get("tts_sample_rate", 24000),
                )
                self.tts_piper = None  # 懒加载，按新 config 下次用时重建
                self.tts_doubao = None  # 同上，换音色/凭证后下次用时重建
                if self.printer:
                    try:
                        self.printer.close()
                    except Exception:
                        pass
                self.printer = None  # 打印机配置可能变（设备/波特率），下次用时重建
                self._resolved_output = None  # 重新检测输出设备
                self.vision = VisionProvider(self.config["vision"], endpoints)
                self.image_gen = ImageGenProvider(
                    self.config["image_gen"], endpoints,
                    output_dir=self.config.get("generated_dir", "generated"),
                )
                self.search = BochaSearchProvider(self.config.get("search", {}))
                # 唤醒词不能在这个线程里重建（监听循环在另一个线程跑，会撞车）。
                # 设个标志；监听线程会立刻退出当前音频流并按新配置重载。
                self.wake_reload_pending = True
                # 更新 system prompt
                if self.permanent_memory:
                    self.permanent_memory[0] = {
                        "role": "system",
                        "content": self.build_system_prompt(),
                    }
                log("[CMD] 配置已重新加载")
            except Exception as e:
                log(f"[CMD] 重载失败: {e}")
        elif action == "speak":
            text = cmd.get("text") or ""
            if text:
                self.speak_text(text)
        elif action == "print":
            # 打印可能很慢（9600 波特），放后台线程，别卡住命令轮询（GUI 线程）
            threading.Thread(target=self._webui_print, args=(cmd,), daemon=True).start()
        elif action == "start_game":
            ok, msg = self.start_game(cmd.get("game") or cmd.get("rom") or "")
            log(f"[WEBUI GAME] start -> {'成功' if ok else '失败'}: {msg}")
        elif action == "pause_game":
            ok, msg = self.pause_game(for_bmo=True, show_bmo=False)
            log(f"[WEBUI GAME] pause -> {'成功' if ok else '失败'}: {msg}")
        elif action == "resume_game":
            ok, msg = self.resume_game()
            log(f"[WEBUI GAME] resume -> {'成功' if ok else '失败'}: {msg}")
        elif action == "stop_game":
            ok, msg = self.stop_game(restore_bmo=True)
            log(f"[WEBUI GAME] stop -> {'成功' if ok else '失败'}: {msg}")
        elif action == "play_media":
            ok, msg = self.play_media(cmd.get("kind") or "music", cmd.get("name") or "")
            log(f"[WEBUI MEDIA] play -> {'成功' if ok else '失败'}: {msg}")
        elif action == "stop_media":
            ok, msg = self.stop_media(restore_bmo=True)
            log(f"[WEBUI MEDIA] stop -> {'成功' if ok else '失败'}: {msg}")
        elif action == "restart_webui":
            self._restart_webui()
        elif action == "restart_agent":
            self._restart_agent()
        elif action == "exit_agent":
            log("[CMD] 网页请求退出 BMO")
            self.safe_exit()

    def _restart_agent(self):
        """重启 BMO：清理子进程后用同一解释器原地 exec agent.py（同 PID）。"""
        global WEBUI_PROC
        log("[CMD] 网页请求重启 BMO...")
        # 先杀掉 webui 子进程，否则重启后新 agent 再 spawn 一个会撞端口
        try:
            if WEBUI_PROC and WEBUI_PROC.poll() is None:
                WEBUI_PROC.terminate()
                try:
                    WEBUI_PROC.wait(timeout=3)
                except Exception:
                    WEBUI_PROC.kill()
        except Exception as e:
            log(f"[CMD] 终止 webui 失败: {e}")
        # 清理：停游戏/媒体/TTS/光标进程，存历史，停音频
        self.exiting = True
        for cleanup in (
            lambda: self.stop_game(restore_bmo=False),
            lambda: self.stop_media(restore_bmo=False),
            lambda: self.current_tts_proc and self.current_tts_proc.terminate(),
            lambda: (self._cursor_hider_proc and self._cursor_hider_proc.poll() is None
                     and self._cursor_hider_proc.terminate()),
            self.save_chat_history,
            sd.stop,
        ):
            try:
                cleanup()
            except Exception:
                pass
        time.sleep(1.0)  # 等端口/音频设备释放，再原地重启
        try:
            os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])
        except Exception as e:
            log(f"[CMD] 重启失败，转为退出: {e}")
            self.safe_exit()

    def _webui_print(self, cmd: dict):
        """网页打印控制：type ∈ {text, history, photo, generated}。"""
        pt = (cmd.get("print_type") or cmd.get("type") or "").lower()
        ok = False
        try:
            if pt == "text":
                ok = self._print_text(cmd.get("content") or "（空内容）")
            elif pt == "history":
                cnt = cmd.get("count")
                ok = self._print_history(int(cnt) if cnt else None)
            elif pt == "photo":
                img = self.capture_image()
                ok = bool(img) and self._print_image(img)
            elif pt == "generated":
                img = self.last_image_for_print
                ok = bool(img) and os.path.exists(img) and self._print_image(img)
        except Exception as e:
            log(f"[WEBUI PRINT ERROR] {e}")
            ok = False
        log(f"[WEBUI PRINT] {pt} -> {'成功' if ok else '失败'}")

    def _restart_webui(self):
        """Kill 当前 webui 子进程并重新 spawn（读最新 config）。"""
        global WEBUI_PROC
        try:
            if WEBUI_PROC and WEBUI_PROC.poll() is None:
                log("[WEBUI] 重启：终止旧进程")
                WEBUI_PROC.terminate()
                try:
                    WEBUI_PROC.wait(timeout=3)
                except Exception:
                    WEBUI_PROC.kill()
        except Exception as e:
            log(f"[WEBUI] 终止失败: {e}")
        # 等 1 秒让端口释放
        time.sleep(1.0)
        WEBUI_PROC = maybe_spawn_webui()

    def _webui_capture_flow(self):
        img = self.capture_image()
        if img:
            self.chat_and_respond("我看到什么了？", img_path=img)

    def update_state_file(self):
        """每 1s 写一次状态到 state.json，让 webui 读取。"""
        try:
            _atomic_write_json(STATE_FILE, {
                "state": self.current_state,
                "status": self.current_status,
                "tts_queue_len": len(self.tts_queue),
                "memory_turns": len(self.session_memory) // 2,
                "game": self._game_state(),
                "media": self._media_state(),
                "timestamp": time.time(),
            })
        except Exception:
            pass
        self.master.after(1000, self.update_state_file)


WEBUI_PROC = None


def maybe_spawn_webui():
    """启动 webui 子进程。Web 控制台强制随 agent 启动。"""
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    try:
        env = os.environ.copy()
        port = str(cfg.get("webui_port", 8087))
        env["WEBUI_PORT"] = port
        # 把 webui 的 stdout/stderr 也写进日志，方便排错
        webui_log = open(os.path.join(LOG_DIR, "webui.log"), "ab")
        proc = subprocess.Popen(
            [sys.executable, "webui.py"],
            stdout=webui_log,
            stderr=webui_log,
            env=env,
        )
        print(f"[INIT] Web 控制台已启动 (PID={proc.pid}, port={port})", flush=True)
        return proc
    except Exception as e:
        print(f"[INIT] Web 控制台启动失败: {e}", flush=True)
        return None


if __name__ == "__main__":
    print("--- BMO 在线版启动 ---", flush=True)
    if not os.getenv("SILICONFLOW_API_KEY"):
        print("[INIT] 未配置 SILICONFLOW_API_KEY；需要硅基流动时可在 Web 控制台 API Key 中填写", flush=True)

    WEBUI_PROC = maybe_spawn_webui()

    def _kill_webui():
        global WEBUI_PROC
        if WEBUI_PROC and WEBUI_PROC.poll() is None:
            try:
                WEBUI_PROC.terminate()
                WEBUI_PROC.wait(timeout=3)
            except Exception:
                try:
                    WEBUI_PROC.kill()
                except Exception:
                    pass
    atexit.register(_kill_webui)

    root = tk.Tk()
    app = BotGUI(root)
    root.mainloop()
