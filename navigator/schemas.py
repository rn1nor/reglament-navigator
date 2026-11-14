"""Pydantic-схемы: документы, пункты, связи, патчи, ответы, проверка."""
from __future__ import annotations

import datetime as _dt
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

DocType = Literal[
    "приказ", "распоряжение", "положение", "регламент", "правила", "порядок",
    "инструкция", "устав", "договор", "протокол", "письмо", "методические рекомендации", "форма", "иное",
]
DocStatus = Literal["active", "cancelled", "superseded", "expired", "unknown"]
RelationType = Literal["cancels", "amends", "approves", "approved_by", "replaces", "references"]
Role = Literal["student", "postgraduate", "applicant", "teacher", "researcher", "admin", "staff", "all"]

ROLE_LABELS: dict[str, str] = {
    "student": "студент",
    "postgraduate": "аспирант",
    "applicant": "абитуриент",
    "teacher": "преподаватель (ППС)",
    "researcher": "научный работник",
    "admin": "административно-управленческий персонал",
    "staff": "работник (любой)",
    "all": "все",
}


# ---------------------------------------------------------------- документы
class Clause(BaseModel):
    id: str
    doc_id: str
    number: Optional[str] = None
    level: int = 1
    kind: Literal["heading", "clause", "item", "order_item", "paragraph", "preamble", "table"] = "clause"
    title: Optional[str] = None
    text: str
    parent_id: Optional[str] = None
    children: list[str] = Field(default_factory=list)
    path: list[str] = Field(default_factory=list)
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    attachment: Optional[str] = None
    deleted: bool = False
    provenance: list[str] = Field(default_factory=list)  # приказы-поправки, применённые к пункту
    original_text: Optional[str] = None

    @property
    def label(self) -> str:
        return clause_display(self)


def clause_display(c: Clause) -> str:
    """Человекочитаемая ссылка на фрагмент: «п. 3.2», «п. 16, абз. 2», «пп. «а» п. 2.3», «Прил. 1, п. 4», «абз. 5»,
    «п. 3, абз. 1, перечисление 2» (элемент перечня внутри абзаца пункта)."""
    import re as _re

    att = ""
    if c.attachment:
        att = c.attachment.replace("Прил.", "Прил. ") + ", "
    cid = _re.sub(r"#\d+", "", c.id)  # суффикс уникальности («6#2») в ссылке не нужен
    if "@" in cid:
        cid = cid.split("@", 1)[0]
    if ":" in cid:
        cid = cid.split(":", 1)[1]
    if c.number:
        return f"{att}п. {c.number}"
    m = _re.match(r"^(.+?)\.([а-яёa-z])$", cid)
    if m and ".-" not in cid:
        return f"{att}пп. «{m.group(2)}» п. {m.group(1)}"
    head, *chain = cid.split(".-")
    if chain and all(x.isdigit() for x in chain):
        pm = _re.match(r"^(?:p|pre)(\d+)$", head)
        if pm:  # перечень внутри ненумерованного абзаца
            base, first_word = f"абз. {pm.group(1)}", "перечисление"
        else:
            lm = _re.match(r"^(.+)\.([а-яёa-z])$", head)
            base, first_word = (f"пп. «{lm.group(2)}» п. {lm.group(1)}" if lm else f"п. {head}"), "абз."
        parts = [f"{first_word} {chain[0]}"] + [f"перечисление {x}" for x in chain[1:]]
        return f"{att}{base}, " + ", ".join(parts)
    m = _re.match(r"^(?:p|pre)(\d+)$", cid)
    if m:
        return f"{att}абз. {m.group(1)}" if c.kind != "preamble" else f"{att}преамбула"
    m = _re.match(r"^h(\d+)$", cid)
    if m:
        return f"{att}заголовок раздела"
    return f"{att}{cid}"


class Relation(BaseModel):
    type: RelationType
    source_doc_id: str
    target_doc_id: Optional[str] = None
    target_ref: str
    target_kind: Optional[str] = None
    target_number: Optional[str] = None
    target_date: Optional[_dt.date] = None
    target_title: Optional[str] = None
    clause_id: Optional[str] = None
    confidence: float = 1.0
    method: Literal["regex", "llm", "manual"] = "regex"
    external: bool = False  # ссылка на внешний акт (ТК РФ, ФЗ, постановление)
    approving_number: Optional[str] = None  # для «Положение ..., утверждённое приказом от ... № ...»
    approving_date: Optional[_dt.date] = None


