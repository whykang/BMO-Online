"""STT provider —— 云端（硅基流动 SenseVoice）或本地（Sherpa-ONNX + SenseVoice）。"""
import os
import requests


def create_stt(config: dict, env_endpoints: dict):
    """按 config.provider 选择：local_sherpa = 本地离线，其它 = 云端 API。"""
    provider = (config.get("provider") or "siliconflow").lower()
    if provider in ("local_sherpa", "local", "sherpa", "sherpa_onnx"):
        from providers.stt_sherpa import SherpaSTTProvider
        return SherpaSTTProvider(config)
    return STTProvider(config, env_endpoints)


class STTProvider:
    def __init__(self, config: dict, env_endpoints: dict):
        self.provider = config.get("provider", "siliconflow")
        self.model = config.get("model", "FunAudioLLM/SenseVoiceSmall")
        self.base_url = env_endpoints.get(self.provider, "")
        self.api_key = self._get_key()

    def _get_key(self) -> str:
        env_map = {
            "siliconflow": "SILICONFLOW_API_KEY",
            "openai": "OPENAI_API_KEY",
        }
        return os.getenv(env_map.get(self.provider, ""), "")

    def transcribe(self, audio_path: str) -> str:
        if not self.api_key:
            raise RuntimeError(f"{self.provider} 缺少 API key")

        with open(audio_path, "rb") as f:
            files = {"file": (os.path.basename(audio_path), f, "audio/wav")}
            data = {"model": self.model}
            r = requests.post(
                f"{self.base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                files=files,
                data=data,
                timeout=60,
            )
        r.raise_for_status()
        result = r.json()
        return (result.get("text") or "").strip()
