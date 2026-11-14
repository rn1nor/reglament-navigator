"""Регулярные выражения для шапки документа и ссылок между документами.

Извлекает: тип, номер, дату, заголовок, даты действия, роли, связи (отменяет / изменяет / утверждает /
утверждён / ссылается). Это первый, дешёвый проход; LLM-экстракция уточняет его, но не заменяет.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

from ..schemas import Clause, Document, Relation

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
_M = "|".join(MONTHS)
DATE_NUM = r"(?P<d1>\d{1,2})[.\-/](?P<m1>\d{1,2})[.\-/](?P<y1>\d{4})"
DATE_WORD = rf"[«\"]?\s*(?P<d2>\d{{1,2}})\s*[»\"]?\s+(?P<m2>{_M})\s+(?P<y2>\d{{4}})"
DATE_ANY = rf"(?:{DATE_NUM}|{DATE_WORD})"
NUMTOK = r"[0-9][0-9A-Za-zА-Яа-я\-/–]*(?:\.[0-9]+)?"

DATE_ANY_RE = re.compile(DATE_ANY, re.I)
ANCHOR_A = re.compile(rf"\bот\s+{DATE_ANY}\s*(?:г\.|года)?\s*№\s*(?P<num>{NUMTOK})", re.I)
ANCHOR_B = re.compile(rf"№\s*(?P<num>{NUMTOK})\s+от\s+{DATE_ANY}", re.I)
HEADER_C = re.compile(rf"{DATE_ANY}\s*(?:г\.|года)?[^№\n]{{0,80}}\n?[^№\n]{{0,40}}№\s*(?P<num>{NUMTOK})", re.I)
HEADER_D = re.compile(rf"№\s*(?P<num>{NUMTOK})[^\n]{{0,60}}\n?[^\n]{{0,60}}?{DATE_ANY}", re.I)
KIND_RE = re.compile(
    r"(приказ|распоряжени|положени|регламент|правил|порядк|порядок|инструкци|устав|договор|протокол|письм|"
    r"постановлени|федеральн\w*\s+закон|закон|кодекс|решени)\w*",
    re.I,
)
EXTERNAL_KIND = re.compile(r"постановлени|федеральн|закон|кодекс|письм", re.I)
CANCEL_CTX = re.compile(r"утратив\w+\s+силу|отменить|отмен\w+\s+действ|признать\s+не\s*действующ|считать\s+утратив", re.I)
AMEND_CTX = re.compile(r"внести\s+(?:следующ\w+\s+)?изменени|изменени\w*\s+(?:и\s+дополнени\w*\s+)?в\b|изложить|дополнить|исключить", re.I)
APPROVED_CTX = re.compile(r"утвержд[её]н\w*|утвердить", re.I)
REPLACE_CTX = re.compile(r"взамен|вместо", re.I)
APPROVE_RE = re.compile(
    r"утвердить\s+(?:и\s+ввести\s+в\s+действие\s+)?(?:прилагаем\w+\s+)?(?:новую\s+редакцию\s+)?"
    r"(?P<kind>положение|регламент|правила|порядок|инструкцию|перечень|форму|состав|план|программу|типовую\s+форму)\s+"
    r"(?P<title>[^;\n]{3,220}?)(?=\s*(?:\(|согласно|\.\s|;|$))",
    re.I,
)
APPROVED_BY_HDR = re.compile(
    rf"(?:УТВЕРЖДЕН[ОАЫ]?|Утвержден[оаы]?|Приложение(?:\s*№\s*\d+)?\s+к)\s*(?:приказом|распоряжением|решением|постановлением)?[\s\S]{{0,120}}?"
    rf"(?:от\s+{DATE_ANY}\s*(?:г\.|года)?\s*№\s*(?P<num>{NUMTOK})|№\s*(?P<num2>{NUMTOK})\s+от\s+(?P<dd>\d{{1,2}})[.\-](?P<mm>\d{{1,2}})[.\-](?P<yy>\d{{4}}))",
    re.I | re.S,
)
TYPE_WORDS = [
    ("коллективный договор", "договор"), ("приказ", "приказ"), ("распоряжение", "распоряжение"), ("положение", "положение"),
    ("регламент", "регламент"), ("правила", "правила"), ("порядок", "порядок"), ("инструкция", "инструкция"),
    ("устав", "устав"), ("договор", "договор"), ("протокол", "протокол"), ("методические рекомендации", "методические рекомендации"),
]
EFFECTIVE_FROM = re.compile(rf"(?:вступа\w+\s+в\s+силу|ввести\s+в\s+действие|ввод\w*\s+в\s+действие|утвердить)[^.;\n]{{0,60}}?\s(?:с|со)\s+(?:момента|дня|даты)?\s*(?P<when>подписания|{DATE_ANY})", re.I)
EFFECTIVE_TO = re.compile(rf"(?:действует|срок\s+действия)\s+(?:до|по)\s+{DATE_ANY}", re.I)
ROLE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("student", re.compile(r"обучающ|студент", re.I)),
    ("postgraduate", re.compile(r"аспирант|ординатор|докторант", re.I)),
    ("applicant", re.compile(r"абитуриент|поступающ", re.I)),
    ("teacher", re.compile(r"преподават|профессорско|\bППС\b|педагогическ", re.I)),
    ("researcher", re.compile(r"научн\w+\s+(?:работник|сотрудник)|\bНПР\b|научно-педагогич", re.I)),
    ("admin", re.compile(r"административно-управленческ|\bАУП\b|руководител\w+\s+структурн", re.I)),
    ("staff", re.compile(r"\bработник|\bсотрудник", re.I)),
]


def parse_date_match(m: re.Match) -> Optional[date]:
    g = m.groupdict()
    try:
        if g.get("d1"):
            return date(int(g["y1"]), int(g["m1"]), int(g["d1"]))
        if g.get("d2"):
            return date(int(g["y2"]), MONTHS[g["m2"].lower()], int(g["d2"]))
        if g.get("dd"):
            return date(int(g["yy"]), int(g["mm"]), int(g["dd"]))
    except (ValueError, KeyError):
        return None
    return None


def parse_date(s: str) -> Optional[date]:
    m = DATE_ANY_RE.search(s)
    return parse_date_match(m) if m else None


def norm_number(n: Optional[str]) -> Optional[str]:
    if not n:
        return None
    n = n.strip().rstrip(".,;:)").replace("–", "-").replace(" ", "").upper()
    return n or None


def _kind_to_type(kind: str) -> str:
    k = kind.lower()
    for stem, t in [("приказ", "приказ"), ("распоряж", "распоряжение"), ("положен", "положение"), ("регламент", "регламент"),
                    ("правил", "правила"), ("порядк", "порядок"), ("порядок", "порядок"), ("инструкц", "инструкция"),
                    ("устав", "устав"), ("договор", "договор"), ("протокол", "протокол")]:
        if k.startswith(stem):
            return t
    return "иное"


# ------------------------------------------------------------------ шапка
FORM_HINT = re.compile(r"\b(бланк|шаблон|форма\s+(?:заявки|заявления|сметы|приказа)|смет[аы])\b", re.I)


def detect_type(text: str, filename: str = "") -> str:
    head = "\n".join(text.splitlines()[:60]).lower()
    fn = filename.lower()
    if fn.endswith((".xlsx", ".xlsm", ".xls")) or re.match(r"^(копия\s+)?приложени", fn) and not re.search(r"(?m)^\s*(регламент|положение|порядок|правила|инструкция)\b", head):
        return "форма"
    if FORM_HINT.search(fn) and not re.search(r"(?m)^\s*(регламент|положение|порядок|правила|инструкция)\b", head):
        return "форма"
    for word, t in TYPE_WORDS:
        if re.search(rf"(?m)^\s*{re.escape(word)}\b", head):
            return t
    for stem, t in [("prikaz", "приказ"), ("приказ", "приказ"), ("polozh", "положение"), ("положен", "положение"), ("reglament", "регламент"),
                    ("pravil", "правила"), ("правил", "правила"), ("poryadok", "порядок"), ("порядок", "порядок"), ("instrukc", "инструкция"),
                    ("ustav", "устав"), ("устав", "устав"), ("dogovor", "договор"), ("договор", "договор"), ("rasporyazh", "распоряжение")]:
        if stem in fn:
            return t
    for word, t in TYPE_WORDS:
        if word in head[:600]:
            return t
    return "иное"


EDO_RE = re.compile(rf"Зарегистрирован\w*\s+в\s+(?:СЭД|ЭДО)[^\n]{{0,40}}?от\s+{DATE_ANY}\s*(?:г\.?)?\s*№\s*(?P<num>{NUMTOK})", re.I)
APPROVAL_YEAR_RE = re.compile(
    r"(?:УТВЕРЖДЕН\w*|Утвержден\w*|УТВЕРЖДАЮ|Приложени\w*(?:\s*№\s*\d+)?\s+к\s+(?:приказу|распоряжению))[\s\S]{0,160}?(?P<year>(?:19|20)\d{2})\s*г", re.I)
FILENAME_NUM_RE = re.compile(r"(?:Приказ\w*|Распоряжени\w*|\bN|№)\s*№?\s*(?P<num>\d{1,5}(?:[-–]\d{1,2})?)(?:\s+от\s+(?P<d>\d{2}\.\d{2}\.\d{4}))?|^(?P<num2>\d{1,5}-\d{1,2})\b", re.I)
ORDER_TYPES = ("приказ", "распоряжение")


@dataclass
class HeaderInfo:
    number: Optional[str] = None
    date: Optional[date] = None
    approved_number: Optional[str] = None
    approved_date: Optional[date] = None
    date_partial: bool = False
    source: str = ""


def _head_lines(text: str, n: int = 14) -> str:
    lines = [l for l in text.splitlines() if l.strip()]
    return "\n".join(lines[:n])


def _from_filename(filename: str) -> tuple[Optional[str], Optional[date]]:
    m = FILENAME_NUM_RE.search(filename)
    if not m:
        return None, None
    num = norm_number(m.group("num") or m.group("num2"))
    dt = None
    if m.group("d"):
        try:
            d_, m_, y_ = m.group("d").split(".")
            dt = date(int(y_), int(m_), int(d_))
        except ValueError:
            dt = None
    return num, dt


def extract_header(text: str, doc_type: str, filename: str = "") -> HeaderInfo:
    """Номер и дата документа из его шапки, а не из ссылок в тексте.

    Приказ и распоряжение: номер и дата в первых строках; если шапки нет (скан без первой страницы), берём из имени файла.
    Регламент, положение и прочие акты: собственного номера нет; дата и номер утверждающего приказа берутся из грифа
    «УТВЕРЖДЕН приказом ...», «Приложение к приказу ...» или штампа «Зарегистрировано в ЭДО ...». Ссылки на законы и
    другие акты в тексте никогда не становятся номером документа.
    """
    head = _head_lines(text)
    info = HeaderInfo()
    if doc_type in ORDER_TYPES:
        best = None
        ref_before = re.compile(r"(приказ[аеуо]м?|распоряжени[яюе]м?|пункт\w*|в\s+соответствии|изложить|утвержд\w+|согласно|во\s+изменение)[^\n]{0,40}$", re.I)
        for prio, rx in enumerate((ANCHOR_A, ANCHOR_B, HEADER_C, HEADER_D)):
            for m in rx.finditer(head):
                if ref_before.search(head[max(0, m.start() - 60):m.start()]):
                    continue  # это ссылка на другой акт, а не собственная шапка
                if best is None or (m.start(), prio) < (best[0].start(), best[1]):
                    best = (m, prio)
                break
        if best:
            info.number, info.date, info.source = norm_number(best[0].group("num")), parse_date_match(best[0]), "шапка"
        else:
            m = re.search(rf"(?m)^\s*№\s*(?P<num>{NUMTOK})\s*$", head)
            md = DATE_ANY_RE.search(head)
            if m:
                info.number, info.source = norm_number(m.group("num")), "шапка"
            if md and re.search(r"\bот\b|^\s*[«\"]?\d", head[max(0, md.start() - 8):md.start() + 1], re.I | re.M) and not ref_before.search(head[max(0, md.start() - 60):md.start()]):
                info.date = parse_date_match(md)
        fn_num, fn_date = _from_filename(filename)
        if not info.number and fn_num:
            info.number, info.source = fn_num, "имя файла"
        if not info.date and fn_date and (not info.number or info.number == fn_num):
            info.date = fn_date
        edo = EDO_RE.search(text[:800])
        if edo and not info.number:
            info.number, info.date, info.source = norm_number(edo.group("num")), parse_date_match(edo), "штамп ЭДО"
        return info
    # акты, утверждаемые приказом
    zone = text[:900]
    edo = EDO_RE.search(zone)
    if edo:
        info.approved_number, info.approved_date = norm_number(edo.group("num")), parse_date_match(edo)
        info.date, info.source = info.approved_date, "штамп ЭДО"
        return info
    m = APPROVED_BY_HDR.search(zone)
    if m:
        info.approved_number = norm_number(m.group("num") or m.group("num2"))
        info.approved_date = parse_date_match(m)
        info.date, info.source = info.approved_date, "гриф утверждения"
        if info.date:
            return info
    y = APPROVAL_YEAR_RE.search(zone)
    if y:
        info.date, info.date_partial, info.source = date(int(y.group("year")), 1, 1), True, "гриф утверждения (только год)"
    return info


def extract_number_date(text: str, doc_type: str = "приказ", filename: str = "") -> tuple[Optional[str], Optional[date]]:
    h = extract_header(text, doc_type, filename)
    return h.number, h.date


def extract_title(text: str, doc_type: str, filename: str = "", hint: Optional[str] = None) -> str:
    title = _extract_title_raw(text, doc_type, filename, hint)
    return re.sub(r"(?<=[»)\"”])\d{1,2}(?=\s|$)", "", title).strip()  # знак сноски, приклеившийся к названию


def _extract_title_raw(text: str, doc_type: str, filename: str = "", hint: Optional[str] = None) -> str:
    lines = [l.strip() for l in text.splitlines()[:80]]
    lines = [l for l in lines if l]
    title = ""
    if doc_type in ("приказ", "распоряжение"):
        for i, l in enumerate(lines[:50]):
            if re.match(r"^(О|Об|О́б|об|о)\s+[а-яёА-ЯЁ«]", l) and len(l) > 6 and not re.match(r"^(о|об)\s+(том|чем|нем|ней)\b", l):
                buf = [l]
                for nxt in lines[i + 1:i + 6]:
                    if re.match(r"^(В целях|В соответствии|На основании|Руководствуясь|В связи|ПРИКАЗЫВАЮ|Во исполнение|С целью|Внести|Утвердить|Установить|Признать|Контроль|Отменить|Назначить|Ввести|Руководител|Проректор|Ректор|\d+[.)])", nxt, re.I):
                        break
                    if re.search(r"[.:]$", buf[-1]):
                        break
                    buf.append(nxt)
                title = " ".join(buf)
                break
    else:
        type_rx = re.compile(r"^(ПОЛОЖЕНИЕ|ПРАВИЛА|РЕГЛАМЕНТ|ПОРЯДОК|ИНСТРУКЦИЯ|УСТАВ|КОЛЛЕКТИВНЫЙ ДОГОВОР|Положение|Правила|Регламент|Порядок|Инструкция)\b(.*)$")
        for i, l in enumerate(lines[:60]):
            m = type_rx.match(l)
            if m:
                buf = [l]
                for nxt in lines[i + 1:i + 6]:
                    if re.match(r"^(\d+[.)]|I\.|1\.|УТВЕРЖД|Утвержд|Приложение|Москва|г\.\s)", nxt):
                        break
                    if re.match(r"^(\d+\.?\s*)?(Общие положения|ОБЩИЕ ПОЛОЖЕНИЯ|Термины|ТЕРМИНЫ|Основные понятия|Введение|ВВЕДЕНИЕ)\b", nxt):
                        break
                    if re.match(r"^(В\s*$|В\s+целях|В\s+соответствии|Настоящ|На\s+основании|С\s+целью)", nxt):
                        break
                    if len(nxt) > 160 or (buf[-1].endswith(".") and len(buf) > 1):
                        break
                    buf.append(nxt)
                    if len(" ".join(buf)) > 220:
                        break
                title = " ".join(buf)
                break
    title = re.sub(r"\s+", " ", title).strip(" .")
    title = re.sub(r"\s*(Сертификат|ДОКУМЕНТ ПОДПИСАН|Подписано|Владелец|Действителен|Электронная подпись)\b.*$", "", title, flags=re.I).strip(" .,;")
    if len(title) < 8:
        if hint and len(hint) >= 8:
            title = hint
        else:
            base = re.sub(r"_[0-9a-f]{8}$", "", filename.rsplit(".", 1)[0])
            title = re.sub(r"(?<!\d)-|-(?!\d)", " ", base)
            title = re.sub(r"_+", ": ", title, count=1) if re.match(r"^\d+-\d+\s", title) else title
            title = re.sub(r"[_]+", " ", title)
            title = re.sub(r"\s+", " ", title).strip()
    return title[:300]


def extract_effective(text: str, doc_date: Optional[date]) -> tuple[Optional[date], Optional[date]]:
    eff_from = eff_to = None
    m = EFFECTIVE_FROM.search(text)
    if m:
        if m.group("when") and m.group("when").lower().startswith("подписан"):
            eff_from = doc_date
        else:
            eff_from = parse_date_match(m)
    m2 = EFFECTIVE_TO.search(text)
    if m2:
        eff_to = parse_date_match(m2)
    return eff_from, eff_to


def detect_roles(text: str, title: str = "") -> list[str]:
    """Роли адресатов документа: сначала заголовок, затем «распространяется на ...», затем частоты упоминаний."""
    title_roles: list[str] = []
    if title and re.search(r"коллективн\w+\s+договор|трудов\w+\s+распорядк|оплат\w+\s+труда|работник", title, re.I):
        return ["staff", "teacher", "researcher", "admin"]
    for role, rx in ROLE_PATTERNS:
        if title and rx.search(title):
            title_roles.append(role)
    if title_roles:
        if "student" in title_roles and re.search(r"аспирант", text[:5000], re.I):
            title_roles.append("postgraduate")
        return list(dict.fromkeys(title_roles))
    scope = ""
    m = re.search(r"(распространяется на|применяется к|обязательн\w+ для)[^.\n]{0,300}", text, re.I)
    if m:
        scope = m.group(0)
        scope_roles = [role for role, rx in ROLE_PATTERNS if rx.search(scope)]
        if scope_roles:
            return scope_roles
    counts = {role: len(rx.findall(text)) for role, rx in ROLE_PATTERNS}
    total = sum(counts.values()) or 1
    roles = [role for role, c in counts.items() if c >= 6 and c / total >= 0.15]
    return roles or ["all"]


# ------------------------------------------------------------------ связи
def _classify(ctx: str, kind: str, in_header: bool, doc_is_order: bool) -> str:
    if CANCEL_CTX.search(ctx):
        return "cancels" if doc_is_order else "references"
    if REPLACE_CTX.search(ctx) and doc_is_order:
        return "replaces"
    if AMEND_CTX.search(ctx):
        return "amends" if doc_is_order else "references"
    m = APPROVED_CTX.search(ctx)
    if m:
        imperative = m.group(0).lower().startswith("утвердить")
        if doc_is_order:
            return "approves" if imperative else "references"  # «утверждённым приказом № X» в приказе — это ссылка
        return "approved_by" if (in_header and not imperative) else "references"
    return "references"


EXTERNAL_NUM = re.compile(r"ФЗ|ФКЗ|[нН]$|-[рРпП]$|^\d+-?[рР]$")
EXTERNAL_CTX = re.compile(r"Правительств|Министерств|Минфин|Минобрнауки|Минздрав|Минтруд|Казначейств|федеральн\w+\s+закон|Закон\w*\s+(?:РФ|Российск)|Президент|Кодекс", re.I)


def extract_relations(doc: Document, clauses: list[Clause]) -> list[Relation]:
    rels: list[Relation] = []
    doc_is_order = doc.doc_type in ("приказ", "распоряжение")
    by_id = {c.id: c for c in clauses}
    seen: set[tuple] = set()
    for ci, c in enumerate(clauses):
        parent_txt = by_id[c.parent_id].text[-240:] if c.parent_id and c.parent_id in by_id else ""
        text = c.text
        in_header = ci < 6 and c.kind in ("preamble", "paragraph", "heading")
        for rx in (ANCHOR_A, ANCHOR_B):
            for m in rx.finditer(text):
                num = norm_number(m.group("num"))
                dt = parse_date_match(m)
                if num and num == doc.number and (dt is None or doc.date is None or dt == doc.date) and ci < 4:
                    continue  # собственная шапка документа, а не ссылка
                window = text[max(0, m.start() - 260):m.start()]
                kinds = list(KIND_RE.finditer(window))
                kind = kinds[-1].group(0) if kinds else "документ"
                ctx = (parent_txt + " " + window) if window else parent_txt
                rtype = _classify(ctx, kind, in_header, doc_is_order)
                external = bool(EXTERNAL_KIND.search(kind)) or bool(num and EXTERNAL_NUM.search(num)) or bool(EXTERNAL_CTX.search(window[-160:]))
                title_m = re.search(r"[«\"]([^»\"]{4,200})[»\"]", window[kinds[-1].start():] if kinds else window)
                target_title = title_m.group(1).strip() if title_m else None
                key = (rtype, num, dt, kind.lower()[:6])
                if key in seen:
                    continue
                seen.add(key)
                rel = Relation(type=rtype, source_doc_id=doc.id, target_ref=window[-120:].strip() + " " + m.group(0),
                               target_kind=_kind_to_type(kind), target_number=num, target_date=dt, target_title=target_title,
                               clause_id=c.id, confidence=0.8 if rtype == "references" else 0.9, external=external)
                # «Положение ..., утверждённое приказом от ... № ...»: цель — положение, приказ лишь утверждающий
                utv = re.search(r",?\s*утвержд[её]нн\w*", window, re.I)
                if rtype in ("amends", "cancels", "replaces") and kinds and len(kinds) >= 2 and utv:
                    for k2 in kinds[:-1]:
                        if k2.start() >= utv.start() or k2.group(0).lower().startswith(("приказ", "распоряж")):
                            continue
                        rel.target_kind = _kind_to_type(k2.group(0))
                        rel.approving_number = num
                        rel.approving_date = dt
                        rel.target_number = None
                        rel.target_date = None
                        span = window[k2.start():utv.start()]
                        tm = re.search(r"[«\"]([^»\"]{4,200})[»\"]", span)
                        rel.target_title = (tm.group(1).strip() if tm else re.sub(r"\s+", " ", span).strip(" ,"))[:200]
                        break
                rels.append(rel)
        if doc_is_order:
            for m in APPROVE_RE.finditer(text):
                kind = m.group("kind")
                title = re.sub(r"\s+", " ", m.group("title")).strip(" .;,")
                key = ("approves", kind.lower(), title.lower()[:60])
                if key in seen:
                    continue
                seen.add(key)
                rels.append(Relation(type="approves", source_doc_id=doc.id, target_ref=m.group(0)[:200], target_kind=_kind_to_type(kind),
                                     target_title=f"{kind.capitalize()} {title}", clause_id=c.id, confidence=0.85))
    if not doc_is_order:
        m = APPROVED_BY_HDR.search(doc_text_head(clauses))
        if m:
            num = norm_number(m.group("num") or m.group("num2"))
            dt = parse_date_match(m)
            if num and not any(r.type == "approved_by" and r.target_number == num for r in rels):
                rels.insert(0, Relation(type="approved_by", source_doc_id=doc.id, target_ref=m.group(0)[:200], target_kind="приказ",
                                        target_number=num, target_date=dt, clause_id=clauses[0].id if clauses else None, confidence=0.9))
    return rels


def doc_text_head(clauses: list[Clause], n: int = 8) -> str:
    return "\n".join(c.text for c in clauses[:n])


def title_key(doc_type: str, title: str) -> str:
    t = title.lower()
    t = re.sub(r"[«»\"'()]", " ", t)
    t = re.sub(r"\b(новая|новой)\s+редакци\w*", " ", t)
    t = re.sub(r"\bв\s+ред\w*\.?.*$", " ", t)
    t = re.sub(r"\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}", " ", t)
    t = re.sub(r"\b(19|20)\d{2}(\s*г\.?|\s*года)?\b", " ", t)
    t = re.sub(r"№\s*\S+", " ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return f"{doc_type}|{t}"


# ------------------------------------------------------------------ разметка: темы и подразделение-владелец
TOPIC_RULES: list[tuple[str, re.Pattern]] = [
    ("Кадры и трудовые отношения", re.compile(r"трудов\w+\s+договор|приём\s+на\s+работу|прием\s+на\s+работу|увольнен|должностн|штатн\w+\s+расписан|внутренн\w+\s+трудов|отпуск\w*\s+работник|аттестац\w+\s+работник|конкурс\w*\s+на\s+замещение", re.I)),
    ("Оплата труда и выплаты", re.compile(r"оплат\w+\s+труда|заработн\w+\s+плат|премир|надбавк|стимулирующ|материальн\w+\s+помощ|компенсац", re.I)),
    ("Командировки", re.compile(r"командиров|суточн|проездн|авансов\w+\s+отч", re.I)),
    ("Закупки и договоры", re.compile(r"закупк|контракт|44-ФЗ|223-ФЗ|поставщик|тендер|договор\w*\s+(?:об\s+оказании|поставки|подряда|ГПХ)|гражданско-правов", re.I)),
    ("Финансы, бухгалтерия и отчётность", re.compile(r"бухгалтер|учетн\w+\s+политик|учётн\w+\s+политик|финансов\w+\s+отчет|финансов\w+\s+отчёт|смет|бюджетн|платёжн|платежн|первичн\w+\s+документ|инвентаризац", re.I)),
    ("Стоимость обучения и платные услуги", re.compile(r"стоимост\w+\s+обучени|платн\w+\s+образовательн|оплат\w+\s+обучени|скидк|снижени\w+\s+стоимост", re.I)),
    ("Учебный процесс и аттестация", re.compile(r"промежуточн\w+\s+аттестац|текущ\w+\s+контрол|государственн\w+\s+итогов|график\s+учебн|учебн\w+\s+план|расписани|экзамен|зач[её]т|академическ\w+\s+задолженност|курсов\w+\s+работ|выпускн\w+\s+квалификац", re.I)),
    ("Движение контингента", re.compile(r"отчислен|восстановлен|перевод\w*\s+обучающ|академическ\w+\s+отпуск|зачислен", re.I)),
    ("Приём на обучение", re.compile(r"правил\w*\s+при[её]ма|поступающ|абитуриент|вступительн\w+\s+испытан|приёмн\w+\s+комисси|приемн\w+\s+комисси", re.I)),
    ("Стипендии и поддержка студентов", re.compile(r"стипенди|материальн\w+\s+поддержк|социальн\w+\s+выплат", re.I)),
    ("Общежития и кампус", re.compile(r"общежит|пропускн|кампус|жил\w+\s+помещен", re.I)),
    ("Наука, аспирантура и гранты", re.compile(r"аспирант|диссертац|научн\w+\s+исследован|грант|НИР\b|научн\w+\s+работник", re.I)),
    ("Документооборот и подписи", re.compile(r"документооборот|делопроизводств|номенклатур\w+\s+дел|архивн\w+\s+хранен|порядок\s+подписани|право\s+подписи", re.I)),
    ("Персональные данные и ИБ", re.compile(r"персональн\w+\s+данн|информационн\w+\s+безопасност|конфиденциальн", re.I)),
    ("Дисциплина и споры", re.compile(r"дисциплинарн|взыскан|урегулировани\w+\s+спор|жалоб|внутренн\w+\s+распорядк", re.I)),
    ("Имущество и охрана труда", re.compile(r"охран\w+\s+труда|пожарн|имуществ|материальн\w+\s+ответственност|инвентар", re.I)),
    ("Управление и структура", re.compile(r"устав|учёный\s+совет|ученый\s+совет|структурн\w+\s+подразделен|положение\s+о\s+(?:кафедре|факультете|школе|управлении|отделе|центре)", re.I)),
]
OWNER_RE = re.compile(r"контрол\w*\s+(?:за\s+)?исполнени\w*[^.\n]{0,40}?возложить\s+на\s+(?P<who>[^.\n;]{5,120})", re.I)
UNIT_RE = re.compile(
    r"\b((?:Управлени\w+|Отдел\w*|Департамент\w*|Дирекци\w*|Служб\w+|Центр\w*)\s+(?:по\s+)?[а-яё\-]{4,}(?:\s+(?:и|по|с)?\s*[а-яё\-]{3,}){0,4}"
    r"|Бухгалтери\w*|Деканат\w*|Планово-?финансов\w+\s+\w+|Финансово-?экономическ\w+\s+\w+|Юридическ\w+\s+\w+|Приёмн\w+\s+комисси\w*|Приемн\w+\s+комисси\w*|Учебн\w+\s+управлени\w*)",
    re.U,
)


def detect_topics(text: str, title: str = "", max_topics: int = 3) -> list[str]:
    """Темы по словарю: заголовок весит больше текста. Это разметка для фильтров и карточек, а не истина в последней инстанции."""
    scores: dict[str, float] = {}
    head = text[:6000]
    for topic, rx in TOPIC_RULES:
        score = 3.0 * len(rx.findall(title)) + 1.0 * len(rx.findall(head)) + 0.3 * len(rx.findall(text[6000:60000]))
        if score >= 1.0:
            scores[topic] = score
    return [t for t, _ in sorted(scores.items(), key=lambda kv: -kv[1])[:max_topics]]


def detect_owner_unit(text: str) -> Optional[str]:
    m = OWNER_RE.search(text)
    if m:
        who = re.sub(r"\s+", " ", m.group("who")).strip(" ,")
        return who[:120]
    tail = text[-3000:]
    m2 = UNIT_RE.search(tail) or UNIT_RE.search(text[:3000])
    if not m2:
        return None
    who = re.sub(r"\s+", " ", m2.group(1)).strip(" ,.;")
    who = re.sub(r"\s+(и|по|с|в|на|за|о|об)$", "", who)
    return who[:120] if len(who) >= 8 else None
