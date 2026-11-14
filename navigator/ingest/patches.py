"""Поправки приказов: «изложить в редакции», «дополнить пунктом», «исключить», «слова ... заменить словами ...».

Из текста приказа-поправки извлекаются JSON-патчи, затем они применяются к дереву пунктов целевого документа.
У изменённого пункта сохраняется исходный текст и список приказов-поправок (provenance).
"""
from __future__ import annotations

import re
from typing import Optional

from rapidfuzz import fuzz

from ..schemas import Clause, Document, Patch

NUM = r"(?P<num>\d{1,3}(?:\.\d{1,3})*)"
UNIT = r"(?:пункт\w*|подпункт\w*|абзац\w*|п\.|пп\.)"
REPLACE_RE = re.compile(rf"{UNIT}\s*{NUM}[.)]?\s*(?P<tgt>[^:«\"]{{0,220}}?)изложить\s+в\s+(?:следующей|новой)?\s*редакции\s*:?", re.I | re.S)
INSERT_RE = re.compile(rf"дополнить\s+(?P<tgt>[^:«\"]{{0,220}}?)(?:пунктом|подпунктом|абзацем)\s*{NUM}?[.)]?\s*(?:следующего\s+содержания)?\s*:?", re.I | re.S)
DELETE_RE = re.compile(rf"{UNIT}\s*{NUM}[.)]?\s*(?P<tgt>[^.;«\"]{{0,220}}?)(?:исключить|признать\s+утратившим\s+силу|утратил\s+силу)", re.I | re.S)
SUBST_RE = re.compile(
    rf"в\s+{UNIT}\s*{NUM}[.)]?\s*(?P<tgt>[^:«\"]{{0,220}}?)(?:слова|слово|цифры|цифру|число|дату)\s*(?P<old>«[^»]*»|\"[^\"]*\")\s*"
    rf"заменить\s+(?:словами|словом|цифрами|цифрой|числом|датой)\s*(?P<new>«[^»]*»|\"[^\"]*\")",
    re.I | re.S,
)
KIND_IN_TGT = re.compile(r"(положени|правил|регламент|порядк|порядок|инструкци|устав|приложени)\w*", re.I)


def extract_quoted(text: str, pos: int) -> Optional[tuple[str, int]]:
    """Текст в кавычках, начиная с pos. «» считаются с вложенностью, прямые кавычки — до следующей."""
    n = len(text)
    i = pos
    while i < n and text[i] in " \t\n:;-–—":
        i += 1
    if i >= n:
        return None
    ch = text[i]
    if ch == "«":
        depth = 0
        j = i
        while j < n:
            if text[j] == "«":
                depth += 1
            elif text[j] == "»":
                depth -= 1
                if depth == 0:
                    return text[i + 1:j], j + 1
            j += 1
        return text[i + 1:].strip(), n
    if ch in "\"“„":
        close = {"\"": "\"", "“": "”", "„": "“"}[ch]
        j = text.find(close, i + 1)
        if j == -1:
            j = text.find("\"", i + 1)
        if j == -1:
            return None
        return text[i + 1:j], j + 1
    return None


def _strip_leading_number(text: str, num: Optional[str]) -> str:
    if num:
        text = re.sub(rf"^\s*{re.escape(num)}[.)]?\s+", "", text)
    return text.strip()


def extract_patches(doc: Document, clauses: list[Clause]) -> list[Patch]:
    patches: list[Patch] = []
    for c in clauses:
        text = c.text
        for m in REPLACE_RE.finditer(text):
            q = extract_quoted(text, m.end())
            if not q:
                patches.append(Patch(op="replace", source_doc_id=doc.id, source_clause_id=c.id, clause_number=m.group("num"),
                                     target_ref=m.group("tgt").strip(), note="текст новой редакции не найден (возможно, «согласно приложению»)"))
                continue
            patches.append(Patch(op="replace", source_doc_id=doc.id, source_clause_id=c.id, clause_number=m.group("num"),
                                 target_ref=m.group("tgt").strip(), new_text=_strip_leading_number(q[0], m.group("num"))))
        for m in INSERT_RE.finditer(text):
            q = extract_quoted(text, m.end())
            num = m.group("num")
            if not q:
                patches.append(Patch(op="insert", source_doc_id=doc.id, source_clause_id=c.id, clause_number=num,
                                     target_ref=m.group("tgt").strip(), note="текст дополнения не найден"))
                continue
            new_text = q[0]
            if not num:
                mm = re.match(r"^\s*(\d{1,3}(?:\.\d{1,3})*)[.)]\s+", new_text)
                if mm:
                    num = mm.group(1)
            patches.append(Patch(op="insert", source_doc_id=doc.id, source_clause_id=c.id, clause_number=num,
                                 target_ref=m.group("tgt").strip(), new_text=_strip_leading_number(new_text, num)))
        for m in DELETE_RE.finditer(text):
            if REPLACE_RE.match(text, m.start()):
                continue
            patches.append(Patch(op="delete", source_doc_id=doc.id, source_clause_id=c.id, clause_number=m.group("num"),
                                 target_ref=m.group("tgt").strip()))
        for m in SUBST_RE.finditer(text):
            patches.append(Patch(op="substitute", source_doc_id=doc.id, source_clause_id=c.id, clause_number=m.group("num"),
                                 target_ref=m.group("tgt").strip(), old_fragment=m.group("old")[1:-1], new_fragment=m.group("new")[1:-1]))
    # дедупликация
    uniq: list[Patch] = []
    seen = set()
    for p in patches:
        key = (p.op, p.clause_number, (p.new_text or p.old_fragment or "")[:80])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


