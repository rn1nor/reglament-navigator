"""Состояние документов на произвольную дату: какая редакция действовала «по состоянию на».

Статусы в базе посчитаны на день сборки. Если вопрос задан на другую дату («как это было в 2019 году»), действовавшие документы
определяются заново: по дате вступления в силу самого документа и по датам документов, которые его отменили или заменили."""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from ..schemas import Document, KnowledgeBase


def doc_start(kb: KnowledgeBase, d: Document) -> Optional[_dt.date]:
    """Дата, с которой документ действует: «ввести в действие с ...», дата самого документа, дата утвердившего его приказа."""
    if d.effective_from:
        return d.effective_from
    partial = any("только год" in w for w in d.warnings)
    if d.date and not partial:
        return d.date
    appr = next((r for r in d.relations if r.type == "approved_by" and r.target_date), None)
    if appr is not None:
        return appr.target_date
    order = kb.get(d.approved_by) if d.approved_by else None
    if order is not None and (order.effective_from or order.date):
        return order.effective_from or order.date
    return _dt.date(d.date.year, 1, 1) if d.date else None  # известен только год: считаем с начала года


def status_on(kb: KnowledgeBase, d: Document, when: _dt.date) -> tuple[str, str]:
    """(статус, пояснение) документа на дату: active | not_yet | cancelled | superseded | expired."""
    start = doc_start(kb, d)
    if start and start > when:
        return "not_yet", f"на {when.strftime('%d.%m.%Y')} ещё не действовал (действует с {start.strftime('%d.%m.%Y')})"
    enders = [(cid, "cancelled") for cid in d.cancelled_by]
    if d.superseded_by and d.superseded_by not in d.cancelled_by:
        enders.append((d.superseded_by, "superseded"))
    for cid, kind in enders:
        c = kb.get(cid)
        if c is None:
            continue
        c_start = doc_start(kb, c)
        if c_start is None or c_start <= when:
            status = "superseded" if (kind == "superseded" or d.status == "superseded") else "cancelled"
            verb = "заменён" if status == "superseded" else "отменён"
            since = f" с {c_start.strftime('%d.%m.%Y')}" if c_start else ""
            return status, f"на {when.strftime('%d.%m.%Y')} уже {verb}{since}: {c.short_name}"
    if d.status in ("cancelled", "superseded") and not enders:
        return d.status, d.status_reason or ""  # основание отмены известно только текстом: дату определить нельзя
    if d.effective_to and d.effective_to < when:
        return "expired", f"срок действия истёк {d.effective_to.strftime('%d.%m.%Y')}"
    return "active", f"действовал на {when.strftime('%d.%m.%Y')}"


def snapshot(kb: KnowledgeBase, when: _dt.date) -> dict[str, tuple[str, str]]:
    return {d.id: status_on(kb, d, when) for d in kb.documents}


def later_amendments(kb: KnowledgeBase, provenance: list[str], when: _dt.date) -> list[Document]:
    """Приказы-поправки к пункту, вступившие в силу после даты запроса."""
    out = []
    for oid in provenance:
        o = kb.get(oid)
        if o is None:
            continue
        s = doc_start(kb, o)
        if s is not None and s > when:
            out.append(o)
    return out
