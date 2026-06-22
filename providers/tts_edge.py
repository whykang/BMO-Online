"""TTS provider —— 微软 Edge-TTS（免费、中文音色好、不要 key）。

输出 MP3，调用 mpg123 解码到 PCM 再走 sounddevice 播放。
也可以直接拿到完整 MP3 数据，主程序自己处理。
"""
import asyncio
import edge_tts


class EdgeTTSProvider:
    def __init__(self, config: dict):
        self.voice = config.get("voice", "zh-CN-XiaoyiNeural")
        self.rate = config.get("rate", "+0%")
        self.volume = config.get("volume", "+0%")

    def synthesize_mp3(self, text: str) -> bytes:
        """同步包装：返回完整 MP3 字节流。"""
        return asyncio.run(self._synth_async(text))

    async def _synth_async(self, text: str) -> bytes:
        communicate = edge_tts.Communicate(
            text, self.voice, rate=self.rate, volume=self.volume
        )
        buf = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
        return bytes(buf)

    @staticmethod
    async def list_voices(language_prefix: str | None = None):
        """列出所有 Edge-TTS 音色，可按语言前缀过滤（如 'zh-CN'）。"""
        voices = await edge_tts.list_voices()
        if language_prefix:
            voices = [v for v in voices if v["ShortName"].startswith(language_prefix)]
        return voices

    @staticmethod
    def list_voices_sync(language_prefix: str | None = None):
        return asyncio.run(EdgeTTSProvider.list_voices(language_prefix))
