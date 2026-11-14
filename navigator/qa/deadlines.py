"""Сроки из текста пунктов и их расчёт под даты пользователя.

Правило хранится структурно: смещение, единица (календарные/рабочие дни, недели, месяцы), направление (до/после), якорь.
Считает код, а не модель; рабочие дни — по производственному календарю РФ (2026 год заложен, остальные годы — по фиксированным праздникам).
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Optional

WORDS = {
    "одного": 1, "одной": 1, "один": 1, "двух": 2, "два": 2, "трех": 3, "трёх": 3, "три": 3, "четырех": 4, "четырёх": 4, "пяти": 5, "пять": 5,
    "шести": 6, "семи": 7, "восьми": 8, "десяти": 10, "десять": 10, "четырнадцати": 14, "пятнадцати": 15, "двадцати": 20, "тридцати": 30,
    "сорока пяти": 45, "шестидесяти": 60, "девяноста": 90,
}
NUM = r"(?P<n>\d{1,3}|" + "|".join(sorted(WORDS, key=len, reverse=True)) + r")"
UNIT = r"(?P<unit>(?:календарн\w+\s+|рабоч\w+\s+|учебн\w+\s+)?(?:дн\w+|недел\w+|месяц\w+|год\w*))"
BEFORE_RE = re.compile(rf"(?:не\s+позднее\s*,?\s*чем\s+)?за\s+{NUM}\s+{UNIT}\s+до\s+(?P<anchor>[^.;:«»]{{3,90}})", re.I)
WITHIN_RE = re.compile(rf"в\s+(?:течение|срок\s+до|срок\s+не\s+позднее)\s+{NUM}\s+{UNIT}\s*(?P<anchor>(?:со\s+дня|с\s+даты|с\s+момента|после|от\s+даты|с\s+начала)[^.;:«»]{{3,90}})?", re.I)
NOT_LATER_RE = re.compile(rf"не\s+позднее\s+{NUM}\s+{UNIT}\s*(?P<anchor>(?:со\s+дня|с\s+даты|с\s+момента|после|до)[^.;:«»]{{3,90}})?", re.I)
FIXED_RE = re.compile(r"(?:не\s+позднее|до)\s+(?P<d>\d{1,2})\s+(?P<m>января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s+(?P<y>\d{4}))?", re.I)
MONTHS = {m: i for i, m in enumerate(["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"], start=1)}

# Нерабочие дни РФ (праздники и переносы). 2026 — по постановлению Правительства о переносе выходных.
HOLIDAYS: dict[int, set[tuple[int, int]]] = {
    2026: {(1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (1, 6), (1, 7), (1, 8), (1, 9), (2, 23), (3, 9), (5, 1), (5, 11), (6, 12), (11, 4), (12, 31)},
    2027: {(1, 1), (1, 4), (1, 5), (1, 6), (1, 7), (1, 8), (2, 23), (3, 8), (5, 3), (5, 10), (6, 14), (11, 4)},
}
FIXED_HOLIDAYS = {(1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (1, 6), (1, 7), (1, 8), (2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4)}


@dataclass
class DeadlineRule:
    offset: int
    unit: str  # calendar_days | working_days | weeks | months | years
    direction: str  # before | after
    anchor: str
    source_text: str
    doc_id: str = ""
    clause_id: str = ""
    label: str = ""
    fixed_date: Optional[_dt.date] = None
    computed: Optional[_dt.date] = None
    explanation: str = ""
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        unit_ru = {"calendar_days": "календарных дней", "working_days": "рабочих дней", "weeks": "недель", "months": "месяцев", "years": "лет"}[self.unit]
        if self.fixed_date:
            base = f"не позднее {self.fixed_date.strftime('%d.%m.%Y')}"
        else:
            base = f"{'не позднее чем за' if self.direction == 'before' else 'в течение'} {self.offset} {unit_ru} {'до' if self.direction == 'before' else 'после'} {self.anchor}".strip()
        where = f" ({self.label})" if self.label else ""
        if self.computed:
            short_anchor = " ".join(self.anchor.split()[:5])
            return f"{base}{where}. Расчёт от даты «{short_anchor}» = {self.explanation} → не позднее {self.computed.strftime('%d.%m.%Y')}"
        return base + where


def is_working_day(d: _dt.date) -> bool:
    if d.weekday() >= 5:
        return False
    hol = HOLIDAYS.get(d.year, FIXED_HOLIDAYS)
    return (d.month, d.day) not in hol


def shift_working_days(start: _dt.date, days: int, direction: str) -> _dt.date:
    step = -1 if direction == "before" else 1
    d = start
    left = days
    while left > 0:
        d += _dt.timedelta(days=step)
        if is_working_day(d):
            left -= 1
    return d


def _to_int(tok: str) -> Optional[int]:
    tok = tok.lower()
    if tok.isdigit():
        return int(tok)
    return WORDS.get(tok)


def _unit_kind(u: str) -> str:
    u = u.lower()
    if "рабоч" in u:
        return "working_days"
    if "недел" in u:
        return "weeks"
    if "месяц" in u:
        return "months"
    if u.startswith("год") or " год" in u:
        return "years"
    return "calendar_days"


def extract_rules(text: str, doc_id: str = "", clause_id: str = "", label: str = "") -> list[DeadlineRule]:
    rules: list[DeadlineRule] = []
    seen: set[tuple] = set()
    for rx, direction in ((BEFORE_RE, "before"), (WITHIN_RE, "after"), (NOT_LATER_RE, "after")):
        for m in rx.finditer(text):
            n = _to_int(m.group("n"))
            if n is None:
                continue
            anchor = re.sub(r"\s+", " ", (m.group("anchor") or "")).strip(" ,") or ("события" if direction == "after" else "начала")
            anchor = re.sub(r"^(со дня|с даты|с момента|после|от даты|с начала)\s*", "", anchor, flags=re.I)
            anchor = " ".join(anchor.split()[:8])
            key = (direction, n, _unit_kind(m.group("unit")), anchor[:30].lower())
            if key in seen:
                continue
            seen.add(key)
            rules.append(DeadlineRule(offset=n, unit=_unit_kind(m.group("unit")), direction=direction, anchor=anchor[:90],
                                      source_text=m.group(0)[:200], doc_id=doc_id, clause_id=clause_id, label=label))
    for m in FIXED_RE.finditer(text):
        try:
            year = int(m.group("y")) if m.group("y") else _dt.date.today().year
            fd = _dt.date(year, MONTHS[m.group("m").lower()], int(m.group("d")))
        except ValueError:
            continue
        key = ("fixed", fd)
        if key in seen:
            continue
        seen.add(key)
        rules.append(DeadlineRule(offset=0, unit="calendar_days", direction="before", anchor="", source_text=m.group(0), doc_id=doc_id,
                                  clause_id=clause_id, label=label, fixed_date=fd))
    return rules


def compute(rule: DeadlineRule, anchor_date: _dt.date) -> DeadlineRule:
    if rule.fixed_date:
        rule.computed = rule.fixed_date
        rule.explanation = "фиксированная дата"
        return rule
    if rule.unit == "working_days":
        rule.computed = shift_working_days(anchor_date, rule.offset, rule.direction)
        rule.explanation = f"{anchor_date.strftime('%d.%m.%Y')} {'минус' if rule.direction == 'before' else 'плюс'} {rule.offset} рабочих дней (выходные и праздники по производственному календарю)"
        if anchor_date.year not in HOLIDAYS:
            rule.warnings.append(f"производственный календарь на {anchor_date.year} год не заложен, учтены только фиксированные праздники")
        return rule
    if rule.unit == "calendar_days":
        delta = _dt.timedelta(days=rule.offset)
    elif rule.unit == "weeks":
        delta = _dt.timedelta(weeks=rule.offset)
    elif rule.unit == "months":
        m = anchor_date.month - rule.offset if rule.direction == "before" else anchor_date.month + rule.offset
        y = anchor_date.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        day = min(anchor_date.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
        rule.computed = _dt.date(y, m, day)
        rule.explanation = f"{anchor_date.strftime('%d.%m.%Y')} {'минус' if rule.direction == 'before' else 'плюс'} {rule.offset} мес."
        return rule
    else:
        rule.computed = anchor_date.replace(year=anchor_date.year + (-rule.offset if rule.direction == "before" else rule.offset))
        rule.explanation = f"{rule.offset} лет от {anchor_date.strftime('%d.%m.%Y')}"
        return rule
    rule.computed = anchor_date - delta if rule.direction == "before" else anchor_date + delta
    rule.explanation = f"{anchor_date.strftime('%d.%m.%Y')} {'минус' if rule.direction == 'before' else 'плюс'} {rule.offset} {'календарных дней' if rule.unit == 'calendar_days' else 'недель'}"
    return rule


def deadlines_for_units(units, situation_dates: list[str]) -> list[DeadlineRule]:
    """units: ContextUnit с полями doc_id, clause_id, label, text. Если в вопросе есть дата — считаем срок от неё."""
    anchor: Optional[_dt.date] = None
    for s in situation_dates:
        try:
            anchor = _dt.date.fromisoformat(s)
            break
        except ValueError:
            continue
    out: list[DeadlineRule] = []
    for u in units:
        for r in extract_rules(u.text, u.doc_id, u.clause_id, u.label):
            if anchor is not None:
                compute(r, anchor)
            out.append(r)
    return out
