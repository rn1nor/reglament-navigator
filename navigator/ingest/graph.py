"""Граф документов: разрешение ссылок, применение поправок, статусы, редакции, аудит базы."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Optional

from rapidfuzz import fuzz

from ..schemas import AuditFinding, AuditReport, Document, Relation
from .metadata import norm_number, title_key
from .patches import apply_patch, choose_target


class DocumentGraph:
    def __init__(self, documents: list[Document], today: Optional[date] = None):
        self.docs = documents
        self.today = today or date.today()
        self.by_id = {d.id: d for d in documents}
        self.stubs: dict[str, Document] = {}
        self._reindex()

    def _reindex(self) -> None:
        self.by_num_date: dict[tuple, list[Document]] = defaultdict(list)
        self.by_num: dict[str, list[Document]] = defaultdict(list)
        for d in self.docs:
            n = norm_number(d.number)
            if n:
                self.by_num[n].append(d)
                if d.date:
                    self.by_num_date[(n, d.date)].append(d)

    # ------------------------------------------------------------ разрешение
    def _find_by_title(self, kind: Optional[str], title: Optional[str], exclude: str) -> Optional[Document]:
        if not title:
            return None
        best, best_score = None, 0
        tk = title_key(kind or "", title).split("|", 1)[1]
        for d in self.docs:
            if d.id == exclude or d.doc_type in ("приказ", "распоряжение"):
                continue
            if kind and kind != "иное" and d.doc_type != kind:
                continue
            dk = title_key(d.doc_type, d.title).split("|", 1)[1]
            score = 0.6 * fuzz.token_sort_ratio(tk, dk) + 0.4 * fuzz.ratio(tk[:60], dk[:60])
            if score > best_score:
                best, best_score = d, score
        return best if best_score >= 82 else None

    def _find_by_number(self, num: Optional[str], dt: Optional[date], kind: Optional[str]) -> Optional[Document]:
        n = norm_number(num)
        if not n:
            return None
        if dt and self.by_num_date.get((n, dt)):
            return self.by_num_date[(n, dt)][0]
        cands = self.by_num.get(n, [])
        if kind and kind != "иное":
            cands = [c for c in cands if c.doc_type == kind] or cands
        if len(cands) == 1 and (dt is None or cands[0].date is None):
            return cands[0]
        if dt and len(cands) > 1:
            for c in cands:
                if c.date == dt:
                    return c
        return cands[0] if len(cands) == 1 else None

    def _stub(self, rel: Relation) -> Document:
        key = f"stub:{rel.target_kind or 'документ'}:{norm_number(rel.target_number)}:{rel.target_date or ''}"
        if key not in self.stubs:
            title = rel.target_title or ""
            self.stubs[key] = Document(id=key, source_path="", title=title, doc_type=rel.target_kind if rel.target_kind in (
                "приказ", "распоряжение", "положение", "регламент", "правила", "порядок", "инструкция", "устав", "договор", "протокол") else "иное",
                number=norm_number(rel.target_number), date=rel.target_date, status="unknown",
                status_reason="документ упоминается, но отсутствует в базе")
        return self.stubs[key]

    def _approving_ref(self, cand: Document) -> tuple[Optional[str], Optional[date]]:
        """Чем утверждён документ по его собственной шапке (номер, дата приказа), даже если приказа нет в базе."""
        for r in cand.relations:
            if r.type == "approved_by" and r.target_number:
                return norm_number(r.target_number), r.target_date
        return None, None

    def _acceptable(self, d: Document, rel: Relation, cand: Document) -> bool:
        if cand.id == d.id:
            return False
        if rel.type in ("cancels", "replaces", "amends") and cand.approved_by == d.id:
            return False  # приказ не отменяет и не правит акт, который сам же утверждает
        if rel.type in ("cancels", "replaces") and cand.date and d.date and cand.date > d.date:
            return False
        return True

    def _find_by_approving(self, d: Document, rel: Relation) -> Optional[Document]:
        num = norm_number(rel.approving_number)
        if not num:
            return None
        order = self._find_by_number(num, rel.approving_date, "приказ")
        for cand in self.docs:
            if cand.doc_type in ("приказ", "распоряжение") or not self._acceptable(d, rel, cand):
                continue
            if rel.target_kind and rel.target_kind != "иное" and cand.doc_type != rel.target_kind:
                continue
            if order and cand.approved_by == order.id:
                return cand
            cnum, cdate = self._approving_ref(cand)
            if cnum == num and (cdate is None or rel.approving_date is None or cdate == rel.approving_date):
                return cand
        return None

    def resolve(self) -> None:
        # первый проход: номер+дата, затем утверждающий приказ, затем заголовок
        for d in self.docs:
            for rel in d.relations:
                if rel.external:
                    continue
                target = None
                if rel.target_number:
                    target = self._find_by_number(rel.target_number, rel.target_date, rel.target_kind)
                    if target is not None and not self._acceptable(d, rel, target):
                        target = None
                if target is None and rel.approving_number:
                    target = self._find_by_approving(d, rel)
                    if target is None and rel.type in ("cancels", "replaces"):
                        # утверждающий приказ неизвестен базе: сопоставление по названию опасно (совпадёт с новой редакцией)
                        rel.target_doc_id = self._stub(Relation(type=rel.type, source_doc_id=d.id, target_ref=rel.target_ref,
                                                               target_kind=rel.target_kind, target_number=rel.approving_number,
                                                               target_date=rel.approving_date, target_title=rel.target_title)).id
                        continue
                if target is None and rel.target_title:
                    t = self._find_by_title(rel.target_kind, rel.target_title, exclude=d.id)
                    if t is not None and self._acceptable(d, rel, t):
                        target = t
                if target is not None and target.id != d.id:
                    rel.target_doc_id = target.id
                elif rel.target_number and not rel.external:
                    rel.target_doc_id = self._stub(rel).id
        # approved_by: положение знает свой утверждающий приказ
        for d in self.docs:
            for rel in d.relations:
                if rel.type == "approved_by" and rel.target_doc_id and d.doc_type not in ("приказ", "распоряжение") and rel.target_doc_id != d.id:
                    if d.approved_by and not d.approved_by.startswith("stub:") and d.approved_by in self.by_id:
                        pass  # уже известен реальный утверждающий приказ (например, тот же файл)
                    else:
                        d.approved_by = rel.target_doc_id
                    order = self.by_id.get(rel.target_doc_id)
                    if order and d.date is None:
                        d.date = order.date
                    if d.date is None and rel.target_date:
                        d.date = rel.target_date
                    elif d.date is None and rel.target_date:
                        d.date = rel.target_date
                if rel.type == "approves" and rel.target_doc_id and rel.target_doc_id in self.by_id:
                    t = self.by_id[rel.target_doc_id]
                    t.approved_by = t.approved_by or d.id
                    if t.date is None:
                        t.date = d.date
        # второй проход для amends через approving_number теперь, когда approved_by известен
        for d in self.docs:
            for rel in d.relations:
                if rel.target_doc_id is None and rel.approving_number:
                    t = self._find_by_approving(d, rel)
                    if t is None and rel.target_title:
                        t2 = self._find_by_title(rel.target_kind, rel.target_title, exclude=d.id)
                        if t2 is not None and self._acceptable(d, rel, t2):
                            t = t2
                    if t is not None:
                        rel.target_doc_id = t.id
        self._reindex()

    # ------------------------------------------------------------ поправки
    def apply_patches(self) -> None:
        for d in self.docs:
            if not d.patches:
                continue
            cands = [(r.target_doc_id, self.by_id[r.target_doc_id].doc_type) for r in d.relations
                     if r.type == "amends" and r.target_doc_id and r.target_doc_id in self.by_id]
            # если приказ сам говорит «внести изменения в Положение ...», но связь не разрешилась, патчи остаются неприменёнными
            for p in d.patches:
                tid = choose_target(p, cands)
                if tid is None:
                    p.note = p.note or "не удалось определить целевой документ (нет разрешённой связи «изменяет»)"
                    continue
                p.target_doc_id = tid
                target = self.by_id[tid]
                ok = apply_patch(target, p)
                if ok and d.id not in target.amended_by:
                    target.amended_by.append(d.id)
            d.unapplied_patches = sum(1 for p in d.patches if not p.applied)

    # ------------------------------------------------------------ статусы
    def compute_statuses(self) -> None:
        for d in self.docs:
            d.status = "active"
            d.status_reason = None
        for d in self.docs:
            for rel in d.relations:
                if not rel.target_doc_id or rel.target_doc_id not in self.by_id or rel.target_doc_id == d.id:
                    continue
                t = self.by_id[rel.target_doc_id]
                if rel.type in ("cancels", "replaces"):
                    if d.date and t.date and d.date < t.date:
                        continue  # отменить документ, который появился позже, нельзя
                    t.status = "cancelled" if rel.type == "cancels" else "superseded"
                    if d.id not in t.cancelled_by:
                        t.cancelled_by.append(d.id)
                    t.status_reason = f"{'отменён' if rel.type == 'cancels' else 'заменён'}: {d.short_name}"
                    if rel.type == "replaces":
                        t.superseded_by = d.id
                elif rel.type == "amends":
                    if d.id not in t.amended_by:
                        t.amended_by.append(d.id)
        # заглушки: документы, которых нет в базе, но которые отменены или изменены документами базы
        for d in self.docs:
            for rel in d.relations:
                st = self.stubs.get(rel.target_doc_id or "") if rel.target_doc_id and rel.target_doc_id.startswith("stub:") else None
                if st is None:
                    continue
                if rel.type in ("cancels", "replaces"):
                    st.status = "cancelled" if rel.type == "cancels" else "superseded"
                    if d.id not in st.cancelled_by:
                        st.cancelled_by.append(d.id)
                    st.status_reason = f"{'отменён' if rel.type == 'cancels' else 'заменён'}: {d.short_name} (текста документа нет в базе)"
                elif rel.type == "amends" and d.id not in st.amended_by:
                    st.amended_by.append(d.id)
        # редакции: одинаковый тип и заголовок, разные даты
        groups: dict[str, list[Document]] = defaultdict(list)
        for d in self.docs:
            if d.doc_type in ("приказ", "распоряжение", "протокол", "иное"):
                continue
            groups[title_key(d.doc_type, d.title)].append(d)
        for key, members in groups.items():
            if len(members) < 2:
                continue
            dated = [m for m in members if m.date]
            if len(dated) < 2:
                continue
            newest = max(dated, key=lambda m: m.date)
            for m in members:
                m.edition_group = key
                if m is not newest and m.status == "active":
                    m.status = "superseded"
                    m.superseded_by = newest.id
                    m.status_reason = f"предыдущая редакция; действует {newest.short_name}"
        for d in self.docs:
            if d.effective_to and d.effective_to < self.today and d.status == "active":
                d.status = "expired"
                d.status_reason = f"срок действия истёк {d.effective_to.strftime('%d.%m.%Y')}"
            if d.status == "active" and d.amended_by:
                d.status_reason = "действует с изменениями: " + ", ".join(
                    self.by_id[a].short_name for a in d.amended_by if a in self.by_id)
            if d.status == "active" and d.effective_from and d.effective_from > self.today:
                d.status_reason = f"вступает в силу {d.effective_from.strftime('%d.%m.%Y')}"

    # ------------------------------------------------------------ аудит
    def audit(self, read_errors: Optional[list[tuple[str, str]]] = None, extra: Optional[list[AuditFinding]] = None) -> AuditReport:
        """extra: находки конвейера, не привязанные к прочитанному документу (пропущенные файлы, дубликаты, повреждённый manifest.json)."""
        f: list[AuditFinding] = []
        counts: dict[str, int] = defaultdict(int)
        counts["documents"] = len(self.docs)
        for name, err in read_errors or []:
            counts["read_errors"] += 1
            f.append(AuditFinding(kind="read_error", severity="error", doc_id=None, message=f"файл не прочитан: {name} ({err[:240]})"))
        for note in extra or []:
            if note.kind in ("unsupported_file", "skipped_file", "duplicate_file"):
                counts["skipped_files"] += 1
            f.append(note)
        hashes: dict[str, str] = {}
        for d in self.docs:
            counts[f"status:{d.status}"] += 1
            counts[f"type:{d.doc_type}"] += 1
            if d.needs_ocr:
                f.append(AuditFinding(kind="needs_ocr", severity="error", doc_id=d.id, message="нет текстового слоя: нужен OCR или разбор моделью"))
            elif d.ocr_quality < 0.85:
                f.append(AuditFinding(kind="low_text_quality", severity="warning", doc_id=d.id, message=f"качество текста {d.ocr_quality:.2f}"))
            for w in d.warnings:  # потери при чтении, о которых загрузчик предупредил в самом документе
                if "страниц без текста" in w:
                    f.append(AuditFinding(kind="partial_ocr", severity="warning", doc_id=d.id, message=w))
                elif "не распознавались (OCR недоступен)" in w:
                    f.append(AuditFinding(kind="partial_ocr", severity="info", doc_id=d.id, message=w))
                elif "нарушения структуры" in w:
                    f.append(AuditFinding(kind="damaged_file", severity="info", doc_id=d.id, message=w))
                elif "кодировка файла не определена надёжно" in w:
                    f.append(AuditFinding(kind="encoding", severity="warning", doc_id=d.id, message=w))
            if d.date is None:
                f.append(AuditFinding(kind="no_date", severity="warning", doc_id=d.id, message="не определена дата документа"))
            if d.doc_type in ("приказ", "распоряжение") and not d.number:
                f.append(AuditFinding(kind="no_number", severity="warning", doc_id=d.id, message="не определён номер приказа"))
            if d.text_hash:
                if d.text_hash in hashes:
                    f.append(AuditFinding(kind="duplicate", severity="info", doc_id=d.id, message=f"дубликат текста: {hashes[d.text_hash]}"))
                else:
                    hashes[d.text_hash] = d.id
            if d.unapplied_patches:
                f.append(AuditFinding(kind="unapplied_patches", severity="warning", doc_id=d.id,
                                      message=f"не удалось применить поправок: {d.unapplied_patches} из {len(d.patches)}"))
            for rel in d.relations:
                if rel.target_doc_id and rel.target_doc_id.startswith("stub:"):
                    counts["dangling_refs"] += 1
                    f.append(AuditFinding(kind="dangling_reference", severity="warning" if rel.type != "references" else "info", doc_id=d.id,
                                          message=f"ссылка ({rel.type}) на документ, которого нет в базе: {self.stubs[rel.target_doc_id].title}"))
            if d.status == "cancelled":
                f.append(AuditFinding(kind="cancelled", severity="info", doc_id=d.id, message=d.status_reason or "отменён"))
            if d.edition_group and d.status == "superseded":
                f.append(AuditFinding(kind="old_edition", severity="info", doc_id=d.id, message=d.status_reason or "предыдущая редакция"))
            if not d.clauses or all(c.kind in ("paragraph", "preamble", "heading") for c in d.clauses):
                f.append(AuditFinding(kind="no_numbering", severity="info", doc_id=d.id, message="нумерация пунктов не распознана, используются абзацные чанки"))
        counts["findings"] = len(f)
        return AuditReport(findings=f, counts=dict(counts))
