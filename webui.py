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
import threading
import subprocess
import shlex
import re
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request, Response, Cookie
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse, RedirectResponse
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
CAPTURES_DIR = "captures"
WAKEWORDS_DIR = "wakewords"
ROMS_DIR = "roms"
MEDIA_DIR = "media"
MUSIC_DIR = os.path.join(MEDIA_DIR, "music")
VIDEOS_DIR = os.path.join(MEDIA_DIR, "videos")
ENV_FILE = ".env"

os.makedirs(GENERATED_DIR, exist_ok=True)
os.makedirs(CAPTURES_DIR, exist_ok=True)

app = FastAPI(title="BMO Web Control")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/generated", StaticFiles(directory=GENERATED_DIR), name="generated")
app.mount("/captures", StaticFiles(directory=CAPTURES_DIR), name="captures")
os.makedirs(WAKEWORDS_DIR, exist_ok=True)
os.makedirs(ROMS_DIR, exist_ok=True)
os.makedirs(MUSIC_DIR, exist_ok=True)
os.makedirs(VIDEOS_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
app.mount("/wakewords", StaticFiles(directory=WAKEWORDS_DIR), name="wakewords")
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")

# =========================================================================
# 工具
# =========================================================================

def _atomic_write_json(path: str, data, indent=None):
    """原子写 JSON：先写同目录临时文件 + fsync，再 os.replace 覆盖。
    避免树莓派掉电 / agent 同时读时拿到半截或损坏的文件。"""
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
    if not os.path.exists(CONFIG_FILE) and os.path.exists("config.default.json"):
        shutil.copy("config.default.json", CONFIG_FILE)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    rec = cfg.setdefault("recording", {})
    if float(rec.get("silence_duration", 1.0)) <= 1.0:
        rec["silence_duration"] = 1.8
    if float(rec.get("max_record_seconds", 12.0)) <= 12.0:
        rec["max_record_seconds"] = 25.0
    return cfg


def save_config(cfg: dict):
    _atomic_write_json(CONFIG_FILE, cfg, indent=2)
    # 通知 agent 重载
    queue_command({"action": "reload_config"})


_command_lock = threading.Lock()


def queue_command(cmd: dict):
    """往 commands.json 追加一条命令；agent 主线程会 poll 它。
    加进程内锁 + 原子写，避免多个请求并发追加时互相覆盖丢命令。"""
    with _command_lock:
        cmds = []
        if os.path.exists(COMMANDS_FILE):
            try:
                with open(COMMANDS_FILE, "r", encoding="utf-8") as f:
                    cmds = json.load(f)
            except Exception:
                cmds = []
        cmds.append(cmd)
        _atomic_write_json(COMMANDS_FILE, cmds)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "state": "unknown",
        "status": "agent 未运行",
        "tts_queue_len": 0,
        "memory_turns": 0,
        "game": {"running": False, "paused": False, "rom": None},
        "media": {"running": False, "kind": None, "name": None},
    }


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
    _atomic_write_json(AUTH_FILE, data, indent=2)


SESSION_TOKENS: set[str] = set()


def make_token() -> str:
    return hashlib.sha256(os.urandom(32)).hexdigest()


# 无需登录即可访问的路径：登录页本身、登录/登出/鉴权状态接口、静态资源。
# 其余所有 /api/* 和受保护的静态挂载（生成图/抓拍/唤醒词/媒体）都需要有效 token。
_PUBLIC_PATHS = {"/", "/api/auth/status", "/api/login", "/api/logout"}
_PUBLIC_PREFIXES = ("/static/",)


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """统一鉴权：之前只有 HTML 首页校验密码，所有 /api/* 都是裸奔的。
    这里在入口处集中拦截，没设密码时保持原有'留空=无密码'的放行语义。"""
    auth = load_auth()
    if not (auth and auth.get("password_hash")):
        return await call_next(request)

    path = request.url.path
    if path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
        return await call_next(request)

    token = request.cookies.get("token")
    if token and token in SESSION_TOKENS:
        return await call_next(request)

    if path.startswith("/api/"):
        return JSONResponse({"detail": "未授权，请先登录"}, status_code=401)
    return RedirectResponse("/")


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


# =========================================================================
# 路由：系统硬件信息（仪表盘用）
# =========================================================================

