"""Защита закрытых данных: внешние LLM API разрешены только для открытых документов (условие организаторов).

Каждая база знаний помечается в kb/policy.json: {"confidential": true|false}. Новая база по умолчанию считается закрытой.
Для закрытой базы внешний провайдер (Claude API или OpenAI-совместимый endpoint вне локальной сети) заменяется режимом без модели,
а в карточке доверия появляется предупреждение. Локальная модель (vLLM, Ollama-совместимый endpoint на localhost или в частной сети)
разрешена всегда. Явное разрешение для открытой базы: policy.json {"confidential": false} или NAVIGATOR_ALLOW_EXTERNAL_LLM=1.
"""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from .. import config


def policy_path(kb_dir: Path) -> Path:
    return Path(kb_dir) / "policy.json"


def kb_is_confidential(kb_dir: Path) -> bool:
    if os.environ.get("NAVIGATOR_ALLOW_EXTERNAL_LLM") == "1":
        return False
    p = policy_path(kb_dir)
    if p.exists():
        try:
            return bool(json.loads(p.read_text(encoding="utf-8")).get("confidential", True))
        except (ValueError, OSError):
            return True
    return True


def set_policy(kb_dir: Path, confidential: bool, note: str = "") -> None:
    Path(kb_dir).mkdir(parents=True, exist_ok=True)
    policy_path(kb_dir).write_text(json.dumps({"confidential": confidential, "note": note}, ensure_ascii=False, indent=1), encoding="utf-8")


def _is_private_host(url: str) -> bool:
    host = urlparse(url).hostname or ""
    if host in ("localhost",) or host.endswith((".local", ".internal", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return False


def is_external(provider) -> bool:
    """True, если запросы уходят за пределы контура организации."""
    kind = getattr(provider, "kind", "mock")
    if kind == "mock":
        return False
    if kind == "openai_compat":
        inner = getattr(provider, "inner", provider)
        return not _is_private_host(getattr(inner, "base_url", config.OPENAI_COMPAT_BASE_URL))
    return True  # anthropic и любые облачные API
