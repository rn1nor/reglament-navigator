"""Проверка обоснованности: детерминированные проверки цитат и статусов + судья по каждому шагу."""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from rapidfuzz import fuzz

from .. import config
from ..index.text import normalize_for_match, tokenize
from ..llm.base import LLMProvider, spec
from ..schemas import AnswerDraft, KnowledgeBase, Step, StepVerdicts, TrustCard, VerificationCheck
from .retrieve import ContextUnit

SYSTEM_JUDGE = """Ты строгий проверяющий ответов по нормативным документам. Для каждого шага ответа оцени, подтверждается ли он ТЕКСТОМ процитированных пунктов:
- supported: шаг прямо следует из текста пунктов (допустим пересказ без искажения смысла);
- partial: часть утверждения следует из текста, часть додумана, обобщена или взята не из этих пунктов;
- unsupported: текст пунктов этого не говорит или говорит иное.
Не оценивай полезность и стиль, только опору на текст. Верни вердикт для каждого индекса шага и короткую причину."""


def quote_matches(quote: str, text: str) -> int:
    q = normalize_for_match(quote)
    t = normalize_for_match(text)
    if not q:
        return 0
    if q in t:
        return 100
    return int(fuzz.partial_ratio(q, t))


def deterministic(draft: AnswerDraft, units: list[ContextUnit], kb: KnowledgeBase, as_of: _dt.date,
                  doc_status: Optional[dict[str, tuple[str, str]]] = None) -> tuple[list[VerificationCheck], list[str], dict[int, list[int]]]:
    """Возвращает проверки, предупреждения и для каждого шага индексы валидных цитат."""
    by_key = {u.key: u for u in units}
    checks: list[VerificationCheck] = []
    warnings: list[str] = []
    valid: dict[int, list[int]] = {}
    total_cit = 0
    bad_exist = bad_quote = 0
    cited_docs: set[str] = set()
    for si, step in enumerate(draft.steps):
        valid[si] = []
        for ci, c in enumerate(step.citations):
            total_cit += 1
            u = by_key.get((c.doc_id, c.clause_id))
            if u is None:
                bad_exist += 1
                continue
            score = quote_matches(c.quote, u.text)
            if score < config.QUOTE_MATCH_THRESHOLD:
                bad_quote += 1
                continue
            valid[si].append(ci)
            cited_docs.add(c.doc_id)
    checks.append(VerificationCheck(name="Цитаты указывают на переданные пункты", passed=bad_exist == 0,
                                    detail=f"{total_cit - bad_exist} из {total_cit}" if total_cit else "цитат нет"))
    checks.append(VerificationCheck(name="Текст цитат совпадает с документом", passed=bad_quote == 0,
                                    detail=f"не совпало: {bad_quote}" if bad_quote else "все совпали"))
    # статусы
    inactive = []
    stale = []
    low_ocr = []
    unapplied = []
    for did in cited_docs:
        d = kb.get(did)
        if d is None:
            continue
        status, reason = doc_status.get(did, (d.status, d.status_reason or d.status)) if doc_status is not None else (d.status, d.status_reason or d.status)
        if status != "active":
            inactive.append(f"{d.short_name[:70]}: {reason or status}")
        if d.needs_ocr or d.ocr_quality < 0.85:
            low_ocr.append(d.short_name[:70])
        for a in d.amended_by:
            o = kb.get(a)
            if o and o.unapplied_patches:
                unapplied.append(f"{o.short_name[:60]}: поправок не применено {o.unapplied_patches}")
    cited_now = {(c.doc_id, c.clause_id) for s in draft.steps for c in s.citations}
    for u in units:
        if u.key in cited_now and u.provenance:
            names = [(kb.get(p).short_name[:60] if kb.get(p) else p) for p in u.provenance]
            stale.append(f"{u.label}: в ред. {', '.join(names)}")
    checks.append(VerificationCheck(name="Документы действуют на дату запроса", passed=not inactive,
                                    detail="; ".join(inactive) if inactive else as_of.strftime("%d.%m.%Y")))
    checks.append(VerificationCheck(name="Редакция пунктов актуальна", passed=not unapplied,
                                    detail="; ".join(unapplied) if unapplied else ("применены поправки: " + "; ".join(stale[:3]) if stale else "поправок к процитированным пунктам нет")))
    checks.append(VerificationCheck(name="Качество распознавания текста", passed=not low_ocr, detail="; ".join(low_ocr) if low_ocr else "текстовый слой"))
    if unapplied:
        warnings.append("к документу есть поправки, текст которых не удалось применить автоматически: сверьтесь с приказом")
    if low_ocr:
        warnings.append("текст распознан автоматически, сверьтесь с оригиналом")
    return checks, warnings, valid