class Patch(BaseModel):
    op: Literal["replace", "insert", "delete", "substitute"]
    source_doc_id: str
    source_clause_id: Optional[str] = None
    target_ref: str = ""
    target_doc_id: Optional[str] = None
    clause_number: Optional[str] = None
    new_text: Optional[str] = None
    old_fragment: Optional[str] = None
    new_fragment: Optional[str] = None
    applied: bool = False
    note: Optional[str] = None


class DeadlineRule(BaseModel):
    action: str
    anchor: str
    offset: int
    unit: Literal["calendar_days", "working_days", "months", "years"]
    direction: Literal["before", "after"]
    clause_id: str


class Obligation(BaseModel):
    who: str
    what: str
    deadline: Optional[str] = None
    clause_id: str


class ProcedureStep(BaseModel):
    text: str
    clause_id: str


class Procedure(BaseModel):
    name: str
    applies_to: list[Role] = Field(default_factory=lambda: ["all"])
    trigger: str = ""
    steps: list[ProcedureStep] = Field(default_factory=list)
    forms: list[str] = Field(default_factory=list)
    responsible: Optional[str] = None
    deadlines: list[DeadlineRule] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    clause_ids: list[str] = Field(default_factory=list)


class Document(BaseModel):
    id: str
    source_path: str
    source_url: Optional[str] = None
    file_type: str = ""
    title: str = ""
    doc_type: DocType = "иное"
    number: Optional[str] = None
    date: Optional[_dt.date] = None
    effective_from: Optional[_dt.date] = None
    effective_to: Optional[_dt.date] = None
    issuer: Optional[str] = None
    roles: list[Role] = Field(default_factory=lambda: ["all"])
    status: DocStatus = "unknown"
    status_reason: Optional[str] = None
    edition_group: Optional[str] = None
    superseded_by: Optional[str] = None
    cancelled_by: list[str] = Field(default_factory=list)
    amended_by: list[str] = Field(default_factory=list)
    approved_by: Optional[str] = None
    pages: int = 0
    ocr_quality: float = 1.0
    needs_ocr: bool = False
    text_hash: str = ""
    clauses: list[Clause] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    patches: list[Patch] = Field(default_factory=list)
    unapplied_patches: int = 0
    procedures: list[Procedure] = Field(default_factory=list)
    obligations: list[Obligation] = Field(default_factory=list)
    responsible_units: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    owner_unit: Optional[str] = None
    summary: Optional[str] = None
    extraction_method: Literal["regex", "llm", "mixed"] = "regex"
    warnings: list[str] = Field(default_factory=list)

    def clause_by_id(self, cid: str) -> Optional[Clause]:
        for c in self.clauses:
            if c.id == cid:
                return c
        return None

    def clause_by_number(self, number: str, attachment: Optional[str] = None) -> Optional[Clause]:
        number = number.strip().rstrip(".")
        for c in self.clauses:
            if c.number == number and (attachment is None or c.attachment == attachment):
                return c
        return None

    @property
    def short_name(self) -> str:
        parts = [self.doc_type.capitalize()]
        if self.number:
            parts.append(f"№ {self.number}")
        if self.date:
            parts.append(f"от {self.date.strftime('%d.%m.%Y')}")
        if not self.title:
            return " ".join(parts) + (" (нет в базе)" if self.id.startswith("stub:") else "")
        if self.doc_type not in ("приказ", "распоряжение") and not self.number:
            appr = next((r for r in self.relations if r.type == "approved_by" and r.target_number), None)
            if appr:
                when = f" от {appr.target_date.strftime('%d.%m.%Y')}" if appr.target_date else ""
                return f"{self.title[:120]} (утв. приказом № {appr.target_number}{when})"
            if self.date:
                return f"{self.title[:120]} ({self.date.year} г.)"
            return self.title[:120]
        if self.doc_type in ("приказ", "распоряжение"):
            parts.append(f"«{self.title[:90]}»")
        else:
            parts = [self.title[:120]] + ([f"({self.number} от {self.date.strftime('%d.%m.%Y')})"] if self.number and self.date else [])
        return " ".join(parts)


class AuditFinding(BaseModel):
    kind: str
    severity: Literal["info", "warning", "error"] = "warning"
    doc_id: Optional[str] = None
    message: str


class AuditReport(BaseModel):
    findings: list[AuditFinding] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


class KnowledgeBase(BaseModel):
    documents: list[Document] = Field(default_factory=list)
    built_at: str = ""
    audit: Optional[AuditReport] = None
    stubs: list[Document] = Field(default_factory=list)  # документы, на которые ссылаются, но которых нет в базе

    def get(self, doc_id: str) -> Optional[Document]:
        for d in self.documents:
            if d.id == doc_id:
                return d
        for d in self.stubs:
            if d.id == doc_id:
                return d
        return None


