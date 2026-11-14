"""LLM-экстракция документа: уточнение метаданных, процедуры, обязанности, ответственные, связи, которые не поймали регулярки.
Для сканов без текстового слоя: расшифровка PDF моделью (Claude принимает PDF напрямую)."""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from ..llm.base import LLMProvider, spec
from ..schemas import DocType, Document, Obligation, Procedure, Relation, RelationType, Role
from .loaders import RawDocument, RawPage, normalize_text, text_quality
from .metadata import norm_number

SYSTEM_EXTRACT = """Ты аналитик нормативных документов университета. По тексту документа (пункты пронумерованы, у каждого есть clause_id в квадратных скобках)
заполни схему. Правила: не выдумывай; поле оставляй пустым, если в тексте нет данных. Для каждой процедуры шаги должны ссылаться на clause_id
тех пунктов, откуда они взяты. В relations перечисли документы, которые этот документ утверждает, изменяет, отменяет или на которые ссылается,
с номером и датой, если они указаны. roles — кому адресован документ: student, postgraduate, applicant, teacher, researcher, admin, staff, all.
responsible_units — подразделения, куда подаются документы или кто контролирует исполнение. owner_unit — подразделение-владелец регламента. topics — 1–3 темы из списка: Кадры и трудовые отношения; Оплата труда и выплаты; Командировки; Закупки и договоры; Финансы, бухгалтерия и отчётность; Стоимость обучения и платные услуги; Учебный процесс и аттестация; Движение контингента; Приём на обучение; Стипендии и поддержка студентов; Общежития и кампус; Наука, аспирантура и гранты; Документооборот и подписи; Персональные данные и ИБ; Дисциплина и споры; Имущество и охрана труда; Управление и структура. summary — 2–3 предложения, о чём документ."""

SYSTEM_TRANSCRIBE = """Перед тобой скан внутреннего документа организации. Расшифруй его текст постранично, сохраняя нумерацию пунктов (1., 1.1., а) и т. д.),
заголовки и переносы строк между абзацами. Не исправляй и не пересказывай содержание, не добавляй комментариев. Штампы и подписи передай кратко в квадратных скобках."""


class LLMRelation(BaseModel):
    type: RelationType
    target_kind: Optional[str] = None
    target_number: Optional[str] = None
    target_date: Optional[_dt.date] = None
    target_title: Optional[str] = None
    quote: Optional[str] = None


class DocumentExtraction(BaseModel):
    title: Optional[str] = None
    doc_type: Optional[DocType] = None
    number: Optional[str] = None
    date: Optional[_dt.date] = None
    effective_from: Optional[_dt.date] = None
    effective_to: Optional[_dt.date] = None
    issuer: Optional[str] = None
    roles: list[Role] = Field(default_factory=list)
    summary: Optional[str] = None
    responsible_units: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    owner_unit: Optional[str] = None
    procedures: list[Procedure] = Field(default_factory=list)
    obligations: list[Obligation] = Field(default_factory=list)
    relations: list[LLMRelation] = Field(default_factory=list)


class TranscribedPage(BaseModel):
    number: int
    text: str


class TranscribedDocument(BaseModel):
    pages: list[TranscribedPage] = Field(default_factory=list)
    title_guess: Optional[str] = None


def _doc_text_with_ids(doc: Document, max_chars: int) -> str:
    parts = []
    total = 0
    for c in doc.clauses:
        if c.deleted:
            continue
        line = f"[{c.id}] {c.text}"
        total += len(line)
        if total > max_chars:
            parts.append("… (текст обрезан)")
            break
        parts.append(line)
    return "\n".join(parts)


