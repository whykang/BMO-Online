"""文生图 provider —— 硅基流动 Kolors（快手中文文生图）。"""
import os
import time
import requests


class ImageGenProvider:
    def __init__(self, config: dict, env_endpoints: dict, output_dir: str = "generated"):
        self.model = config.get("model", "Kwai-Kolors/Kolors")
        self.size = config.get("size", "1024x1024")
        self.base_url = env_endpoints.get("siliconflow", "")
        self.api_key = os.getenv("SILICONFLOW_API_KEY", "")
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

    def generate(self, prompt: str) -> str | None:
        """生成图片，返回本地文件路径；失败返回 None。"""
        if not self.api_key:
            raise RuntimeError("缺少 SILICONFLOW_API_KEY")

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
            with open(path, "wb") as f:
                f.write(base64.b64decode(url))
        return path
