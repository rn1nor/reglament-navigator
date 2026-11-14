"""Переранжирование cross-encoder'ом (bge-reranker-v2-m3). Если модели нет — None, и поиск работает без реранкера."""
from __future__ import annotations

from typing import Optional

import numpy as np

from .. import config


def pick_device() -> str:
    """Устройство для моделей: NAVIGATOR_DEVICE либо лучшее из доступных (mps, cuda, cpu)."""
    if config.DEVICE in ("cpu", "mps", "cuda"):
        return config.DEVICE
    try:
        import torch  # type: ignore

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:  # noqa: BLE001
        pass
    return "cpu"


class Reranker:
    def __init__(self, model_name: str = config.RERANKER_MODEL, max_length: int = 1024):
        from sentence_transformers import CrossEncoder  # type: ignore

        self.device = pick_device()
        # на CPU оценка пары стоит дорого: экономный режим считает меньше пар и обрезает длинные тексты
        self.lite = config.RERANK_BUDGET == "lite" or (config.RERANK_BUDGET == "auto" and self.device == "cpu")
        self.model = CrossEncoder(model_name, max_length=384 if self.lite else max_length, device=self.device)
        self.name = model_name

    def score(self, query: str, texts: list[str]) -> np.ndarray:
        return self.score_pairs([(query, t) for t in texts])

    def score_pairs(self, pairs: list[tuple[str, str]]) -> np.ndarray:
        """Оценки для произвольных пар (запрос, текст) одним вызовом: несколько формулировок запроса считаются одной пачкой."""
        if not pairs:
            return np.zeros(0, dtype=np.float32)
        raw = self.model.predict(list(pairs), batch_size=16, show_progress_bar=False)
        raw = np.asarray(raw, dtype=np.float32).reshape(-1)
        # bge-reranker возвращает логиты; приводим к (0,1)
        if raw.min() < 0 or raw.max() > 1:
            raw = 1.0 / (1.0 + np.exp(-raw))
        return raw


_reranker: Optional[Reranker] = None
_tried = False


def get_reranker(mode: Optional[str] = None) -> Optional[Reranker]:
    global _reranker, _tried
    mode = mode or config.USE_RERANKER
    if mode == "off":
        return None
    if _reranker is None and not _tried:
        _tried = True
        try:
            _reranker = Reranker()
        except Exception as e:  # noqa: BLE001
            if mode == "on":
                raise
            print(f"[rerank] реранкер недоступен ({type(e).__name__}): поиск без переранжирования")
            _reranker = None
    return _reranker