def _read_cpu_times():
    try:
        with open("/proc/stat", "r") as f:
            parts = f.readline().split()[1:]
        v = [int(x) for x in parts[:8]]
        return v[3] + v[4], sum(v)   # idle, total
    except Exception:
        return None


def _cpu_temp_c():
    for p in ("/sys/class/thermal/thermal_zone0/temp", "/sys/class/hwmon/hwmon0/temp1_input"):
        try:
            with open(p) as f:
                raw = f.read().strip()
            if raw:
                v = float(raw)
                return round(v / 1000.0 if v > 200 else v, 1)
        except Exception:
            pass
    try:
        out = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True, text=True, timeout=2).stdout
        m = re.search(r"temp=([\d.]+)", out)
        if m:
            return round(float(m.group(1)), 1)
    except Exception:
        pass
    return None


def _meminfo():
    try:
        data = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                data[k] = int(v.strip().split()[0]) * 1024
        total = data.get("MemTotal", 0)
        avail = data.get("MemAvailable", 0)
        used = max(0, total - avail)
        return {"total": total, "used": used,
                "percent": round(used / total * 100, 1) if total else None}
    except Exception:
        return None


def _pi_model():
    try:
        with open("/proc/device-tree/model") as f:
            return f.read().strip("\x00").strip()
    except Exception:
        return ""


def _uptime_text():
    try:
        with open("/proc/uptime") as f:
            sec = int(float(f.read().split()[0]))
        d, r = divmod(sec, 86400)
        h, r = divmod(r, 3600)
        m = r // 60
        return (f"{d}天" if d else "") + (f"{h}小时" if h else "") + f"{m}分钟"
    except Exception:
        return "未知"


@app.get("/api/sysinfo")
async def sysinfo():
    cpu = None
    a = _read_cpu_times()
    if a:
        await asyncio.sleep(0.15)
        b = _read_cpu_times()
        if b and b[1] - a[1] > 0:
            cpu = round(max(0.0, min(100.0, (1.0 - (b[0] - a[0]) / (b[1] - a[1])) * 100.0)), 1)
    try:
        du = shutil.disk_usage("/")
        disk = {"total": du.total, "used": du.used,
                "percent": round(du.used / du.total * 100, 1)}
    except Exception:
        disk = None
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except Exception:
        load = None
    throttled = None
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2).stdout
        m = re.search(r"throttled=(0x[0-9a-fA-F]+)", out)
        if m:
            t = int(m.group(1), 16)
            throttled = {"under_voltage": bool(t & 0x1), "throttled": bool(t & 0x4),
                         "raw": m.group(1)}
    except Exception:
        pass
    return {
        "model": _pi_model(),
        "cpu_percent": cpu,
        "temp_c": _cpu_temp_c(),
        "mem": _meminfo(),
        "disk": disk,
        "load": load,
        "cpu_count": os.cpu_count(),
        "uptime": _uptime_text(),
        "throttled": throttled,
    }


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