def enrich_document(doc: Document, raw: RawDocument, provider: LLMProvider, max_chars: int = 80000) -> None:
    if provider.kind == "mock":
        return
    s = spec("extractor")
    user = (f"Файл: {Path(doc.source_path).name}\nТип по регулярному разбору: {doc.doc_type}; номер: {doc.number}; дата: {doc.date}; заголовок: {doc.title}\n\n"
            f"Текст документа:\n{_doc_text_with_ids(doc, max_chars)}")
    ex = provider.structured(system=SYSTEM_EXTRACT, user=user, schema=DocumentExtraction, model=s.model, effort=s.effort, max_tokens=16000)
    if ex.title and (len(doc.title) < 12 or doc.title.lower().startswith(("приложение", "утвержден")) or doc.title == Path(doc.source_path).stem):
        doc.title = ex.title[:300]
    if ex.doc_type and doc.doc_type == "иное":
        doc.doc_type = ex.doc_type
    if ex.number and not doc.number and doc.doc_type in ("приказ", "распоряжение"):
        doc.number = norm_number(ex.number)
    if ex.date and not doc.date:
        doc.date = ex.date
    if ex.effective_from and not doc.effective_from:
        doc.effective_from = ex.effective_from
    if ex.effective_to and not doc.effective_to:
        doc.effective_to = ex.effective_to
    if ex.issuer and not doc.issuer:
        doc.issuer = ex.issuer
    if ex.roles:
        doc.roles = list(dict.fromkeys(ex.roles))  # type: ignore[assignment]
    if ex.summary:
        doc.summary = ex.summary
    doc.responsible_units = list(dict.fromkeys([*doc.responsible_units, *ex.responsible_units]))
    if ex.topics:
        doc.topics = list(dict.fromkeys([*ex.topics, *doc.topics]))[:4]
    if ex.owner_unit and not doc.owner_unit:
        doc.owner_unit = ex.owner_unit[:120]
    valid_ids = {c.id for c in doc.clauses}
    for pr in ex.procedures:
        pr.steps = [st for st in pr.steps if st.clause_id in valid_ids]
        pr.clause_ids = [cid for cid in pr.clause_ids if cid in valid_ids] or [st.clause_id for st in pr.steps]
        if pr.steps:
            doc.procedures.append(pr)
    doc.obligations.extend(o for o in ex.obligations if o.clause_id in valid_ids)
    known = {(r.type, norm_number(r.target_number), r.target_date) for r in doc.relations}
    for lr in ex.relations:
        num = norm_number(lr.target_number)
        if (lr.type, num, lr.target_date) in known or (not num and not lr.target_title):
            continue
        doc.relations.append(Relation(type=lr.type, source_doc_id=doc.id, target_ref=(lr.quote or lr.target_title or "")[:200], target_kind=lr.target_kind,
                                      target_number=num, target_date=lr.target_date, target_title=lr.target_title, confidence=0.7, method="llm"))
    doc.extraction_method = "mixed"


def transcribe_pdf(path: Path, raw: RawDocument, provider: LLMProvider, max_pages: int = 60) -> RawDocument:
    """Скан без текстового слоя: модель расшифровывает PDF. Возвращает новый RawDocument со страницами."""
    if provider.kind != "anthropic":
        raw.warnings.append("расшифровка скана моделью доступна только через провайдер anthropic")
        return raw
    pdf_bytes = Path(path).read_bytes()
    if len(raw.pages) > max_pages:
        raw.warnings.append(f"скан длиннее {max_pages} страниц: расшифровка моделью пропущена")
        return raw
    s = spec("extractor")
    tr = provider.structured(system=SYSTEM_TRANSCRIBE, user=f"Расшифруй документ «{Path(path).name}» постранично.", schema=TranscribedDocument,
                             model=s.model, effort="medium", max_tokens=60000, pdf_bytes=pdf_bytes)
    if not tr.pages:
        raw.warnings.append("модель не вернула текст скана")
        return raw
    new = RawDocument(source_path=raw.source_path, file_type=raw.file_type, source_url=raw.source_url, title_hint=raw.title_hint or tr.title_guess,
                      warnings=[*raw.warnings, f"текст получен расшифровкой скана моделью ({s.model})"])
    for p in sorted(tr.pages, key=lambda x: x.number):
        new.pages.append(RawPage(p.number, normalize_text(p.text)))
    new.needs_ocr = False
    new.ocr_pages = len(new.pages)
    new.ocr_quality = text_quality(new.text)
    return new
