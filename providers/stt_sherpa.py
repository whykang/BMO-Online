"""本地 STT —— Sherpa-ONNX + SenseVoice（离线、不联网、不花钱）。

模型：sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17
由 setup_pi.sh 下载到 models/sense-voice/（约 230MB，太大不放仓库）。
"""
import os
import wave
import numpy as np

try:
    import sherpa_onnx
    SHERPA_AVAILABLE = True
except ImportError:
    SHERPA_AVAILABLE = False

try:
    import scipy.signal
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False


def _find_model(files):
    """优先 int8 量化版。"""
    for f in files:
        if f.endswith(".onnx") and "int8" in f and "sense" in f.lower():
            return f
    for f in files:
        if f.endswith(".onnx") and "sense" in f.lower():
            return f
    for f in files:
        if f.endswith(".onnx") and "int8" in f:
            return f
    for f in files:
        if f.endswith(".onnx"):
            return f
    return None


def _read_wav_16k_mono(path):
    """读 wav → 16kHz 单声道 float32（-1~1）。"""
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        ch = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        raw = wf.readframes(n)
    if sampwidth != 2:
        raise RuntimeError(f"只支持 16-bit wav，当前 {sampwidth*8}bit")
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)
    if sr != 16000:
        if SCIPY_AVAILABLE:
            n_out = int(round(len(audio) * 16000 / sr))
            audio = scipy.signal.resample(audio, n_out).astype(np.float32)
        else:
            # 退化：最近邻重采样
            idx = (np.arange(int(len(audio) * 16000 / sr)) * sr / 16000).astype(int)
            idx = idx[idx < len(audio)]
            audio = audio[idx]
        sr = 16000
    return audio, sr


class SherpaSTTProvider:
    def __init__(self, config: dict):
        if not SHERPA_AVAILABLE:
            raise RuntimeError("sherpa-onnx 未安装：pip install sherpa-onnx")
        model_dir = config.get("model_dir") or config.get("model") or "models/sense-voice"
        if not os.path.isdir(model_dir):
            raise RuntimeError(f"本地 STT 模型目录不存在: {model_dir}（请跑 setup_pi.sh 下载）")
        files = os.listdir(model_dir)
        model = _find_model(files)
        tokens = next((f for f in files if f == "tokens.txt"), None)
        if not model or not tokens:
            raise RuntimeError(f"模型不完整，需要 *.onnx + tokens.txt：{model_dir}")
        num_threads = int(config.get("num_threads", 2))
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=os.path.join(model_dir, model),
            tokens=os.path.join(model_dir, tokens),
            num_threads=num_threads,
            use_itn=True,
            language=config.get("language", "auto"),
        )

    def transcribe(self, audio_path: str) -> str:
        audio, sr = _read_wav_16k_mono(audio_path)
        stream = self.recognizer.create_stream()
        stream.accept_waveform(sr, audio)
        self.recognizer.decode_stream(stream)
        return (stream.result.text or "").strip()

    @staticmethod
    def is_available() -> bool:
        return SHERPA_AVAILABLE