def _insert_clause(doc: Document, number: str, text: str, provenance: str) -> Clause:
    parts = number.split(".")
    parent = None
    for k in range(len(parts) - 1, 0, -1):
        parent = doc.clause_by_number(".".join(parts[:k]))
        if parent:
            break
    new = Clause(id=number, doc_id=doc.id, number=number, level=len(parts), kind="clause", text=text,
                 parent_id=parent.id if parent else None, path=(parent.path + [f"п. {parent.number}"]) if parent else [doc.title],
                 provenance=[provenance], original_text=None)
    # вставить после последнего пункта с тем же префиксом родителя, иначе после родителя, иначе в конец
    prefix = ".".join(parts[:-1])
    pos = None
    for i, c in enumerate(doc.clauses):
        if c.number and (c.number == prefix or c.number.startswith(prefix + ".") if prefix else True):
            pos = i
    if pos is None:
        doc.clauses.append(new)
    else:
        # после всех потомков найденного пункта
        j = pos + 1
        anchor = doc.clauses[pos]
        while j < len(doc.clauses) and doc.clauses[j].number and anchor.number and doc.clauses[j].number.startswith(anchor.number + "."):
            j += 1
        doc.clauses.insert(j, new)
    if parent:
        parent.children.append(new.id)
    return new


def apply_patch(target: Document, patch: Patch) -> bool:
    prov = patch.source_doc_id
    if patch.op == "insert":
        if not patch.clause_number or patch.new_text is None:
            patch.note = patch.note or "нет номера или текста для вставки"
            return False
        existing = target.clause_by_number(patch.clause_number)
        if existing:
            existing.original_text = existing.original_text or existing.text
            existing.text = patch.new_text
            existing.provenance.append(prov)
            patch.note = "пункт уже существовал: заменён текст"
        else:
            _insert_clause(target, patch.clause_number, patch.new_text, prov)
        patch.applied = True
        return True
    if not patch.clause_number:
        patch.note = patch.note or "нет номера пункта"
        return False
    clause = target.clause_by_number(patch.clause_number)
    if clause is None:
        patch.note = f"пункт {patch.clause_number} не найден в целевом документе"
        return False
    if patch.op == "replace":
        if patch.new_text is None:
            return False
        clause.original_text = clause.original_text or clause.text
        clause.text = patch.new_text
        clause.provenance.append(prov)
        patch.applied = True
        return True
    if patch.op == "delete":
        clause.original_text = clause.original_text or clause.text
        clause.deleted = True
        clause.provenance.append(prov)
        patch.applied = True
        return True
    if patch.op == "substitute":
        old, new = patch.old_fragment or "", patch.new_fragment or ""
        if old and old in clause.text:
            clause.original_text = clause.original_text or clause.text
            clause.text = clause.text.replace(old, new)
            clause.provenance.append(prov)
            patch.applied = True
            return True
        if old:
            al = fuzz.partial_ratio_alignment(old, clause.text)
            if al and al.score >= 90:
                clause.original_text = clause.original_text or clause.text
                clause.text = clause.text[:al.dest_start] + new + clause.text[al.dest_end:]
                clause.provenance.append(prov)
                patch.applied = True
                patch.note = f"нечёткое совпадение {al.score:.0f}"
                return True
        patch.note = "фрагмент для замены не найден"
        return False
    return False


def choose_target(patch: Patch, candidates: list[tuple[str, str]]) -> Optional[str]:
    """candidates: [(doc_id, doc_type)] из связей «amends» приказа. Выбираем по упоминанию типа в tgt."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][0]
    m = KIND_IN_TGT.search(patch.target_ref or "")
    if m:
        stem = m.group(1).lower()[:5]
        for doc_id, dtype in candidates:
            if dtype.lower().startswith(stem):
                return doc_id
    return candidates[0][0]
