# =========================================================================
#  BMO Web 控制台 (FastAPI)
#  浏览器访问: http://树莓派IP:8087
# =========================================================================

import os
import json
import time
import shutil
import hashlib
import datetime
import asyncio
import subprocess
import shlex
import re
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request, Response, Cookie
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

CONFIG_FILE = "config.json"
MEMORY_FILE = "chat_memory.json"
STATE_FILE = "state.json"
COMMANDS_FILE = "commands.json"
AUTH_FILE = "auth.json"
LOG_DIR = "logs"
GENERATED_DIR = "generated"
WAKEWORDS_DIR = "wakewords"
ENV_FILE = ".env"

app = FastAPI(title="BMO Web Control")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/generated", StaticFiles(directory=GENERATED_DIR), name="generated")
os.makedirs(WAKEWORDS_DIR, exist_ok=True)
os.makedirs(GENERATED_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
app.mount("/wakewords", StaticFiles(directory=WAKEWORDS_DIR), name="wakewords")

# =========================================================================
# 工具
# =========================================================================

def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE) and os.path.exists("config.default.json"):
        shutil.copy("config.default.json", CONFIG_FILE)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    # 通知 agent 重载
    queue_command({"action": "reload_config"})


def queue_command(cmd: dict):
    """往 commands.json 追加一条命令；agent 主线程会 poll 它。"""
    cmds = []
    if os.path.exists(COMMANDS_FILE):
        try:
            with open(COMMANDS_FILE, "r", encoding="utf-8") as f:
                cmds = json.load(f)
        except Exception:
            cmds = []
    cmds.append(cmd)
    with open(COMMANDS_FILE, "w", encoding="utf-8") as f:
        json.dump(cmds, f, ensure_ascii=False)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"state": "unknown", "status": "agent 未运行", "tts_queue_len": 0, "memory_turns": 0}


def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


def load_auth() -> dict | None:
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def save_auth(data: dict):
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


SESSION_TOKENS: set[str] = set()


def make_token() -> str:
    return hashlib.sha256(os.urandom(32)).hexdigest()


# =========================================================================
# 路由：HTML
# =========================================================================

@app.get("/", response_class=HTMLResponse)
async def index(token: str = Cookie(default=None)):
    # 禁止缓存：否则登录前缓存的登录页会在登录后被直接复用，看着像没跳转
    no_cache = {"Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache", "Expires": "0"}
    auth = load_auth()
    if auth and auth.get("password_hash") and (not token or token not in SESSION_TOKENS):
        return FileResponse("static/login.html", headers=no_cache)
    return FileResponse("static/index.html", headers=no_cache)


# =========================================================================
# 路由：登录与密码设置
# =========================================================================

class LoginReq(BaseModel):
    password: str


def _set_token_cookie(response: Response):
    token = make_token()
    SESSION_TOKENS.add(token)
    response.set_cookie("token", token, httponly=True,
                        samesite="lax", path="/", max_age=30 * 24 * 3600)


@app.get("/api/auth/status")
async def auth_status(token: str = Cookie(default=None)):
    auth = load_auth()
    has_password = bool(auth and auth.get("password_hash"))
    authed = (not has_password) or (token in SESSION_TOKENS)
    return {"has_password": has_password, "authed": authed}


@app.post("/api/login")
async def login(req: LoginReq, response: Response):
    auth = load_auth()
    if not auth or not auth.get("password_hash"):
        # 没设置密码，直接通过
        _set_token_cookie(response)
        return {"ok": True, "no_password": True}
    if hash_password(req.password) == auth["password_hash"]:
        _set_token_cookie(response)
        return {"ok": True}
    raise HTTPException(401, "密码错误")


@app.post("/api/logout")
async def logout(response: Response, token: str = Cookie(default=None)):
    if token in SESSION_TOKENS:
        SESSION_TOKENS.discard(token)
    response.delete_cookie("token")
    return {"ok": True}


class SetPasswordReq(BaseModel):
    new_password: str
    old_password: str | None = None


