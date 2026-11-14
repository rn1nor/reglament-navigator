"""Генерация ответа в строгой схеме: документы -> шаги -> цитата на пункт у каждого шага.
Без модели работает экстрактивный режим: краткий ответ и шаги собираются из дословных положений документа,
для вопросов «как сделать» — в порядке раздела, который описывает процедуру."""
from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

from ..index.text import tokenize
from ..llm.base import LLMProvider, spec
from ..schemas import ROLE_LABELS, AnswerDraft, Citation, Clause, Document, KnowledgeBase, QueryUnderstanding, Step
from .retrieve import ContextUnit, RetrievalResult, context_unit

SYSTEM_GENERATOR = """Ты — навигатор по внутренним регламентам и приказам МФТИ для сотрудников и студентов.
Отвечай ТОЛЬКО на основании переданных фрагментов документов. Это внутренние нормативные акты; жюри и кадровики будут проверять каждую фразу по первоисточнику.

Правила:
1. Каждый шаг (steps[].text) обязан опираться минимум на одну цитату citations[] = {doc_id, clause_id, quote}. doc_id и clause_id бери ровно из заголовков фрагментов. quote — дословный фрагмент (до 200 символов) из текста именно этого пункта, без правок.
2. Не придумывай шаги «из здравого смысла». Если фрагменты не описывают нужное действие, не добавляй его в steps; перечисли, что не покрыто, в not_covered.
3. answerable: "yes" — фрагменты полностью отвечают на вопрос; "partial" — отвечают частично; "no" — не отвечают (тогда steps пустой, summary объясняет, чего нет в документах).
4. Учитывай статус документов: отменённые и предыдущие редакции не используй как основание, можешь лишь упомянуть их в summary.
5. Если норма зависит от роли (студент / аспирант / преподаватель / АУП), а роль спрашивающего неизвестна и фрагменты дают разные правила, задай один clarifying_question с clarifying_options и заполни role_branches для каждой роли.
6. Если в тексте названо подразделение, куда подавать документы, или ответственный — заполни responsible. Формы и приложения — в forms. Сроки — в deadlines (дословно с указанием пункта).
7. summary — 1–3 предложения по существу. Шаги — кратко, в повелительном наклонении, в порядке выполнения. conditions — условия и исключения из текста.
8. Пиши по-русски. Никаких ссылок на документы вне переданных фрагментов."""


def status_label(d: Document) -> str:
    base = {"active": "действует", "cancelled": "отменён", "superseded": "предыдущая редакция", "expired": "срок истёк", "unknown": "статус не определён"}[d.status]
    if d.status == "active" and d.amended_by:
        base += " (с изменениями)"
    return base


def format_units(units: list[ContextUnit], kb: KnowledgeBase) -> str:
    out = []
    for u in units:
        d = kb.get(u.doc_id)
        if d is None:
            continue
        crumbs = " / ".join(p for p in u.path[-2:] if p)
        prov = ""
        if u.provenance:
            names = [kb.get(o).short_name if kb.get(o) else o for o in u.provenance]
            prov = f" | в ред.: {'; '.join(n[:70] for n in names)}"
        page = f" | стр. {u.page}" if u.page else ""
        out.append(f"[doc_id={u.doc_id} | clause_id={u.clause_id} | {d.short_name[:140]} | статус: {status_label(d)} | {crumbs}{page}{prov}]\n{u.text[:2500]}")
    return "\n\n".join(out)


def build_user_prompt(question: str, qu: QueryUnderstanding, units: list[ContextUnit], kb: KnowledgeBase, as_of: _dt.date) -> str:
    role = ROLE_LABELS.get(qu.role, "не указана") if qu.role else "не указана"
    dates = f"Даты из ситуации: {', '.join(qu.situation_dates)}\n" if qu.situation_dates else ""
    return (f"Вопрос: {question}\nРоль спрашивающего: {role}\nДата, на которую нужен ответ: {as_of.isoformat()}\n{dates}\n"
            f"Фрагменты документов ({len(units)}):\n\n{format_units(units, kb)}")


def generate(question: str, qu: QueryUnderstanding, retrieval: RetrievalResult, kb: KnowledgeBase, provider: LLMProvider,
             as_of: Optional[_dt.date] = None, reranker=None, contradictions: Optional[dict] = None) -> AnswerDraft:
    as_of = as_of or _dt.date.today()
    if provider.kind == "mock" or not retrieval.units:
        return extractive_generate(question, qu, retrieval, kb, reranker=reranker, contradictions=contradictions)
    s = spec("generator")
    try:
        return provider.structured(system=SYSTEM_GENERATOR, user=build_user_prompt(question, qu, retrieval.units, kb, as_of), schema=AnswerDraft,
                                   model=s.model, effort=s.effort, max_tokens=12000)
    except Exception as e:  # noqa: BLE001
        draft = extractive_generate(question, qu, retrieval, kb, reranker=reranker, contradictions=contradictions)
        draft.summary = f"Модель недоступна ({type(e).__name__}); показаны релевантные пункты документа. " + draft.summary
        return draft


