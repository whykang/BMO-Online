"""LLM provider —— OpenAI 兼容客户端，可对接硅基流动 / OpenRouter / OpenAI / DeepSeek 等。"""
import os
from openai import OpenAI


class LLMProvider:
    def __init__(self, config: dict, env_endpoints: dict):
        self.provider = config.get("provider", "siliconflow")
        self.model = config.get("model", "deepseek-ai/DeepSeek-V3")
        self.temperature = config.get("temperature", 0.7)
        self.max_tokens = config.get("max_tokens", 512)

        base_url = env_endpoints.get(self.provider)
        if not base_url:
            raise ValueError(f"未知 provider: {self.provider}")
        self.base_url = base_url

        api_key = self._get_key()
        self.client = None
        if api_key:
            # timeout：连接 + 读取都设上限，避免网络卡住时无限等待
            self.client = OpenAI(api_key=api_key, base_url=self.base_url, timeout=30.0, max_retries=1)

    def _get_key(self) -> str:
        env_map = {
            "siliconflow": "SILICONFLOW_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "openai": "OPENAI_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
        }
        return os.getenv(env_map.get(self.provider, ""), "")

    def _client(self):
        api_key = self._get_key()
        if not api_key:
            raise RuntimeError(f"缺少 {self.provider} API key，请在 Web 控制台 API Key 中填写")
        if self.client is None:
            self.client = OpenAI(api_key=api_key, base_url=self.base_url, timeout=30.0, max_retries=1)
        return self.client

    def chat_stream(self, messages):
        """流式聊天，逐 chunk yield 文本片段。"""
        stream = self._client().chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    def chat_once(self, messages) -> str:
        """非流式，一次拿完整回答。"""
        resp = self._client().chat.completions.create(
            model=self.model,
            messages=messages,
            stream=False,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        return resp.choices[0].message.content or ""