@app.post("/api/password")
async def set_password(req: SetPasswordReq, response: Response):
    if not req.new_password or not req.new_password.strip():
        raise HTTPException(400, "密码不能为空")
    auth = load_auth() or {}
    if auth.get("password_hash"):
        if not req.old_password or hash_password(req.old_password) != auth["password_hash"]:
            raise HTTPException(401, "原密码错误")
    auth["password_hash"] = hash_password(req.new_password)
    save_auth(auth)
    # 设完密码直接把当前浏览器登录上，避免来回跳转
    SESSION_TOKENS.clear()
    _set_token_cookie(response)
    return {"ok": True}


@app.delete("/api/password")
async def remove_password(token: str = Cookie(default=None)):
    if os.path.exists(AUTH_FILE):
        os.remove(AUTH_FILE)
    SESSION_TOKENS.clear()
    return {"ok": True}


# =========================================================================
# 路由：状态与配置
# =========================================================================

@app.get("/api/state")
async def get_state():
    return load_state()


@app.get("/api/config")
async def get_config():
    return load_config()


@app.get("/api/audio/outputs")
async def audio_outputs():
    """列出可用的播放设备（供网页下拉选音响）。"""
    items = []
    try:
        out = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=8).stdout
        for line in out.splitlines():
            m = re.match(r"\s*card (\d+): (\S+) \[(.*?)\].*device (\d+):", line)
            if not m:
                continue
            card, cid, name, dev = m.group(1), m.group(2), m.group(3), m.group(4)
            tag = f"{cid} {name}".lower()
            kind = "HDMI" if ("hdmi" in tag or "vc4" in tag) else ("USB" if "usb" in tag else "其它")
            items.append({
                "device": f"plughw:{card},{dev}",
                "label": f"[{kind}] card {card}: {name}",
                "kind": kind,
            })
    except Exception as e:
        return {"devices": [], "error": str(e)}
    return {"devices": items}


def _output_card_num():
    """取输出声卡号：优先 config 里的 plughw:N，否则自动找 USB/非HDMI 播放卡。"""
    cfg = load_config()
    dev = (cfg.get("audio_output_device") or "auto").strip()
    m = re.search(r"hw:(\d+)", dev)
    if m:
        return int(m.group(1))
    try:
        out = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=8).stdout
        non_hdmi = None
        for line in out.splitlines():
            mm = re.match(r"\s*card (\d+): (\S+) \[(.*?)\]", line)
            if not mm:
                continue
            c = int(mm.group(1))
            tag = (mm.group(2) + " " + mm.group(3)).lower()
            if "usb" in tag:
                return c
            if "hdmi" not in tag and "vc4" not in tag and non_hdmi is None:
                non_hdmi = c
        return non_hdmi
    except Exception:
        return None


def _amixer_control(card):
    """找一个能调的播放音量控件名。"""
    try:
        out = subprocess.run(["amixer", "-c", str(card), "scontrols"],
                             capture_output=True, text=True, timeout=8).stdout
        names = re.findall(r"'([^']+)'", out)
        for pref in ("Master", "PCM", "Speaker", "Headphone", "Playback"):
            if pref in names:
                return pref
        return names[0] if names else None
    except Exception:
        return None


@app.get("/api/volume")
async def get_volume():
    card = _output_card_num()
    if card is None:
        return {"ok": False, "reason": "没找到输出声卡"}
    ctrl = _amixer_control(card)
    if not ctrl:
        return {"ok": False, "card": card, "reason": "该音箱无可调音量控件（音量固定）"}
    try:
        out = subprocess.run(["amixer", "-c", str(card), "sget", ctrl],
                             capture_output=True, text=True, timeout=8).stdout
        m = re.search(r"\[(\d+)%\]", out)
        return {"ok": True, "card": card, "control": ctrl,
                "volume": int(m.group(1)) if m else None}
    except Exception as e:
        return {"ok": False, "card": card, "reason": str(e)}


class VolumeReq(BaseModel):
    percent: int