@app.get("/api/audio/inputs")
async def audio_inputs():
    """列出 PortAudio 可用输入设备，ALSA 硬件列表作为兜底。"""
    items = [{"device": "auto", "label": "自动检测（推荐）", "kind": "AUTO"}]
    seen = set()
    warnings = []

    def append_item(device, label, kind):
        key = str(device).strip().lower()
        if not key or key in seen or any(len(key) >= 5 and key in old for old in seen):
            return
        seen.add(key)
        items.append({"device": device, "label": label, "kind": kind})

    # agent 使用 sounddevice/PortAudio 录音，这里的列表优先采用相同后端，
    # 避免 arecord 能看到、PortAudio 却无法按该名称匹配。
    try:
        import sounddevice as sd
        hostapis = sd.query_hostapis()
        for dev in sd.query_devices():
            channels = int(dev.get("max_input_channels", 0) or 0)
            if channels <= 0:
                continue
            name = str(dev.get("name", "")).strip()
            if not name:
                continue
            lower_name = name.lower()
            virtual = ("sysdefault", "default", "pulse", "pipewire", "dmix", "front", "surround")
            if lower_name in virtual or lower_name.startswith(("surround", "dmix:", "front:")):
                continue
            host_idx = int(dev.get("hostapi", -1))
            host_name = ""
            if 0 <= host_idx < len(hostapis):
                host_name = str(hostapis[host_idx].get("name", ""))
            tag = f"{name} {host_name}".lower()
            kind = "USB" if any(x in tag for x in ("usb", "dji", "mic mini")) else "其它"
            rate = int(float(dev.get("default_samplerate", 0) or 0))
            detail = f"{channels}ch" + (f" / {rate}Hz" if rate else "")
            append_item(name, f"[{kind}] {name} ({detail})", kind)
    except Exception as e:
        warnings.append(f"PortAudio: {e}")

    try:
        env = dict(os.environ)
        env.update({"LC_ALL": "C", "LANG": "C"})
        proc = subprocess.run(
            ["arecord", "-l"], capture_output=True, text=True, timeout=8, env=env,
        )
        if proc.returncode != 0 and proc.stderr.strip():
            warnings.append(f"arecord: {proc.stderr.strip()}")
        out = proc.stdout
        for line in out.splitlines():
            m = re.match(r"\s*card (\d+): (\S+) \[(.*?)\].*device (\d+):", line, re.I)
            if not m:
                continue
            card, cid, name, dev = m.group(1), m.group(2), m.group(3), m.group(4)
            tag = f"{cid} {name}".lower()
            kind = "USB" if any(x in tag for x in ("usb", "dji", "mic mini")) else "其它"
            append_item(name, f"[{kind}] card {card}: {name} (hw:{card},{dev})", kind)
    except Exception as e:
        warnings.append(f"arecord: {e}")
    return {"devices": items, "warnings": warnings}


@app.get("/api/volume")
async def get_volume():
    # 软件音量为准（不被 PipeWire 重置）
    cfg = load_config()
    return {"ok": True, "volume": int(cfg.get("volume_percent", 100))}


class VolumeReq(BaseModel):
    percent: int


@app.put("/api/volume")
async def set_volume(req: VolumeReq):
    # 允许到 200%：软件增益可放大（>100%），解决 USB 音箱本身偏小的问题
    pct = max(0, min(200, int(req.percent)))
    # 只写软件增益到 config（TTS/Piper/音效都会乘上它）。
    # 硬件 amixer 不在这里设：PipeWire 会在每段播放后把它复位（表现为"这句大、
    # 下一句又变小"）。改由 agent 在每次播放前把硬件顶到 100%，软件增益做唯一音量旋钮。
    cfg = load_config()
    cfg["volume_percent"] = pct
    save_config(cfg)
    return {"ok": True, "volume": pct}


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
    if "/" in file.filename or "\\" in file.filename or ".." in file.filename:
        raise HTTPException(400, "非法文件名")
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
# 路由：游戏 ROM 管理（.nes / .zip）
# =========================================================================
ROM_EXTS = (".nes", ".zip", ".fds", ".unf")


@app.get("/api/roms")
async def list_roms():
    """列出 roms/ 里的 ROM 文件。"""
    items = []
    if os.path.isdir(ROMS_DIR):
        for f in sorted(os.listdir(ROMS_DIR)):
            if f.lower().endswith(ROM_EXTS):
                p = os.path.join(ROMS_DIR, f)
                items.append({
                    "filename": f,
                    "size_kb": round(os.path.getsize(p) / 1024, 1),
                })
    return {"roms": items}


@app.post("/api/roms/upload")
async def upload_rom(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(ROM_EXTS):
        raise HTTPException(400, "只接受 .nes / .zip / .fds / .unf 文件")
    if "/" in file.filename or ".." in file.filename:
        raise HTTPException(400, "非法文件名")
    dest = os.path.join(ROMS_DIR, file.filename)
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"ok": True, "filename": file.filename}


@app.delete("/api/roms/{filename}")
async def delete_rom(filename: str):
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "非法文件名")
    path = os.path.join(ROMS_DIR, filename)
    if os.path.exists(path):
        os.remove(path)
        return {"ok": True}
    raise HTTPException(404, "文件不存在")


class RenameRomReq(BaseModel):
    old: str
    new: str


