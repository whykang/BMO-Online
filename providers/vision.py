"""Vision provider —— 默认硅基流动 Qwen2-VL。图片 base64 上传，OpenAI 兼容。"""
import os
import base64
from openai import OpenAI


class VisionProvider:
    def __init__(self, config: dict, env_endpoints: dict):
        self.provider = config.get("provider", "siliconflow")
        self.model = config.get("model", "Qwen/Qwen2-VL-7B-Instruct")
        base_url = env_endpoints.get(self.provider, "")
        api_key = self._get_key()
        if not api_key:
            raise RuntimeError(f"{self.provider} 缺少 API key")
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def _get_key(self) -> str:
        env_map = {
            "siliconflow": "SILICONFLOW_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "openai": "OPENAI_API_KEY",
        }
        return os.getenv(env_map.get(self.provider, ""), "")

    def describe(self, image_path: str, user_text: str) -> str:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")

        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": user_text or "请用一句话简短描述这张图片。"},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }]

        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=256,
        )
        return resp.choices[0].message.content or ""
