"""Кэш структурных ответов LLM (sqlite). Ключ: провайдер, модель, усилие, схема, системный и пользовательский промпт, хеш PDF.
Даёт воспроизводимые демо и прогоны оценки без повторной оплаты."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Optional, TypeVar

from pydantic import BaseModel

from .. import config

T = TypeVar("T", bound=BaseModel)
CACHE_PATH = Path(os.environ.get("NAVIGATOR_LLM_CACHE_PATH", config.DATA_DIR / "llm_cache.sqlite"))
CACHE_ENABLED = os.environ.get("NAVIGATOR_LLM_CACHE", "1") == "1"


class CachedProvider:
    def __init__(self, inner, path: Path = CACHE_PATH):
        self.inner = inner
        self.kind = inner.kind
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, schema TEXT, value TEXT, created REAL DEFAULT (strftime('%s','now')))")
        self.conn.commit()
        self.hits = 0
        self.misses = 0

    def _key(self, schema: type[BaseModel], **kw) -> str:
        pdf = kw.pop("pdf_bytes", None)
        payload = {"kind": self.kind, "schema": schema.__name__, "schema_json": schema.model_json_schema(), **kw,
                   "pdf": hashlib.sha1(pdf).hexdigest() if pdf else None}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def structured(self, *, system: str, user: str, schema: type[T], model: str, effort: str = "medium",
                   max_tokens: int = 8000, pdf_bytes: Optional[bytes] = None) -> T:
        key = self._key(schema, system=system, user=user, model=model, effort=effort, pdf_bytes=pdf_bytes)
        row = self.conn.execute("SELECT value FROM cache WHERE key=?", (key,)).fetchone()
        if row:
            self.hits += 1
            return schema.model_validate_json(row[0])
        self.misses += 1
        result = self.inner.structured(system=system, user=user, schema=schema, model=model, effort=effort, max_tokens=max_tokens, pdf_bytes=pdf_bytes)
        self.conn.execute("INSERT OR REPLACE INTO cache (key, schema, value) VALUES (?,?,?)", (key, schema.__name__, result.model_dump_json()))
        self.conn.commit()
        return result
