"""Оркестрация: вопрос -> проверенные ответы -> понимание -> поиск -> генерация -> проверка -> сроки -> решение -> ответ с трассой."""
from __future__ import annotations

import datetime as _dt
import re
import time
from pathlib import Path
from typing import Optional

from .. import config
from ..index.dense import get_embedder
from ..index.rerank import get_reranker
from ..index.store import HybridIndex, index_dirname
from ..ingest.contradictions import load_contradictions
from ..ingest.pipeline import load_kb
from ..llm.base import LLMProvider, MockProvider, get_provider
from ..llm.privacy import is_external, kb_is_confidential
from ..schemas import ROLE_LABELS, Document, DocumentCard, KnowledgeBase, Response, VerificationCheck
from .approved import ApprovedStore
from .deadlines import deadlines_for_units
from .generate import generate, short_title, status_label
from .policy import decide
from .query import MAX_QUESTION_CHARS, understand, validate_question
from .retrieve import retrieve
from .timeline import later_amendments, snapshot
from .verify import verify


def card(d: Document) -> DocumentCard:
    return DocumentCard(doc_id=d.id, title=d.title, doc_type=d.doc_type, number=d.number, date=d.date, status=d.status,
                        status_reason=d.status_reason, amended_by=list(d.amended_by), cancelled_by=list(d.cancelled_by),
                        source_url=d.source_url, source_path=d.source_path, topics=list(d.topics), owner_unit=d.owner_unit,
                        date_partial=any("только год" in w for w in d.warnings))


DOC_HINT_RE = re.compile(r"\s*Документ:\s*(.{5,200}?)\s*$")


