"""Поиск: гибрид + реранкер + расширение по структуре (секция, перекрёстные ссылки, поправки, приложения)."""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .. import config
from ..index.rerank import Reranker
from ..index.store import Filters, Hit, HybridIndex
from ..index.text import tokenize
from ..schemas import Clause, Document, KnowledgeBase, QueryUnderstanding

EXPANSION_PENALTY = 0.15
CLAUSE_REF_RE = re.compile(r"(?:п\.|пункт\w*|подпункт\w*|пп\.)\s*(\d{1,3}(?:\.\d{1,3})+|\d{1,3})(?![\d.])", re.I)
ATTACH_REF_RE = re.compile(r"приложени\w*\s*(?:№\s*)?(\d+)", re.I)


@dataclass
class ContextUnit:
    doc_id: str
    clause_id: str
    label: str
    text: str
    path: list[str]
    status: str
    page: Optional[int]
    reason: str  # hit | section | reference | amendment | attachment | mentioned
    provenance: list[str] = field(default_factory=list)
    score: float = 0.0

    @property
    def key(self) -> tuple[str, str]:
        return (self.doc_id, self.clause_id)


@dataclass
class RetrievalResult:
    queries: list[str]
    hits: list[Hit]
    units: list[ContextUnit]
    doc_scores: dict[str, float]
    top_rerank: Optional[float]
    top_dense: float
    top_bm25: float
    lexical_overlap: float
    method: str
    mentioned_docs: list[Document] = field(default_factory=list)
    mentioned_missing: list[str] = field(default_factory=list)
    notes: dict = field(default_factory=dict)  # пояснения к сборке ответа: попадают в трассу


def _clause_label(doc: Document, c: Clause) -> str:
    return c.label


def context_unit(doc: Document, c: Clause, reason: str, score: float = 0.0) -> ContextUnit:
    return ContextUnit(doc_id=doc.id, clause_id=c.id, label=_clause_label(doc, c), text=c.text, path=list(c.path), status=doc.status,
                       page=c.page_start, reason=reason, provenance=list(c.provenance), score=score)


_unit = context_unit


def find_mentioned_docs(kb: KnowledgeBase, numbers: list[str]) -> list[Document]:
    """Документы базы по номеру; если документа нет, но он известен как отменённый документами базы, возвращаем его заглушку."""
    out = []
    for n in numbers:
        found = [d for d in kb.documents if d.number and d.number.upper() == n.upper()]
        if not found:
            found = [s for s in kb.stubs if s.number and s.number.upper() == n.upper() and s.status in ("cancelled", "superseded")]
        out.extend(found)
    return out