@app.put("/api/volume")
async def set_volume(req: VolumeReq):
    pct = max(0, min(100, int(req.percent)))
    card = _output_card_num()
    if card is None:
        raise HTTPException(400, "没找到输出声卡")
    ctrl = _amixer_control(card)
    if not ctrl:
        raise HTTPException(400, "该音箱无可调音量控件（音量固定，无法软调）")
    try:
        subprocess.run(["amixer", "-c", str(card), "sset", ctrl, f"{pct}%", "unmute"],
                       capture_output=True, text=True, timeout=8)
        return {"ok": True, "card": card, "control": ctrl, "volume": pct}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/config/default")
async def get_default_config():
    """出厂默认配置（config.default.json），用于'重置'。"""
    try:
        with open("config.default.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


@app.put("/api/config")
async def update_config(cfg: dict):
    # 唤醒词由专属接口 /api/wakewords/config 管理。整份保存（性格/模型/音色等）
    # 时永远保留文件里已有的 wake_word，避免前端 CONFIG 过期把唤醒词覆盖回旧值。
    try:
        existing = load_config()
        if "wake_word" in existing:
            cfg["wake_word"] = existing["wake_word"]
    except Exception:
        pass
    save_config(cfg)
    return {"ok": True}


# =========================================================================
# 路由：音色（Edge-TTS）
# =========================================================================

@app.get("/api/voices")
async def list_voices(lang: str = "zh"):
    """列出 Edge-TTS 音色，按语言前缀过滤（zh, en, ja 等）。"""
    from providers.tts_edge import EdgeTTSProvider
    prefix = f"{lang}-" if lang else None
    voices = await EdgeTTSProvider.list_voices(prefix)
    # 简化返回字段
    return [
        {
            "name": v.get("ShortName"),
            "display": v.get("FriendlyName", v.get("ShortName")),
            "gender": v.get("Gender"),
            "locale": v.get("Locale"),
        }
        for v in voices
    ]


class TestVoiceReq(BaseModel):
    text: str = "你好，我是 BMO，很高兴见到你。"
    voice: str = "zh-CN-XiaoyiNeural"
    rate: str = "+0%"
    volume: str = "+0%"


@app.post("/api/voices/test")
async def test_voice(req: TestVoiceReq):
    """生成一段 MP3 让用户在网页里试听音色。"""
    from providers.tts_edge import EdgeTTSProvider
    provider = EdgeTTSProvider({"voice": req.voice, "rate": req.rate, "volume": req.volume})
    mp3 = await provider._synth_async(req.text)
    return Response(content=mp3, media_type="audio/mpeg")


# =========================================================================
# 路由：唤醒词（双后端 sherpa_onnx / openwakeword）
# =========================================================================

@app.get("/api/wakewords/status")
async def wakewords_status():
    """返回当前后端 + 关键词 + 可用模型。"""
    cfg = load_config()
    ww = cfg.get("wake_word", {})
    sherpa_dir = ww.get("model_dir", "wakewords/sherpa-kws-zh")
    sherpa_ready = os.path.isdir(sherpa_dir) and any(
        f.endswith(".onnx") and "encoder" in f for f in os.listdir(sherpa_dir)
    ) if os.path.isdir(sherpa_dir) else False
    return {
        "enabled": ww.get("enabled", False),
        "backend": ww.get("backend", "sherpa_onnx"),
        "keywords": ww.get("keywords", []),
        "threshold": ww.get("threshold", 0.25),
        "score": ww.get("score", 1.5),
        "sherpa_model_dir": sherpa_dir,
        "sherpa_ready": sherpa_ready,
        "legacy_model": ww.get("model", ""),
        "legacy_threshold": ww.get("legacy_threshold", 0.5),
    }


class WakewordCfgReq(BaseModel):
    enabled: bool | None = None
    backend: str | None = None
    keywords: list[str] | None = None
    threshold: float | None = None
    score: float | None = None
    model: str | None = None
    legacy_threshold: float | None = None


@app.put("/api/wakewords/config")
async def update_wakeword_config(req: WakewordCfgReq):
    cfg = load_config()
    ww = cfg.setdefault("wake_word", {})
    for field in ("enabled", "backend", "keywords", "threshold", "score",
                  "model", "legacy_threshold"):
        v = getattr(req, field)
        if v is not None:
            ww[field] = v
    save_config(cfg)
    return {"ok": True, "wake_word": ww}


@app.get("/api/wakewords")
async def list_wakewords():
    """列出 wakewords/ 里的 .onnx 文件（OpenWakeWord 后端用）。"""
    items = []
    if os.path.isdir(WAKEWORDS_DIR):
        for f in sorted(os.listdir(WAKEWORDS_DIR)):
            if f.endswith(".onnx"):
                items.append({
                    "filename": f,
                    "path": os.path.join(WAKEWORDS_DIR, f),
                    "size_kb": round(os.path.getsize(os.path.join(WAKEWORDS_DIR, f)) / 1024, 1),
                })
    return items


@app.post("/api/wakewords/upload")
async def upload_wakeword(file: UploadFile = File(...)):
    if not file.filename.endswith(".onnx"):
        raise HTTPException(400, "只接受 .onnx 文件")
    dest = os.path.join(WAKEWORDS_DIR, file.filename)
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"ok": True, "path": dest}


