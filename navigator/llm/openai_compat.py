"""OpenAI-совместимые endpoints (vLLM с Qwen, LiteLLM-прокси, GigaChat/YandexGPT через шлюз).
JSON по схеме через response_format; при отказе сервера схема вкладывается в промпт."""
from __future__ import annotations

import json
import re
from typing import Optional, TypeVar

import httpx
from pydantic import BaseModel

from .. import config

T = TypeVar("T", bound=BaseModel)


class OpenAICompatProvider:
    kind = "openai_compat"

    def __init__(self, base_url: str = config.OPENAI_COMPAT_BASE_URL, api_key: str = config.OPENAI_COMPAT_API_KEY,
                 default_model: str = config.OPENAI_COMPAT_MODEL):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model
        self.client = httpx.Client(timeout=300)

    def _post(self, payload: dict) -> dict:
        r = self.client.post(f"{self.base_url}/chat/completions", json=payload,
                             headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        r.raise_for_status()
        return r.json()

    def structured(self, *, system: str, user: str, schema: type[T], model: str, effort: str = "medium",
                   max_tokens: int = 8000, pdf_bytes: Optional[bytes] = None) -> T:
        if pdf_bytes:
            raise RuntimeError("PDF на вход поддерживается только провайдером anthropic; передайте текст")
        use_model = model if not model.startswith("claude") else self.default_model
        js = schema.model_json_schema()
        payload = {
            "model": use_model, "temperature": 0, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {"name": schema.__name__, "schema": js}},
        }
        try:
            data = self._post(payload)
        except httpx.HTTPStatusError:
            payload.pop("response_format")
            payload["messages"][0]["content"] += "\n\nОтвечай строго одним JSON-объектом по схеме:\n" + json.dumps(js, ensure_ascii=False)
            data = self._post(payload)
        text = data["choices"][0]["message"]["content"]
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S)
        return schema.model_validate_json(text)
