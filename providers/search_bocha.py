"""博查(Bocha) Web Search API provider —— 给 BMO 联网搜索能力。

申请 key：https://open.bochaai.com  （环境变量 BOCHA_API_KEY）
接口：POST https://api.bochaai.com/v1/web-search
"""
import os
import requests

BOCHA_ENDPOINT = "https://api.bochaai.com/v1/web-search"


class BochaSearchProvider:
    def __init__(self, config: dict | None = None):
        config = config or {}
        self.endpoint = config.get("endpoint", BOCHA_ENDPOINT)
        self.count = int(config.get("count", 8))
        self.freshness = config.get("freshness", "noLimit")

    @staticmethod
    def _get_key() -> str:
        return os.getenv("BOCHA_API_KEY", "")

    @staticmethod
    def is_configured() -> bool:
        return bool(os.getenv("BOCHA_API_KEY"))

    def search(self, query: str, count: int | None = None) -> list[dict]:
        """调用博查搜索，返回结果列表 [{title,url,snippet,site}, ...]。"""
        key = self._get_key()
        if not key:
            raise RuntimeError("缺少 BOCHA_API_KEY，请在 Web 控制台「API Key」里填写博查搜索 key")
        r = requests.post(
            self.endpoint,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "query": query,
                "summary": True,
                "count": int(count or self.count),
                "freshness": self.freshness,
            },
            timeout=30,
        )
        r.raise_for_status()
        return self._parse(r.json())

    @staticmethod
    def _parse(data: dict) -> list[dict]:
        # 兼容结构：{"data":{"webPages":{"value":[...]}}}，也容忍直接给 webPages 的情况
        d = data.get("data") if isinstance(data.get("data"), dict) else data
        pages = ((d.get("webPages") or {}).get("value")) or []
        out = []
        for p in pages:
            if not isinstance(p, dict):
                continue
            out.append({
                "title": (p.get("name") or "").strip(),
                "url": (p.get("url") or "").strip(),
                "snippet": (p.get("summary") or p.get("snippet") or "").strip(),
                "site": (p.get("siteName") or "").strip(),
            })
        return out

    def search_digest(self, query: str, max_items: int = 5, max_chars: int = 1500) -> str:
        """搜索并拼成喂给 LLM 的纯文本摘要；无结果返回空串。"""
        results = self.search(query)
        if not results:
            return ""
        lines = []
        for i, item in enumerate(results[:max_items], 1):
            piece = f"{i}. {item['title']}" if item["title"] else f"{i}."
            if item["snippet"]:
                piece += f"：{item['snippet']}"
            lines.append(piece)
        return "\n".join(lines)[:max_chars]