@app.delete("/api/wakewords/{filename}")
async def delete_wakeword(filename: str):
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "非法文件名")
    path = os.path.join(WAKEWORDS_DIR, filename)
    if os.path.exists(path):
        os.remove(path)
        return {"ok": True}
    raise HTTPException(404, "文件不存在")


# =========================================================================
# 路由：对话历史
# =========================================================================

@app.get("/api/history")
async def get_history():
    if not os.path.exists(MEMORY_FILE):
        return []
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


@app.delete("/api/history")
async def clear_history():
    queue_command({"action": "clear_memory"})
    return {"ok": True}


# =========================================================================
# 路由：生成图画廊
# =========================================================================

@app.get("/api/images")
async def list_images():
    items = []
    if os.path.isdir(GENERATED_DIR):
        for f in sorted(os.listdir(GENERATED_DIR), reverse=True):
            if f.lower().endswith((".png", ".jpg", ".jpeg")):
                path = os.path.join(GENERATED_DIR, f)
                items.append({
                    "filename": f,
                    "url": f"/generated/{f}",
                    "size_kb": round(os.path.getsize(path) / 1024, 1),
                    "mtime": os.path.getmtime(path),
                })
    return items


@app.delete("/api/images/{filename}")
async def delete_image(filename: str):
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "非法文件名")
    path = os.path.join(GENERATED_DIR, filename)
    if os.path.exists(path):
        os.remove(path)
        return {"ok": True}
    raise HTTPException(404, "文件不存在")


# =========================================================================
# 路由：手动触发（遥控器）
# =========================================================================

class SpeakReq(BaseModel):
    text: str


@app.post("/api/trigger/record")
async def trigger_record():
    queue_command({"action": "ptt_start"})
    return {"ok": True}


@app.post("/api/trigger/stop")
async def trigger_stop():
    queue_command({"action": "ptt_stop"})
    return {"ok": True}


@app.post("/api/trigger/interrupt")
async def trigger_interrupt():
    queue_command({"action": "interrupt"})
    return {"ok": True}


@app.post("/api/trigger/capture")
async def trigger_capture():
    queue_command({"action": "capture"})
    return {"ok": True}


@app.post("/api/trigger/speak")
async def trigger_speak(req: SpeakReq):
    queue_command({"action": "speak", "text": req.text})
    return {"ok": True}


# =========================================================================
# 路由：日志流（SSE）
# =========================================================================

@app.get("/api/logs/tail")
async def logs_tail(lines: int = 200):
    today = datetime.date.today().isoformat()
    log_file = os.path.join(LOG_DIR, f"{today}.log")
    if not os.path.exists(log_file):
        return {"lines": []}
    with open(log_file, "r", encoding="utf-8") as f:
        content = f.readlines()
    return {"lines": content[-lines:]}