def judge(draft: AnswerDraft, units: list[ContextUnit], valid: dict[int, list[int]], provider: LLMProvider, question: str) -> StepVerdicts:
    by_key = {u.key: u for u in units}
    if provider.kind == "mock":
        verdicts = []
        for si, step in enumerate(draft.steps):
            texts = [by_key[(step.citations[ci].doc_id, step.citations[ci].clause_id)].text for ci in valid.get(si, [])]
            if not texts:
                verdicts.append({"step_index": si, "verdict": "unsupported", "reason": "нет валидных цитат"})
                continue
            st = set(tokenize(step.text))
            ct = set(tokenize(" ".join(texts)))
            ov = len(st & ct) / len(st) if st else 0.0
            v = "supported" if ov >= 0.5 else "partial" if ov >= 0.25 else "unsupported"
            verdicts.append({"step_index": si, "verdict": v, "reason": f"лексическое покрытие {ov:.2f} (эвристика)"})
        return StepVerdicts.model_validate({"verdicts": verdicts})
    blocks = []
    for si, step in enumerate(draft.steps):
        cites = [by_key[(step.citations[ci].doc_id, step.citations[ci].clause_id)] for ci in valid.get(si, [])]
        if not cites:
            continue
        ctext = "\n".join(f"  [{u.doc_id} {u.clause_id}] {u.text[:1500]}" for u in cites)
        blocks.append(f"Шаг {si}: {step.text}\nПроцитированные пункты:\n{ctext}")
    if not blocks:
        return StepVerdicts(verdicts=[])
    s = spec("judge")
    user = f"Вопрос пользователя: {question}\n\n" + "\n\n".join(blocks)
    return provider.structured(system=SYSTEM_JUDGE, user=user, schema=StepVerdicts, model=s.model, effort=s.effort, max_tokens=4000)


def verify(draft: AnswerDraft, units: list[ContextUnit], kb: KnowledgeBase, provider: LLMProvider, question: str,
           as_of: Optional[_dt.date] = None, doc_status: Optional[dict[str, tuple[str, str]]] = None) -> tuple[list[Step], TrustCard]:
    """doc_status: статусы документов на дату запроса (если вопрос задан не на сегодня)."""
    as_of = as_of or _dt.date.today()
    checks, warnings, valid = deterministic(draft, units, kb, as_of, doc_status)
    try:
        verdicts = judge(draft, units, valid, provider, question)
        vmap = {v.step_index: v for v in verdicts.verdicts}
    except Exception as e:  # noqa: BLE001
        vmap = {}
        warnings.append(f"судья недоступен ({type(e).__name__}): шаги приняты по детерминированным проверкам")
    kept: list[Step] = []
    dropped: list[str] = []
    for si, step in enumerate(draft.steps):
        step.citations = [c for ci, c in enumerate(step.citations) if ci in valid.get(si, [])]
        v = vmap.get(si)
        if not step.citations:
            step.verdict, step.verdict_reason = "unsupported", "нет цитаты, совпадающей с текстом документа"
        elif v is not None:
            step.verdict, step.verdict_reason = v.verdict, v.reason
        else:
            step.verdict, step.verdict_reason = "supported", "цитата совпадает с документом"
        if step.verdict == "unsupported":
            dropped.append(step.text)
        else:
            kept.append(step)
    supported = sum(1 for s in kept if s.verdict == "supported")
    checks.append(VerificationCheck(name="Шаги подтверждены документами", passed=not dropped,
                                    detail=f"подтверждено {supported}, частично {len(kept) - supported}, отброшено {len(dropped)}"))
    card = TrustCard(checks=checks, supported_steps=supported, total_steps=len(draft.steps), dropped_steps=dropped, warnings=warnings)
    return kept, card
