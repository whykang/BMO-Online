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
import traceback
import datetime
import warnings
import atexit
import subprocess
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
from providers.stt import STTProvider
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

# --- 搜索 ---
try:
    from duckduckgo_search import DDGS
except Exception:
    DDGS = None

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
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


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
        try:
            master.attributes('-fullscreen', True)
        except Exception:
            pass
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
        self.stt = STTProvider(self.config["stt"], endpoints)
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

        # 唤醒词
        self.oww_model = None
        self.wake_reload_pending = False
        self._load_wake_word()

        # GUI 组件
        self.background_label = tk.Label(master)
        self.background_label.place(x=0, y=0, width=self.BG_WIDTH, height=self.BG_HEIGHT)
        self.background_label.bind('<Button-1>', self.toggle_hud_visibility)

        self.overlay_label = tk.Label(master, bg='black')
        self.overlay_label.bind('<Button-1>', self.toggle_hud_visibility)

        self.response_text = tk.Text(master, height=6, width=60, wrap=tk.WORD,
                                     state=tk.DISABLED, bg="#ffffff", fg="#000000",
                                     font=('Arial', 12))

        self.status_var = tk.StringVar(value=self.current_status)
        self.status_label = ttk.Label(master, textvariable=self.status_var,
                                      background="#2e2e2e", foreground="white")
        self.exit_button = ttk.Button(master, text="退出", command=self.safe_exit)

        self.load_animations()
        self.update_animation()
        # 默认显示 HUD（状态 + 识别文字），可点屏幕切换
        if self.config.get("show_hud", True):
            self.response_text.place(relx=0.5, rely=0.82, anchor=tk.S)
            self.status_label.place(relx=0.5, rely=1.0, anchor=tk.S, relwidth=1)
            self.exit_button.place(x=10, y=10)
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

    def toggle_hud_visibility(self, event=None):
        try:
            if self.response_text.winfo_ismapped():
                self.response_text.place_forget()
                self.status_label.place_forget()
                self.exit_button.place_forget()
            else:
                self.response_text.place(relx=0.5, rely=0.82, anchor=tk.S)
                self.status_label.place(relx=0.5, rely=1.0, anchor=tk.S, relwidth=1)
                self.exit_button.place(x=10, y=10)
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
        self.master.after(0, _update)

    def append_to_text(self, text, newline=True):
        def _update():
            self.response_text.config(state=tk.NORMAL)
            if newline:
                self.response_text.insert(tk.END, text + "\n")
            else:
                self.response_text.insert(tk.END, text)
            self.response_text.see(tk.END)
            self.response_text.config(state=tk.DISABLED)
        self.master.after(0, _update)

    def _stream_to_text(self, chunk):
        def _update():
            self.response_text.config(state=tk.NORMAL)
            self.response_text.insert(tk.END, chunk)
            self.response_text.see(tk.END)
            self.response_text.config(state=tk.DISABLED)
        self.master.after(0, _update)

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

            conv_cfg = self.config.get("conversation", {})
            follow_up = conv_cfg.get("follow_up", True)
            follow_up_rounds = int(conv_cfg.get("follow_up_max_rounds", 6))

            while not self.exiting:
                trigger = self.detect_wake_word_or_ptt()
                if self.exiting:
                    return
                if self.interrupted.is_set():
                    self.interrupted.clear()
                    self.set_state(BotStates.IDLE, "重置")
                    continue

                # 一轮唤醒后，可连续对话若干回合（无需重新唤醒）
                rounds = 0
                while not self.exiting:
                    self.set_state(BotStates.LISTENING, "在听..." if rounds == 0 else "请说...")

                    if trigger == "PTT":
                        audio_file = self.record_voice_ptt()
                    else:
                        audio_file = self.record_voice_adaptive()

                    if not audio_file:
                        # 追问窗口里没听到 → 结束本次对话，回到等待唤醒
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

                    rounds += 1
                    # PTT 模式不做自动追问（按键本身就是触发）；唤醒模式才连续
                    if not follow_up or trigger == "PTT" or rounds >= follow_up_rounds:
                        break
                    # 给扬声器留一点尾音时间，避免录到 BMO 自己的话
                    time.sleep(0.4)

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
        if self.wake_reload_pending:
            self.wake_reload_pending = False
            log("[CMD] 重载唤醒词后端...")
            try:
                self._load_wake_word()
            except Exception as e:
                log(f"[CMD] 唤醒词重载失败: {e}")

        # 没装任何唤醒词后端 → 纯 PTT 模式
        if self.wake_backend is None:
            while not self.ptt_event.is_set():
                if self.exiting:
                    return "PTT"
                time.sleep(0.1)
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
                           blocksize=in_chunk, device=self.input_device)
        try:
            return self._listen_loop(stream_args, in_chunk, target_chunk, use_resample)
        except StopIteration as si:
            return str(si)
        except Exception as e:
            log(f"[AUDIO] 唤醒词监听失败，回落 PTT: {e}")
            while not self.ptt_event.is_set() and not self.exiting:
                time.sleep(0.1)
            self.ptt_event.clear()
            return "PTT"

    def _listen_loop(self, args, in_chunk, target_chunk, use_resample):
        oww_threshold = self.config.get("wake_word", {}).get("legacy_threshold", 0.5)
        with sd.InputStream(**args) as stream:
            log(f"[AUDIO] 听唤醒词 backend={self.wake_backend} sr={args['samplerate']}")
            while True:
                if self.exiting:
                    raise StopIteration("EXIT")
                if self.ptt_event.is_set():
                    self.ptt_event.clear()
                    raise StopIteration("PTT")
                try:
                    data, _ = stream.read(in_chunk)
                except Exception as e:
                    raise RuntimeError(f"audio read: {e}")
                audio = np.frombuffer(data, dtype=np.int16)
                if audio.ndim > 1:
                    audio = audio.flatten()
                if use_resample:
                    step = len(audio) / target_chunk
                    idx = np.arange(0, len(audio), step)[:target_chunk].astype(int)
                    audio = audio[idx]

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
    def record_voice_adaptive(self, filename="input.wav"):
        log("录音（自适应）...")
        time.sleep(0.3)
        sr = choose_input_samplerate(self.input_device, self.config.get("input_sample_rate"))

        rec_cfg = self.config.get("recording", {})
        silence_duration = float(rec_cfg.get("silence_duration", 1.0))
        max_record = float(rec_cfg.get("max_record_seconds", 12.0))
        min_record = float(rec_cfg.get("min_record_seconds", 1.0))
        # 噪音地板倍数：动态阈值 = 噪音地板 * 这个倍数（再设个下限）
        noise_mult = float(rec_cfg.get("silence_multiplier", 3.0))
        floor_min = float(rec_cfg.get("silence_floor_min", 0.012))

        chunk_dur = 0.05
        chunk_size = int(sr * chunk_dur)
        num_silent = int(silence_duration / chunk_dur)
        max_chunks = int(max_record / chunk_dur)
        min_chunks = int(min_record / chunk_dur)
        calib_chunks = 8   # 前 0.4 秒用来测噪音地板

        buf = []
        state = {"recorded": 0, "silent": 0, "done": False,
                 "noise": 0.0, "thr": floor_min, "peak": 0.0}

        def cb(indata, frames, time_info, status):
            vn = float(np.linalg.norm(indata) / np.sqrt(len(indata)))
            buf.append(indata.copy())
            state["recorded"] += 1
            n = state["recorded"]

            # 校准阶段：测噪音地板
            if n <= calib_chunks:
                state["noise"] = max(state["noise"], vn)
                if n == calib_chunks:
                    state["thr"] = max(floor_min, state["noise"] * noise_mult)
                return

            state["peak"] = max(state["peak"], vn)
            # 至少录够 min_record 才允许判静音
            if n < min_chunks:
                return
            if vn < state["thr"]:
                state["silent"] += 1
                if state["silent"] >= num_silent:
                    state["done"] = True
            else:
                state["silent"] = 0

        try:
            sd.stop()
            time.sleep(0.2)
            with sd.InputStream(samplerate=sr, channels=1, callback=cb,
                                device=self.input_device, blocksize=chunk_size):
                while not state["done"] and state["recorded"] < max_chunks and not self.exiting:
                    sd.sleep(int(chunk_dur * 1000))
        except Exception as e:
            log(f"[AUDIO ERROR] 自适应录音失败: {e}")
            return None

        dur = state["recorded"] * chunk_dur
        log(f"[REC] 时长 {dur:.1f}s 噪音地板={state['noise']:.4f} 阈值={state['thr']:.4f} 峰值={state['peak']:.4f}")
        # 峰值没明显超过阈值 = 大概率没说话
        if state["peak"] < state["thr"] * 1.5:
            log("[REC] 似乎没听到有效语音")
            return None
        return self.save_audio_buffer(buf, filename, sr)

    def record_voice_ptt(self, filename="input.wav"):
        log("录音（PTT）...")
        time.sleep(0.3)
        sr = choose_input_samplerate(self.input_device, self.config.get("input_sample_rate"))
        buf = []

        def cb(indata, frames, time_info, status):
            buf.append(indata.copy())

        try:
            sd.stop()
            time.sleep(0.2)
            with sd.InputStream(samplerate=sr, channels=1, callback=cb, device=self.input_device):
                while self.recording_active.is_set() and not self.exiting:
                    sd.sleep(50)
        except Exception as e:
            log(f"[AUDIO ERROR] PTT 录音失败: {e}")
            return None
        return self.save_audio_buffer(buf, filename, sr)

    def save_audio_buffer(self, buf, filename, sr=16000):
        if not buf:
            return None
        audio = np.concatenate(buf, axis=0).flatten()
        audio = np.nan_to_num(audio, nan=0.0, posinf=0.0, neginf=0.0)
        audio = (audio * 32767).astype(np.int16)
        with wave.open(filename, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(audio.tobytes())
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

    def execute_action(self, action_data):
        raw = (action_data.get("action") or "").lower().strip()
        value = action_data.get("value") or action_data.get("query") or action_data.get("prompt")

        ALIASES = {
            "google": "search_web", "browser": "search_web", "news": "search_web",
            "look": "capture_image", "see": "capture_image",
            "check_time": "get_time", "draw": "generate_image", "paint": "generate_image",
        }
        action = ALIASES.get(raw, raw)
        log(f"[ACTION] {raw} -> {action}")

        if action == "get_time":
            now = datetime.datetime.now().strftime("%H:%M")
            return f"现在是 {now}。"

        if action == "search_web":
            if not value:
                return "SEARCH_EMPTY"
            if DDGS is None:
                return "SEARCH_ERROR"
            try:
                with DDGS() as ddgs:
                    results = []
                    try:
                        results = list(ddgs.news(value, max_results=1))
                    except Exception:
                        pass
                    if not results:
                        try:
                            results = list(ddgs.text(value, max_results=1))
                        except Exception:
                            results = []
                    if not results:
                        return "SEARCH_EMPTY"
                    r = results[0]
                    title = r.get("title", "")
                    body = r.get("body", r.get("snippet", ""))
                    return f"SEARCH 结果 '{value}':\n标题: {title}\n摘要: {body[:300]}"
            except Exception as e:
                log(f"[SEARCH] {e}")
                return "SEARCH_ERROR"

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

        self.set_state(BotStates.THINKING, "思考中...", overlay_path=img_path)

        # 构造 messages
        if img_path:
            # 视觉单独走一次：先让 Vision 模型描述图，再让 LLM 基于描述对话
            try:
                desc = self.vision.describe(img_path, text or "请描述你看到的图片。")
                log(f"[VISION] {desc}")
                messages = self.permanent_memory + self.session_memory + [
                    {"role": "user", "content": f"{text}\n（我看到的画面：{desc}）"}
                ]
            except Exception as e:
                log(f"[VISION ERROR] {e}")
                messages = self.permanent_memory + self.session_memory + [
                    {"role": "user", "content": text}
                ]
        else:
            messages = self.permanent_memory + self.session_memory + [
                {"role": "user", "content": text}
            ]

        full_buf = ""
        sentence_buf = ""
        sentence_re = re.compile(r'[。！？.!?\n]')
        is_action = False

        try:
            for chunk in self.llm.chat_stream(messages):
                if self.interrupted.is_set():
                    break
                full_buf += chunk
                # 工具调用检测：回复里只要出现 { 就当作工具调用，立刻停止朗读
                # （模型有时会在 JSON 前加一句话，所以不能只看开头）
                if not is_action and "{" in full_buf:
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

        # 搜索失败/无结果（如国内 DuckDuckGo 不可达）→ 让 LLM 用已知信息回答
        if result in ("SEARCH_EMPTY", "SEARCH_ERROR"):
            fallback_messages = self.permanent_memory + self.session_memory + [
                {"role": "user", "content": original_text},
                {"role": "user", "content": "（实时联网搜索不可用。请基于你已有的知识回答用户，"
                                            "如果涉及最新内容就说明你可能不知道最新情况。一两句话即可。）"}
            ]
            try:
                final_text = self.llm.chat_once(fallback_messages)
            except Exception as e:
                log(f"[LLM ERROR] {e}")
                final_text = "我现在查不到最新信息呢。"
            self._say(final_text, remember=original_text)
            return

        # search_web 返回了正文 / get_time 返回了字符串：让 LLM 再总结一遍
        if isinstance(result, str) and result:
            summary_messages = self.permanent_memory + self.session_memory + [
                {"role": "user", "content": original_text},
                {"role": "user", "content": f"工具返回结果：{result}\n请用一两句话告诉用户。"}
            ]
            try:
                final_text = self.llm.chat_once(summary_messages)
            except Exception as e:
                log(f"[LLM ERROR] {e}")
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

    def _say(self, text, remember=None):
        """说一句话并等播放完，再回 IDLE。remember 不为空时把这轮对话存进记忆。"""
        self.set_state(BotStates.SPEAKING, "说话中...")
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
            if provider == "edge":
                self._speak_edge(clean)
            else:
                self._speak_siliconflow(clean)
        except Exception as e:
            log(f"[TTS] {provider} 失败: {e}, 尝试兜底")
            try:
                self._speak_siliconflow(clean)
            except Exception as e2:
                log(f"[TTS] 兜底也失败: {e2}")

    def _speak_edge(self, text):
        # Edge-TTS 输出 MP3 → 用 mpg123 解码到扬声器
        mp3 = self.tts_edge.synthesize_mp3(text)
        if not mp3:
            return
        try:
            self.current_tts_proc = subprocess.Popen(
                ["mpg123", "-q", "-"], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
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
        base = self.config.get("system_prompt", "")
        extras = self.config.get("system_prompt_extras", "")
        if extras:
            return f"{base}\n\n{extras}"
        return base

    def load_chat_history(self):
        sysp = {"role": "system", "content": self.build_system_prompt() if hasattr(self, "config") else ""}
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            sysp["content"] = cfg.get("system_prompt", "") + "\n\n" + cfg.get("system_prompt_extras", "")
        except Exception:
            pass
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
                self.tts_edge = EdgeTTSProvider(self.config["tts"])
                self.tts_sf = SiliconFlowTTSProvider(
                    self.config["tts"], endpoints,
                    sample_rate=self.config.get("tts_sample_rate", 24000),
                )
                self.vision = VisionProvider(self.config["vision"], endpoints)
                self.image_gen = ImageGenProvider(
                    self.config["image_gen"], endpoints,
                    output_dir=self.config.get("generated_dir", "generated"),
                )
                # 唤醒词不能在这个线程里重建（监听循环在另一个线程跑，会撞车）。
                # 设个标志，让监听线程在下一轮自己重载。
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