@app.get("/api/logs/stream")
async def logs_stream():
    async def gen():
        today = datetime.date.today().isoformat()
        log_file = os.path.join(LOG_DIR, f"{today}.log")
        last_size = 0
        if os.path.exists(log_file):
            last_size = os.path.getsize(log_file)
        while True:
            try:
                if os.path.exists(log_file):
                    size = os.path.getsize(log_file)
                    if size > last_size:
                        with open(log_file, "r", encoding="utf-8") as f:
                            f.seek(last_size)
                            new = f.read()
                        last_size = size
                        for line in new.splitlines():
                            yield f"data: {line}\n\n"
                await asyncio.sleep(0.5)
            except Exception:
                await asyncio.sleep(1)
    return StreamingResponse(gen(), media_type="text/event-stream")


# =========================================================================
# 路由：可选模型清单（前端下拉用）
# =========================================================================

PRESETS = {
    "llm": [
        {"provider": "siliconflow", "model": "deepseek-ai/DeepSeek-V3", "desc": "DeepSeek V3（综合最强）"},
        {"provider": "siliconflow", "model": "Qwen/Qwen2.5-72B-Instruct", "desc": "Qwen 2.5 72B（中文好）"},
        {"provider": "siliconflow", "model": "Qwen/Qwen2.5-7B-Instruct", "desc": "Qwen 2.5 7B（便宜）"},
        {"provider": "siliconflow", "model": "THUDM/glm-4-9b-chat", "desc": "GLM-4 9B"},
        {"provider": "siliconflow", "model": "meta-llama/Meta-Llama-3.1-8B-Instruct", "desc": "Llama 3.1 8B"},
        {"provider": "openrouter", "model": "anthropic/claude-haiku-4.5", "desc": "Claude Haiku 4.5（OpenRouter）"},
        {"provider": "openrouter", "model": "openai/gpt-4o-mini", "desc": "GPT-4o mini（OpenRouter）"},
    ],
    "vision": [
        {"provider": "openrouter", "model": "openai/gpt-4o-mini", "desc": "GPT-4o mini（OpenRouter）"},
        {"provider": "openrouter", "model": "google/gemini-2.5-flash", "desc": "Gemini 2.5 Flash（OpenRouter）"},
    ],
    "stt": [
        {"provider": "siliconflow", "model": "FunAudioLLM/SenseVoiceSmall", "desc": "SenseVoice 云端（多语种、便宜）"},
        {"provider": "local_sherpa", "model": "models/sense-voice", "desc": "SenseVoice 本地（Sherpa-ONNX，离线免费）"},
    ],
    "tts_fallback": [
        {"provider": "siliconflow", "model": "FunAudioLLM/CosyVoice2-0.5B",
         "voice": "FunAudioLLM/CosyVoice2-0.5B:alex", "desc": "CosyVoice2 alex"},
        {"provider": "siliconflow", "model": "FunAudioLLM/CosyVoice2-0.5B",
         "voice": "FunAudioLLM/CosyVoice2-0.5B:anna", "desc": "CosyVoice2 anna"},
        {"provider": "siliconflow", "model": "fishaudio/fish-speech-1.5",
         "voice": "fishaudio/fish-speech-1.5:alex", "desc": "Fish Speech 1.5"},
    ],
    "image_gen": [
        {"provider": "siliconflow", "model": "Kwai-Kolors/Kolors", "desc": "Kolors（中文 prompt 好）"},
        {"provider": "siliconflow", "model": "black-forest-labs/FLUX.1-schnell", "desc": "FLUX.1-schnell（最快）"},
        {"provider": "siliconflow", "model": "stabilityai/stable-diffusion-3-5-large", "desc": "SD 3.5 Large"},
        {"provider": "openrouter", "model": "google/gemini-3.1-flash-image",
         "desc": "Gemini 3.1 Flash Image（OpenRouter）"},
        {"provider": "openrouter", "model": "google/gemini-3-pro-image",
         "desc": "Gemini 3 Pro Image（OpenRouter）"},
        {"provider": "openrouter", "model": "recraft/recraft-v4.1",
         "desc": "Recraft V4.1（OpenRouter）"},
    ],
}


@app.get("/api/presets")
async def get_presets():
    return PRESETS


# =========================================================================
# 路由：实时拉取 OpenRouter / 硅基流动 全量模型
# =========================================================================

