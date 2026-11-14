"""Гибридный индекс пунктов: BM25 + плотные векторы, слияние RRF, фильтры по статусу/роли, сохранение на диск."""
from __future__ import annotations

import json
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
from rank_bm25 import BM25Okapi

from ..schemas import Clause, Document, KnowledgeBase
from .dense import Embedder, get_embedder
from .text import tokenize

RRF_K = 60


@dataclass
class Unit:
    uid: int
    doc_id: str
    clause_id: str
    text: str
    index_text: str
    status: str
    roles: list[str]
    doc_type: str
    doc_date: Optional[str]
    page: Optional[int]
    kind: str
    path: list[str] = field(default_factory=list)


@dataclass
class Hit:
    unit: Unit
    score: float
    bm25: float = 0.0
    dense: float = 0.0
    bm25_rank: Optional[int] = None
    dense_rank: Optional[int] = None
    rerank: Optional[float] = None


@dataclass
class Filters:
    statuses: Optional[set[str]] = None  # None = без фильтра
    roles: Optional[set[str]] = None
    doc_ids: Optional[set[str]] = None
    exclude_doc_ids: Optional[set[str]] = None

    def accept(self, u: Unit) -> bool:
        if self.statuses is not None and u.status not in self.statuses:
            return False
        if self.doc_ids is not None and u.doc_id not in self.doc_ids:
            return False
        if self.exclude_doc_ids and u.doc_id in self.exclude_doc_ids:
            return False
        if self.roles is not None and "all" not in u.roles and not (self.roles & set(u.roles)):
            return False
        return True