@app.post("/api/roms/rename")
async def rename_rom(req: RenameRomReq):
    old, new = req.old.strip(), req.new.strip()
    if not old or not new:
        raise HTTPException(400, "文件名不能为空")
    for x in (old, new):
        if "/" in x or "\\" in x or ".." in x:
            raise HTTPException(400, "非法文件名")
    src = os.path.join(ROMS_DIR, old)
    if not os.path.exists(src):
        raise HTTPException(404, "原文件不存在")
    # 新名没带 ROM 后缀就沿用原后缀
    if not new.lower().endswith(ROM_EXTS):
        new = new + os.path.splitext(old)[1]
    dst = os.path.join(ROMS_DIR, new)
    if os.path.abspath(dst) != os.path.abspath(src) and os.path.exists(dst):
        raise HTTPException(409, "已存在同名文件")
    os.rename(src, dst)
    return {"ok": True, "filename": new}


class GameReq(BaseModel):
    action: str
    filename: str | None = None


@app.post("/api/game")
async def control_game(req: GameReq):
    action = (req.action or "").lower().strip()
    if action not in ("start", "pause", "resume", "stop"):
        raise HTTPException(400, "action 必须是 start/pause/resume/stop")
    if action == "start":
        filename = (req.filename or "").strip()
        if not filename:
            raise HTTPException(400, "请选择 ROM")
        if "/" in filename or ".." in filename:
            raise HTTPException(400, "非法文件名")
        if not filename.lower().endswith(ROM_EXTS):
            raise HTTPException(400, "只接受 .nes / .zip / .fds / .unf 文件")
        if not os.path.exists(os.path.join(ROMS_DIR, filename)):
            raise HTTPException(404, "ROM 不存在")
        queue_command({"action": "start_game", "game": filename})
    elif action == "pause":
        queue_command({"action": "pause_game"})
    elif action == "resume":
        queue_command({"action": "resume_game"})
    elif action == "stop":
        queue_command({"action": "stop_game"})
    return {"ok": True, "note": "已发送游戏控制命令"}


# =========================================================================
# 路由：媒体管理（音乐 / 视频）
# =========================================================================
MUSIC_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus")
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v")


def _media_info(kind: str):
    kind = (kind or "").lower().strip()
    if kind in ("music", "audio", "song", "songs"):
        return "music", MUSIC_DIR, MUSIC_EXTS
    if kind in ("video", "videos", "movie", "movies"):
        return "video", VIDEOS_DIR, VIDEO_EXTS
    raise HTTPException(400, "kind 必须是 music 或 video")


def _check_media_filename(filename: str, exts: tuple[str, ...]):
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "非法文件名")
    if not filename.lower().endswith(exts):
        raise HTTPException(400, "不支持的文件类型")


@app.get("/api/media/{kind}")
async def list_media(kind: str):
    kind, folder, exts = _media_info(kind)
    items = []
    if os.path.isdir(folder):
        for f in sorted(os.listdir(folder)):
            if f.lower().endswith(exts):
                p = os.path.join(folder, f)
                url_dir = "music" if kind == "music" else "videos"
                items.append({
                    "filename": f,
                    "size_kb": round(os.path.getsize(p) / 1024, 1),
                    "url": f"/media/{url_dir}/{f}",
                })
    return {"kind": kind, "items": items}


@app.post("/api/media/{kind}/upload")
async def upload_media(kind: str, file: UploadFile = File(...)):
    _, folder, exts = _media_info(kind)
    _check_media_filename(file.filename, exts)
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, file.filename)
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"ok": True, "filename": file.filename}


@app.delete("/api/media/{kind}/{filename}")
async def delete_media(kind: str, filename: str):
    _, folder, exts = _media_info(kind)
    _check_media_filename(filename, exts)
    path = os.path.join(folder, filename)
    if os.path.exists(path):
        os.remove(path)
        return {"ok": True}
    raise HTTPException(404, "文件不存在")


class RenameMediaReq(BaseModel):
    old: str
    new: str


