"""Sherpa-ONNX 唤醒词 / 关键词检测（中文原生支持，无需训练）。

工作原理：用一个小型流式 ASR 模型实时转写，匹配预设关键词列表。
关键词在 config.json 里就是普通中文字符串，启动时自动转成 sherpa 需要的
token 格式写入临时 keywords.txt。
"""
import os
import numpy as np

try:
    import sherpa_onnx
    SHERPA_AVAILABLE = True
except ImportError:
    SHERPA_AVAILABLE = False


def _split_chinese_to_tokens(text: str) -> str:
    """把'你好小明' → '你 好 小 明'。
    sherpa-onnx 的 wenetspeech KWS 模型用字符级 token，
    每个中文字符一个 token，用空格分隔。
    英文/数字按原样保留，与汉字间用空格隔开。
    """
    out = []
    for ch in text.strip():
        if ch.isspace():
            continue
        out.append(ch)
    return " ".join(out)


def _find_one(files, *patterns):
    for p in patterns:
        for f in files:
            if p in f and f.endswith(".onnx"):
                return f
    return None


class SherpaWakeWord:
    """关键词检测：喂 16kHz int16 PCM，返回命中的关键词字符串或 None。"""

    def __init__(self, model_dir: str, keywords: list[str],
                 threshold: float = 0.25, score: float = 1.5,
                 num_threads: int = 2):
        if not SHERPA_AVAILABLE:
            raise RuntimeError(
                "sherpa-onnx 未安装。请运行：pip install sherpa-onnx"
            )
        if not os.path.isdir(model_dir):
            raise RuntimeError(f"Sherpa 模型目录不存在: {model_dir}")
        if not keywords:
            raise RuntimeError("未配置任何关键词")

        files = os.listdir(model_dir)
        encoder = _find_one(files, "encoder")
        decoder = _find_one(files, "decoder")
        joiner = _find_one(files, "joiner")
        tokens_file = next((f for f in files if f == "tokens.txt"), None)
        if not all([encoder, decoder, joiner, tokens_file]):
            raise RuntimeError(
                f"模型文件不完整，需要 encoder/decoder/joiner/tokens.txt：{model_dir}"
            )

        # 把关键词写入 model_dir 下的临时文件，sherpa 在初始化时一次性读取
        self.keywords_file = os.path.join(model_dir, "_bmo_keywords.txt")
        self._write_keywords_file(keywords)

        self.keywords = list(keywords)
        self.spotter = sherpa_onnx.KeywordSpotter(
            encoder=os.path.join(model_dir, encoder),
            decoder=os.path.join(model_dir, decoder),
            joiner=os.path.join(model_dir, joiner),
            tokens=os.path.join(model_dir, tokens_file),
            keywords_file=self.keywords_file,
            num_threads=num_threads,
            sample_rate=16000,
            feature_dim=80,
            keywords_score=float(score),
            keywords_threshold=float(threshold),
            num_trailing_blanks=1,
            provider="cpu",
        )
        self.stream = self.spotter.create_stream()

    def _write_keywords_file(self, keywords):
        with open(self.keywords_file, "w", encoding="utf-8") as f:
            for kw in keywords:
                kw = kw.strip()
                if not kw:
                    continue
                tokens = _split_chinese_to_tokens(kw)
                # 格式：tok1 tok2 tok3 @display_name
                f.write(f"{tokens} @{kw}\n")

    def feed(self, pcm_int16: np.ndarray) -> str | None:
        """喂一段 16kHz int16 PCM，返回检测到的关键词（如 '你好小明'）或 None。"""
        if pcm_int16.dtype != np.int16:
            pcm_int16 = pcm_int16.astype(np.int16)
        audio = pcm_int16.astype(np.float32) / 32768.0
        self.stream.accept_waveform(16000, audio)
        while self.spotter.is_ready(self.stream):
            self.spotter.decode_stream(self.stream)
        result = self.spotter.get_result(self.stream)
        if result:
            self.spotter.reset_stream(self.stream)
            return result
        return None

    def reset(self):
        try:
            self.spotter.reset_stream(self.stream)
        except Exception:
            pass

    @staticmethod
    def is_available() -> bool:
        return SHERPA_AVAILABLE
