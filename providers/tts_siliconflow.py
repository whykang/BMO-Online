"""TTS provider —— 硅基流动 CosyVoice2（Edge-TTS 失败时的兜底）。

直接拿 PCM 原始数据（24kHz 16-bit），sounddevice 流式播放。
"""
import os
import requests


class SiliconFlowTTSProvider:
    def __init__(self, config: dict, env_endpoints: dict, sample_rate: int = 24000):
        self.model = config.get("fallback_model", "FunAudioLLM/CosyVoice2-0.5B")
        self.voice = config.get("fallback_voice", "FunAudioLLM/CosyVoice2-0.5B:alex")
        self.base_url = env_endpoints.get("siliconflow", "")
        self.api_key = os.getenv("SILICONFLOW_API_KEY", "")
        self.sample_rate = sample_rate

    def synthesize_pcm_stream(self, text: str):
        """生成器：逐 chunk yield PCM bytes。"""
        if not self.api_key:
            raise RuntimeError("缺少 SILICONFLOW_API_KEY")

        r = requests.post(
            f"{self.base_url}/audio/speech",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "voice": self.voice,
                "input": text,
                "response_format": "pcm",
                "sample_rate": self.sample_rate,
            },
            stream=True,
            timeout=30,
        )
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=4096):
            if chunk:
                yield chunk
