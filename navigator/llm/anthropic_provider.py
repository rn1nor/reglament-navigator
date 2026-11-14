"""Claude через официальный SDK: structured outputs (messages.parse), PDF на вход, серверные fallbacks при отказе."""
from __future__ import annotations

import base64
from typing import Optional, TypeVar

import anthropic
from pydantic import BaseModel

from .. import config

T = TypeVar("T", bound=BaseModel)


class AnthropicProvider:
    kind = "anthropic"

    def __init__(self) -> None:
        self.client = anthropic.Anthropic()
        self.use_fallbacks = config.ANTHROPIC_FALLBACKS

    def structured(self, *, system: str, user: str, schema: type[T], model: str, effort: str = "medium",
                   max_tokens: int = 8000, pdf_bytes: Optional[bytes] = None) -> T:
        content: list[dict] = []
        if pdf_bytes:
            content.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(pdf_bytes).decode("ascii")},
            })
        content.append({"type": "text", "text": user})
        kwargs = dict(model=model, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": content}],
                      output_format=schema, output_config={"effort": effort})
        if self.use_fallbacks:
            try:
                response = self.client.beta.messages.parse(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            except anthropic.BadRequestError as e:
                if "fallback" not in str(e).lower():
                    raise
                self.use_fallbacks = False
                response = self.client.messages.parse(**kwargs)
        else:
            response = self.client.messages.parse(**kwargs)
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise RuntimeError(f"модель отказалась отвечать: {getattr(details, 'category', None)}")
        parsed = response.parsed_output
        if parsed is None:
            text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), "")
            parsed = schema.model_validate_json(text)
        return parsed