import requests

_models_cache = {"ts": 0, "data": None}
_MODELS_TTL = 600  # 10 分钟缓存


def _openrouter_item(m):
    arch = m.get("architecture") or {}
    pricing = m.get("pricing") or {}
    return {
        "provider": "openrouter",
        "model": m.get("id"),
        "desc": m.get("name") or m.get("id"),
        "modality": arch.get("modality", ""),
        "input_modalities": arch.get("input_modalities") or [],
        "output_modalities": arch.get("output_modalities") or [],
        "pricing": {
            "prompt": pricing.get("prompt"),
            "completion": pricing.get("completion"),
            "image": pricing.get("image"),
        },
    }


def _fetch_openrouter():
    """OpenRouter 模型列表（公开，无需 key）。返回 LLM / Vision / Image Gen。"""
    try:
        r = requests.get("https://openrouter.ai/api/v1/models", timeout=20)
        r.raise_for_status()
        data = r.json().get("data", [])
    except Exception as e:
        print(f"[MODELS] OpenRouter 拉取失败: {e}", flush=True)
        return [], [], []
    llm, vision, image_gen = [], [], []
    for m in data:
        mid = m.get("id")
        if not mid:
            continue
        arch = m.get("architecture") or {}
        inputs = set(arch.get("input_modalities") or [])
        outputs = set(arch.get("output_modalities") or [])
        item = _openrouter_item(m)
        if "text" in outputs:
            llm.append(item)
        if "image" in inputs and "text" in outputs:
            vision.append(item)
        if "image" in outputs:
            image_gen.append(item)

    # 再用 OpenRouter 官方过滤参数补一遍，避免全量列表字段变动时漏掉图片输出模型。
    try:
        r = requests.get(
            "https://openrouter.ai/api/v1/models",
            params={"output_modalities": "image"},
            timeout=20,
        )
        r.raise_for_status()
        for m in r.json().get("data", []):
            if m.get("id"):
                image_gen.append(_openrouter_item(m))
    except Exception as e:
        print(f"[MODELS] OpenRouter image 模型拉取失败: {e}", flush=True)

    return dedupe_models(llm), dedupe_models(vision), dedupe_models(image_gen)