@app.post("/api/media/{kind}/rename")
async def rename_media(kind: str, req: RenameMediaReq):
    _, folder, exts = _media_info(kind)
    old, new = req.old.strip(), req.new.strip()
    _check_media_filename(old, exts)
    if not new or "/" in new or "\\" in new or ".." in new:
        raise HTTPException(400, "非法文件名")
    src = os.path.join(folder, old)
    if not os.path.exists(src):
        raise HTTPException(404, "原文件不存在")
    if not new.lower().endswith(exts):
        new = new + os.path.splitext(old)[1]
    _check_media_filename(new, exts)
    dst = os.path.join(folder, new)
    if os.path.abspath(dst) != os.path.abspath(src) and os.path.exists(dst):
        raise HTTPException(409, "已存在同名文件")
    os.rename(src, dst)
    return {"ok": True, "filename": new}


class MediaReq(BaseModel):
    action: str
    kind: str | None = None
    filename: str | None = None


@app.post("/api/media/control")
async def control_media(req: MediaReq):
    action = (req.action or "").lower().strip()
    if action == "stop":
        queue_command({"action": "stop_media"})
        return {"ok": True, "note": "已发送媒体控制命令"}
    if action != "play":
        raise HTTPException(400, "action 必须是 play/stop")
    kind, folder, exts = _media_info(req.kind or "")
    filename = (req.filename or "").strip()
    _check_media_filename(filename, exts)
    if not os.path.exists(os.path.join(folder, filename)):
        raise HTTPException(404, "媒体文件不存在")
    queue_command({"action": "play_media", "kind": kind, "name": filename})
    return {"ok": True, "note": "已发送媒体控制命令"}


# =========================================================================
# 路由：对话历史
# =========================================================================

@app.get("/api/history")
async def get_history():
    if not os.path.exists(MEMORY_FILE):
        return []
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 不显示 system（内部设定，不是对话），只看用户/助手的真实对话
        return [m for m in data if m.get("role") != "system"]
    except Exception:
        return []


@app.delete("/api/history")
async def clear_history():
    queue_command({"action": "clear_memory"})
    return {"ok": True}


# =========================================================================
# 路由：图片
# =========================================================================

def _list_image_dir(folder: str, url_prefix: str):
    items = []
    if os.path.isdir(folder):
        for f in sorted(os.listdir(folder), reverse=True):
            if f.lower().endswith((".png", ".jpg", ".jpeg")):
                path = os.path.join(folder, f)
                items.append({
                    "filename": f,
                    "url": f"{url_prefix}/{f}",
                    "size_kb": round(os.path.getsize(path) / 1024, 1),
                    "mtime": os.path.getmtime(path),
                })
    return items


@app.get("/api/images")
async def list_images():
    return {
        "generated": _list_image_dir(GENERATED_DIR, "/generated"),
        "captures": _list_image_dir(CAPTURES_DIR, "/captures"),
    }


@app.delete("/api/images/{kind}/{filename}")
async def delete_image_by_kind(kind: str, filename: str):
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "非法文件名")
    folders = {
        "generated": GENERATED_DIR,
        "captures": CAPTURES_DIR,
    }
    folder = folders.get(kind)
    if not folder:
        raise HTTPException(400, "未知图片类型")
    path = os.path.join(folder, filename)
    if os.path.exists(path):
        os.remove(path)
        return {"ok": True}
    raise HTTPException(404, "文件不存在")


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


@app.post("/api/trigger/wake")
async def trigger_wake():
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


class PrintReq(BaseModel):
    type: str                 # text | history | photo | generated
    content: str | None = None
    count: int | None = None


@app.post("/api/print")
async def trigger_print(req: PrintReq):
    t = (req.type or "").lower()
    if t not in ("text", "history", "photo", "generated"):
        raise HTTPException(400, "type 必须是 text/history/photo/generated")
    cmd = {"action": "print", "print_type": t}
    if t == "text":
        if not (req.content and req.content.strip()):
            raise HTTPException(400, "打印文字不能为空")
        cmd["content"] = req.content
    if t == "history" and req.count:
        cmd["count"] = int(req.count)
    queue_command(cmd)
    return {"ok": True, "note": "已发送打印任务"}


# =========================================================================
# 路由：日志流（SSE）
# =========================================================================

