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
LOG_DIR = "logs"

os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"{datetime.date.today().isoformat()}.log")


def log(msg: str):
    """同时打印到终端和写入日志文件。"""
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config() -> dict:
    # config.json 不在 git 里；首次运行从 config.default.json 复制
    if not os.path.exists(CONFIG_FILE) and os.path.exists("config.default.json"):
        import shutil
        shutil.copy("config.default.json", CONFIG_FILE)
        print("[INIT] 已从 config.default.json 创建 config.json", flush=True)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# =========================================================================
# 工具调用说明（硬编码，用户在网页改不到）
# =========================================================================
TOOLS_PROMPT = (
    "你有这几个工具，需要用时整条回复必须是纯 JSON，第一个字符就是 {，"
    "前后绝对不要加任何文字、不要说'让我试试'之类的话：\n"
    "1. 查时间：{\"action\": \"get_time\"}\n"
    "2. 拍照看：{\"action\": \"capture_image\"}\n"
    "3. 画图：{\"action\": \"generate_image\", \"prompt\": \"图片描述\"}\n"
    "4. 查看系统状态/温度/CPU/内存/磁盘：{\"action\": \"get_system_status\"}\n\n"
    "不需要工具时，正常聊天即可。聊天回复尽量短，1~3 句话。"
)


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


# =========================================================================
# 音频工具
# =========================================================================

def resolve_input_device(requested):
    if requested in (None, "", "default"):
        return None
    try:
        devices = sd.query_devices()
    except Exception as e:
        log(f"[AUDIO] 设备查询失败: {e}")
        return None
    if isinstance(requested, int) or (isinstance(requested, str) and str(requested).isdigit()):
        idx = int(requested)
        if 0 <= idx < len(devices):
            return idx
        return None
    req_low = str(requested).lower()
    for idx, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) > 0 and req_low in dev.get("name", "").lower():
            return idx
    return None


