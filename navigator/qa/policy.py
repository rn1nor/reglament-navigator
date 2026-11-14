"""Политика исхода: полный ответ / частичный / отказ / уточнение / уведомление об отмене / вне области."""
from __future__ import annotations

from dataclasses import dataclass

from .. import config
from ..schemas import AnswerDraft, KnowledgeBase, QueryUnderstanding, Step, TrustCard
from .retrieve import RetrievalResult


@dataclass
class Decision:
    outcome: str
    reason: str
    gate_ok: bool
    gate_value: float
    gate_kind: str


def gate(retrieval: RetrievalResult, embedder_name: str) -> tuple[bool, float, str]:
    if retrieval.top_rerank is not None:
        return retrieval.top_rerank >= config.GATE_MIN_RERANK, retrieval.top_rerank, "rerank"
    if "hash" in embedder_name:
        ok = retrieval.top_dense >= config.GATE_MIN_HASH or retrieval.lexical_overlap >= 0.5
        return ok, max(retrieval.top_dense, retrieval.lexical_overlap), "hash/overlap"
    return retrieval.top_dense >= config.GATE_MIN_DENSE or retrieval.lexical_overlap >= 0.5, retrieval.top_dense, "dense"


def role_dependent(retrieval: RetrievalResult, kb: KnowledgeBase) -> bool:
    """Верхние документы адресованы разным ролям (например, студентам и работникам)."""
    top_docs = sorted(retrieval.doc_scores.items(), key=lambda kv: -kv[1])[:3]
    if len(top_docs) < 2:
        return False
    best = top_docs[0][1]
    role_sets = []
    for did, sc in top_docs:
        if sc < best * (1 - config.CLARIFY_SCORE_GAP * 4):
            continue
        d = kb.get(did)
        if d:
            role_sets.append(frozenset(d.roles))
    student_side = {"student", "postgraduate", "applicant"}
    staff_side = {"teacher", "researcher", "admin", "staff"}
    has_student = any(rs & student_side and not rs & {"teacher", "researcher", "admin"} for rs in role_sets)
    has_staff = any(rs & staff_side and not rs & student_side for rs in role_sets)
    return has_student and has_staff


def decide(qu: QueryUnderstanding, retrieval: RetrievalResult, draft: AnswerDraft, kept: list[Step], trust: TrustCard,
           kb: KnowledgeBase, embedder_name: str) -> Decision:
    ok, val, kind = gate(retrieval, embedder_name)
    if qu.out_of_scope:
        return Decision("out_of_scope", qu.out_of_scope_reason or "вне области", ok, val, kind)
    if retrieval.mentioned_missing and not retrieval.mentioned_docs:
        return Decision("refuse", "упомянутый документ отсутствует в базе: " + ", ".join(retrieval.mentioned_missing), ok, val, kind)
    cancelled_mentioned = [d for d in retrieval.mentioned_docs if d.status in ("cancelled", "superseded", "expired")]
    if cancelled_mentioned:
        return Decision("cancelled_notice", "вопрос о документе, который отменён, заменён или истёк", ok, val, kind)
    if draft.clarifying_question and draft.clarify_kind == "document":
        return Decision("clarify", "вопрос слишком общий: подходит несколько документов", ok, val, kind)
    if draft.clarifying_question and not qu.role and (draft.role_branches or role_dependent(retrieval, kb)):
        return Decision("clarify", "норма зависит от роли, роль не указана", ok, val, kind)
    if ok and not qu.role and not draft.clarifying_question and role_dependent(retrieval, kb) and draft.answerable != "no" and not draft.role_branches:
        return Decision("clarify", "верхние документы адресованы разным ролям", ok, val, kind)
    if draft.answerable == "no" or not kept:
        return Decision("refuse", "в базе нет пункта, подтверждающего ответ" if not ok else "генератор не нашёл опоры в найденных пунктах", ok, val, kind)
    if not ok:
        return Decision("partial", f"низкая уверенность поиска ({kind}={val:.2f})", ok, val, kind)
    if trust.dropped_steps or draft.answerable == "partial" or draft.not_covered:
        return Decision("partial", "часть шагов не подтверждена или вопрос покрыт частично", ok, val, kind)
    return Decision("answer", "все шаги подтверждены", ok, val, kind)
