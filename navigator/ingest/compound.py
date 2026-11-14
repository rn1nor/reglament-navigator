"""Составные файлы: приказ + утверждённое им положение в одном PDF. Делим по страницам на логические документы."""
from __future__ import annotations

import re
from dataclasses import replace

from .loaders import RawDocument
from .structure import ATTACH_RE

TYPE_HEAD = re.compile(r"^\s*(ПОЛОЖЕНИЕ|ПРАВИЛА|ПОРЯДОК|РЕГЛАМЕНТ|ИНСТРУКЦИЯ|УСТАВ|МЕТОДИЧЕСКИЕ\s+РЕКОМЕНДАЦИИ|КОДЕКС|ПОЛИТИКА|ПЕРЕЧЕНЬ|ТИПОВАЯ\s+ФОРМА)\b")
TYPE_HEAD_TC = re.compile(r"^\s*(?:(Положение|Правила|Порядок|Регламент|Инструкция|Политика|Кодекс)\s+(о|об|по|проведения|перевода|перехода|применения|организации|оказания|предоставления|работы|оформления|восстановления|приема|приёма|отчисления|снижения)\b|Устав\s+(федеральн|государствен|образователь|МФТИ))")
TYPE_ALONE = re.compile(r"^\s*(Положение|Правила|Порядок|Регламент|Инструкция|Устав|Политика|Кодекс)\s*$", re.I)
APPROVED_HEAD = re.compile(r"^\s*(УТВЕРЖДЕН[ОАЫ]?|Утвержден[оаы]?)\s*$|^\s*УТВЕРЖДЕН[ОАЫ]?\s+(приказом|решением|распоряжением)", re.I)
ORDER_MARK = re.compile(r"(?mi)^\s*п\s*р\s*и\s*к\s*а\s*з\s*$|приказываю|^\s*распоряжение\s*$")


def _sub(raw: RawDocument, start: int, end: int) -> RawDocument:
    return replace(raw, pages=raw.pages[start:end], warnings=list(raw.warnings))


def split_compound(raw: RawDocument) -> list[tuple[RawDocument, str]]:
    """Возвращает [(часть, роль)], роль: main | attachment. Делим только приказы с приложенными нормативными актами."""
    if len(raw.pages) < 2:
        return [(raw, "main")]
    head = "\n".join(p.text for p in raw.pages[:2])
    first = raw.pages[0].text
    order_like = ORDER_MARK.search(head) or re.search(r"утвердить|изложить\s+в\s+следующей\s+редакции|контроль\s+за\s+исполнением|ввести\s+в\s+действие", first, re.I)
    if not order_like:
        return [(raw, "main")]
    cuts: list[int] = []
    for k, page in enumerate(raw.pages[1:], start=1):
        top = [l.strip() for l in page.text.splitlines() if l.strip()][:15]
        has_attach = any((ATTACH_RE.match(l) and len(l) <= 60) or APPROVED_HEAD.match(l) for l in top[:6])
        type_pos = next((i for i, l in enumerate(top) if TYPE_HEAD.match(l) and len(l) < 90), None)
        if type_pos is None:
            type_pos = next((i for i, l in enumerate(top[:9 if has_attach else 3]) if (TYPE_HEAD_TC.match(l) or (has_attach and TYPE_ALONE.match(l))) and len(l) < 90), None)
        if type_pos is not None and (has_attach or type_pos <= 3):
            cuts.append(k)
    if not cuts:
        return _split_inline(raw)
    parts: list[tuple[RawDocument, str]] = [(_sub(raw, 0, cuts[0]), "main")]
    for i, k in enumerate(cuts):
        end = cuts[i + 1] if i + 1 < len(cuts) else len(raw.pages)
        parts.append((_sub(raw, k, end), "attachment"))
    return parts


INLINE_TYPE = re.compile(r"^\s*(Регламент|Положение|Порядок|Правила|Инструкция|РЕГЛАМЕНТ|ПОЛОЖЕНИЕ|ПОРЯДОК|ПРАВИЛА|ИНСТРУКЦИЯ)\b[^.]{3,160}$")


def _split_inline(raw: RawDocument) -> list[tuple[RawDocument, str]]:
    """Приказ и утверждённый им акт в одном потоке текста (DOCX без разрывов страниц): ищем строку-гриф приложения,
    за которой в пределах шести строк идёт заголовок акта, и режем по ней."""
    from .loaders import RawPage

    flat: list[tuple[int, str]] = [(p.number, line) for p in raw.pages for line in p.text.splitlines()]
    nonempty = [i for i, (_, l) in enumerate(flat) if l.strip()]
    cut_at = None
    for pos, i in enumerate(nonempty):
        line = flat[i][1].strip()
        if pos < 8:
            continue  # шапка самого приказа
        if not ((ATTACH_RE.match(line) or re.match(r"^\s*Приложени\w*(\s*№\s*\d+)?\s+к\s+приказу", line, re.I)) and len(line) <= 90) and not APPROVED_HEAD.match(line):
            continue
        window = [flat[j][1].strip() for j in nonempty[pos + 1:pos + 7]]
        if any(INLINE_TYPE.match(w) for w in window):
            cut_at = i
            break
    if cut_at is None:
        return [(raw, "main")]

    def pages_from(rows: list[tuple[int, str]]) -> list:
        out: dict[int, list[str]] = {}
        for num, line in rows:
            out.setdefault(num, []).append(line)
        return [RawPage(n, "\n".join(ls)) for n, ls in out.items()]

    main = replace(raw, pages=pages_from(flat[:cut_at]), warnings=list(raw.warnings))
    att = replace(raw, pages=pages_from(flat[cut_at:]), warnings=list(raw.warnings))
    return [(main, "main"), (att, "attachment")]
