"""Понимание вопроса: роль, упомянутые документы, дата «по состоянию на», переформулировки на языке регламентов."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml

from .. import config
from ..ingest.metadata import ANCHOR_A, ANCHOR_B, DATE_ANY_RE, norm_number, parse_date_match
from ..llm.base import LLMProvider, spec
from ..schemas import KnowledgeBase, QueryUnderstanding

ROLE_HINTS: list[tuple[str, re.Pattern]] = [
    ("postgraduate", re.compile(r"аспирант", re.I)),
    ("applicant", re.compile(r"абитуриент|поступаю|хочу поступить", re.I)),
    ("student", re.compile(r"\bстудент|учусь|бакалавр|магистрант|магистратур|обучающ|первокурсник|курсе\b", re.I)),
    ("teacher", re.compile(r"преподава|доцент|профессор|ассистент|\bппс\b|педагог", re.I)),
    ("researcher", re.compile(r"научн\w+\s+сотрудник|\bмнс\b|\bснс\b|\bнс\b|научный работник|лаборатори", re.I)),
    ("admin", re.compile(r"\bауп\b|административн|начальник\s+(отдела|управления)|руководител\w+\s+(отдела|управления|подразделения)", re.I)),
    ("staff", re.compile(r"сотрудник|работник|работаю|я\s+в\s+штате", re.I)),
]
AS_OF_RE = re.compile(r"(?:по\s+состоянию\s+на|на\s+дату|действовал\w*\s+(?:в|на))\s+(?P<when>[^?.,;]{4,40})", re.I)
YEAR_RE = re.compile(r"(?:\bв|по\s+состоянию\s+на|действовал\w*\s+на)\s+(?P<y>(?:19|20)\d{2})\s*(?:г\.?|год[уа]?)(?![а-яё])", re.I)
# «приказ 1465-1», «распоряжение от 28.12.2024 700-1»: номер вида 1465-1 рядом со словом «приказ» и без знака №
BARE_ORDER_NUM_RE = re.compile(r"(?:приказ\w*|распоряжени\w*)[^?!№\n]{0,45}?(?<![\d.\-])(\d{1,5}-\d{1,2})(?![\d\-])", re.I)
OUT_OF_SCOPE = re.compile(r"погод|курс\s+доллар|рецепт|анекдот|стих|напиши\s+код|переведи\s+на", re.I)

_glossary: Optional[dict[str, list[str]]] = None


def glossary() -> dict[str, list[str]]:
    global _glossary
    if _glossary is None:
        p = Path(config.GLOSSARY_PATH)
        _glossary = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    return _glossary


def expansions_for(text: str) -> list[str]:
    """Формулировки регламентов для бытовых слов вопроса («почта» -> «электронная почта, техническая поддержка»)."""
    low = text.lower().replace("ё", "е")
    out: list[str] = []
    matched: list[str] = []
    for key in sorted(glossary(), key=len, reverse=True):  # длинный ключ важнее: «электронный бюджет» не должен расширяться как «бюджет»
        if key in low and not any(key in m for m in matched):
            matched.append(key)
            for t in glossary()[key]:
                if t.lower() not in low and t not in out:
                    out.append(t)
    return out


def detect_role(text: str) -> tuple[Optional[str], float]:
    for role, rx in ROLE_HINTS:
        if rx.search(text):
            return role, 0.6
    return None, 0.0


def heuristics(question: str, role_hint: Optional[str] = None) -> QueryUnderstanding:
    q = question.strip()
    qu = QueryUnderstanding(intent=q)
    role, conf = detect_role(q)
    if role_hint:
        qu.role, qu.role_confidence = role_hint, 1.0  # type: ignore[assignment]
    elif role:
        qu.role, qu.role_confidence = role, conf  # type: ignore[assignment]
    for rx in (ANCHOR_A, ANCHOR_B):
        for m in rx.finditer(q):
            num = norm_number(m.group("num"))
            if num:
                qu.mentioned_documents.append(num)
    for m in re.finditer(r"№\s*([0-9][0-9A-Za-zА-Яа-я\-/]*)", q):
        n = norm_number(m.group(1))
        if n and n not in qu.mentioned_documents:
            qu.mentioned_documents.append(n)
    for m in BARE_ORDER_NUM_RE.finditer(q):
        n = norm_number(m.group(1))
        if n and n not in qu.mentioned_documents:
            qu.mentioned_documents.append(n)
    m = AS_OF_RE.search(q)
    if m:
        dm = DATE_ANY_RE.search(m.group("when"))
        if dm:
            qu.as_of = parse_date_match(dm)
    ym = YEAR_RE.search(q)
    if ym and qu.as_of is None:
        import datetime as _dt

        today = _dt.date.fromisoformat(config.TODAY_OVERRIDE) if config.TODAY_OVERRIDE else _dt.date.today()
        if int(ym.group("y")) != today.year:  # текущий год — это «сейчас», а не дата в прошлом
            qu.as_of = _dt.date(int(ym.group("y")), 12, 31)
    for dm in DATE_ANY_RE.finditer(q):
        d = parse_date_match(dm)
        if d and (qu.as_of is None or d != qu.as_of):
            qu.situation_dates.append(d.isoformat())
    low = q.lower().replace("ё", "е")
    expansions = expansions_for(q)
    # составной вопрос: части тоже идут в поиск («сколько длится заём и какой штраф за просрочку?»)
    parts = [x.strip(" ,;") for x in re.split(r"[?;]|\s+(?:и|а\s+также|а)\s+(?=(?:как|какой|какая|какие|каков\w*|кто|куда|где|когда|сколько|что|можно|нужно|надо|в\s+какой|в\s+какие)\b)", q) if x and x.strip()]
    sub = [x for x in parts if len(x) >= 12 and x != q.strip(" ?")]
    qu.sub_questions = sub[:3] if len(sub) >= 2 else []
    qu.reformulations = list(dict.fromkeys([*sub[:3], *expansions[:6]]))
    qu.keywords = [w for w in re.findall(r"[а-яёa-z0-9-]{3,}", low) if w not in ("как", "что", "где", "мне", "нужно", "можно", "если")][:12]
    if OUT_OF_SCOPE.search(q):
        qu.out_of_scope = True
        qu.out_of_scope_reason = "вопрос не относится к внутренним регламентам"
    return qu


SYSTEM_QUERY = """Ты помогаешь поисковой системе по внутренним регламентам и приказам университета (МФТИ).
По вопросу сотрудника или студента определи:
- intent: краткая формулировка, что именно человек хочет сделать или узнать;
- role: роль спрашивающего, если она ясна из текста (student, postgraduate, applicant, teacher, researcher, admin, staff), иначе null;
- reformulations: 2–4 переформулировки вопроса языком локальных нормативных актов (например, «уйти в академ» -> «порядок предоставления академического отпуска обучающимся»);
- keywords: ключевые термины для лексического поиска;
- mentioned_documents: номера приказов или названия документов, которые человек назвал;
- out_of_scope: true, если вопрос вообще не о правилах и процедурах организации.
Не отвечай на вопрос, только разбери его. Названия документов в базе (для ориентира): {titles}"""


def understand(question: str, role_hint: Optional[str], provider: LLMProvider, kb: Optional[KnowledgeBase] = None) -> QueryUnderstanding:
    h = heuristics(question, role_hint)
    if provider.kind == "mock":
        return h
    titles = "; ".join(sorted({d.title[:80] for d in (kb.documents if kb else [])})[:60])
    s = spec("judge")
    try:
        llm = provider.structured(system=SYSTEM_QUERY.format(titles=titles), user=question, schema=QueryUnderstanding, model=s.model,
                                 effort=s.effort, max_tokens=2000)
    except Exception as e:  # noqa: BLE001
        h.intent = h.intent or question
        h.keywords.append(f"[llm-error:{type(e).__name__}]")
        return h
    merged = h.model_copy()
    merged.intent = llm.intent or question
    if not role_hint and llm.role:
        merged.role, merged.role_confidence = llm.role, max(h.role_confidence, 0.7)
    merged.reformulations = list(dict.fromkeys([*llm.reformulations, *h.reformulations]))[:6]
    merged.keywords = list(dict.fromkeys([*llm.keywords, *h.keywords]))[:16]
    merged.mentioned_documents = list(dict.fromkeys([*h.mentioned_documents, *[norm_number(x) or x for x in llm.mentioned_documents]]))
    merged.out_of_scope = llm.out_of_scope or h.out_of_scope
    merged.out_of_scope_reason = llm.out_of_scope_reason or h.out_of_scope_reason
    return merged


MAX_QUESTION_CHARS = 2000


def validate_question(question: str) -> Optional[str]:
    """Причина, по которой запрос нельзя обработать, или None."""
    q = (question or "").strip()
    if not q:
        return "пустой запрос: опишите ситуацию или задайте вопрос"
    letters = re.findall(r"[а-яёa-z]", q.lower())
    if len(letters) < 3:
        return "в запросе нет слов: сформулируйте вопрос текстом, например «Как оформить командировку?»"
    words = re.findall(r"[а-яёa-z]{2,}", q.lower())
    if len(words) == 1 and len(words[0]) > 25:
        return "запрос не похож на вопрос: сформулируйте его словами"
    return None
