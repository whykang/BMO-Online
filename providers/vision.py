"""Vision provider —— 默认硅基流动 Qwen2-VL。图片 base64 上传，OpenAI 兼容。"""
import os
import base64
import requests
from openai import APIStatusError, OpenAI


class VisionProvider:
    def __init__(self, config: dict, env_endpoints: dict):
        self.provider = config.get("provider", "siliconflow")
        self.model = config.get("model", "Qwen/Qwen2-VL-7B-Instruct")
        self.base_url = env_endpoints.get(self.provider, "")
        self.api_key = self._get_key()
        self.client = None
        if self.api_key:
            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=45.0, max_retries=1)

    def _get_key(self) -> str:
        env_map = {
            "siliconflow": "SILICONFLOW_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "openai": "OPENAI_API_KEY",
        }
        return os.getenv(env_map.get(self.provider, ""), "")

    def _client(self):
        self.api_key = self._get_key()
        if not self.api_key:
            raise RuntimeError(f"{self.provider} 缺少 API key，请在 Web 控制台 API Key 中填写")
        if self.client is None:
            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=45.0, max_retries=1)
        return self.client

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

        try:
            resp = self._describe_once(messages, self.model)
        except APIStatusError as e:
            if not self._is_model_missing_error(e):
                raise
            fallback = self._find_available_vision_model()
            if not fallback or fallback == self.model:
                raise
            print(f"[VISION] 模型不存在，自动切换到可用模型: {fallback}", flush=True)
            self.model = fallback
            resp = self._describe_once(messages, fallback)
        return resp.choices[0].message.content or ""

    def _describe_once(self, messages, model: str):
        args = {"model": model, "messages": messages}
        if self.provider == "openai" and model.lower().startswith(("gpt-5", "o1", "o3", "o4")):
            args["max_completion_tokens"] = 256
            if model.lower().startswith("gpt-5"):
                args["reasoning_effort"] = "low"
        else:
            args["max_tokens"] = 256
        return self._client().chat.completions.create(**args)

    def _is_model_missing_error(self, err: APIStatusError) -> bool:
        body = getattr(err, "body", None)
        text = str(body or err)
        return getattr(err, "status_code", None) == 400 and (
            "Model does not exist" in text or "20012" in text
        )

    def _find_available_vision_model(self) -> str | None:
        try:
            if self.provider == "siliconflow":
                r = requests.get(
                    f"{self.base_url}/models",
                    params={"type": "text", "sub_type": "chat"},
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=20,
                )
            else:
                r = requests.get(f"{self.base_url}/models", timeout=20)
            r.raise_for_status()
            models = r.json().get("data", [])
        except Exception as e:
            print(f"[VISION] 拉取模型列表失败，无法自动切换: {e}", flush=True)
            return None

        candidates = []
        for m in models:
            mid = m.get("id")
            if not mid:
                continue
            if self.provider == "openrouter":
                arch = m.get("architecture") or {}
                inputs = set(arch.get("input_modalities") or [])
                outputs = set(arch.get("output_modalities") or [])
                if "image" in inputs and "text" in outputs:
                    candidates.append(mid)
            elif self._looks_like_vision_model(mid):
                candidates.append(mid)

        if not candidates:
            return None
        candidates.sort(key=self._vision_priority)
        return candidates[0]

    def _looks_like_vision_model(self, model_id: str) -> bool:
        m = model_id.lower()
        return any(k in m for k in (
            "-vl", "vl-", "vision", "internvl", "glm-4v", "qwen-vl",
            "qwen2-vl", "qwen2.5-vl", "qwen3-vl", "qvq",
        ))

    def _vision_priority(self, model_id: str):
        m = model_id.lower()
        preferred = (
            "qwen2.5-vl-32b", "qwen2.5-vl-72b", "qwen3-vl",
            "qwen2-vl-72b", "qwen2.5-vl-7b", "qwen2-vl-7b",
            "internvl", "glm-4v", "qvq",
        )
        for i, token in enumerate(preferred):
            if token in m:
                return i
        return len(preferred)