class Navigator:
    def __init__(self, kb_dir: Path | str, index_dir: Optional[Path | str] = None, provider: Optional[LLMProvider] = None,
                 dense: Optional[str] = None, reranker: Optional[str] = None, build_if_missing: bool = True):
        self.kb_dir = Path(kb_dir)
        self.kb: KnowledgeBase = load_kb(self.kb_dir)
        self.use_dense = (dense or config.USE_DENSE) != "off"
        self.embedder = get_embedder(dense)
        default_dir = index_dirname(self.embedder.name) if self.use_dense else "index-bm25"
        self.index_dir = Path(index_dir) if index_dir else self.kb_dir / default_dir
        need_dense = self.use_dense and "hash" not in self.embedder.name
        if (self.index_dir / "units.json").exists() and not HybridIndex.is_stale(self.index_dir, self.kb, need_dense=need_dense):
            self.index = HybridIndex.load(self.index_dir, self.embedder)
        elif build_if_missing:
            if (self.index_dir / "units.json").exists():
                print(f"[index] индекс {self.index_dir} устарел или неполный: пересобираю ({self.embedder.name})")
            self.index = HybridIndex.build(self.kb, self.embedder, dense=self.use_dense, cache_dir=self.index_dir if self.use_dense else None)
            self.index.save(self.index_dir)
        else:
            raise FileNotFoundError(f"индекс не найден: {self.index_dir}")
        self.provider = provider or get_provider()
        self.confidential = kb_is_confidential(self.kb_dir)
        self.privacy_note: Optional[str] = None
        if self.confidential and is_external(self.provider):
            self.privacy_note = ("база помечена как закрытая: внешняя модель отключена, ответы строятся без модели "
                                 "(подключите локальную модель или пометьте базу как открытую в policy.json)")
            self.provider = MockProvider()
        self.reranker = get_reranker(reranker)
        self.approved = ApprovedStore(self.kb_dir / "approved.json", self.embedder)
        self.contradictions = load_contradictions(self.kb_dir)

    # ------------------------------------------------------------------
    def today(self) -> _dt.date:
        return _dt.date.fromisoformat(config.TODAY_OVERRIDE) if config.TODAY_OVERRIDE else _dt.date.today()

    def _ref(self, key: tuple[str, str], quote_len: int = 150) -> str:
        """«Название документа, п. N («начало текста пункта»)» для предупреждений о расхождениях."""
        d = self.kb.get(key[0])
        c = d.clause_by_id(key[1]) if d else None
        if d is None or c is None:
            return f"{key[0]} {key[1]}"
        text = " ".join(c.text.split())
        text = text if len(text) <= quote_len else text[:quote_len].rsplit(" ", 1)[0] + "…"
        return f"{short_title(d, 70)}, {c.label} («{text.rstrip(' ;.')}»)"

    def approve(self, response: Response, approved_by: str):
        return self.approved.add(response, self.kb, approved_by)

    def _from_approved(self, entry, sim: float, question: str) -> Response:
        resp = Response.model_validate(entry.response)
        resp.question = question
        resp.trust.verified_by_human = f"{entry.approved_by}, {entry.approved_at[:10]}"
        resp.trust.checks.append(VerificationCheck(name="Подтверждено сотрудником", passed=True, detail=f"{entry.approved_by}, {entry.approved_at[:10]}; совпадение вопроса {sim:.2f}"))
        resp.trace = {"provider": "approved", "approved_entry": entry.id, "similarity": round(sim, 3), "original_question": entry.question}
        return resp

    def ask(self, question: str, role: Optional[str] = None, as_of: Optional[_dt.date] = None) -> Response:
        t0 = time.perf_counter()
        timings: dict[str, int] = {}
        today = self.today()
        problem = validate_question(question)
        if problem:
            return Response(outcome="out_of_scope", question=question or "", role=role if role in ROLE_LABELS else None, as_of=as_of or today,
                            summary=f"Запрос не обработан: {problem}.", trace={"provider": "validation", "decision": {"outcome": "invalid", "reason": problem}})
        if len(question) > MAX_QUESTION_CHARS:
            question = question[:MAX_QUESTION_CHARS]
        if role not in ROLE_LABELS:
            role = None  # неизвестная роль не должна ронять запрос: отвечаем без учёта роли
        try:
            return self._ask(question, role, as_of, t0, timings, today)
        except Exception as e:  # noqa: BLE001  техническая ошибка не роняет интерфейс и API
            return Response(outcome="refuse", question=question, role=role if role in ROLE_LABELS else None, as_of=as_of or today,
                            summary=f"Техническая ошибка при обработке запроса ({type(e).__name__}). Ответ не сформирован, чтобы не выдать непроверенные сведения; повторите запрос или сообщите администратору.",
                            trace={"provider": self.provider.kind, "error": f"{type(e).__name__}: {e}"})

    def _doc_hint(self, question: str) -> tuple[str, Optional[Document]]:
        """Вопрос после уточнения «... Документ: <название>»: возвращает сам вопрос и выбранный документ."""
        m = DOC_HINT_RE.search(question)
        if not m:
            return question, None
        hint = " ".join(m.group(1).split()).rstrip("…").lower()
        for d in self.kb.documents:
            if d.status != "active":
                continue
            names = [short_title(d, 90).lower(), " ".join(d.title.split()).lower(), d.short_name.lower()]
            if any(n.rstrip("…").startswith(hint[:60]) or hint.startswith(n.rstrip("…")[:60]) for n in names if n):
                return question[:m.start()].strip(), d
        return question, None

    def _ask(self, question: str, role: Optional[str], as_of: Optional[_dt.date], t0: float, timings: dict[str, int], today: _dt.date) -> Response:
        stale_note: Optional[str] = None
        asked = question
        question, chosen_doc = self._doc_hint(question)
        hit = self.approved.match(question, role)
        if hit is not None:
            entry, sim = hit
            ok, reasons = self.approved.validate(entry, self.kb)
            if ok:
                resp = self._from_approved(entry, sim, question)
                resp.timings_ms = {"approved_lookup": int((time.perf_counter() - t0) * 1000)}
                return resp
            stale_note = "ранее подтверждённый ответ устарел и не показан: " + "; ".join(reasons[:3])
        qu = understand(question, role, self.provider, self.kb)
        as_of = as_of or qu.as_of or today
        timings["understand"] = int((time.perf_counter() - t0) * 1000)
        t1 = time.perf_counter()
        # вопрос «по состоянию на дату»: ищем по документам, действовавшим на эту дату, а не по сегодняшним статусам
        doc_status = snapshot(self.kb, as_of) if as_of != today else None
        active_then = {i for i, (st, _) in doc_status.items() if st == "active"} if doc_status is not None else None

        def card_at(d: Document) -> DocumentCard:
            """Карточка документа со статусом на дату запроса (и сегодняшним статусом в пояснении)."""
            c = card(d)
            if doc_status is not None and d.id in doc_status:
                st, reason = doc_status[d.id]
                c.status = st if st in ("active", "cancelled", "superseded", "expired") else "unknown"
                c.status_reason = f"{reason}; сейчас: {status_label(d)}"
            return c

        scope = active_then
        if chosen_doc is not None:  # пользователь сам выбрал документ в ответ на уточнение
            scope = {chosen_doc.id} if scope is None else (scope & {chosen_doc.id})
        retrieval = retrieve(self.kb, self.index, qu, question, reranker=self.reranker, as_of=as_of, use_dense=self.use_dense, doc_ids=scope)
        if chosen_doc is not None and retrieval.top_rerank is not None and retrieval.hits:
            # документ назван явно, поэтому общий вопрос («какой срок подачи заявки?») оцениваем мягче: показываем лучшее из выбранного документа
            retrieval.top_rerank = min(1.0, retrieval.top_rerank + config.GATE_MIN_RERANK / 2)
        timings["retrieve"] = int((time.perf_counter() - t1) * 1000)
        t2 = time.perf_counter()
        draft = generate(question, qu, retrieval, self.kb, self.provider, as_of=as_of, reranker=self.reranker, contradictions=self.contradictions)
        timings["generate"] = int((time.perf_counter() - t2) * 1000)
        t3 = time.perf_counter()
        kept, trust = verify(draft, retrieval.units, self.kb, self.provider, question, as_of=as_of, doc_status=doc_status)
        timings["verify"] = int((time.perf_counter() - t3) * 1000)
        decision = decide(qu, retrieval, draft, kept, trust, self.kb, self.embedder.name)
        for s in kept:  # ссылка должна читаться сама по себе: название документа и пункт рядом с каждым шагом
            for c in s.citations:
                d = self.kb.get(c.doc_id)
                cl = d.clause_by_id(c.clause_id) if d else None
                c.doc_title = short_title(d, 120) if d else None
                c.clause_label = cl.label if cl else c.clause_id
                c.page = cl.page_start if cl else None
        if stale_note:
            trust.warnings.append(stale_note)
        if self.privacy_note:
            trust.warnings.append(self.privacy_note)
        if doc_status is not None:
            trust.warnings.append(f"Ответ дан по состоянию на {as_of.strftime('%d.%m.%Y')}: использованы документы, действовавшие на эту дату.")
            for s in kept:  # пункт мог быть изменён позже даты запроса: показываем, что редакция на ту дату была другой
                for c in s.citations:
                    d = self.kb.get(c.doc_id)
                    cl = d.clause_by_id(c.clause_id) if d else None
                    later = later_amendments(self.kb, cl.provenance, as_of) if cl is not None and cl.provenance else []
                    if later and cl.original_text and cl.original_text != cl.text:
                        old = " ".join(cl.original_text.split())
                        trust.warnings.append(f"{cl.label} позже изменён ({'; '.join(o.short_name[:70] for o in later)}); на {as_of.strftime('%d.%m.%Y')} "
                                              f"он действовал в редакции: «{old[:300]}{'…' if len(old) > 300 else ''}»")
        trust.checks.append(VerificationCheck(name="Уверенность поиска", passed=decision.gate_ok, detail=f"{decision.gate_kind}={decision.gate_value:.2f}"))
        # расхождения с другими действующими пунктами: называем оба документа и оба пункта дословно
        cited_keys = {(c.doc_id, c.clause_id) for s in kept for c in s.citations}
        conflicts: list[str] = []
        seen_pairs: set[frozenset] = set()
        for key in sorted(cited_keys):
            for other in self.contradictions.get(key, []):
                okey = (other.get("doc_id", ""), other.get("clause_id", ""))
                pair = frozenset((key, okey))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                conflicts.append(f"{self._ref(key)} и {self._ref(okey)}")
        trust.checks.append(VerificationCheck(name="Противоречий с другими действующими пунктами не найдено", passed=not conflicts,
                                              detail="; ".join(conflicts[:3]) if conflicts else "по списку кандидатов паспорта базы"))
        if conflicts:
            trust.warnings.append("Расхождение между документами: " + "; ".join(conflicts[:2]) + ". Сверьте оба пункта перед применением.")
        # сроки, посчитанные под даты из вопроса
        cited_units = [u for u in retrieval.units if u.key in cited_keys]
        top_doc_ids = {c.doc_id for c in cited_units} or set(draft.applicable_documents)
        other_units = [u for u in retrieval.units if u.key not in cited_keys and u.doc_id in top_doc_ids and u.reason in ("hit", "section")]
        rules = deadlines_for_units(cited_units + other_units, qu.situation_dates)
        deadlines = [r.describe() for r in rules] + [d for d in draft.deadlines if d not in {r.source_text for r in rules}]
        for r in rules:
            for w in r.warnings:
                if w not in trust.warnings:
                    trust.warnings.append(w)
        top_docs = [self.kb.get(did) for did, _ in sorted(retrieval.doc_scores.items(), key=lambda kv: -kv[1])[:5]]
        top_docs = [d for d in top_docs if d]
        cited_ids = {c.doc_id for s in kept for c in s.citations}
        documents = [card_at(self.kb.get(i)) for i in dict.fromkeys([*draft.applicable_documents, *cited_ids]) if self.kb.get(i)]
        resp = Response(outcome=decision.outcome, question=asked, role=qu.role, as_of=as_of, summary=draft.summary, steps=kept,
                        conditions=draft.conditions, not_covered=draft.not_covered, documents=documents,
                        checked_documents=[card_at(d) for d in top_docs], clarifying_question=draft.clarifying_question,
                        clarifying_options=draft.clarifying_options, responsible=draft.responsible, forms=draft.forms,
                        deadlines=deadlines[:6], trust=trust, timings_ms=timings,
                        answer_kind=draft.answer_kind if kept else None, procedure_source=draft.procedure_source if kept else None)
        if decision.outcome in ("refuse", "out_of_scope"):
            resp.answer_kind = resp.procedure_source = None
        if decision.outcome == "refuse":
            resp.steps = []
            resp.deadlines = []
            checked = "; ".join(f"«{short_title(d, 90)}» ({status_label(d)})" for d in top_docs[:3]) or "ничего релевантного"
            missing = ""
            if retrieval.mentioned_missing:
                notes = []
                verbs = {"amends": "изменяет его", "cancels": "отменяет его", "replaces": "заменяет его", "references": "ссылается на него",
                         "approves": "утверждает его", "approved_by": "утверждён им"}
                for num in retrieval.mentioned_missing:
                    refs = []
                    for d in self.kb.documents:
                        for r in d.relations:
                            st = self.kb.get(r.target_doc_id) if r.target_doc_id else None
                            if st is not None and st.id.startswith("stub:") and st.number and st.number.upper() == num.upper():
                                refs.append(f"{short_title(d, 90)} ({verbs.get(r.type, 'упоминает его')})")
                    note = f"Документ № {num} в базе отсутствует"
                    if refs:
                        note += "; он упоминается в документах базы: " + "; ".join(dict.fromkeys(refs[:3]))
                    notes.append(note + ". ")
                missing = "".join(notes)
            if chosen_doc is not None:
                resp.summary = f"В документе «{short_title(chosen_doc, 110)}» не найдено положение, отвечающее на этот вопрос. Уточните формулировку или выберите другой документ."
            else:
                resp.summary = (f"{missing}В базе не найден регламент или приказ, подтверждающий ответ на этот вопрос. Проверены ближайшие по теме документы: {checked}. "
                                f"{draft.summary if draft.answerable == 'no' and draft.summary else ''}").strip()
        elif decision.outcome == "cancelled_notice":
            notes = []
            words = {"cancelled": "отменён", "superseded": "заменён новой редакцией", "expired": "больше не действует: срок действия истёк"}
            for d in retrieval.mentioned_docs:
                if d.status in ("cancelled", "superseded", "expired"):
                    by = [self.kb.get(x) for x in d.cancelled_by] or ([self.kb.get(d.superseded_by)] if d.superseded_by else [])
                    by_s = "; ".join(short_title(b, 110) for b in by if b) or "сведения об отменяющем документе отсутствуют"
                    stub = " (текста самого документа в базе нет)" if d.id.startswith("stub:") else ""
                    notes.append(f"{short_title(d, 110)} {words[d.status]}{stub}. Основание: {by_s}.")
            resp.summary = " ".join(notes) + (f" Действующие положения по теме: {draft.summary}" if draft.summary and kept else "")
            resp.documents = [card_at(d) for d in retrieval.mentioned_docs] + [c for c in documents if c.doc_id not in {d.id for d in retrieval.mentioned_docs}]
        elif decision.outcome == "clarify":
            resp.steps = []
            resp.deadlines = []
            resp.answer_kind = resp.procedure_source = None
            resp.clarify_kind = draft.clarify_kind or "role"
            resp.clarifying_question = draft.clarifying_question or "Уточните, пожалуйста, вашу роль: правила различаются для обучающихся и работников."
            resp.clarifying_options = draft.clarifying_options or ["студент", "аспирант", "преподаватель", "сотрудник АУП"]
            if resp.clarify_kind == "document":
                resp.summary = draft.summary
                resp.documents = [card_at(self.kb.get(i)) for i in draft.applicable_documents if self.kb.get(i)]
        elif decision.outcome == "out_of_scope":
            resp.steps = []
            resp.summary = "Вопрос не относится к внутренним регламентам и приказам, по которым работает навигатор."
        resp.trace = {
            "provider": self.provider.kind, "embedder": self.embedder.name if self.use_dense else "bm25-only", "confidential": self.confidential, "reranker": getattr(self.reranker, "name", None),
            "query": qu.model_dump(mode="json"), "queries": retrieval.queries, "method": retrieval.method,
            "decision": decision.__dict__,
            "hits": [{"doc_id": h.unit.doc_id, "clause_id": h.unit.clause_id, "rrf": round(h.score, 4), "bm25": round(h.bm25, 2),
                      "dense": round(h.dense, 3), "rerank": (round(h.rerank, 3) if h.rerank is not None else None)} for h in retrieval.hits[:15]],
            "context_units": [{"doc_id": u.doc_id, "clause_id": u.clause_id, "reason": u.reason} for u in retrieval.units],
            "deadline_rules": [r.__dict__ | {"computed": r.computed.isoformat() if r.computed else None, "fixed_date": r.fixed_date.isoformat() if r.fixed_date else None} for r in rules],
            "draft": draft.model_dump(mode="json"), **retrieval.notes,
        }
        timings["total"] = int((time.perf_counter() - t0) * 1000)
        resp.timings_ms = dict(timings)
        return resp
