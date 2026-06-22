"""文生图 provider —— 硅基流动 / OpenRouter。"""
import os
import time
import requests


class ImageGenProvider:
    def __init__(self, config: dict, env_endpoints: dict, output_dir: str = "generated"):
        self.provider = config.get("provider", "siliconflow")
        self.model = config.get("model", "Kwai-Kolors/Kolors")
        self.size = config.get("size", "1024x1024")
        self.base_url = env_endpoints.get(self.provider, "")
        self.api_key = self._get_key()
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

    def _get_key(self) -> str:
        env_map = {
            "siliconflow": "SILICONFLOW_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "openai": "OPENAI_API_KEY",
        }
        return os.getenv(env_map.get(self.provider, ""), "")

    def generate(self, prompt: str) -> str | None:
        """生成图片，返回本地文件路径；失败返回 None。"""
        if not self.api_key:
            raise RuntimeError(f"缺少 {self.provider} API key")
        if self.provider == "openrouter":
            return self._generate_openrouter(prompt)
        return self._generate_siliconflow(prompt)

    def _generate_siliconflow(self, prompt: str) -> str | None:
        r = requests.post(
            f"{self.base_url}/images/generations",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "prompt": prompt,
                "image_size": self.size,
                "batch_size": 1,
            },
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        images = data.get("images") or data.get("data") or []
        if not images:
            return None

        # 兼容两种返回结构：{"images":[{"url":...}]} 或 {"data":[{"url":...}]}
        url = images[0].get("url") or images[0].get("b64_json")
        if not url:
            return None
        return self._save_image_url(url, prompt)

    def _generate_openrouter(self, prompt: str) -> str | None:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "modalities": ["image", "text"],
            "stream": False,
            "image_config": self._openrouter_image_config(),
        }
        data = self._post_openrouter(payload)
        url = self._extract_openrouter_image(data)
        if not url:
            # 有些纯图片输出模型不接受 text 输出模态，自动重试一次。
            payload["modalities"] = ["image"]
            data = self._post_openrouter(payload)
            url = self._extract_openrouter_image(data)
        if not url:
            return None
        return self._save_image_url(url, prompt)

    def _post_openrouter(self, payload: dict) -> dict:
        r = requests.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/whykang/BMO-Online",
                "X-Title": "BMO-Online",
            },
            json=payload,
            timeout=180,
        )
        r.raise_for_status()
        return r.json()

    def _openrouter_image_config(self) -> dict:
        aspect_map = {
            "1024x1024": "1:1",
            "512x512": "1:1",
            "768x1024": "3:4",
            "1024x768": "4:3",
        }
        size_map = {
            "512x512": "0.5K",
            "1024x1024": "1K",
            "768x1024": "1K",
            "1024x768": "1K",
        }
        return {
            "aspect_ratio": aspect_map.get(self.size, "1:1"),
            "image_size": size_map.get(self.size, "1K"),
        }

    def _extract_openrouter_image(self, data: dict) -> str | None:
        choices = data.get("choices") or []
        if not choices:
            return None
        msg = choices[0].get("message") or {}
        images = msg.get("images") or []
        for image in images:
            image_url = image.get("image_url") or image.get("imageUrl") or {}
            if isinstance(image_url, dict):
                url = image_url.get("url")
            else:
                url = image_url
            if url:
                return url

        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                image_url = part.get("image_url") or part.get("imageUrl") or {}
                if isinstance(image_url, dict) and image_url.get("url"):
                    return image_url["url"]
                if isinstance(image_url, str):
                    return image_url
        return None

    def _save_image_url(self, url: str, prompt: str) -> str:
        filename = f"{int(time.time())}_{prompt[:20].replace('/', '_')}.png"
        # 避免文件名里的中文/特殊字符把 Windows 弄坏（树莓派 Linux 没事，但保险一点）
        safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in filename)
        path = os.path.join(self.output_dir, safe_name)

        if url.startswith("http"):
            img = requests.get(url, timeout=60).content
            with open(path, "wb") as f:
                f.write(img)
        else:
            import base64
            if "," in url and url.startswith("data:"):
                url = url.split(",", 1)[1]
            with open(path, "wb") as f:
                f.write(base64.b64decode(url))
        return path