def choose_input_samplerate(device, preferred=None) -> int:
    candidates = []
    if preferred:
        candidates.append(preferred)
    try:
        info = sd.query_devices(device)
        if "default_samplerate" in info:
            candidates.append(int(info["default_samplerate"]))
    except Exception:
        pass
    candidates.extend([48000, 44100, 32000, 16000])
    seen = set()
    for rate in candidates:
        if not rate or rate in seen:
            continue
        seen.add(rate)
        try:
            sd.check_input_settings(device=device, samplerate=rate, channels=1, dtype="int16")
            return rate
        except Exception:
            continue
    return int(candidates[0]) if candidates else 44100


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
        master.bind('<Return>', self.handle_ptt_toggle)
        master.bind('<space>', self.handle_speaking_interrupt)
        atexit.register(self.safe_exit)

        self.config = load_config()
        self.input_device = resolve_input_device(self.config.get("input_device"))

        # 状态
        self.current_state = BotStates.WARMUP
        self.current_status = "启动中..."
        self.animations = {}
        self.current_frame_index = 0
        self.current_overlay_image = None
        self.exiting = False

        # 记忆
        self.permanent_memory = self.load_chat_history()
        self.session_memory = []

        # 同步原语
        self.last_ptt_time = 0
        self.ptt_event = threading.Event()
        self.recording_active = threading.Event()
        self.interrupted = threading.Event()
        self.thinking_sound_active = threading.Event()

        # TTS 队列
        self.tts_queue = []
        self.tts_queue_lock = threading.Lock()
        self.tts_active = threading.Event()
        self.current_tts_proc = None

        # Providers
        endpoints = self.config["providers_endpoints"]
        self.llm = LLMProvider(self.config["llm"], endpoints)
        self.stt = create_stt(self.config["stt"], endpoints)
        self.vision = VisionProvider(self.config["vision"], endpoints)
        self.image_gen = ImageGenProvider(
            self.config["image_gen"], endpoints,
            output_dir=self.config.get("generated_dir", "generated"),
        )
        self.tts_edge = EdgeTTSProvider(self.config["tts"])
        self.tts_sf = SiliconFlowTTSProvider(
            self.config["tts"], endpoints,
            sample_rate=self.config.get("tts_sample_rate", 24000),
        )
        self.tts_piper = None   # 本地 Piper TTS，懒加载（用到才加载模型）
        self._resolved_output = None   # 自动检测的音响输出设备缓存

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
        self.recording_active.clear()
        self.thinking_sound_active.clear()
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

    def handle_ptt_toggle(self, event=None):
        now = time.time()
        if now - self.last_ptt_time < 0.5:
            return
        self.last_ptt_time = now
        if self.recording_active.is_set():
            log("[PTT] 停止")
            self.recording_active.clear()
        else:
            if self.current_state == BotStates.IDLE or "等" in self.current_status or "Wait" in self.current_status:
                log("[PTT] 开始")
                self.recording_active.set()
                self.ptt_event.set()

    def handle_speaking_interrupt(self, event=None):
        if self.current_state in (BotStates.SPEAKING, BotStates.THINKING):
            log("[打断] 用户按了空格")
            self.interrupted.set()
            self.thinking_sound_active.clear()
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
    def load_animations(self):
        base = "faces"
        states = ["idle", "listening", "thinking", "speaking", "error", "capturing", "warmup"]
        for s in states:
            folder = os.path.join(base, s)
            self.animations[s] = []
            if os.path.isdir(folder):
                files = sorted(f for f in os.listdir(folder) if f.lower().endswith(".png"))
                for f in files:
                    try:
                        img = Image.open(os.path.join(folder, f)).resize((self.BG_WIDTH, self.BG_HEIGHT))
                        self.animations[s].append(ImageTk.PhotoImage(img))
                    except Exception:
                        pass
            if not self.animations[s]:
                blank = Image.new('RGB', (self.BG_WIDTH, self.BG_HEIGHT), color='#0000FF')
                self.animations[s].append(ImageTk.PhotoImage(blank))

    def update_animation(self):
        frames = self.animations.get(self.current_state) or self.animations.get(BotStates.IDLE)
        if not frames:
            self.master.after(500, self.update_animation)
            return
        if self.current_state == BotStates.SPEAKING and len(frames) > 1:
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
        def _update():
            if msg:
                log(f"[STATE] {state.upper()}: {msg}")
            if self.current_state != state:
                self.current_state = state
                self.current_frame_index = 0
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
            # 启动 TTS 后台线程
            self.tts_active.set()
            threading.Thread(target=self._tts_worker, daemon=True).start()
            self.set_state(BotStates.IDLE, "准备好啦")

            while not self.exiting:
                trigger = self.detect_wake_word_or_ptt()
                if self.exiting:
                    return
                if self.interrupted.is_set():
                    self.interrupted.clear()
                    self.set_state(BotStates.IDLE, "重置")
                    continue

                # 每轮都重新读 config，网页改了立即生效
                conv_cfg = self.config.get("conversation", {})
                follow_up = conv_cfg.get("follow_up", True)
                awake_secs = float(conv_cfg.get("awake_seconds", 15))
                followup_secs = float(conv_cfg.get("follow_up_seconds", 15))
                post_delay = float(conv_cfg.get("post_response_delay", 1.0))

                # 一轮唤醒后，可连续对话（无需重新唤醒），直到超时没人说话
                first = True
                while not self.exiting:
                    self.set_state(BotStates.LISTENING, "在听..." if first else "请说...")

                    if trigger == "PTT":
                        audio_file = self.record_voice_ptt()
                    else:
                        # 第一句用"维持唤醒时间"，后续追问用"连续对话时间"
                        onset = awake_secs if first else followup_secs
                        audio_file = self.record_voice_adaptive(onset_timeout=onset)

                    if not audio_file:
                        self.set_state(BotStates.IDLE, "没听到")
                        break

                    user_text = self.transcribe_audio(audio_file)
                    if not user_text:
                        self.set_state(BotStates.IDLE, "没听清")
                        break

                    log(f"[USER] {user_text}")
                    self.append_to_text(f"你: {user_text}")
                    self.interrupted.clear()
                    self.chat_and_respond(user_text, img_path=None)

                    first = False
                    # PTT 模式不做自动追问；唤醒模式才连续对话
                    if not follow_up or trigger == "PTT":
                        break
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
        self.ptt_event.clear()

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

        input_rate = choose_input_samplerate(self.input_device, self.config.get("input_sample_rate"))
        use_resample = (input_rate != TARGET_SR)
        in_chunk = int(target_chunk * (input_rate / TARGET_SR)) if use_resample else target_chunk

        stream_args = dict(samplerate=input_rate, channels=1, dtype='int16',
                           blocksize=in_chunk, device=self.input_device,
                           latency=self.config.get("audio_latency", "high"))
        refresh_secs = float(self.config.get("wake_word", {}).get("stream_refresh_seconds", 180))

        # 自动重开：定期刷新音频流 + 读取出错时重开，避免长时间空闲后唤不醒
        fail_count = 0
        while not self.exiting:
            try:
                result = self._listen_loop(stream_args, in_chunk, target_chunk,
                                           use_resample, refresh_secs)
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

    def _reset_portaudio(self):
        """彻底重启 PortAudio，强制重新枚举音频设备（清掉卡死的 ALSA 句柄）。"""
        try:
            log("[AUDIO] 重置 PortAudio（重新枚举设备）...")
            sd.stop()
            sd._terminate()
            time.sleep(0.8)
            sd._initialize()
            time.sleep(0.4)
        except Exception as e:
            log(f"[AUDIO] 重置 PortAudio 失败: {e}")

    def _listen_loop(self, args, in_chunk, target_chunk, use_resample, refresh_secs=180):
        ww_cfg = self.config.get("wake_word", {})
        oww_threshold = ww_cfg.get("legacy_threshold", 0.5)
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
            log(f"[AUDIO] 听唤醒词 backend={self.wake_backend} sr={args['samplerate']}")
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

                audio = np.asarray(data, dtype=np.int16)
                if audio.ndim > 1:
                    audio = audio.flatten()
                if use_resample:
                    audio = scipy.signal.resample_poly(
                        audio.astype(np.float32),
                        target_chunk,
                        len(audio),
                    )
                    if len(audio) > target_chunk:
                        audio = audio[:target_chunk]
                    elif len(audio) < target_chunk:
                        audio = np.pad(audio, (0, target_chunk - len(audio)))
                    audio = np.clip(audio, -32768, 32767).astype(np.int16)

                # ---- Sherpa-ONNX KWS 后端 ----
                if self.wake_backend == "sherpa_onnx":
                    hit = self.sherpa_kws.feed(audio)
                    if hit:
                        log(f"[WAKE] 触发 '{hit}'")
                        return "WAKE"
                    continue

                # ---- OpenWakeWord 后端 ----
                vol = np.max(np.abs(audio))
                if vol > 200:
                    self.oww_model.predict(audio)
                    for k in self.oww_model.prediction_buffer.keys():
                        score = list(self.oww_model.prediction_buffer[k])[-1]
                        if score > oww_threshold:
                            log(f"[WAKE] 触发 '{k}' score={score:.2f}")
                            self.oww_model.reset()
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
        sr = choose_input_samplerate(self.input_device, self.config.get("input_sample_rate"))

        rec_cfg = self.config.get("recording", {})
        silence_duration = float(rec_cfg.get("silence_duration", 1.0))
        max_speech = float(rec_cfg.get("max_record_seconds", 12.0))
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

        def _cb(indata, frames, time_info, status):
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

        try:
            last_audio = time.time()
            with sd.InputStream(samplerate=sr, channels=1, dtype='int16',
                                blocksize=chunk_size, device=self.input_device,
                                latency=self.config.get("audio_latency", "high"),
                                callback=_cb):
                while not self.exiting:
                    try:
                        indata = audio_q.get(timeout=0.5)
                        last_audio = time.time()
                    except queue.Empty:
                        if time.time() - last_audio > stall_timeout:
                            log("[AUDIO] 录音音频流卡住，放弃本次录音")
                            stalled = True
                            break
                        continue

                    audio = np.asarray(indata, dtype=np.float32).flatten()
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
        sr = choose_input_samplerate(self.input_device, self.config.get("input_sample_rate"))
        chunk_size = int(sr * 0.05)
        buf = []
        try:
            with sd.InputStream(samplerate=sr, channels=1, dtype='int16',
                                blocksize=chunk_size, device=self.input_device,
                                latency=self.config.get("audio_latency", "high")) as stream:
                while self.recording_active.is_set() and not self.exiting:
                    try:
                        data, _ = stream.read(chunk_size)
                    except Exception as e:
                        log(f"[AUDIO] PTT 读取失败: {e}")
                        break
                    buf.append(np.frombuffer(data, dtype=np.int16).astype(np.float32))
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
            return BMO_IMAGE_FILE
        except Exception as e:
            log(f"[CAM ERROR] {e}")
            return None

    # -------------------------------------------------------------------
    # 工具执行
    # -------------------------------------------------------------------
    def extract_json_from_text(self, text):
        try:
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                return json.loads(m.group(0))
        except Exception:
            pass
        return None

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
        raw = (action_data.get("action") or "").lower().strip()
        value = action_data.get("value") or action_data.get("query") or action_data.get("prompt")

        ALIASES = {
            "look": "capture_image", "see": "capture_image",
            "check_time": "get_time", "draw": "generate_image", "paint": "generate_image",
            "status": "get_system_status", "system_status": "get_system_status",
            "system": "get_system_status", "health": "get_system_status",
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

        if action == "search_web":
            return f"SEARCH_DISABLED::{value or ''}"

        if action in ("google", "browser", "news"):
            return f"SEARCH_DISABLED::{value or ''}"

        if action == "capture_image":
            return "IMAGE_CAPTURE_TRIGGERED"

        if action == "generate_image":
            if not value:
                return "IMAGE_GEN_EMPTY"
            return f"IMAGE_GEN_TRIGGERED::{value}"

        return "INVALID_ACTION"

    # -------------------------------------------------------------------
    # 主对话
    # -------------------------------------------------------------------
    def chat_and_respond(self, text, img_path=None):
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
                # 工具调用检测：回复里只要出现 { 就当作工具调用，立刻停止朗读
                if allow_tools and not is_action and "{" in full_buf:
                    is_action = True
                    self.thinking_sound_active.clear()
                    continue
                if is_action:
                    continue

                if self.current_state != BotStates.SPEAKING:
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

            # 收尾：剩余的最后一段
            if not is_action and sentence_buf.strip():
                with self.tts_queue_lock:
                    self.tts_queue.append(sentence_buf.strip())

            # 工具分支
            if is_action:
                action_data = self.extract_json_from_text(full_buf)
                if action_data:
                    result = self.execute_action(action_data)
                    self.handle_action_result(result, text, img_path)
                    return

            # 保存对话历史
            if not is_action and full_buf.strip():
                log(f"[BMO] {full_buf.strip()}")
            self.session_memory.append({"role": "user", "content": text})
            self.session_memory.append({"role": "assistant", "content": full_buf})
            self.trim_memory()
            self.save_chat_history()

            self.wait_for_tts()
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

    def handle_action_result(self, result, original_text, img_path):
        """工具执行结果处理。"""
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
                    self.set_state(BotStates.SPEAKING, "看我画的~", overlay_path=path)
                    self.speak_text("画好啦！")
                    self.wait_for_tts()
                    # 保持显示一会儿
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

        # 兼容旧配置/旧上下文：如果模型仍输出 search_web JSON，不联网搜索，改让模型直接回答。
        if isinstance(result, str) and result.startswith("SEARCH_DISABLED::"):
            query = result.split("::", 1)[1].strip() or original_text
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
            return

        # get_time / get_system_status 返回了字符串：让 LLM 再总结一遍
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
            self.session_memory.append({"role": "user", "content": remember})
            self.session_memory.append({"role": "assistant", "content": text})
            self.trim_memory()
            self.save_chat_history()
        self.wait_for_tts()
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
                self.speak(text)
                with self.tts_queue_lock:
                    if not self.tts_queue:
                        self.tts_active.clear()
            else:
                time.sleep(0.05)

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

    def _resolve_output_device(self):
        """auto/空 → 自动检测 USB 音响；显式值 → 直接用。结果缓存。"""
        dev = (self.config.get("audio_output_device") or "auto").strip()
        if dev and dev.lower() != "auto":
            return dev
        if getattr(self, "_resolved_output", None) is None:
            self._resolved_output = self._detect_usb_playback()
            if self._resolved_output:
                log(f"[AUDIO] 自动选用输出设备: {self._resolved_output}")
            else:
                log("[AUDIO] 未检测到 USB 音响，用系统默认输出")
        return self._resolved_output

    def _play_pcm_aplay(self, pcm: bytes, sr: int):
        """用 aplay 播 16-bit 单声道 PCM，自动/指定到音响。"""
        if not pcm:
            return
        dev = self._resolve_output_device()
        cmd = ["aplay", "-q", "-f", "S16_LE", "-c", "1", "-r", str(sr)]
        if dev:
            cmd += ["-D", dev]
        cmd += ["-"]
        try:
            self.current_tts_proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.current_tts_proc.stdin.write(pcm)
            self.current_tts_proc.stdin.close()
            while self.current_tts_proc.poll() is None:
                if self.interrupted.is_set() or self.exiting:
                    self.current_tts_proc.terminate()
                    break
                time.sleep(0.05)
        finally:
            self.current_tts_proc = None

    def _speak_piper(self, text):
        if self.tts_piper is None:
            from providers.tts_piper import PiperTTSProvider
            self.tts_piper = PiperTTSProvider(self.config["tts"])
        pcm, sr = self.tts_piper.synthesize_pcm16(text)
        self._play_pcm_aplay(pcm, sr)

    def _speak_edge(self, text):
        # Edge-TTS 输出 MP3 → 用 mpg123 解码播放（可指定 USB 音箱）
        mp3 = self.tts_edge.synthesize_mp3(text)
        if not mp3:
            return
        dev = self._resolve_output_device()
        cmd = ["mpg123", "-q"]
        if dev:
            cmd += ["-a", dev]
        cmd += ["-"]
        try:
            self.current_tts_proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            self.current_tts_proc.stdin.write(mp3)
            self.current_tts_proc.stdin.close()
            while self.current_tts_proc.poll() is None:
                if self.interrupted.is_set() or self.exiting:
                    self.current_tts_proc.terminate()
                    break
                time.sleep(0.05)
        finally:
            self.current_tts_proc = None

    def _speak_siliconflow(self, text):
        sr = self.config.get("tts_sample_rate", 24000)
        with sd.RawOutputStream(samplerate=sr, channels=1, dtype='int16', latency='low') as stream:
            for chunk in self.tts_sf.synthesize_pcm_stream(text):
                if self.interrupted.is_set() or self.exiting:
                    break
                stream.write(chunk)

    # -------------------------------------------------------------------
    # 记忆
    # -------------------------------------------------------------------
    def build_system_prompt(self) -> str:
        # 工具说明硬编码，始终追加在用户性格之后，用户在网页改不到、删不掉
        personality = (self.config.get("system_prompt", "") or "").strip()
        extras = (self.config.get("system_prompt_extras", "") or "").strip()
        parts = [personality, TOOLS_PROMPT]
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
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                json.dump([sysp] + rest, f, ensure_ascii=False, indent=2)
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
            if self.current_state in (BotStates.IDLE,):
                self.recording_active.set()
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
                self.config = load_config()
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
                self._resolved_output = None  # 重新检测输出设备
                self.vision = VisionProvider(self.config["vision"], endpoints)
                self.image_gen = ImageGenProvider(
                    self.config["image_gen"], endpoints,
                    output_dir=self.config.get("generated_dir", "generated"),
                )
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
        elif action == "restart_webui":
            self._restart_webui()

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
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "state": self.current_state,
                    "status": self.current_status,
                    "tts_queue_len": len(self.tts_queue),
                    "memory_turns": len(self.session_memory) // 2,
                    "timestamp": time.time(),
                }, f, ensure_ascii=False)
        except Exception:
            pass
        self.master.after(1000, self.update_state_file)


WEBUI_PROC = None


def maybe_spawn_webui():
    """如果 config 里开了，就 spawn webui 子进程。返回 Popen 句柄。"""
    try:
        cfg = load_config()
    except Exception:
        return None
    if not cfg.get("webui_auto_start", True):
        return None
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
        print("❌ 缺少 SILICONFLOW_API_KEY，请检查 .env 文件", flush=True)
        sys.exit(1)

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