def dedupe_models(items):
    seen, out = set(), []
    for item in items:
        key = (item.get("provider"), item.get("model"))
        if item.get("model") and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _fetch_siliconflow(sub_type, mtype="text"):
    """硅基流动按类型拉取。需要 key。"""
    key = os.getenv("SILICONFLOW_API_KEY", "")
    if not key:
        return []
    try:
        r = requests.get(
            "https://api.siliconflow.cn/v1/models",
            params={"type": mtype, "sub_type": sub_type},
            headers={"Authorization": f"Bearer {key}"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
    except Exception as e:
        print(f"[MODELS] 硅基流动({mtype}/{sub_type}) 拉取失败: {e}", flush=True)
        return []
    return [{"provider": "siliconflow", "model": m["id"], "desc": m["id"]}
            for m in data if m.get("id")]


def _build_live_models():
    or_llm, or_vision, or_img = _fetch_openrouter()
    sf_chat = _fetch_siliconflow("chat", "text")
    sf_img = _fetch_siliconflow("text-to-image", "image")
    sf_tts = _fetch_siliconflow("text-to-speech", "audio")
    sf_stt = _fetch_siliconflow("speech-to-text", "audio")

    # 视觉：硅基流动里名字含 VL / vision / internvl 的算视觉模型
    def is_vision(mid):
        m = mid.lower()
        return any(k in m for k in ("-vl", "vl-", "vision", "internvl", "glm-4v", "qwen-vl", "qwen2-vl", "qwen2.5-vl"))
    sf_vision = [x for x in sf_chat if is_vision(x["model"])]

    return {
        "llm": or_llm + sf_chat,
        "vision": or_vision + sf_vision,
        "image_gen": or_img + sf_img,
        "tts_fallback": sf_tts,
        "stt": sf_stt,
        "counts": {
            "llm": len(or_llm) + len(sf_chat),
            "vision": len(or_vision) + len(sf_vision),
            "image_gen": len(or_img) + len(sf_img),
            "tts_fallback": len(sf_tts),
            "stt": len(sf_stt),
        },
    }


@app.get("/api/models/live")
async def models_live(refresh: bool = False):
    """实时全量模型列表，10 分钟缓存。refresh=true 强制刷新。"""
    now = time.time()
    if not refresh and _models_cache["data"] and now - _models_cache["ts"] < _MODELS_TTL:
        return _models_cache["data"]
    data = await asyncio.to_thread(_build_live_models)
    _models_cache["data"] = data
    _models_cache["ts"] = now
    return data


# =========================================================================
# 路由：API key 管理
# =========================================================================

@app.get("/api/keys")
async def list_keys():
    """只返回是否已配置，不返回 key 本身。"""
    return {
        "siliconflow": bool(os.getenv("SILICONFLOW_API_KEY")),
        "openrouter": bool(os.getenv("OPENROUTER_API_KEY")),
        "openai": bool(os.getenv("OPENAI_API_KEY")),
    }


class UpdateKeyReq(BaseModel):
    provider: str
    key: str


@app.put("/api/keys")
async def update_key(req: UpdateKeyReq):
    env_map = {
        "siliconflow": "SILICONFLOW_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "openai": "OPENAI_API_KEY",
    }
    env_name = env_map.get(req.provider)
    if not env_name:
        raise HTTPException(400, f"未知 provider: {req.provider}")
    # 改 .env 文件
    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    found = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith(f"{env_name}="):
            lines[i] = f"{env_name}={req.key}\n"
            found = True
            break
    if not found:
        lines.append(f"{env_name}={req.key}\n")
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines)
    # 立即生效
    os.environ[env_name] = req.key
    queue_command({"action": "reload_config"})
    return {"ok": True, "note": "已写入 .env，agent 也已重载"}


# =========================================================================
# 路由：Web 控制台自身管理（端口、重启）
# =========================================================================

class PortReq(BaseModel):
    port: int


@app.put("/api/webui/port")
async def set_webui_port(req: PortReq):
    if not (1 <= req.port <= 65535):
        raise HTTPException(400, "端口范围 1~65535")
    if req.port in (22, 80, 443):
        raise HTTPException(400, "请勿使用 22/80/443 等系统端口")
    cfg = load_config()
    cfg["webui_port"] = req.port
    save_config(cfg)
    return {"ok": True, "new_port": req.port, "note": "下次启动 webui 时生效。点'重启 webui'立即生效"}


@app.post("/api/webui/restart")
async def restart_webui():
    """通过 agent 重启 webui（agent 关闭再 spawn 一次）。"""
    queue_command({"action": "restart_webui"})
    return {"ok": True, "note": "已通知 agent 重启 webui，几秒后到新端口刷新页面"}


# =========================================================================
# 路由：开机自启（Pi 桌面 XDG autostart）
# 说明：Pi OS labwc 的系统默认 autostart 会调用 lxsession-xdg-autostart，
#       它负责处理 ~/.config/autostart/*.desktop。所以只用标准 XDG .desktop
#       即可，不要去碰 ~/.config/labwc/autostart（一旦创建会覆盖系统默认、
#       连带把面板和 lxsession-xdg-autostart 一起干掉）。
# =========================================================================

AUTOSTART_DIR = os.path.expanduser("~/.config/autostart")
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, "bmo.desktop")
PROJECT_DIR = os.path.abspath(os.path.dirname(__file__))

# 旧版本残留（需要主动清理，否则会破坏桌面 / 留下 masked 服务）
_LEGACY_LABWC_AUTOSTART = os.path.expanduser("~/.config/labwc/autostart")
_LEGACY_SYSTEMD_SERVICE = os.path.expanduser("~/.config/systemd/user/bmo-agent.service")
_BMO_BEGIN = "# >>> BMO-Online autostart >>>"
_BMO_END = "# <<< BMO-Online autostart <<<"


