"""Sherpa-ONNX 唤醒词 / 关键词检测（中文原生支持，无需训练）。

工作原理：用一个小型流式 ASR 模型实时转写，匹配预设关键词。
这个 wenetspeech KWS 模型用的是【拼音 token】（声母 + 带声调韵母），
所以中文关键词要先用 pypinyin 转换，例如：
    你好小明 → n ǐ h ǎo x iǎo m íng @你好小明
转换后每个 token 都会和模型的 tokens.txt 校验，避免喂坏数据导致进程崩溃。
"""
import os

try:
    import sherpa_onnx
    SHERPA_AVAILABLE = True
except ImportError:
    SHERPA_AVAILABLE = False

try:
    from pypinyin import pinyin, Style
    PYPINYIN_AVAILABLE = True
except ImportError:
    PYPINYIN_AVAILABLE = False

import numpy as np


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿"


def _find_one(files, *patterns):
    """优先选 int8 量化版（更快、更小）。"""
    for p in patterns:
        # 先找 int8
        for f in files:
            if p in f and f.endswith(".onnx") and "int8" in f:
                return f
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
            raise RuntimeError("sherpa-onnx 未安装。请运行：pip install sherpa-onnx")
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

        tokens_path = os.path.join(model_dir, tokens_file)
        self._valid_tokens = self._load_valid_tokens(tokens_path)

        # 把关键词转成拼音 token 行，校验后写入临时文件
        self.keywords_file = os.path.join(model_dir, "_bmo_keywords.txt")
        self.accepted, self.rejected = self._write_keywords_file(keywords)
        if not self.accepted:
            raise RuntimeError(
                f"没有可用关键词（全部无法转换/校验失败）。被拒: {self.rejected}"
            )

        self.spotter = sherpa_onnx.KeywordSpotter(
            encoder=os.path.join(model_dir, encoder),
            decoder=os.path.join(model_dir, decoder),
            joiner=os.path.join(model_dir, joiner),
            tokens=tokens_path,
            keywords_file=self.keywords_file,
            num_threads=num_threads,
            keywords_score=float(score),
            keywords_threshold=float(threshold),
            num_trailing_blanks=1,
            provider="cpu",
        )
        self.stream = self.spotter.create_stream()

    # ---------------------------------------------------------------
    @staticmethod
    def _load_valid_tokens(tokens_path) -> set:
        valid = set()
        with open(tokens_path, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split()
                if parts:
                    valid.add(parts[0])
        return valid

    def _keyword_to_tokens(self, keyword: str):
        """中文 → ['n','ǐ','h','ǎo',...]。返回 None 表示无法转换（含未知 token）。"""
        toks = []
        for ch in keyword.strip():
            if ch.isspace():
                continue
            if _is_cjk(ch):
                if not PYPINYIN_AVAILABLE:
                    return None
                ini = pinyin(ch, style=Style.INITIALS, strict=False)
                fin = pinyin(ch, style=Style.FINALS_TONE, strict=False)
                i = ini[0][0] if ini and ini[0] else ""
                f = fin[0][0] if fin and fin[0] else ""
                if i:
                    toks.append(i)
                if f:
                    toks.append(f)
            else:
                # 非中文（英文/数字）：这个拼音模型基本不支持，按大写字母拆
                toks.append(ch.upper())
        # 校验所有 token 都在模型词表里
        for t in toks:
            if t not in self._valid_tokens:
                return None
        return toks if toks else None

    def _write_keywords_file(self, keywords):
        accepted, rejected = [], []
        lines = []
        for kw in keywords:
            kw = kw.strip()
            if not kw:
                continue
            toks = self._keyword_to_tokens(kw)
            if toks:
                lines.append(f"{' '.join(toks)} @{kw}")
                accepted.append(kw)
            else:
                rejected.append(kw)
        with open(self.keywords_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return accepted, rejected

    # ---------------------------------------------------------------
    def feed(self, pcm_int16: np.ndarray) -> str | None:
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

    @staticmethod
    def preview_tokens(model_dir: str, keyword: str):
        """给 webui 用：预览某个关键词会转成什么 token，以及是否可用。"""
        tokens_path = os.path.join(model_dir, "tokens.txt")
        if not os.path.exists(tokens_path):
            return {"ok": False, "reason": "tokens.txt 不存在"}
        valid = SherpaWakeWord._load_valid_tokens(tokens_path)
        toks = []
        bad = []
        for ch in keyword.strip():
            if ch.isspace():
                continue
            if _is_cjk(ch):
                if not PYPINYIN_AVAILABLE:
                    return {"ok": False, "reason": "未安装 pypinyin"}
                ini = pinyin(ch, style=Style.INITIALS, strict=False)
                fin = pinyin(ch, style=Style.FINALS_TONE, strict=False)
                i = ini[0][0] if ini and ini[0] else ""
                f = fin[0][0] if fin and fin[0] else ""
                for t in (i, f):
                    if t:
                        toks.append(t)
                        if t not in valid:
                            bad.append(t)
            else:
                t = ch.upper()
                toks.append(t)
                if t not in valid:
                    bad.append(t)
        return {"ok": len(bad) == 0, "tokens": toks, "unknown": bad}