def _current_log_file() -> str:
    """优先今天的 agent 日志；若它还没有内容（比如 agent 进程跨天仍在写昨天的文件），
    就取 logs/ 里最新的那个按日期命名的 .log，避免跨午夜后日志面板变空。"""
    today = os.path.join(LOG_DIR, f"{datetime.date.today().isoformat()}.log")
    if os.path.exists(today) and os.path.getsize(today) > 0:
        return today
    try:
        cands = [
            os.path.join(LOG_DIR, f) for f in os.listdir(LOG_DIR)
            if f.endswith(".log") and f != "webui.log"
        ]
        cands = [p for p in cands if os.path.isfile(p)]
        if cands:
            return max(cands, key=os.path.getmtime)
    except Exception:
        pass
    return today


@app.get("/api/logs/tail")
async def logs_tail(lines: int = 200):
    log_file = _current_log_file()
    if not os.path.exists(log_file):
        return {"lines": []}
    with open(log_file, "r", encoding="utf-8") as f:
        content = f.readlines()
    return {"lines": content[-lines:]}


@app.get("/api/logs/download")
async def logs_download():
    """下载当前日志文件（面板里正在看的那份）。"""
    log_file = _current_log_file()
    if not os.path.exists(log_file):
        raise HTTPException(404, "暂无日志")
    return FileResponse(
        log_file,
        media_type="text/plain; charset=utf-8",
        filename=os.path.basename(log_file),
    )


@app.get("/api/logs/stream")
async def logs_stream():
    async def gen():
        log_file = _current_log_file()
        last_size = os.path.getsize(log_file) if os.path.exists(log_file) else 0
        ticks = 0
        while True:
            try:
                # 每 ~5s 重新挑一次最新日志文件（跨午夜 / agent 重启后自动跟上）
                ticks += 1
                if ticks % 10 == 0:
                    newest = _current_log_file()
                    if newest != log_file:
                        log_file = newest
                        last_size = 0
                if os.path.exists(log_file):
                    size = os.path.getsize(log_file)
                    if size < last_size:   # 文件被换了/截断，从头读
                        last_size = 0
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
        {"provider": "deepseek", "model": "deepseek-v4-flash", "desc": "DeepSeek V4 Flash（快）"},
        {"provider": "deepseek", "model": "deepseek-v4-pro", "desc": "DeepSeek V4 Pro（强）"},
        {"provider": "openai", "model": "gpt-5.2", "desc": "GPT-5.2"},
        {"provider": "openai", "model": "gpt-5-mini", "desc": "GPT-5 mini（快速、经济）"},
        {"provider": "openai", "model": "gpt-4.1", "desc": "GPT-4.1"},
        {"provider": "openai", "model": "gpt-4.1-mini", "desc": "GPT-4.1 mini（快速、经济）"},
        {"provider": "openai", "model": "gpt-4o-mini", "desc": "GPT-4o mini（轻量）"},
    ],
    "vision": [
        {"provider": "openrouter", "model": "openai/gpt-4o-mini", "desc": "GPT-4o mini（OpenRouter）"},
        {"provider": "openrouter", "model": "google/gemini-2.5-flash", "desc": "Gemini 2.5 Flash（OpenRouter）"},
        {"provider": "openai", "model": "gpt-5.2", "desc": "GPT-5.2 视觉"},
        {"provider": "openai", "model": "gpt-5-mini", "desc": "GPT-5 mini 视觉"},
        {"provider": "openai", "model": "gpt-4.1-mini", "desc": "GPT-4.1 mini 视觉"},
        {"provider": "openai", "model": "gpt-4o-mini", "desc": "GPT-4o mini 视觉"},
    ],
    "stt": [
        {"provider": "siliconflow", "model": "FunAudioLLM/SenseVoiceSmall", "desc": "SenseVoice 云端（多语种、便宜）"},
        {"provider": "local_sherpa", "model": "models/sense-voice", "desc": "SenseVoice 本地（Sherpa-ONNX，离线免费）"},
        {"provider": "openai", "model": "gpt-4o-transcribe", "desc": "GPT-4o Transcribe"},
        {"provider": "openai", "model": "gpt-4o-mini-transcribe", "desc": "GPT-4o mini Transcribe（经济）"},
        {"provider": "openai", "model": "whisper-1", "desc": "Whisper（经典）"},
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
        {"provider": "openai", "model": "gpt-image-1.5", "desc": "GPT Image 1.5（画质最佳）"},
        {"provider": "openai", "model": "gpt-image-1", "desc": "GPT Image 1"},
        {"provider": "openai", "model": "gpt-image-1-mini", "desc": "GPT Image 1 mini（经济）"},
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


def _fetch_openai():
    """拉取当前账号可用的 OpenAI 模型，并按 BMO 支持的接口分类。"""
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        return [], [], [], []
    try:
        r = requests.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=20,
        )
        r.raise_for_status()
        model_ids = [m.get("id", "") for m in r.json().get("data", [])]
    except Exception as e:
        print(f"[MODELS] OpenAI 拉取失败: {e}", flush=True)
        return [], [], [], []

    def is_chat(mid):
        name = mid.lower()
        excluded = (
            "audio", "realtime", "transcribe", "tts", "embedding", "moderation",
            "whisper", "dall-e", "gpt-image", "sora", "codex", "search",
        )
        return (name.startswith("gpt-") or name.startswith(("o1", "o3", "o4"))) \
            and not any(token in name for token in excluded)

    def is_vision(mid):
        name = mid.lower()
        return is_chat(mid) and name.startswith(("gpt-4o", "gpt-4.1", "gpt-5", "o1", "o3", "o4"))

    def item(mid, modalities):
        return {
            "provider": "openai",
            "model": mid,
            "desc": mid,
            "input_modalities": modalities[0],
            "output_modalities": modalities[1],
        }

    llm = [item(mid, (["text"], ["text"])) for mid in model_ids if is_chat(mid)]
    vision = [item(mid, (["text", "image"], ["text"])) for mid in model_ids if is_vision(mid)]
    image_gen = [
        item(mid, (["text", "image"], ["image"]))
        for mid in model_ids
        if mid.lower().startswith(("gpt-image", "chatgpt-image-latest"))
    ]
    stt = [
        item(mid, (["audio"], ["text"]))
        for mid in model_ids
        if "transcribe" in mid.lower() or mid.lower() == "whisper-1"
    ]
    return (
        dedupe_models(llm), dedupe_models(vision),
        dedupe_models(image_gen), dedupe_models(stt),
    )


