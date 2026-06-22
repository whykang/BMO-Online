# =========================================================================
#  BMO Web 控制台 (FastAPI)
#  浏览器访问: http://树莓派IP:8080
# =========================================================================

import os
import json
import time
import shutil
import hashlib
import datetime
import asyncio
import subprocess
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
    auth = load_auth()
    if auth and auth.get("password_hash") and (not token or token not in SESSION_TOKENS):
        # 需要登录
        return FileResponse("static/login.html")
    return FileResponse("static/index.html")


# =========================================================================
# 路由：登录与密码设置
# =========================================================================

class LoginReq(BaseModel):
    password: str


@app.post("/api/login")
async def login(req: LoginReq, response: Response):
    auth = load_auth()
    if not auth or not auth.get("password_hash"):
        # 没设置密码，直接通过
        token = make_token()
        SESSION_TOKENS.add(token)
        response.set_cookie("token", token, httponly=True)
        return {"ok": True, "no_password": True}
    if hash_password(req.password) == auth["password_hash"]:
        token = make_token()
        SESSION_TOKENS.add(token)
        response.set_cookie("token", token, httponly=True)
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
async def set_password(req: SetPasswordReq, _: dict = None):
    auth = load_auth() or {}
    if auth.get("password_hash"):
        if not req.old_password or hash_password(req.old_password) != auth["password_hash"]:
            raise HTTPException(401, "原密码错误")
    auth["password_hash"] = hash_password(req.new_password)
    save_auth(auth)
    SESSION_TOKENS.clear()  # 强制重新登录
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


@app.put("/api/config")
async def update_config(cfg: dict):
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
# 路由：唤醒词
# =========================================================================

@app.get("/api/wakewords")
async def list_wakewords():
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
        {"provider": "siliconflow", "model": "Qwen/Qwen2-VL-7B-Instruct", "desc": "Qwen2-VL 7B"},
        {"provider": "siliconflow", "model": "OpenGVLab/InternVL2-8B", "desc": "InternVL2 8B"},
        {"provider": "openrouter", "model": "openai/gpt-4o-mini", "desc": "GPT-4o mini"},
    ],
    "stt": [
        {"provider": "siliconflow", "model": "FunAudioLLM/SenseVoiceSmall", "desc": "SenseVoice（多语种、便宜）"},
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
    ],
}


@app.get("/api/presets")
async def get_presets():
    return PRESETS


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

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("WEBUI_PORT", "8080"))
    print(f"--- BMO Web 控制台启动: http://0.0.0.0:{port} ---", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