def _text_key(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def index_dirname(embedder_name: str) -> str:
    import re

    return "index-" + re.sub(r"[^\w.-]+", "_", embedder_name.split("/")[-1].lower())


def kb_fingerprint(kb: KnowledgeBase) -> str:
    return f"{kb.built_at}|{len(kb.documents)}|{sum(len(d.clauses) for d in kb.documents)}"


def _unit_from_clause(uid: int, d: Document, c: Clause) -> Unit:
    crumbs = [p for p in c.path if p][-2:]
    head = " / ".join(crumbs)
    label = f"п. {c.number}" if c.number else ""
    index_text = "\n".join(x for x in (d.title, head, f"{label} {c.text}".strip()) if x)
    return Unit(uid=uid, doc_id=d.id, clause_id=c.id, text=c.text, index_text=index_text, status=d.status, roles=list(d.roles),
                doc_type=d.doc_type, doc_date=d.date.isoformat() if d.date else None, page=c.page_start, kind=c.kind, path=list(c.path))


def iter_units(kb: KnowledgeBase) -> Iterable[Unit]:
    uid = 0
    for d in kb.documents:
        for c in d.clauses:
            if c.deleted or c.kind == "heading" or len(c.text) < 15:
                continue
            if c.text.count("_") >= 16 or c.text.count("…") >= 8:
                continue  # бланки и образцы заявлений
            yield _unit_from_clause(uid, d, c)
            uid += 1


class HybridIndex:
    def __init__(self, units: list[Unit], tokens: list[list[str]], embeddings: Optional[np.ndarray], embedder: Embedder):
        self.units = units
        self.tokens = tokens
        self.bm25 = BM25Okapi(tokens) if tokens else None
        self.embeddings = embeddings
        self.embedder = embedder
        self.by_key = {(u.doc_id, u.clause_id): u for u in units}

    # ------------------------------------------------------------ build / io
    @classmethod
    def build(cls, kb: KnowledgeBase, embedder: Optional[Embedder] = None, dense: bool = True, cache_dir: Optional[Path] = None) -> HybridIndex:
        """cache_dir: каталог прежнего индекса; эмбеддинги неизменившихся пунктов берутся оттуда (пересборка за секунды)."""
        embedder = embedder or get_embedder()
        units = list(iter_units(kb))
        tokens = [tokenize(u.index_text) for u in units]
        emb = None
        if dense and units:
            cache: dict[str, np.ndarray] = {}
            if cache_dir and (Path(cache_dir) / "dense.npy").exists() and (Path(cache_dir) / "meta.json").exists():
                meta = json.loads((Path(cache_dir) / "meta.json").read_text(encoding="utf-8"))
                if meta.get("embedder") == embedder.name:
                    old_units = json.loads((Path(cache_dir) / "units.json").read_text(encoding="utf-8"))
                    old_emb = np.load(Path(cache_dir) / "dense.npy")
                    if len(old_units) == old_emb.shape[0]:
                        for u, v in zip(old_units, old_emb):
                            cache[_text_key(u["index_text"])] = v
            emb = np.zeros((len(units), embedder.dim), dtype=np.float32)
            todo = [i for i, u in enumerate(units) if _text_key(u.index_text) not in cache]
            for i, u in enumerate(units):
                k = _text_key(u.index_text)
                if k in cache:
                    emb[i] = cache[k]
            if todo:
                fresh = embedder.encode([units[i].index_text for i in todo])
                for j, i in enumerate(todo):
                    emb[i] = fresh[j]
            if cache:
                print(f"[index] эмбеддинги: из кэша {len(units) - len(todo)}, посчитано заново {len(todo)}")
        idx = cls(units, tokens, emb, embedder)
        idx.kb_fingerprint = kb_fingerprint(kb)
        return idx

    def save(self, out_dir: Path) -> None:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "units.json").write_text(json.dumps([u.__dict__ for u in self.units], ensure_ascii=False), encoding="utf-8")
        with open(out_dir / "tokens.pkl", "wb") as f:
            pickle.dump(self.tokens, f)
        if self.embeddings is not None:
            np.save(out_dir / "dense.npy", self.embeddings)
        (out_dir / "meta.json").write_text(json.dumps({"embedder": self.embedder.name, "dim": self.embedder.dim, "n": len(self.units),
                                                        "kb_fingerprint": getattr(self, "kb_fingerprint", "")}), encoding="utf-8")

    @classmethod
    def load(cls, in_dir: Path, embedder: Optional[Embedder] = None) -> HybridIndex:
        in_dir = Path(in_dir)
        units = [Unit(**u) for u in json.loads((in_dir / "units.json").read_text(encoding="utf-8"))]
        if (in_dir / "tokens.pkl").exists():
            with open(in_dir / "tokens.pkl", "rb") as f:
                tokens = pickle.load(f)  # noqa: S301 (свой файл)
        else:
            tokens = [tokenize(u.index_text) for u in units]  # токены BM25 дёшево восстановить из текста пунктов
        meta = json.loads((in_dir / "meta.json").read_text(encoding="utf-8"))
        embedder = embedder or get_embedder()
        emb = np.load(in_dir / "dense.npy") if (in_dir / "dense.npy").exists() else None
        if emb is not None and (meta.get("embedder") != embedder.name or emb.shape[1] != embedder.dim):
            print(f"[index] эмбеддер изменился ({meta.get('embedder')} -> {embedder.name}): плотный поиск отключён до пересборки")
            emb = None
        idx = cls(units, tokens, emb, embedder)
        idx.kb_fingerprint = meta.get("kb_fingerprint", "")
        return idx

    @staticmethod
    def is_stale(in_dir: Path, kb: KnowledgeBase, need_dense: bool = False) -> bool:
        mp = Path(in_dir) / "meta.json"
        if not mp.exists() or not (Path(in_dir) / "units.json").exists():
            return True
        if need_dense and not (Path(in_dir) / "dense.npy").exists():
            return True  # векторы не сохранены (например, чистый клон): пересчитать
        return json.loads(mp.read_text(encoding="utf-8")).get("kb_fingerprint") != kb_fingerprint(kb)

    # ------------------------------------------------------------ search
    def search(self, queries: list[str], top_k: int = 40, filters: Optional[Filters] = None, use_dense: bool = True) -> list[Hit]:
        if not self.units:
            return []
        filters = filters or Filters()
        allowed = np.array([filters.accept(u) for u in self.units], dtype=bool)
        if not allowed.any():
            return []
        fused: dict[int, Hit] = {}

        def add_list(scores: np.ndarray, kind: str) -> None:
            masked = np.where(allowed, scores, -np.inf)
            order = np.argsort(-masked)[: top_k * 2]
            rank = 0
            for idx in order:
                if not np.isfinite(masked[idx]) or masked[idx] <= 0:
                    break
                rank += 1
                h = fused.get(int(idx))
                if h is None:
                    h = Hit(unit=self.units[int(idx)], score=0.0)
                    fused[int(idx)] = h
                h.score += 1.0 / (RRF_K + rank)
                if kind == "bm25":
                    h.bm25 = max(h.bm25, float(scores[idx]))
                    h.bm25_rank = rank if h.bm25_rank is None else min(h.bm25_rank, rank)
                else:
                    h.dense = max(h.dense, float(scores[idx]))
                    h.dense_rank = rank if h.dense_rank is None else min(h.dense_rank, rank)

        for q in queries:
            if self.bm25 is not None:
                toks = tokenize(q)
                if toks:
                    add_list(np.asarray(self.bm25.get_scores(toks), dtype=np.float32), "bm25")
            if use_dense and self.embeddings is not None:
                qv = self.embedder.encode([q], is_query=True)[0]
                add_list(self.embeddings @ qv, "dense")
        hits = sorted(fused.values(), key=lambda h: -h.score)
        return hits[:top_k]

    def bm25_max(self, query: str) -> float:
        if self.bm25 is None:
            return 0.0
        toks = tokenize(query)
        return float(np.max(self.bm25.get_scores(toks))) if toks else 0.0