def _build_live_models():
    or_llm, or_vision, or_img = _fetch_openrouter()
    oa_llm, oa_vision, oa_img, oa_stt = _fetch_openai()
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
        "llm": or_llm + sf_chat + oa_llm,
        "vision": or_vision + sf_vision + oa_vision,
        "image_gen": or_img + sf_img + oa_img,
        "tts_fallback": sf_tts,
        "stt": sf_stt + oa_stt,
        "counts": {
            "llm": len(or_llm) + len(sf_chat) + len(oa_llm),
            "vision": len(or_vision) + len(sf_vision) + len(oa_vision),
            "image_gen": len(or_img) + len(sf_img) + len(oa_img),
            "tts_fallback": len(sf_tts),
            "stt": len(sf_stt) + len(oa_stt),
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
        "deepseek": bool(os.getenv("DEEPSEEK_API_KEY")),
        "volc_apikey": bool(os.getenv("VOLC_TTS_API_KEY")),
        "bocha": bool(os.getenv("BOCHA_API_KEY")),
    }


KEY_ENV_MAP = {
    "siliconflow": "SILICONFLOW_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "volc_apikey": "VOLC_TTS_API_KEY",
    "bocha": "BOCHA_API_KEY",
}


class UpdateKeyReq(BaseModel):
    provider: str
    key: str


@app.put("/api/keys")
async def update_key(req: UpdateKeyReq):
    env_name = KEY_ENV_MAP.get(req.provider)
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
    _models_cache.update({"ts": 0, "data": None})
    queue_command({"action": "reload_config"})
    return {"ok": True, "note": "已写入 .env，agent 也已重载"}


@app.delete("/api/keys/{provider}")
async def delete_key(provider: str):
    env_name = KEY_ENV_MAP.get(provider)
    if not env_name:
        raise HTTPException(400, f"未知 provider: {provider}")
    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    kept = []
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith(f"{env_name}=") or stripped.startswith(f"export {env_name}="):
            continue
        kept.append(ln)
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(kept)
    os.environ.pop(env_name, None)
    _models_cache.update({"ts": 0, "data": None})
    queue_command({"action": "reload_config"})
    return {"ok": True, "note": "已从 .env 删除，agent 也已重载"}


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