def _make_desktop_content() -> str:
    # 用 sh -c 包一层 sleep，等合成器/Xwayland 起来再启动 BMO
    start = os.path.join(PROJECT_DIR, "start_agent.sh")
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=BMO Agent\n"
        "Comment=Be More Agent (Online) auto-starter\n"
        f"Exec=sh -c 'sleep 8; exec {start}'\n"
        f"Path={PROJECT_DIR}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def _cleanup_legacy_autostart():
    """清理旧版本写坏的 labwc 覆盖文件和 masked 的 systemd 服务。"""
    # 1. labwc autostart：只删我们标记的块；若文件因此变空则删除整个文件，
    #    让系统重新回退到 /etc/xdg/labwc/autostart（恢复面板）。
    try:
        if os.path.exists(_LEGACY_LABWC_AUTOSTART):
            with open(_LEGACY_LABWC_AUTOSTART, "r", encoding="utf-8") as f:
                content = f.read()
            pattern = re.compile(
                rf"\n?{re.escape(_BMO_BEGIN)}.*?{re.escape(_BMO_END)}\n?", re.DOTALL)
            cleaned = pattern.sub("\n", content).strip()
            if cleaned:
                with open(_LEGACY_LABWC_AUTOSTART, "w", encoding="utf-8") as f:
                    f.write(cleaned + "\n")
            else:
                os.remove(_LEGACY_LABWC_AUTOSTART)
    except Exception:
        pass
    # 2. systemd user 服务：disable + 删除 + unmask，清干净
    for args in (["disable", "bmo-agent.service"],
                 ["unmask", "bmo-agent.service"]):
        try:
            subprocess.run(["systemctl", "--user", *args],
                           capture_output=True, timeout=10)
        except Exception:
            pass
    try:
        if os.path.exists(_LEGACY_SYSTEMD_SERVICE):
            os.remove(_LEGACY_SYSTEMD_SERVICE)
    except Exception:
        pass


def _autostart_enabled() -> bool:
    # 文件存在且非空才算真正开启
    try:
        return os.path.exists(AUTOSTART_FILE) and os.path.getsize(AUTOSTART_FILE) > 0
    except Exception:
        return False


@app.get("/api/autostart")
async def get_autostart():
    return {
        "enabled": _autostart_enabled(),
        "path": AUTOSTART_FILE,
        "project_dir": PROJECT_DIR,
    }


class AutostartReq(BaseModel):
    enabled: bool


@app.put("/api/autostart")
async def set_autostart(req: AutostartReq):
    try:
        # 不管开还是关，都先清掉旧版本写坏的东西
        _cleanup_legacy_autostart()
        if req.enabled:
            os.makedirs(AUTOSTART_DIR, exist_ok=True)
            content = _make_desktop_content()
            with open(AUTOSTART_FILE, "w", encoding="utf-8") as f:
                f.write(content)
            ok = _autostart_enabled()
            return {"ok": ok, "enabled": ok, "path": AUTOSTART_FILE,
                    "bytes": len(content)}
        else:
            if os.path.exists(AUTOSTART_FILE):
                os.remove(AUTOSTART_FILE)
            return {"ok": True, "enabled": False}
    except Exception as e:
        raise HTTPException(500, str(e))


# =========================================================================
# 路由：花费查询（占位，硅基流动暂未提供官方 endpoint）
# =========================================================================

@app.get("/api/cost")
async def get_cost():
    return {
        "note": "硅基流动暂未提供公开的余额/消费查询接口，可在官网控制台查看。",
        "dashboard_url": "https://cloud.siliconflow.cn/account/billing",
    }


# =========================================================================
# 入口
# =========================================================================

def resolve_port() -> int:
    """端口优先级：config.json > 环境变量 > 默认 8087。"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        p = cfg.get("webui_port")
        if isinstance(p, int) and 1 <= p <= 65535:
            return p
    except Exception:
        pass
    try:
        return int(os.getenv("WEBUI_PORT", "8087"))
    except Exception:
        return 8087


if __name__ == "__main__":
    import uvicorn
    port = resolve_port()
    print(f"--- BMO Web 控制台启动: http://0.0.0.0:{port} ---", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