# ---------------------------------------------------------------- запрос и ответ
class QueryUnderstanding(BaseModel):
    intent: str = ""
    role: Optional[Role] = None
    role_confidence: float = 0.0
    mentioned_documents: list[str] = Field(default_factory=list)
    as_of: Optional[_dt.date] = None
    situation_dates: list[str] = Field(default_factory=list)
    reformulations: list[str] = Field(default_factory=list)
    sub_questions: list[str] = Field(default_factory=list)  # части составного вопроса («сколько длится ... и какой штраф ...»)
    keywords: list[str] = Field(default_factory=list)
    out_of_scope: bool = False
    out_of_scope_reason: Optional[str] = None


class Citation(BaseModel):
    doc_id: str
    clause_id: str
    quote: str
    # заполняются при сборке ответа, чтобы ссылка читалась без обращения к базе: «п. 3.2» и название документа
    clause_label: Optional[str] = None
    doc_title: Optional[str] = None
    page: Optional[int] = None


class Step(BaseModel):
    text: str
    citations: list[Citation] = Field(default_factory=list)
    verdict: Optional[Literal["supported", "partial", "unsupported"]] = None
    verdict_reason: Optional[str] = None


class RoleBranch(BaseModel):
    role: Role
    steps: list[Step] = Field(default_factory=list)


class AnswerDraft(BaseModel):
    """Строгая схема вывода генератора."""
    answerable: Literal["yes", "partial", "no"]
    summary: str = ""
    applicable_documents: list[str] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    not_covered: list[str] = Field(default_factory=list)
    role_branches: list[RoleBranch] = Field(default_factory=list)
    clarifying_question: Optional[str] = None
    clarifying_options: list[str] = Field(default_factory=list)
    responsible: Optional[str] = None
    forms: list[str] = Field(default_factory=list)
    deadlines: list[str] = Field(default_factory=list)
    answer_kind: Optional[Literal["procedure", "facts"]] = None  # шаги — порядок действий или пункты-основания
    procedure_source: Optional[str] = None  # раздел документа, из которого взят порядок действий
    clarify_kind: Optional[Literal["role", "document"]] = None


class StepVerdict(BaseModel):
    step_index: int
    verdict: Literal["supported", "partial", "unsupported"]
    reason: str = ""


class StepVerdicts(BaseModel):
    verdicts: list[StepVerdict] = Field(default_factory=list)


class AnswerabilityVerdict(BaseModel):
    answerable: Literal["yes", "partial", "no"]
    reason: str = ""
    needs_role: bool = False


class VerificationCheck(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class TrustCard(BaseModel):
    checks: list[VerificationCheck] = Field(default_factory=list)
    supported_steps: int = 0
    total_steps: int = 0
    dropped_steps: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    verified_by_human: Optional[str] = None


class DocumentCard(BaseModel):
    doc_id: str
    title: str
    doc_type: str
    number: Optional[str] = None
    date: Optional[_dt.date] = None
    status: DocStatus = "unknown"
    status_reason: Optional[str] = None
    amended_by: list[str] = Field(default_factory=list)
    cancelled_by: list[str] = Field(default_factory=list)
    source_url: Optional[str] = None
    source_path: Optional[str] = None
    topics: list[str] = Field(default_factory=list)
    owner_unit: Optional[str] = None
    date_partial: bool = False

    @property
    def date_label(self) -> str:
        if not self.date:
            return ""
        return f"{self.date.year} г." if self.date_partial else self.date.strftime("%d.%m.%Y")


Outcome = Literal["answer", "partial", "refuse", "clarify", "cancelled_notice", "out_of_scope"]


class Response(BaseModel):
    outcome: Outcome
    question: str
    role: Optional[Role] = None
    as_of: _dt.date
    summary: str = ""
    steps: list[Step] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    not_covered: list[str] = Field(default_factory=list)
    documents: list[DocumentCard] = Field(default_factory=list)
    checked_documents: list[DocumentCard] = Field(default_factory=list)
    clarifying_question: Optional[str] = None
    clarifying_options: list[str] = Field(default_factory=list)
    clarify_kind: Optional[Literal["role", "document"]] = None  # что уточняем: роль спрашивающего или документ
    responsible: Optional[str] = None
    forms: list[str] = Field(default_factory=list)
    deadlines: list[str] = Field(default_factory=list)
    answer_kind: Optional[Literal["procedure", "facts"]] = None  # шаги — порядок действий или пункты-основания
    procedure_source: Optional[str] = None  # раздел документа, из которого взят порядок действий
    trust: TrustCard = Field(default_factory=TrustCard)
    trace: dict[str, Any] = Field(default_factory=dict)
    timings_ms: dict[str, int] = Field(default_factory=dict)
