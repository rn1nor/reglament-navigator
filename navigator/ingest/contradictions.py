"""Возможные противоречия между действующими пунктами разных документов.

Кандидаты: пары пунктов с высоким сходством эмбеддингов, разными числовыми параметрами (сроки, проценты, суммы) и из разных
документов, не являющихся редакциями одного акта. Это список «на проверку» для администратора, а не вердикт."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import numpy as np
from rapidfuzz import fuzz

from ..index.store import HybridIndex
from ..schemas import KnowledgeBase
from .metadata import title_key

NUMERIC = re.compile(r"(?<![\d.,])(\d{1,4})\s*(?:\([^)]{0,40}\)\s*)?(календарн\w*|рабоч\w*|учебн\w*)?\s*(дн\w*|недел\w*|месяц\w*|процент\w*|%|руб\w*|час\w*|семестр\w*|курс\w*|раз\w*|год\w*|лет)", re.I)
POLARITY = re.compile(r"\b(не\s+допускается|запрещ\w+|не\s+вправе|не\s+может)\b|\b(допускается|вправе|может|разрешается)\b", re.I)


def _signature(text: str) -> set[tuple[int, str]]:
    sig = set()
    for m in NUMERIC.finditer(text):
        n = int(m.group(1))
        unit = (m.group(3) or "").lower()[:4]
        if 1990 <= n <= 2040 and unit.startswith(("год", "лет")):
            continue  # годы не параметры нормы
        sig.add((n, unit))
    return sig


def find_contradictions(kb: KnowledgeBase, index: HybridIndex, threshold: float = 0.80, max_pairs: int = 80) -> list[dict[str, Any]]:
    if index.embeddings is None:
        return []
    docs = {d.id: d for d in kb.documents}
    cand = []
    for i, u in enumerate(index.units):
        d = docs.get(u.doc_id)
        if d is None or d.status != "active" or u.kind not in ("clause", "item", "order_item"):
            continue
        sig = _signature(u.text)
        if not sig:
            continue
        cand.append((i, sig))
    if len(cand) < 2:
        return []
    idx = np.array([c[0] for c in cand])
    emb = index.embeddings[idx]
    sims = emb @ emb.T
    pairs: list[dict[str, Any]] = []
    n = len(cand)
    for a in range(n):
        for b in range(a + 1, n):
            s = float(sims[a, b])
            if s < threshold:
                continue
            ua, ub = index.units[cand[a][0]], index.units[cand[b][0]]
            if ua.doc_id == ub.doc_id:
                continue
            da, db = docs[ua.doc_id], docs[ub.doc_id]
            if title_key(da.doc_type, da.title) == title_key(db.doc_type, db.title):
                continue  # редакции одного акта или ежегодные приказы с одинаковым названием
            if fuzz.token_set_ratio(title_key(da.doc_type, da.title), title_key(db.doc_type, db.title)) >= 70:
                continue  # одна «семья» документов: ежегодные приказы о стоимости, порядки ГИА по разным программам
            if db.id in da.amended_by or da.id in db.amended_by or da.approved_by == db.id or db.approved_by == da.id:
                continue
            if cand[a][1] == cand[b][1]:
                continue
            pairs.append({
                "similarity": round(s, 3),
                "a": {"doc_id": da.id, "clause_id": ua.clause_id, "doc": da.short_name[:100], "text": ua.text[:400], "numbers": sorted(f"{n_} {u_}" for n_, u_ in cand[a][1])},
                "b": {"doc_id": db.id, "clause_id": ub.clause_id, "doc": db.short_name[:100], "text": ub.text[:400], "numbers": sorted(f"{n_} {u_}" for n_, u_ in cand[b][1])},
                "polarity_conflict": bool(POLARITY.search(ua.text)) and bool(POLARITY.search(ub.text)) and (
                    bool(POLARITY.search(ua.text).group(1)) != bool(POLARITY.search(ub.text).group(1))),
            })
    pairs.sort(key=lambda p: -p["similarity"])
    return pairs[:max_pairs]


def save_contradictions(pairs: list[dict[str, Any]], kb_dir: Path) -> Path:
    p = Path(kb_dir) / "contradictions.json"
    p.write_text(json.dumps(pairs, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def load_contradictions(kb_dir: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Пары-кандидаты по ключу (doc_id, clause_id). Повреждённый файл не роняет запуск: он откладывается в contradictions.json.broken,
    список считается пустым; отдельные записи неверного вида пропускаются."""
    p = Path(kb_dir) / "contradictions.json"
    if not p.exists():
        return {}
    try:
        pairs = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(pairs, list):
            raise ValueError("ожидался список пар")
    except (ValueError, OSError) as e:
        print(f"[contradictions] {p.name} повреждён ({type(e).__name__}): список противоречий пуст до пересборки базы")
        try:
            p.replace(p.with_name(p.name + ".broken"))
        except OSError:
            pass
        return {}
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for pair in pairs:
        try:
            a, b, sim = pair["a"], pair["b"], pair.get("similarity", 0.0)
            ka, kb_ = (a["doc_id"], a["clause_id"]), (b["doc_id"], b["clause_id"])
            a.setdefault("numbers", [])
            b.setdefault("numbers", [])
            a.setdefault("doc", a["doc_id"])
            b.setdefault("doc", b["doc_id"])
        except (KeyError, TypeError, AttributeError):
            continue  # запись неверного вида: пропускаем её, остальные остаются
        out.setdefault(ka, []).append(b | {"similarity": sim})
        out.setdefault(kb_, []).append(a | {"similarity": sim})
    return out
