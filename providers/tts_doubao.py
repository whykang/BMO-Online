"""TTS provider —— 豆包 / 火山引擎语音合成 2.0（seed-tts，V3 单向流式 WebSocket）。

文档：火山引擎 语音技术 → 语音合成大模型 2.0 单向流式
  endpoint: wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream
  鉴权    : 请求头  X-Api-Key: <api_key>   +   X-Api-Resource-Id: seed-tts-2.0
  协议    : 火山二进制帧（见 volc_protocols.py，官方示例同款）

凭证从环境变量读（网页「API Key」标签或 .env 都能写）：
  VOLC_TTS_API_KEY   —— 火山控制台「语音合成」应用的 API Key（2.0 用单个 apikey，
                        不再是旧版的 appid + token + cluster）

音色(speaker)/采样率/资源 ID 在 config.json 的 tts 段配置。
固定请求 pcm（16-bit 单声道），逐帧 yield，由 agent 边收边播（低首字延迟）。
内部用 asyncio + websockets，包成同步生成器供 agent 的 TTS 线程调用。
"""
import os
import json
import queue
import asyncio
import threading
import uuid

try:
    import websockets  # pip install websockets
    from providers.volc_protocols import (
        EventType, MsgType, full_client_request, receive_message,
    )
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False


class DoubaoTTSProvider:
    WS_URL = "wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream"

    def __init__(self, config: dict):
        if not WS_AVAILABLE:
            raise RuntimeError("缺少 websockets：pip install websockets")
        self.api_key = os.getenv("VOLC_TTS_API_KEY", "")
        if not self.api_key:
            raise RuntimeError(
                "缺少豆包 TTS 凭证：在网页「API Key」或 .env 填 VOLC_TTS_API_KEY"
            )
        self.resource_id = config.get("doubao_resource_id", "seed-tts-2.0")
        self.voice_type = config.get("doubao_voice_type", "zh_female_gaolengyujie_uranus_bigtts")
        self.rate = int(config.get("doubao_rate", 24000))

    def _headers(self) -> dict:
        return {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

    def _body(self, text: str) -> bytes:
        return json.dumps({
            "req_params": {
                "speaker": self.voice_type,
                "text": text,
                "audio_params": {"format": "pcm", "sample_rate": self.rate},
            }
        }).encode("utf-8")

    async def _run(self, text: str, q: "queue.Queue"):
        headers = self._headers()
        try:
            ws = await websockets.connect(
                self.WS_URL, additional_headers=headers, max_size=10 * 1024 * 1024,
            )
        except TypeError:
            # 老版本 websockets 用 extra_headers
            ws = await websockets.connect(
                self.WS_URL, extra_headers=headers, max_size=10 * 1024 * 1024,
            )
        try:
            await full_client_request(ws, self._body(text))
            while True:
                msg = await receive_message(ws)
                if msg.type == MsgType.FullServerResponse and msg.event == EventType.SessionFinished:
                    break
                if msg.type == MsgType.AudioOnlyServer and msg.payload:
                    q.put(msg.payload)
                elif msg.type == MsgType.Error:
                    raise RuntimeError(f"豆包 TTS 错误: {msg}")
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    def synthesize_pcm_stream(self, text: str):
        """同步生成器：后台跑 WS，逐帧 yield PCM bytes（16-bit 单声道）。"""
        q: "queue.Queue" = queue.Queue()
        sentinel = object()

        def worker():
            try:
                asyncio.run(self._run(text, q))
            except Exception as e:  # 把异常透传给主线程，触发 agent 的兜底
                q.put(e)
            finally:
                q.put(sentinel)

        threading.Thread(target=worker, daemon=True).start()
        while True:
            item = q.get()
            if item is sentinel:
                break
            if isinstance(item, Exception):
                raise item
            yield item

    @staticmethod
    def is_available() -> bool:
        return WS_AVAILABLE
