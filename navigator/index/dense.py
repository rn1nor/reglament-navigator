"""Плотные эмбеддинги: BGE-M3 через sentence-transformers, запасной хеш-эмбеддинг символьных n-грамм без torch."""
from __future__ import annotations

import hashlib
import re
from typing import Optional, Protocol

import numpy as np

from .. import config


class Embedder(Protocol):
    name: str
    dim: int

    def encode(self, texts: list[str], is_query: bool = False) -> np.ndarray: ...


class HashEmbedder:
    """Символьные n-граммы (3–5) внутри слов, хешированные в 2^15 измерений. Терпит опечатки и словоформы,
    но не понимает синонимов. Нужен только чтобы конвейер работал без GPU/моделей."""

    name = "hash-ngram"
    dim = 32768

    def _feats(self, text: str) -> dict[int, float]:
        feats: dict[int, float] = {}
        for w in re.findall(r"[а-яёa-z0-9]+", text.lower().replace("ё", "е")):
            padded = f" {w} "
            grams = {padded[i:i + n] for n in (3, 4, 5) for i in range(max(1, len(padded) - n + 1))}
            for g in grams:
                h = int(hashlib.blake2b(g.encode("utf-8"), digest_size=4).hexdigest(), 16) % self.dim
                feats[h] = feats.get(h, 0.0) + 1.0
        return feats

    def encode(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for h, v in self._feats(t).items():
                out[i, h] = np.sqrt(v)
            n = np.linalg.norm(out[i])
            if n > 0:
                out[i] /= n
        return out


class STEmbedder:
    def __init__(self, model_name: str = config.EMBEDDING_MODEL, max_seq_length: int = 1024):
        from sentence_transformers import SentenceTransformer  # type: ignore

        from .rerank import pick_device

        self.model = SentenceTransformer(model_name, device=pick_device())
        self.model.max_seq_length = max_seq_length
        self.name = model_name
        getter = getattr(self.model, "get_embedding_dimension", None) or self.model.get_sentence_embedding_dimension
        self.dim = int(getter() or 1024)

    def encode(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        emb = self.model.encode(texts, batch_size=16, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=len(texts) > 200)
        return emb.astype(np.float32)


_cache: dict[str, Embedder] = {}


def get_embedder(mode: Optional[str] = None) -> Embedder:
    mode = mode or config.USE_DENSE
    key = f"{mode}"
    if key in _cache:
        return _cache[key]
    emb: Embedder
    if mode in ("off", "hash"):
        emb = HashEmbedder()
    else:
        try:
            emb = STEmbedder()
        except Exception as e:  # noqa: BLE001
            if mode == "on":
                raise
            print(f"[dense] sentence-transformers недоступен ({type(e).__name__}): используем хеш-эмбеддинг")
            emb = HashEmbedder()
    _cache[key] = emb
    return emb
