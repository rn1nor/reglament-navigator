"""Анализ влияния нового приказа: что он меняет, отменяет, какие документы и роли затрагивает,
какие вопросы из журнала/тест-набора получат другой ответ. Работает на копии базы, исходная не меняется."""
from __future__ import annotations

import copy
import datetime as _dt
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..schemas import KnowledgeBase
from .graph import DocumentGraph
from .loaders import load_file
from .pipeline import build_documents


class DocChange(BaseModel):
    doc_id: str
    title: str
    before: str
    after: str
    reason: str = ""


class ClauseChange(BaseModel):
    doc_id: str
    title: str
    clause: str
    op: str
    before: Optional[str] = None
    after: Optional[str] = None


class ImpactReport(BaseModel):
    new_documents: list[dict[str, Any]] = Field(default_factory=list)
    relations: list[dict[str, Any]] = Field(default_factory=list)
    status_changes: list[DocChange] = Field(default_factory=list)
    clause_changes: list[ClauseChange] = Field(default_factory=list)
    unapplied: list[dict[str, Any]] = Field(default_factory=list)
    affected_roles: list[str] = Field(default_factory=list)
    affected_questions: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""


def analyze(kb: KnowledgeBase, new_file: Path, today: Optional[_dt.date] = None, questions: Optional[list[dict]] = None,
            navigator_factory=None) -> ImpactReport:
    base_docs = copy.deepcopy(kb.documents)
    before = {d.id: d for d in copy.deepcopy(kb.documents)}
    raw = load_file(new_file)
    new_docs = build_documents(raw, Path(new_file))
    new_ids = {d.id for d in new_docs}
    graph = DocumentGraph(base_docs + new_docs, today=today)
    graph.resolve()
    graph.apply_patches()
    graph.compute_statuses()
    after = {d.id: d for d in graph.docs}
    rep = ImpactReport()
    for d in new_docs:
        rep.new_documents.append({"doc_id": d.id, "title": d.title, "type": d.doc_type, "number": d.number, "date": str(d.date or ""),
                                  "clauses": len(d.clauses), "patches": len(d.patches)})
        for r in d.relations:
            tgt = after.get(r.target_doc_id) if r.target_doc_id else None
            rep.relations.append({"type": r.type, "target": tgt.short_name if tgt else (r.target_doc_id or r.target_ref[-80:]), "resolved": bool(tgt)})
        for p in d.patches:
            if not p.applied:
                rep.unapplied.append({"op": p.op, "clause": p.clause_number, "note": p.note, "target": p.target_ref[:80]})
    roles: set[str] = set()
    for did, d_after in after.items():
        if did in new_ids:
            continue
        d_before = before.get(did)
        if d_before is None:
            continue
        if d_before.status != d_after.status or set(d_before.amended_by) != set(d_after.amended_by):
            rep.status_changes.append(DocChange(doc_id=did, title=d_after.short_name, before=d_before.status, after=d_after.status,
                                                reason=d_after.status_reason or ""))
            roles.update(d_after.roles)
        b_cl = {c.id: c for c in d_before.clauses}
        for c in d_after.clauses:
            bc = b_cl.get(c.id)
            if bc is None:
                rep.clause_changes.append(ClauseChange(doc_id=did, title=d_after.short_name, clause=c.label, op="insert", after=c.text[:400]))
                roles.update(d_after.roles)
            elif bc.text != c.text or bc.deleted != c.deleted:
                rep.clause_changes.append(ClauseChange(doc_id=did, title=d_after.short_name, clause=c.label, op="delete" if c.deleted else "replace",
                                                       before=bc.text[:400], after=None if c.deleted else c.text[:400]))
                roles.update(d_after.roles)
    rep.affected_roles = sorted(roles)
    # вопросы, ответы на которые изменятся: прогон до/после по журналу вопросов
    if questions and navigator_factory is not None:
        affected_docs = {c.doc_id for c in rep.status_changes} | {c.doc_id for c in rep.clause_changes} | new_ids
        kb_after = KnowledgeBase(documents=graph.docs, built_at=_dt.datetime.now().isoformat(timespec="seconds"), audit=graph.audit(), stubs=list(graph.stubs.values()))
        nav_before = navigator_factory(kb)
        nav_after = navigator_factory(kb_after)
        for q in questions:
            r1 = nav_before.ask(q["question"], role=q.get("role"))
            touches = {c.doc_id for s in r1.steps for c in s.citations} | {d.doc_id for d in r1.checked_documents[:3]}
            if not (touches & affected_docs):
                continue
            r2 = nav_after.ask(q["question"], role=q.get("role"))
            s1 = [s.text for s in r1.steps]
            s2 = [s.text for s in r2.steps]
            if s1 != s2 or r1.outcome != r2.outcome or r1.summary != r2.summary:
                rep.affected_questions.append({"question": q["question"], "before": r1.outcome, "after": r2.outcome,
                                               "steps_before": len(s1), "steps_after": len(s2)})
    n_new = len(rep.new_documents)
    rep.summary = (f"Новых документов: {n_new}; связей: {len(rep.relations)} (разрешено {sum(1 for r in rep.relations if r['resolved'])}); "
                   f"изменений статусов: {len(rep.status_changes)}; изменённых пунктов: {len(rep.clause_changes)}; "
                   f"непримененных поправок: {len(rep.unapplied)}; затронутые роли: {', '.join(rep.affected_roles) or '—'}; "
                   f"вопросов с изменившимся ответом: {len(rep.affected_questions)}")
    return rep