# ------------------------------------------------------------------ экстрактивный режим
_SENT = re.compile(r"(?<=[.!?;])\s+(?=[А-ЯЁA-Z«(\d])")
RESP_RE = re.compile(r"\b(?:в|во)\s+((?:Управлени\w+|Отдел\w*|Деканат\w*|Учебн\w+\s+част\w*|Учебн\w+\s+управлени\w*|Приёмн\w+\s+комисси\w*|Приемн\w+\s+комисси\w*|Бухгалтери\w*|Канцеляри\w*|Дирекци\w*|Ректорат\w*)[^.,;:()]{0,60})", re.I)
FORM_RE = re.compile(r"(приложени\w*\s*№?\s*\d+|форм\w+\s+заявлени\w*|по\s+установленной\s+форме)", re.I)
ACTION_RE = re.compile(r"\b(подаёт|подает|подать|представля\w+|обращ\w+|издаётся|издается|оформля\w+|согласов\w+|направля\w+|уведомля\w+|рассматрива\w+|принима\w+|предоставля\w+|обязан\w*|должн\w*|вправе|допускается|не\s+позднее|в\s+течение|срок)\b", re.I)
DEADLINE_RE = re.compile(r"в\s+течение\s+\S+|не\s+позднее|не\s+ранее|за\s+\d+\s+\S*\s*(?:рабоч|календарн)?\S*\s*дн|до\s+\d+\s+числа|в\s+срок", re.I)
# вопрос о порядке действий: «как оформить», «что нужно сделать», «с чего начать», «какой порядок»
PROC_RE = re.compile(r"(?<![а-яёa-z])(?:(?<!так )как(?![а-яёa-z])(?!\s+(?:долго|часто|скоро|давно|много|быстро)(?![а-яё]))|каков\w*\s+порядок|в\s+каком\s+порядке|"
                     r"порядок\s+[а-яё]{4,}|процедур\w*|что\s+(?:мне\s+)?(?:нужно|надо|необходимо|требуется|делать|сделать)|с\s+чего\s+начать|какие\s+шаги|каким\s+образом)", re.I)
DEFINITION_RE = re.compile(r"^[^.:;]{3,90}\s[–—-]\s(?:это\s+)?[а-яё]")  # «Служебная командировка – поездка работника ...»: определение, а не действие
TERMS_RE = re.compile(r"следующие\s+(?:основные\s+)?(?:термины|понятия|определения|сокращения)", re.I)
ORDER_CONTENT_RE = re.compile(r"что\s+(?:говорит|изменил\w*|установил\w*|утвердил\w*|предписывает|содержит|сказано)|о\s+ч[её]м\s|содержание", re.I)
BLANK_RE = re.compile(r"_{5,}|\.{8,}|…{3,}")  # строки бланков и форм с полями для заполнения
MAX_STEPS = 8
FULL_SECTION = 12  # раздел с процедурой не длиннее этого показывается целиком
PART_MIN_RERANK = 0.08  # часть составного вопроса считается покрытой, если её лучший пункт набирает хотя бы столько (сам вопрос уже прошёл общий порог)
STEP_KINDS = ("clause", "paragraph", "item", "order_item")


def _sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT.split(text.strip()) if p.strip()]
    return parts or [text.strip()]


def _score_sentence(sent: str, q_tokens: set[str]) -> float:
    st = set(tokenize(sent))
    if not st:
        return 0.0
    overlap = len(st & q_tokens) / (len(q_tokens) or 1)
    action = 0.15 if ACTION_RE.search(sent) else 0.0
    length_pen = 0.1 if len(sent) > 400 else 0.0
    return overlap + action - length_pen


def is_procedural(text: str) -> bool:
    return bool(PROC_RE.search(text.lower()))


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-–")
    return cut + "…"


def _lead(text: str, limit: int = 340) -> str:
    """Начало пункта целыми предложениями в пределах limit; длинное первое предложение обрезается по слову."""
    sents = _sentences(text)
    out = sents[0]
    for s_ in sents[1:]:
        if len(out) + 1 + len(s_) > limit:
            break
        out = f"{out} {s_}"
    return _clip(out, limit + 80)


def _quote(text: str) -> str:
    """Цитата — дословное начало пункта: всегда подстрока текста, по которой шаг сверяется с документом."""
    first = _sentences(text)[0]
    q = first if len(first) >= 20 else text
    return q[:200].strip()


def _rerank(reranker, query: str, texts: list[str]) -> Optional[list[float]]:
    if reranker is None or not texts:
        return None
    try:
        return [float(x) for x in reranker.score(query, texts)]
    except Exception:  # noqa: BLE001  реранкер не обязателен: без него работает лексическая оценка
        return None


