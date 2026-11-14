"""Единый интерфейс к LLM: структурный вывод по Pydantic-схеме. Провайдер выбирается конфигом."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Protocol, TypeVar

from pydantic import BaseModel

from .. import config

T = TypeVar("T", bound=BaseModel)


class LLMProvider(Protocol):
    kind: str

    def structured(self, *, system: str, user: str, schema: type[T], model: str, effort: str = "medium",
                   max_tokens: int = 8000, pdf_bytes: Optional[bytes] = None) -> T: ...


class MockProvider:
    """Заглушка: задачи сами используют эвристики, когда provider.kind == 'mock'."""

    kind = "mock"

    def structured(self, **kwargs):  # noqa: D401
        raise RuntimeError("MockProvider не вызывает модель; используйте эвристический путь")


@dataclass
class ModelSpec:
    model: str
    effort: str


_provider: Optional[LLMProvider] = None


def has_anthropic_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def get_provider(force: Optional[str] = None) -> LLMProvider:
    global _provider
    if _provider is not None and force is None:
        return _provider
    choice = force or config.LLM_PROVIDER
    if choice == "auto":
        choice = "anthropic" if has_anthropic_credentials() else "mock"
    if choice == "anthropic":
        from .anthropic_provider import AnthropicProvider

        p: LLMProvider = AnthropicProvider()
    elif choice == "openai_compat":
        from .openai_compat import OpenAICompatProvider

        p = OpenAICompatProvider()
    else:
        p = MockProvider()
    if p.kind != "mock":
        from .cache import CACHE_ENABLED, CachedProvider

        if CACHE_ENABLED:
            p = CachedProvider(p)
    if force is None:
        _provider = p
    return p


def spec(role: str) -> ModelSpec:
    if role == "generator":
        return ModelSpec(config.MODEL_GENERATOR, config.EFFORT_GENERATOR)
    if role == "extractor":
        return ModelSpec(config.MODEL_EXTRACTOR, config.EFFORT_EXTRACTOR)
    return ModelSpec(config.MODEL_JUDGE, config.EFFORT_JUDGE)