def retrieve(kb: KnowledgeBase, index: HybridIndex, qu: QueryUnderstanding, question: str, reranker: Optional[Reranker] = None,
             as_of: Optional[_dt.date] = None, k_final: int = config.TOP_K_FINAL, statuses: Optional[set[str]] = None,
             use_dense: bool = True, doc_ids: Optional[set[str]] = None) -> RetrievalResult:
    """doc_ids: если задано, поиск идёт только по этим документам (документы, действовавшие на дату запроса) вместо фильтра по статусу."""
    queries = list(dict.fromkeys([question, *(x for x in [qu.intent] if x and x != question), *qu.reformulations]))
    statuses = statuses or {"active"}
    mentioned = find_mentioned_docs(kb, qu.mentioned_documents)
    found_numbers = {d.number.upper() for d in mentioned if d.number}
    mentioned_missing = [n for n in qu.mentioned_documents if n.upper() not in found_numbers]
    filters = Filters(statuses=statuses)
    # если спрашивают про конкретный отменённый документ, разрешаем искать и в нём
    if mentioned:
        filters = Filters(statuses=statuses | {d.status for d in mentioned})
    if doc_ids is not None:
        filters = Filters(statuses=None, doc_ids=set(doc_ids) | {d.id for d in mentioned})
    hits = index.search(queries, top_k=config.TOP_K_CANDIDATES, filters=filters, use_dense=use_dense)
    # мягкий буст по роли и по упомянутому документу
    role = qu.role
    mentioned_ids = {d.id for d in mentioned}
    for h in hits:
        if role and role in h.unit.roles:
            h.score *= 1.15
        if h.unit.doc_id in mentioned_ids:
            h.score *= 1.5
    hits.sort(key=lambda h: -h.score)
    method = "rrf"
    if reranker is not None and hits:
        lite = bool(getattr(reranker, "lite", False))  # CPU: меньше кандидатов и короче тексты
        pool = hits[:config.RERANK_TOP_LITE] if lite else hits
        texts = [h.unit.index_text[:1100 if lite else 2000] for h in pool]
        scores = np.array(reranker.score(question, texts), dtype=np.float32)
        expansions = [r for r in qu.reformulations if r not in qu.sub_questions]
        if expansions:
            # бытовая формулировка («заболел») и формулировка регламента («временная нетрудоспособность»): берём лучшую оценку
            expanded = f"{question} ({'; '.join(expansions[:4])})"
            if lite:  # вторую формулировку считаем только для лучших по первой и для лексически ближайших к расширениям
                first = set(np.argsort(-scores)[:8].tolist())
                et = set(tokenize(" ".join(expansions[:4])))
                lex = sorted(range(len(pool)), key=lambda i: -len(et & set(tokenize(pool[i].unit.text))))[:6]
                again = sorted(first | set(lex))
                sc2 = reranker.score(expanded, [texts[i] for i in again])
                for i, v in zip(again, sc2):
                    scores[i] = max(scores[i], float(v) - EXPANSION_PENALTY)
            else:
                scores = np.maximum(scores, reranker.score(expanded, texts) - EXPANSION_PENALTY)  # расширение — более слабое свидетельство
        for h, s in zip(pool, scores):
            h.rerank = float(s)
            if h.unit.doc_id in mentioned_ids:
                h.rerank = min(1.0, h.rerank + 0.15)
        hits.sort(key=lambda h: (-(h.rerank or 0.0), -h.score))
        method = "rerank"
    top = hits[:k_final]
    docs = {d.id: d for d in kb.documents}
    doc_scores: dict[str, float] = {}
    for rank, h in enumerate(top):
        w = (h.rerank if h.rerank is not None else h.score) * (1.0 / (1 + 0.15 * rank))
        doc_scores[h.unit.doc_id] = doc_scores.get(h.unit.doc_id, 0.0) + w
    # ------------------------------------------------------------ расширение
    units: dict[tuple[str, str], ContextUnit] = {}
    budget = config.MAX_CONTEXT_CHARS
    used = 0

    def push(u: ContextUnit) -> bool:
        nonlocal used
        if u.key in units:
            return True
        if used + len(u.text) > budget:
            return False
        units[u.key] = u
        used += len(u.text)
        return True

    for h in top:
        d = docs.get(h.unit.doc_id)
        c = d.clause_by_id(h.unit.clause_id) if d else None
        if d is None or c is None:
            continue
        push(_unit(d, c, "hit", score=h.rerank if h.rerank is not None else h.score))
    for h in top:
        d = docs.get(h.unit.doc_id)
        c = d.clause_by_id(h.unit.clause_id) if d else None
        if d is None or c is None:
            continue
        # секция: соседние пункты того же родителя (small-to-big), ограниченно
        if c.parent_id:
            siblings = [x for x in d.clauses if x.parent_id == c.parent_id and not x.deleted and x.kind != "heading"]
            section_chars = sum(len(x.text) for x in siblings)
            if section_chars <= 6000:
                for s in siblings:
                    push(_unit(d, s, "section"))
            else:
                # только непосредственные соседи
                idx = next((i for i, x in enumerate(siblings) if x.id == c.id), None)
                if idx is not None:
                    for s in siblings[max(0, idx - 1): idx + 2]:
                        push(_unit(d, s, "section"))
        # дочерние подпункты
        for ch in d.clauses:
            if ch.parent_id == c.id and not ch.deleted:
                push(_unit(d, ch, "section"))
        # перекрёстные ссылки «в соответствии с п. 4.1»
        for m in CLAUSE_REF_RE.finditer(c.text):
            ref = d.clause_by_number(m.group(1), attachment=c.attachment) or d.clause_by_number(m.group(1))
            if ref and not ref.deleted:
                push(_unit(d, ref, "reference"))
        # приложения-формы
        for m in ATTACH_REF_RE.finditer(c.text):
            att = f"Прил.{m.group(1)}"
            first = next((x for x in d.clauses if x.attachment == att and x.kind != "heading"), None)
            if first:
                push(_unit(d, first, "attachment"))
        # поправки: пункт приказа-поправки, который изменил этот пункт
        for order_id in c.provenance:
            o = docs.get(order_id)
            if not o:
                continue
            for p in o.patches:
                if p.target_doc_id == d.id and p.clause_number == c.number and p.source_clause_id:
                    pc = o.clause_by_id(p.source_clause_id)
                    if pc:
                        push(_unit(o, pc, "amendment"))
    # упомянутый документ: его первые пункты, даже если поиск их не поднял; и верхняя строка среди документов
    for md in mentioned:
        for c in [x for x in md.clauses if x.kind not in ("heading",)][:3]:
            push(_unit(md, c, "mentioned"))
        doc_scores[md.id] = max(doc_scores.get(md.id, 0.0), max(doc_scores.values(), default=0.0) + 0.01)
    # порядок: документ по убыванию score, внутри документа — по порядку пунктов
    order_in_doc = {d.id: {c.id: i for i, c in enumerate(d.clauses)} for d in kb.documents}
    ordered = sorted(units.values(), key=lambda u: (-doc_scores.get(u.doc_id, 0.0), u.doc_id, order_in_doc.get(u.doc_id, {}).get(u.clause_id, 0)))
    # лексическое покрытие вопроса лучшим пунктом (сигнал уверенности, когда реранкера нет): считаем по значимым словам вопроса,
    # а в тексте пункта учитываем название документа и раздел
    stop = set(tokenize("как какой какая какие каков что кто где когда куда сколько можно нужно надо ли для при или это мне если чтобы есть мой наш по на за от до из об про у меня я мы"))
    q_tokens = {t for t in tokenize(question) if t not in stop and len(t) >= 3} or set(tokenize(question))
    top_tokens = set(tokenize(top[0].unit.index_text)) if top else set()
    overlap = len(q_tokens & top_tokens) / len(q_tokens) if q_tokens else 0.0
    return RetrievalResult(
        queries=queries, hits=hits, units=ordered, doc_scores=doc_scores,
        top_rerank=(top[0].rerank if top and top[0].rerank is not None else None),
        top_dense=max((h.dense for h in top), default=0.0), top_bm25=max((h.bm25 for h in top), default=0.0),
        lexical_overlap=overlap, method=method, mentioned_docs=mentioned, mentioned_missing=mentioned_missing,
    )