def _key_sentence(text: str, query: str, q_tokens: set[str], reranker=None, limit: int = 300) -> str:
    """Положение пункта, отвечающее на вопрос: короткий пункт целиком, в длинном — предложение, лучшее по оценке реранкера
    (без него — по пересечению слов с вопросом)."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    sents = [x for x in _sentences(text) if len(x) >= 15] or [flat]
    use_model = len(sents) > 1 and not getattr(reranker, "lite", False)  # на CPU ключевое предложение выбираем по словам
    scores = _rerank(reranker, query, [x[:600] for x in sents]) if use_model else None
    if scores is not None:
        best = sents[max(range(len(sents)), key=lambda i: scores[i])]
    else:
        best = max(sents, key=lambda s_: _score_sentence(s_, q_tokens))
    return _clip(best, limit)


def short_title(d: Document, limit: int = 96) -> str:
    """Короткое название документа для текста ответа: без полного наименования учреждения, с реквизитами приказа."""
    if d.doc_type in ("приказ", "распоряжение") and d.number:
        when = f" от {d.date.strftime('%d.%m.%Y')}" if d.date else ""
        return f"{d.doc_type.capitalize()} № {d.number}{when}"
    title = " ".join(d.title.split())
    title = re.sub(r"\s+федеральн\w+\s+государственн\w+\s+автономн\w+.*$", "", title, flags=re.I)
    title = re.sub(r"\s*«?Московск\w+\s+физико-техническ\w+\s+институт.*$", "", title, flags=re.I)
    first, _, rest = title.partition(" ")
    if first.isupper() and len(first) > 3:
        title = first.capitalize() + (" " + rest if rest else "")
    title = _clip(title, limit)
    appr = next((r for r in d.relations if r.type == "approved_by" and r.target_number), None)
    if appr:
        when = f" от {appr.target_date.strftime('%d.%m.%Y')}" if appr.target_date else ""
        return f"{title} (утв. приказом № {appr.target_number}{when})"
    return title


class _DocView:
    """Быстрый доступ к структуре документа: пункт по id, порядок, раздел и «пункт уровня шага»."""

    def __init__(self, doc: Document):
        self.doc = doc
        self.by_id = {c.id: c for c in doc.clauses}
        self.order = {c.id: i for i, c in enumerate(doc.clauses)}
        self.children: dict[str, list[Clause]] = {}
        for c in doc.clauses:
            if c.parent_id:
                self.children.setdefault(c.parent_id, []).append(c)
        # раздел: заголовок либо нумерованный пункт верхнего уровня, у которого есть нумерованные подпункты
        # («2. Порядок бронирования Концертного зала.» с пунктами 2.1, 2.2 ... разобран как пункт, но по смыслу это раздел)
        self.sections = {c.id for c in doc.clauses if c.kind == "heading" or (
            c.number and "." not in c.number and sum(1 for x in self.children.get(c.id, []) if x.number) >= 2)}

    def heading_of(self, c: Clause) -> Optional[Clause]:
        """Раздел, в котором стоит пункт."""
        cur, hops = c, 0
        while cur.parent_id and hops < 12:
            par = self.by_id.get(cur.parent_id)
            if par is None:
                return None
            if par.id in self.sections:
                return par
            cur, hops = par, hops + 1
        return None

    def step_level(self, c: Clause) -> Clause:
        """Предок пункта, который стоит непосредственно в разделе: «3.2.1» и абзацы пункта относятся к шагу «3.2»."""
        cur, hops = c, 0
        while cur.parent_id and hops < 12:
            par = self.by_id.get(cur.parent_id)
            if par is None or par.id in self.sections:
                return cur
            cur, hops = par, hops + 1
        return cur

    def intro_parent(self, c: Clause) -> Optional[Clause]:
        """Пункт-родитель, вводящий перечень («Размер суточных ...:»): без него элемент перечня непонятен."""
        par = self.by_id.get(c.parent_id) if c.parent_id else None
        if par is not None and par.id not in self.sections and not par.deleted and par.text.rstrip().endswith(":"):
            return par
        return None

    def section_title(self, section: Optional[Clause]) -> Optional[str]:
        if section is None:
            return None
        title = " ".join(section.text.split()).rstrip(" .:")
        if title.lower()[:40] == " ".join(self.doc.title.split()).lower()[:40] or re.match(r"(?i)^(утвержд|приложение\b)", title) or BLANK_RE.search(title):
            return None  # это шапка документа, а не раздел
        return _clip(title, 140)


def _usable(c: Clause) -> bool:
    return (not c.deleted) and c.kind in STEP_KINDS and len(c.text) >= 25 and not BLANK_RE.search(c.text)


def _fact_step(view: _DocView, c: Clause, limit: int = 420) -> Step:
    """Пункт-основание: дословное начало пункта; элемент перечня показываем вместе с вводной фразой родителя."""
    did = view.doc.id
    par = view.intro_parent(c)
    if par is not None:
        text = f"{_clip(par.text, 220)} {_lead(c.text, limit - 120)}"
        return Step(text=text, citations=[Citation(doc_id=did, clause_id=par.id, quote=_quote(par.text)), Citation(doc_id=did, clause_id=c.id, quote=_quote(c.text))])
    return _step_from_clause(view, c, limit)


def _step_from_clause(view: _DocView, c: Clause, limit: int = 340) -> Step:
    """Шаг = начало пункта; если пункт вводит перечень («...:»), добавляем его первые элементы и цитируем каждый."""
    text = _lead(c.text, limit)
    cites = [Citation(doc_id=view.doc.id, clause_id=c.id, quote=_quote(c.text))]
    items = [x for x in view.children.get(c.id, []) if x.kind in ("item", "clause") and not x.deleted and len(x.text) >= 8 and not BLANK_RE.search(x.text)]
    if items and c.text.rstrip().endswith(":") and len(text) < limit:
        shown = []
        for it in items[:4]:
            piece = _clip(it.text, 150).rstrip(".;")
            if len(text) + sum(len(x) + 2 for x in shown) + len(piece) > limit + 260:
                break
            shown.append(piece)
            cites.append(Citation(doc_id=view.doc.id, clause_id=it.id, quote=_quote(it.text)))
        if shown:
            more = len(items) - len(shown)
            text = f"{text} {'; '.join(shown)}" + (f"; и ещё {more} (см. пункт)" if more > 0 else "") + "."
    return Step(text=text, citations=cites)


def _pick_procedure(view: _DocView, question: str, hit_scores: dict[str, float], reranker, q_tokens: set[str],
                    anchor_id: Optional[str] = None) -> tuple[list[tuple[Clause, float]], Optional[Clause], int]:
    """Порядок действий: раздел документа и его пункты в порядке документа.
    Раздел выбирается по трём признакам: сколько найденных пунктов в нём лежит, насколько название раздела отвечает вопросу
    («Порядок бронирования Концертного зала») и где стоит лучший найденный пункт.
    Короткий раздел (до FULL_SECTION пунктов) показывается целиком: порядок получается полным. В длинном берём пункты от первого
    до последнего относящегося к вопросу; если и их больше MAX_STEPS, оставляем самые существенные (оценка реранкера, сроки).
    Возвращает [(пункт уровня шага, оценка)], раздел и число пунктов раздела."""
    lifted: dict[str, float] = {}
    sec_of: dict[str, Optional[str]] = {}
    own: dict[Optional[str], float] = {}  # оценка самого заголовка-пункта раздела, если он найден
    for cid, sc in hit_scores.items():
        c = view.by_id.get(cid)
        if c is None:
            continue
        if c.id in view.sections:
            own[c.id] = max(own.get(c.id, 0.0), sc)
            continue
        step = view.step_level(c)
        h = view.heading_of(step)
        sec_of[step.id] = h.id if h else None
        lifted[step.id] = max(lifted.get(step.id, 0.0), sc)
    mass: dict[Optional[str], float] = {}
    for sid in set(sec_of.values()) | set(own):
        mine = [sc for step_id, sc in lifted.items() if sec_of.get(step_id) == sid]
        # лучший найденный пункт раздела и небольшая надбавка за число найденных шагов
        mass[sid] = max([*mine, own.get(sid, 0.0)]) + 0.25 * min(len(mine), 4)
    if not mass:
        return [], None, 0
    titled = [sid for sid in mass if sid is not None and view.section_title(view.by_id[sid])]
    tscores = _rerank(reranker, question, [view.by_id[sid].text[:300] for sid in titled]) if titled else None
    title_score = dict(zip(titled, tscores)) if tscores is not None else {
        sid: min(1.0, 2.0 * _score_sentence(view.by_id[sid].text, q_tokens)) for sid in titled}
    anchor = view.by_id.get(anchor_id) if anchor_id else None
    anchor_sec = None
    if anchor is not None:
        h = view.heading_of(view.step_level(anchor)) if anchor.id not in view.sections else anchor
        anchor_sec = h.id if h else None

    def total_score(sid: Optional[str]) -> float:
        return mass[sid] + 2.0 * title_score.get(sid, 0.0) + (0.5 if anchor is not None and sid == anchor_sec else 0.0)

    sec_id = max(mass, key=total_score)
    section = view.by_id.get(sec_id) if sec_id else None
    sec_num = section.number if section is not None and section.number and "." not in section.number else None
    cands = [c for c in view.doc.clauses if _usable(c) and c.id not in view.sections and view.step_level(c) is c
             and ((view.heading_of(c).id if view.heading_of(c) else None) == sec_id)
             and not (sec_num and c.number and c.number.split(".")[0] != sec_num)]  # «3.1» под разделом «2»: заголовок раздела 3 не распознан
    total = len(cands)
    if not cands:
        return [], section, 0
    if section is not None and view.section_title(section) and total <= FULL_SECTION:
        return [(c, lifted.get(c.id, 0.0)) for c in cands], section, total  # короткий раздел целиком
    if len(cands) > 30:  # очень длинный раздел или документ без заголовков: окно вокруг найденных пунктов
        idx = [i for i, c in enumerate(cands) if c.id in lifted]
        lo, hi = (max(0, min(idx) - 3), min(len(cands), max(idx) + 9)) if idx else (0, 30)
        cands = cands[lo:hi][:30]
    need = [c for c in cands if c.id not in lifted]
    lite = bool(getattr(reranker, "lite", False))
    if lite and len(need) > 10:  # CPU: досчитываем только пункты рядом с найденными, остальные оцениваем по словам
        near = {i + k for i, c in enumerate(cands) if c.id in lifted for k in range(-3, 4)}
        need_ids = {c.id for i, c in enumerate(cands) if i in near and c.id not in lifted}
        scored = [c for c in need if c.id in need_ids][:10]
    else:
        scored = need
    head = f"{view.doc.title}\n{section.text if section else ''}".strip()
    extra = _rerank(reranker, question, [f"{head}\n{c.text}"[:1100 if lite else 1500] for c in scored])
    scores: dict[str, float] = dict(lifted)
    by_scored = {c.id: i for i, c in enumerate(scored)}
    for c in need:
        if extra is not None and c.id in by_scored:
            scores[c.id] = extra[by_scored[c.id]]
        else:
            scores[c.id] = 0.6 * _score_sentence(c.text, q_tokens) if extra is None else 0.3 * _score_sentence(c.text, q_tokens)
    best = max(scores.get(c.id, 0.0) for c in cands)
    floor = max(0.06, 0.18 * best)
    rel = [i for i, c in enumerate(cands) if scores.get(c.id, 0.0) >= floor and not (DEFINITION_RE.match(c.text) and scores.get(c.id, 0.0) < best)]
    if not rel:
        return [], section, total
    window = cands[rel[0]: rel[-1] + 1]
    if len(window) <= MAX_STEPS:
        chosen = window
    else:
        def priority(c: Clause) -> float:
            return scores.get(c.id, 0.0) + (0.15 if DEADLINE_RE.search(c.text) else 0.0)
        chosen = sorted((cands[i] for i in rel), key=lambda c: -priority(c))[:MAX_STEPS]
        chosen.sort(key=lambda c: view.order[c.id])
    return [(c, scores.get(c.id, 0.0)) for c in chosen], section, total


def _part_candidates(parts: list[str], retrieval: RetrievalResult, views: dict, clause_of, lite: bool = False) -> list[tuple[str, str]]:
    """Пункты, среди которых ищется опора для каждой части вопроса: лучшие по общей оценке и те, что по словам близки
    именно этой части (с учётом глоссария): часть «куда писать» может опираться на пункт, слабый для вопроса в целом."""
    from .query import expansions_for

    pool = [h for h in retrieval.hits[:40] if h.unit.doc_id in views and clause_of(h.unit.doc_id, h.unit.clause_id) is not None]
    chosen = [(h.unit.doc_id, h.unit.clause_id) for h in pool[:(6 if lite else 10)]]
    for part in parts:
        pt = set(tokenize(" ".join([part, *expansions_for(part)[:4]])))
        ranked = sorted(pool, key=lambda h: -len(pt & set(tokenize(h.unit.text))))
        for h in ranked[:4]:
            key = (h.unit.doc_id, h.unit.clause_id)
            if key not in chosen and pt & set(tokenize(h.unit.text)):
                chosen.append(key)
    return chosen[:(12 if lite else 18)]


ANAPHORA_RE = re.compile(r"(?<![а-яё])(его|её|ее|их|него|неё|нее|ним|ней|этого|этом|этой|эту|такой|такого|там|тогда)(?![а-яё])", re.I)


def _score_parts(parts: list[str], cand: list[tuple[str, str]], texts: list[str], reranker, views: dict,
                 out_runners: list) -> list[tuple[str, Optional[tuple[str, str]], float]]:
    """Для каждой части составного вопроса: опорный пункт и признак «покрыта документами».
    Часть оценивается в нескольких формулировках одной пачкой: сама по себе, с расширением по глоссарию и, если в ней местоимение
    («если не вернуть его вовремя»), вместе с первой частью вопроса. Покрыта, если лучшая оценка проходит порог; опорный пункт берётся
    по собственной формулировке части, чтобы контекст соседней части не перетягивал выбор."""
    from .query import expansions_for

    out: list[tuple[str, Optional[tuple[str, str]], float]] = []
    runners: list[tuple[tuple[str, str], float]] = []
    if not cand:
        return [(p, None, 0.0) for p in parts]
    if reranker is None:
        for part in parts:
            pt = set(tokenize(" ".join([part, *expansions_for(part)])))
            sc = [_score_sentence(views[d].by_id[c].text, pt) for d, c in cand]
            i = max(range(len(sc)), key=lambda k: sc[k])
            out.append((part, cand[i] if sc[i] >= 0.34 else None, sc[i]))
        return out
    variants: list[tuple[int, str, str]] = []  # (номер части, вид формулировки, текст запроса)
    for pi, part in enumerate(parts):
        variants.append((pi, "own", part))
        exp = expansions_for(part)
        if exp:
            variants.append((pi, "glossary", f"{part} ({'; '.join(exp[:3])})"))
        if pi > 0 and ANAPHORA_RE.search(part):
            variants.append((pi, "context", f"{part} ({parts[0]})"))
    try:
        flat = reranker.score_pairs([(q, t) for _, _, q in variants for t in texts])
    except Exception:  # noqa: BLE001
        return [(p, cand[0], 0.0) for p in parts]
    n = len(texts)
    rows = [[float(x) for x in flat[k * n:(k + 1) * n]] for k in range(len(variants))]
    for pi, part in enumerate(parts):
        mine = {kind: rows[k] for k, (vi, kind, _) in enumerate(variants) if vi == pi}
        own = mine["own"]
        best_any = max(max(r) for r in mine.values())
        if best_any < PART_MIN_RERANK:
            out.append((part, None, best_any))
            continue
        # опорный пункт: по собственной формулировке части; формулировка с глоссарием побеждает, если она заметно увереннее;
        # формулировка с контекстом первой части нужна только для признака «покрыта» (иначе контекст перетягивает выбор)
        gloss = mine.get("glossary")
        pick = gloss if gloss is not None and max(gloss) > max(own) + 0.1 else own
        if max(pick) < 0.02:
            pick = max(mine.values(), key=max)
        i = max(range(n), key=lambda k: pick[k])
        out.append((part, cand[i], best_any))
        second = sorted((k for k in range(n) if k != i), key=lambda k: -pick[k])[:1]
        if second and pick[second[0]] >= max(PART_MIN_RERANK, 0.5 * pick[i]):
            runners.append((cand[second[0]], float(pick[second[0]])))
    out_runners.extend(runners)
    return out


def extractive_generate(question: str, qu: QueryUnderstanding, retrieval: RetrievalResult, kb: KnowledgeBase, reranker=None,
                        contradictions: Optional[dict] = None) -> AnswerDraft:
    """Ответ без модели: только дословные положения документов.
    Вопрос «как сделать» -> шаги из раздела с процедурой в порядке документа; вопрос о факте -> пункты-основания.
    Составной вопрос разбирается по частям: для каждой части свой пункт, непокрытые части перечисляются отдельно.
    Если у выбранного пункта есть расходящийся пункт в другом действующем документе, он показывается рядом."""
    if not retrieval.units:
        return AnswerDraft(answerable="no", summary="В переданной базе не найдено фрагментов, относящихся к вопросу.")
    from .. import config

    if retrieval.top_rerank is not None:
        weak = retrieval.top_rerank < config.GATE_MIN_RERANK  # реранкер — лучший сигнал без модели
    else:
        weak = retrieval.lexical_overlap < 0.5 and retrieval.top_dense < (0.3 if "hash" in (retrieval.method or "") else 0.55)
    if weak:
        return _maybe_clarify(question, qu, retrieval, kb) or AnswerDraft(
            answerable="no", summary="Найденные пункты слабо связаны с вопросом: подтверждающего регламента в базе нет.", applicable_documents=[], steps=[])
    ranked_docs = sorted(retrieval.doc_scores.items(), key=lambda kv: -kv[1]) if retrieval.doc_scores else [(retrieval.units[0].doc_id, 1.0)]
    best_doc, best_score = ranked_docs[0]
    # несколько релевантных документов: второй и третий берём, если их вес близок к лучшему и их лучший пункт сам проходит порог уверенности
    gate = config.GATE_MIN_RERANK if retrieval.top_rerank is not None else 0.0
    extra_docs: list[str] = []
    for did, sc in ranked_docs[1:4]:
        top_unit = max((u.score for u in retrieval.units if u.doc_id == did and u.reason == "hit"), default=0.0)
        doc = kb.get(did)
        if doc is not None and doc.status == "active" and sc >= 0.5 * best_score and top_unit >= max(gate, 0.3):
            extra_docs.append(did)
        if len(extra_docs) >= 2:
            break
    docs_used = [best_doc, *extra_docs]
    views: dict[str, _DocView] = {did: _DocView(kb.get(did)) for did in docs_used if kb.get(did) is not None}
    if best_doc not in views:
        return AnswerDraft(answerable="no", summary="Найденный документ недоступен в базе.")
    q_tokens = set(tokenize(" ".join([question, *qu.reformulations])))
    # найденные пункты (после реранкера) по документам: clause_id -> оценка
    top_hits = [h for h in retrieval.hits[:12] if h.unit.doc_id in views]
    hit_scores: dict[str, dict[str, float]] = {did: {} for did in views}
    for h in top_hits:
        sc = h.rerank if h.rerank is not None else h.score
        if h.unit.clause_id in views[h.unit.doc_id].by_id and (retrieval.top_rerank is None or sc >= 0.5 * gate):
            hit_scores[h.unit.doc_id][h.unit.clause_id] = float(sc)

    def clause_of(did: str, cid: str) -> Optional[Clause]:
        v = views.get(did)
        c = v.by_id.get(cid) if v else None
        return c if c is not None and _usable(c) else None

    # ---------------------------------------------------------------- части вопроса и «опорный» пункт каждой
    named = next((d for d in retrieval.mentioned_docs if d.status == "active" and d.doc_type in ("приказ", "распоряжение") and d.id in views), None)
    if named is not None and ORDER_CONTENT_RE.search(question) and len(qu.sub_questions) < 2:
        draft = _order_content(kb, views[named.id], retrieval)
        if draft is not None:
            return draft
    parts = list(qu.sub_questions) if len(qu.sub_questions) >= 2 else [question]
    cand = [(h.unit.doc_id, h.unit.clause_id) for h in top_hits if clause_of(h.unit.doc_id, h.unit.clause_id) is not None]
    anchors: list[tuple[str, Optional[tuple[str, str]], float]] = []  # (часть, (doc_id, clause_id) | None, оценка)
    runners_up: list[tuple[tuple[str, str], float]] = []  # вторые по силе пункты частей вопроса
    not_covered: list[str] = []
    if len(parts) == 1:
        if cand:
            first = next(h for h in top_hits if (h.unit.doc_id, h.unit.clause_id) == cand[0])
            anchors.append((question, cand[0], float(first.rerank if first.rerank is not None else first.score)))
    else:
        cand = _part_candidates(parts, retrieval, views, clause_of, lite=bool(getattr(reranker, "lite", False)))
        texts = []
        for d, c in cand:
            cl = views[d].by_id[c]
            par = views[d].intro_parent(cl)
            texts.append(f"{views[d].doc.title}\n{(par.text + ' ') if par else ''}{cl.text}"[:1500])
        for part, key, sc in _score_parts(parts, cand, texts, reranker, views, runners_up):
            anchors.append((part, key, sc))
            if key is None:
                not_covered.append(part.strip().rstrip("?").strip() + "?")
    # ---------------------------------------------------------------- шаги
    chosen: dict[tuple[str, str], float] = {}  # (doc_id, clause_id) -> оценка
    kind = "facts"
    section_title: Optional[str] = None
    section_total = 0
    proc_part = next((part for part, key, _ in anchors if key is not None and is_procedural(part)), None)
    if proc_part is not None and hit_scores[best_doc]:
        view = views[best_doc]
        anchor_id = next((key[1] for part, key, _ in anchors if key is not None and key[0] == best_doc and part == proc_part), None)
        picked, section, section_total = _pick_procedure(view, question, hit_scores[best_doc], reranker, q_tokens, anchor_id)
        if len(picked) >= 2:
            kind = "procedure"
            section_title = view.section_title(section)
            for c, sc in picked:
                chosen[(best_doc, c.id)] = sc
            # сильные найденные пункты того же документа вне раздела (например, основание из «Общих положений») тоже показываем
            top_sc = max(hit_scores[best_doc].values())
            outside = 0
            first_step = min(view.order[c.id] for c, _ in picked)
            for cid, sc in sorted(hit_scores[best_doc].items(), key=lambda kv: -kv[1]):
                step = view.step_level(view.by_id[cid])
                if (best_doc, step.id) in chosen or outside >= 1 or not _usable(step) or DEFINITION_RE.match(step.text) or step.id in view.sections \
                        or TERMS_RE.search(step.text[:200]):
                    continue
                if view.order[step.id] > first_step:
                    continue  # после процедуры идут частные случаи других разделов: в порядок действий их не подмешиваем
                if sc >= max(0.8 * top_sc, gate):
                    chosen[(best_doc, step.id)] = sc
                    outside += 1
    for _, key, sc in anchors:  # опорные пункты частей вопроса присутствуют всегда
        if key is not None:
            v = views[key[0]]
            step = v.step_level(v.by_id[key[1]])
            target = step if (kind == "procedure" and key[0] == best_doc and _usable(step) and step.id not in v.sections) else v.by_id[key[1]]
            if target.id in v.sections and kind == "procedure":
                continue  # найден сам заголовок раздела: его пункты уже стоят шагами
            chosen[(key[0], target.id)] = max(chosen.get((key[0], target.id), 0.0), sc)
    if kind == "facts":
        for key, sc in runners_up:
            chosen.setdefault(key, sc)
        # вопрос о факте: к опорным пунктам добавляем близкие по оценке найденные пункты, по документам с квотой
        quota = {best_doc: (4 if not extra_docs else 3), **{x: 2 for x in extra_docs}}
        taken = {did: sum(1 for d, _ in chosen if d == did) for did in views}
        top_sc = max((sc for d in hit_scores.values() for sc in d.values()), default=0.0)
        for h in top_hits:
            did, cid = h.unit.doc_id, h.unit.clause_id
            sc = hit_scores.get(did, {}).get(cid)
            if sc is None or clause_of(did, cid) is None or (did, cid) in chosen:
                continue
            doc_top = max(hit_scores[did].values())
            need = max(gate, 0.45 * (top_sc if did == best_doc else doc_top))
            if taken.get(did, 0) < quota.get(did, 0) and sc >= need:
                chosen[(did, cid)] = sc
                taken[did] = taken.get(did, 0) + 1
        for did in extra_docs:  # у каждого дополнительного документа показываем хотя бы его лучший пункт
            if not any(d == did for d, _ in chosen) and hit_scores.get(did):
                cid = max(hit_scores[did], key=lambda k: hit_scores[did][k])
                if clause_of(did, cid) is not None:
                    chosen[(did, cid)] = hit_scores[did][cid]
    # расходящиеся пункты других действующих документов показываем рядом с выбранными
    counterparts: list[tuple[tuple[str, str], tuple[str, str]]] = []
    for key in list(chosen):
        for other in (contradictions or {}).get(key, []):
            od = kb.get(other.get("doc_id", ""))
            if od is None or od.status != "active":
                continue
            if od.id not in views:
                views[od.id] = _DocView(od)
                docs_used.append(od.id)
            oc = views[od.id].by_id.get(other["clause_id"])
            if oc is not None and _usable(oc):
                chosen.setdefault((od.id, oc.id), chosen[key])
                counterparts.append((key, (od.id, oc.id)))
    if not chosen:
        return AnswerDraft(answerable="no", summary="Найденные пункты слабо связаны с вопросом: подтверждающего регламента в базе нет.")
    doc_rank = {did: i for i, did in enumerate(docs_used)}
    ordered = sorted(chosen, key=lambda k: (doc_rank.get(k[0], 9), views[k[0]].order.get(k[1], 0)))
    # пункт, уже показанный внутри шага-родителя (перечень), отдельным шагом не повторяем
    steps: list[Step] = []
    shown: set[tuple[str, str]] = set()
    responsible = None
    forms: list[str] = []
    have = {u.key for u in retrieval.units}
    for did, cid in ordered:
        if (did, cid) in shown:
            continue
        v = views[did]
        c = v.by_id[cid]
        step = _step_from_clause(v, c) if kind == "procedure" and did == best_doc else _fact_step(v, c)
        steps.append(step)
        for cit in step.citations:  # процитированный пункт должен быть в контексте: по нему проверяется цитата и считаются сроки
            shown.add((cit.doc_id, cit.clause_id))
            if (cit.doc_id, cit.clause_id) not in have:
                retrieval.units.append(context_unit(v.doc, v.by_id[cit.clause_id], "section"))
                have.add((cit.doc_id, cit.clause_id))
        if responsible is None:
            m = RESP_RE.search(c.text)
            if m:
                responsible = m.group(1).strip()
        for m in FORM_RE.finditer(c.text):
            if m.group(1) not in forms:
                forms.append(m.group(1))
    used_docs = [did for did in docs_used if any(d == did for d, _ in ordered)]
    summary = _summary(kb, views, anchors, kind, section_title, section_total, used_docs, hit_scores, counterparts, question, q_tokens, reranker, steps)
    retrieval.notes["answer_plan"] = {
        "kind": kind, "section": section_title, "section_clauses": section_total,
        "parts": [{"part": part, "clause": list(key) if key else None, "score": round(sc, 3)} for part, key, sc in anchors],
        "counterparts": [[list(a), list(b)] for a, b in counterparts],
    }
    return AnswerDraft(answerable="partial" if not_covered else "yes", summary=summary, applicable_documents=used_docs, steps=steps,
                       not_covered=not_covered, responsible=responsible, forms=forms[:4], answer_kind=kind, procedure_source=section_title)


def _order_content(kb: KnowledgeBase, view: _DocView, retrieval: RetrievalResult) -> Optional[AnswerDraft]:
    """«Что говорит приказ № ...»: пункты приказа по порядку (то, что приказ утверждает, изменяет и отменяет)."""
    d = view.doc
    items = [c for c in d.clauses if _usable(c) and c.kind in ("clause", "order_item") and c.number and "." not in c.number and not c.attachment]
    if len(items) < 1:
        return None
    steps: list[Step] = []
    have = {u.key for u in retrieval.units}
    for c in items[:10]:
        step = _step_from_clause(view, c, 420)
        steps.append(step)
        for cit in step.citations:
            if (cit.doc_id, cit.clause_id) not in have:
                retrieval.units.append(context_unit(d, view.by_id[cit.clause_id], "mentioned"))
                have.add((cit.doc_id, cit.clause_id))
    title = _clip(" ".join(d.title.split()), 200)
    n = len(steps)
    more = f" (первые {n} из {len(items)})" if len(items) > n else ""
    summary = f"{short_title(d)} «{title}»: {n} {_plural(n, 'пункт', 'пункта', 'пунктов')} приказа приведены ниже по порядку{more}."
    retrieval.notes["answer_plan"] = {"kind": "order_content", "doc": d.id, "items": [c.id for c in items[:10]]}
    return AnswerDraft(answerable="yes", summary=summary, applicable_documents=[d.id], steps=steps, answer_kind="facts")


def _summary(kb: KnowledgeBase, views: dict[str, _DocView], anchors: list, kind: str, section_title: Optional[str], section_total: int, used_docs: list[str],
             hit_scores: dict[str, dict[str, float]], counterparts: list, question: str, q_tokens: set[str], reranker, steps: list[Step]) -> str:
    """Краткий ответ: дословное ключевое положение по каждой части вопроса (и по каждому документу) со ссылкой на пункт."""
    multi_doc = len(used_docs) > 1
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()

    def add(did: str, cid: str, query: str, tokens: set[str]) -> None:
        if (did, cid) in seen:
            return
        seen.add((did, cid))
        v = views[did]
        c = v.by_id[cid]
        par = v.intro_parent(c)
        sent = _key_sentence(c.text, query, tokens, reranker, 280).rstrip(" .;:")
        if par is not None:
            sent = f"{_clip(par.text, 160).rstrip(' :')}: {sent[:1].lower() + sent[1:]}"
        ref = c.label + (f", {short_title(v.doc, 70)}" if multi_doc else "")
        lines.append(f"{sent[:1].upper() + sent[1:]} ({ref}).")

    for part, key, _ in anchors:
        if key is not None:
            add(key[0], key[1], part, set(tokenize(part)) | (q_tokens if len(anchors) == 1 else set()))
    for a, b in counterparts:  # расхождение показываем парой: оба положения рядом
        for did, cid in (a, b):
            if did in views and cid in views[did].by_id:
                add(did, cid, question, q_tokens)
    for did in used_docs:  # в многодокументном ответе у каждого документа своё ключевое положение
        if multi_doc and not any(d == did for d, _ in seen) and hit_scores.get(did):
            cid = max(hit_scores[did], key=lambda k: hit_scores[did][k])
            if cid in views[did].by_id:
                add(did, cid, question, q_tokens)
    names = "; ".join(f"«{short_title(views[d].doc)}»" for d in used_docs)
    if kind == "procedure":
        doc0 = views[used_docs[0]].doc
        n = sum(1 for s in steps if s.citations and s.citations[0].doc_id == doc0.id)
        where = f"по разделу «{section_title}» документа «{short_title(doc0)}»" if section_title else f"по документу «{short_title(doc0)}»"
        scope = f"{n} {_plural(n, 'шаг', 'шага', 'шагов')} в порядке документа" + (
            f" (в разделе {section_total} {_plural(section_total, 'пункт', 'пункта', 'пунктов')}, показаны относящиеся к вопросу)" if section_total > n else "")
        tail = f"Порядок действий {where}: {scope}."
        if multi_doc:
            tail += " Дополнительно: " + "; ".join(f"«{short_title(views[d].doc)}»" for d in used_docs[1:]) + "."
    else:
        tail = f"Источник: {names}." if not multi_doc else f"Источники: {names}."
    return " ".join([*lines, tail])


def _plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


STOP_TOKENS = {"как", "какой", "какая", "какие", "какое", "каков", "что", "кто", "где", "когда", "куда", "сколько", "можно", "нужно", "надо", "ли", "для", "при",
               "или", "это", "мне", "если", "чтобы", "есть", "быть", "мой", "моя", "нам", "наш", "по", "на", "за", "от", "до", "из", "об", "про"}


def _maybe_clarify(question: str, qu: QueryUnderstanding, retrieval: RetrievalResult, kb: KnowledgeBase) -> Optional[AnswerDraft]:
    """Слишком общий вопрос («Какой срок подачи заявки?»): его слова есть в нескольких документах, а уверенного пункта нет.
    Вместо отказа просим уточнить, о каком документе речь."""
    if qu.mentioned_documents or len(question) > 90:
        return None
    stop = {t for w in STOP_TOKENS for t in tokenize(w)} | STOP_TOKENS
    content = [t for t in dict.fromkeys(tokenize(question)) if t not in stop and len(t) >= 3]
    if not 1 <= len(content) <= 3:
        return None
    by_doc: dict[str, set[str]] = {}
    best: dict[str, float] = {}
    for h in retrieval.hits[:40]:
        toks = set(tokenize(h.unit.text))
        by_doc.setdefault(h.unit.doc_id, set()).update(t for t in content if t in toks)
        sc = h.rerank if h.rerank is not None else h.score
        best[h.unit.doc_id] = max(best.get(h.unit.doc_id, 0.0), float(sc))
    present = [t for t in content if any(t in found for found in by_doc.values())]
    if len(present) < 2 or len(content) - len(present) > 1:
        return None  # слова вопроса в документах почти не встречаются: это отказ, а не уточнение
    need = max(2, len(present) - 1)
    full = [did for did, found in by_doc.items() if len(found) >= need and kb.get(did) is not None and kb.get(did).status == "active"
            and kb.get(did).doc_type != "форма"]
    if len(full) < 2:
        return None
    full.sort(key=lambda d: -best.get(d, 0.0))
    options = [short_title(kb.get(d), 90) for d in full[:4]]
    return AnswerDraft(answerable="no", clarify_kind="document", clarifying_question="Вопрос слишком общий: подходящие положения есть в нескольких документах. О каком из них идёт речь?",
                       clarifying_options=options, applicable_documents=full[:4],
                       summary="В базе несколько документов, к которым может относиться вопрос; без уточнения ответ был бы догадкой.")


# обратная совместимость
mock_generate = extractive_generate
