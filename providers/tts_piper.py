"""本地 TTS —— Piper 语音模型（用已装的 sherpa-onnx OfflineTts 跑，离线、免费）。

模型：vits-piper-zh_CN-huayan-medium（由 setup_pi.sh 下载到 models/piper-zh/）
目录里需要：*.onnx + tokens.txt + espeak-ng-data/
生成 16-bit PCM，由 agent 用 aplay 指定到 USB 音箱播放。
"""
import os
import numpy as np

try:
    import sherpa_onnx
    SHERPA_AVAILABLE = True
except ImportError:
    SHERPA_AVAILABLE = False


class PiperTTSProvider:
    def __init__(self, config: dict):
        if not SHERPA_AVAILABLE:
            raise RuntimeError("sherpa-onnx 未安装：pip install sherpa-onnx")
        model_dir = config.get("piper_model_dir") or "models/piper-zh"
        if not os.path.isdir(model_dir):
            raise RuntimeError(f"Piper 模型目录不存在: {model_dir}（请跑 setup_pi.sh 下载）")
        files = os.listdir(model_dir)
        model = next((f for f in files if f.endswith(".onnx")), None)
        if not model or "tokens.txt" not in files:
            raise RuntimeError(f"Piper 模型不完整，需要 *.onnx + tokens.txt：{model_dir}")
        data_dir = os.path.join(model_dir, "espeak-ng-data")
        lexicon = os.path.join(model_dir, "lexicon.txt")

        vits = sherpa_onnx.OfflineTtsVitsModelConfig(
            model=os.path.join(model_dir, model),
            tokens=os.path.join(model_dir, "tokens.txt"),
            data_dir=data_dir if os.path.isdir(data_dir) else "",
            lexicon=lexicon if os.path.isfile(lexicon) else "",
        )
        tts_config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=vits,
                num_threads=int(config.get("piper_threads", 2)),
                provider="cpu",
            ),
        )
        self.tts = sherpa_onnx.OfflineTts(tts_config)
        self.speed = float(config.get("piper_speed", 1.0))
        self.sid = int(config.get("piper_sid", 0))

    def synthesize_pcm16(self, text: str):
        """返回 (pcm_bytes, sample_rate)；pcm 为 16-bit 单声道。"""
        audio = self.tts.generate(text, sid=self.sid, speed=self.speed)
        samples = np.asarray(audio.samples, dtype=np.float32)
        pcm = np.clip(samples * 32767.0, -32768, 32767).astype("<i2").tobytes()
        return pcm, int(audio.sample_rate)

    @staticmethod
    def is_available() -> bool:
        return SHERPA_AVAILABLE
