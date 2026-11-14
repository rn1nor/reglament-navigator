"""«Мой навигатор»: что относится к роли, что изменилось, какие требования и сроки, что ещё не прочитано."""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any, Optional

from ..schemas import Document, KnowledgeBase
from .deadlines import extract_rules

STUDENT_SIDE = {"student", "postgraduate", "applicant"}
STAFF_SIDE = {"teacher", "researcher", "admin", "staff"}
DUTY_RE = re.compile(r"\b(обучающ\w+|студент\w*|аспирант\w*|работник\w*|сотрудник\w*|преподавател\w*)\s+(обязан\w*|должн\w*|не\s+вправе|не\s+допускается)", re.I)


def applicable_documents(kb: KnowledgeBase, role: Optional[str]) -> list[Document]:
    out = []
    for d in kb.documents:
        if d.status != "active" or d.doc_type in ("договор", "протокол", "иное"):
            continue
        roles = set(d.roles)
        if not role or "all" in roles or role in roles:
            out.append(d)
        elif role in STUDENT_SIDE and roles & STUDENT_SIDE and not roles & STAFF_SIDE:
            out.append(d)
        elif role in STAFF_SIDE and roles & STAFF_SIDE and not roles & STUDENT_SIDE:
            out.append(d)
    return sorted(out, key=lambda d: (d.doc_type, d.title))


def recent_changes(kb: KnowledgeBase, docs: list[Document], since: _dt.date) -> list[dict[str, Any]]:
    """Новые документы и поправки к применимым документам с даты since."""
    rows: list[dict[str, Any]] = []
    for d in docs:
        if d.date and d.date >= since:
            rows.append({"date": d.date, "kind": "новый документ", "doc": d, "detail": d.title[:120]})
        for a in d.amended_by:
            o = kb.get(a)
            if o and o.date and o.date >= since:
                changed = [p for p in o.patches if p.target_doc_id == d.id and p.applied]
                detail = "; ".join(f"{p.op} п. {p.clause_number}" for p in changed[:5]) or "поправка (текст см. в приказе)"
                rows.append({"date": o.date, "kind": "изменён приказом", "doc": d, "order": o, "detail": detail})
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


def duties_and_deadlines(docs: list[Document], limit: int = 40) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for d in docs:
        for c in d.clauses:
            if c.deleted or c.kind == "heading" or len(c.text) < 30:
                continue
            duty = bool(DUTY_RE.search(c.text))
            rules = extract_rules(c.text, d.id, c.id, c.label)
            if duty or rules:
                rows.append({"doc": d, "clause": c, "duty": duty, "deadlines": [r.describe() for r in rules[:2]], "text": c.text[:260]})
    rows.sort(key=lambda r: (not r["deadlines"], not r["duty"]))
    return rows[:limit]


class ReadState:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, dict[str, str]] = {}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def is_read(self, role: str, doc_id: str) -> bool:
        return doc_id in self.data.get(role, {})

    def mark(self, role: str, doc_id: str, read: bool) -> None:
        bucket = self.data.setdefault(role, {})
        if read:
            bucket[doc_id] = _dt.datetime.now().isoformat(timespec="seconds")
        else:
            bucket.pop(doc_id, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")

    def unread(self, role: str, docs: list[Document]) -> list[Document]:
        return [d for d in docs if not self.is_read(role, d.id)]
