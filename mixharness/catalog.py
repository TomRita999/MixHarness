import asyncio
from dataclasses import dataclass, field

import httpx2
from google import genai
from google.genai.errors import APIError as GeminiError
from openai import APIError as OpenAIError, AsyncOpenAI

from .reasoning import api_group


# 无密钥时展示官方文档中的常用文本模型。首项也是新会话默认值。
MODELS = {
    "gemini": ["gemini-4-hydrogen", "gemini-4-neon", "gemini-4-carbon", "gemini-4-argon",
               "gemini-3.8-flash", "gemini-3.5-flash"],
    "openai": ["gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.1-terra", "gpt-6-luna", "gpt-6.1-sol",
               "gpt-6-astra", "gpt-6-sol", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
               "gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-4.1"],
}


async def account_models(provider, key):
    if provider == "gemini":
        options = {"api_version": "v1beta", "timeout": 20000}
        async with genai.Client(api_key=key, vertexai=False, http_options=options).aio as client:
            models = await client.models.list(config={"page_size": 100})
            return {m.name.removeprefix("models/") async for m in models if m.name}
    async with AsyncOpenAI(api_key=key, timeout=20, max_retries=0) as client:
        return {m.id async for m in client.models.list() if m.id}


@dataclass
class ModelCatalog:
    provider: str
    key: str = field(repr=False)
    listed: list[str] | None = field(default=None, init=False)
    error: str = field(default="", init=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def view(self):
        models = MODELS[self.provider] if self.listed is None else self.listed
        return {"models": models, "groups": [api_group(self.provider, m) for m in models],
                "source": "reference" if self.listed is None else "account",
                "configured": bool(self.key), "loading": self.lock.locked(), "error": self.error}

    async def refresh(self):
        if not self.key:
            return
        if self.lock.locked():
            async with self.lock:
                return
        async with self.lock:
            self.error = ""
            try:
                async with asyncio.timeout(30):
                    found = await account_models(self.provider, self.key)
            except (GeminiError, OpenAIError, httpx2.HTTPError, TimeoutError, OSError) as exc:
                code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
                if code in (401, 403):
                    self.error = f"密钥或项目权限未通过（HTTP {code}）"
                elif code == 429:
                    self.error = "服务商限制了请求频率，请稍后刷新"
                else:
                    self.error = f"模型目录请求失败（HTTP {code}）" if isinstance(code, int) else "无法连接服务商，请检查网络后刷新"
                return
            preferred = [model for model in MODELS[self.provider] if model in found]
            self.listed = preferred + sorted(found.difference(preferred), key=str.casefold)
