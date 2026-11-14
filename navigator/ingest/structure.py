"""Разбор текста документа в дерево пунктов: раздел -> пункт -> подпункт -> абзац.

Единица индекса и цитаты — пункт с номером («3.2.1»), приложения получают префикс («Прил.1:2.3»).
Если нумерации почти нет, включается запасной режим: абзацные чанки с идентификаторами p1, p2, ...
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from ..schemas import Clause
from .loaders import RawDocument, heading_key

NUM_RE = re.compile(r"^\s*(?:(\d{1,3}(?:\.\d{1,3})+)\.?|(\d{1,3})[.)])(?:\s+(?=\S)|(?=[А-ЯЁ][а-яё]{2}))")  # пробел после номера могли пропустить
BARE_SECTION_RE = re.compile(r"^\s*(\d{1,2})\s+(?=[А-ЯЁ][а-яёА-ЯЁ])")  # «4 Правила рассмотрения ...» без точки после номера
NUM_ONLY_RE = re.compile(r"^\s*(?:(\d{1,3}(?:\.\d{1,3})+)\.?|(\d{1,3})[.)])\s*$")
LETTER_RE = re.compile(r"^\s*([а-яёa-z])\)\s+(?=\S)")
DASH_RE = re.compile(r"^\s*[-–—•●▪]\s+(?=\S)")
ROMAN_RE = re.compile(r"^\s*([IVXLC]{1,6})\s*[.)]\s+(\S.*)$")
SECTION_RE = re.compile(r"^\s*(Раздел|Глава|Статья|РАЗДЕЛ|ГЛАВА|СТАТЬЯ)\s+(\d+|[IVXLC]+)\s*[.)]?\s*(.*)$")
ATTACH_RE = re.compile(r"^\s*ПРИЛОЖЕНИЕ\s*(?:№\s*)?(\d+)?\s*(?:к\s.*)?$", re.I)
PAGE_NUM_RE = re.compile(r"^\s*(?:стр(?:аница)?\.?\s*)?\d{1,3}(?:\s*(?:из|/)\s*\d{1,3})?\s*$", re.I)
LEAD_FIX = str.maketrans({"З": "3", "О": "0", "о": "0", "l": "1", "I": "1", "і": "1", "Б": "6", "б": "6"})


@dataclass
class Block:
    kind: str  # clause | item | heading | paragraph | attachment | order_marker
    text: str
    page_start: int
    page_end: int
    number: Optional[str] = None
    marker: Optional[str] = None
    attachment: Optional[str] = None
    lines: list[str] = field(default_factory=list)
    styled: bool = False  # заголовок распознан по оформлению (полужирный по центру), а не по тексту


@dataclass
class StructureStats:
    numbered: int = 0
    headings: int = 0
    items: int = 0
    attachments: int = 0
    fallback: bool = False
    noise_lines: int = 0


def _fix_leading_number(line: str) -> str:
    """OCR часто путает «З.2.» и «3.2.»: чиним только ведущий токен, если после замены он похож на номер."""
    m = re.match(r"^(\s*)(\S{1,12})(\s+)", line)
    if not m:
        return line
    tok = m.group(2)
    if re.search(r"\d", tok) or tok.count(".") >= 1:
        fixed = tok.translate(LEAD_FIX)
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3})*[.)]?", fixed) and fixed != tok:
            return m.group(1) + fixed + line[m.end(2):]
    return line


def _looks_like_date(num: str) -> bool:
    parts = num.split(".")
    return len(parts) == 3 and len(parts[-1]) == 4


def _is_caps_heading(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    if len(letters) < 3 or len(s) > 160:
        return False
    if not all(c.isupper() for c in letters):
        return False
    return not s.rstrip().endswith((";", ",", ":"))


def _noise_lines(raw: RawDocument) -> set[str]:
    """Колонтитулы: строки, повторяющиеся на многих страницах."""
    if len(raw.pages) < 3:
        return set()
    counter: Counter[str] = Counter()
    for p in raw.pages:
        seen = set()
        for line in p.text.splitlines():
            key = re.sub(r"\d+", "#", line.strip().lower())
            if 4 <= len(key) <= 120 and key not in seen:
                seen.add(key)
                counter[key] += 1
    n = len(raw.pages)
    return {k for k, c in counter.items() if c >= 3 and c / n >= 0.4}


class _NumberContext:
    """Проверка правдоподобия номера пункта относительно уже встреченных."""

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.last: Optional[str] = None
        self.last_top: Optional[int] = None

    def reset(self) -> None:
        self.__init__()

    def accept(self, num: str) -> bool:
        if _looks_like_date(num):
            return False
        parts = num.split(".")
        try:
            ints = [int(p) for p in parts]
        except ValueError:
            return False
        if any(i > 300 for i in ints) or any(i == 0 for i in ints):
            return False
        if len(parts) == 1:
            n = ints[0]
            if n == 1 or self.last_top is None:
                return n <= 3 if self.last_top is None else True
            return 0 < n <= self.last_top + 2
        parent = ".".join(parts[:-1])
        if parent in self.seen:
            return True
        if ints[-1] > 1 and f"{parent}.{ints[-1] - 1}" in self.seen:
            return True  # предыдущий соседний пункт уже был (заголовок раздела без номера)
        if self.last and self.last.rsplit(".", 1)[0] == parent:
            return True
        if not self.seen:
            return ints[-1] <= 10  # документ может начинаться с середины (скан без первой страницы)
        # родитель не встречался: допускаем «первый подпункт» нового раздела (2.1 после 1.x, даже без строки «2.»)
        if ints[-1] == 1 and (self.last is None or num.startswith(self.last.split(".")[0])):
            return True
        if ints[-1] == 1 and self.last_top is not None and len(parts) == 2 and ints[0] in (self.last_top, self.last_top + 1):
            return True
        return False

    def add(self, num: str) -> None:
        self.seen.add(num)
        self.last = num
        top = int(num.split(".")[0])
        if "." not in num or self.last_top is None:
            self.last_top = top
        else:
            self.last_top = max(self.last_top, top)


def _styled_heading(s: str, styled: set[str]) -> bool:
    """Строка оформлена как заголовок и по тексту похожа на него: с заглавной буквы, без завершающей пунктуации, не бланк."""
    if not styled or heading_key(s) not in styled or len(s) > 160 or "___" in s:
        return False
    first = next((c for c in s if c.isalpha()), "")
    return bool(first) and first.isupper() and not s.rstrip().endswith((".", ";", ",", ":"))


def split_blocks(raw: RawDocument) -> tuple[list[Block], StructureStats]:
    stats = StructureStats()
    noise = _noise_lines(raw)
    styled = getattr(raw, "heading_lines", None) or set()
    para_based = raw.file_type in ("docx", "doc", "rtf", "odt")  # строка = абзац: переносов внутри предложения нет
    blocks: list[Block] = []
    cur: Optional[Block] = None
    attachment: Optional[str] = None
    attach_counter = 0
    ctx = _NumberContext()
    in_order_body = False
    blank_since_last = False
    nonempty_seen = 0

    def close() -> None:
        nonlocal cur
        if cur is not None:
            cur.text = "\n".join(l for l in cur.lines).strip()
            if cur.text:
                blocks.append(cur)
            cur = None

    def start(kind: str, text: str, page: int, **kw) -> None:
        nonlocal cur
        close()
        cur = Block(kind=kind, text="", page_start=page, page_end=page, attachment=attachment, **kw)
        cur.lines.append(text.strip())

    for page in raw.pages:
        for raw_line in page.text.splitlines():
            line = raw_line.rstrip()
            s = line.strip()
            if not s:
                blank_since_last = True
                continue
            key = re.sub(r"\d+", "#", s.lower())
            if key in noise or PAGE_NUM_RE.match(s):
                stats.noise_lines += 1
                continue
            compact = re.sub(r"\s+", "", s).upper()
            if compact.startswith("ПРИКАЗЫВАЮ") and len(compact) <= 14:
                close()
                blocks.append(Block(kind="order_marker", text="ПРИКАЗЫВАЮ:", page_start=page.number, page_end=page.number, attachment=attachment))
                in_order_body = True
                if attachment is None:
                    ctx.reset()
                blank_since_last = False
                continue
            nonempty_seen += 1
            am = ATTACH_RE.match(s) or (re.match(r"^\s*Приложени\w*(\s*№\s*\d+)?\s+к\s+(приказу|распоряжению)", s, re.I) if nonempty_seen <= 8 else None)
            if am and len(s) <= 90 and nonempty_seen <= 8 and not any(b.kind == "clause" for b in blocks):
                # «Приложение № 1 к приказу ...» в самой шапке файла: это заголовок документа, а не вложение
                start("paragraph", s, page.number)
                blank_since_last = False
                continue
            if am and len(s) <= 60:
                attach_counter += 1
                attachment = f"Прил.{am.group(1) or attach_counter}"
                close()
                blocks.append(Block(kind="attachment", text=s, page_start=page.number, page_end=page.number, attachment=attachment))
                ctx.reset()
                stats.attachments += 1
                blank_since_last = False
                continue
            fixed = _fix_leading_number(line)
            nm = NUM_RE.match(fixed) or NUM_ONLY_RE.match(fixed)
            if nm and nm.group(2) and fixed[:nm.end()].rstrip().endswith(")") and ctx.last and "." in ctx.last and cur is not None:
                # перечень «1) ... 2) ...» внутри пункта «2.10»: это элементы перечня, иначе «3)» станет родителем для «3.1»
                start("item", f"{nm.group(2)}) {fixed[nm.end():].strip()}", page.number, marker="-")
                stats.items += 1
                blank_since_last = False
                continue
            if nm:
                num = (nm.group(1) or nm.group(2)).rstrip(".")
                if ctx.accept(num):
                    ctx.add(num)
                    rest = fixed[nm.end():]
                    start("clause", rest, page.number, number=num)
                    if not rest.strip():
                        cur.lines = []  # текст пункта придёт со следующей строки
                    elif "." not in num and styled and (heading_key(s) in styled or heading_key(rest) in styled):
                        cur.styled = True  # «1. Общие положения» оформлено как заголовок: следующий абзац — уже текст раздела
                    stats.numbered += 1
                    blank_since_last = False
                    continue
            bm = BARE_SECTION_RE.match(fixed)
            if bm and ctx.last_top is not None and int(bm.group(1)) == ctx.last_top + 1 and len(s) < 110 and not s.rstrip().endswith((".", ";", ",", ":")):
                num = bm.group(1)
                ctx.add(num)
                start("clause", fixed[bm.end():], page.number, number=num)
                stats.numbered += 1
                blank_since_last = False
                continue
            sm = SECTION_RE.match(s)
            rm = ROMAN_RE.match(s)
            # хвост предложения, перенесённый на отдельную строку («... актами\nМФТИ.»), заголовком не считаем
            tail = (s.endswith(".") and len(s.split()) <= 2 and cur is not None and cur.kind != "heading" and bool(cur.lines)
                    and not cur.lines[-1].rstrip().endswith((".", ";", ":", "!", "?")))
            if (cur is not None and cur.kind == "clause" and cur.number and "." not in cur.number and not blank_since_last and (_is_caps_heading(s) or heading_key(s) in styled)
                    and (not cur.lines or (len(cur.lines) == 1 and _is_caps_heading(cur.lines[0]) and len(cur.lines[0]) < 160))):
                if cur.lines:
                    prev = cur.lines[0]
                    cur.lines[0] = prev + s if prev.endswith("-") else f"{prev} {s}"
                else:
                    cur.lines.append(s)
                cur.styled = True
                cur.page_end = page.number
                blank_since_last = False
                continue
            if sm or (rm and _is_caps_heading(rm.group(2))) or (_is_caps_heading(s) and not tail and (cur is None or cur.kind != "heading" or blank_since_last)):
                if cur is not None and cur.kind == "heading" and not blank_since_last:
                    cur.lines.append(s)
                    cur.page_end = page.number
                else:
                    start("heading", s, page.number)
                    stats.headings += 1
                blank_since_last = False
                continue
            # заголовок раздела без номера, распознанный по оформлению (после первого пункта: шапка документа разбирается отдельно)
            if styled and stats.numbered > 0:
                if cur is not None and cur.kind == "heading" and cur.lines and not blank_since_last and heading_key(s) in styled:
                    cur.lines[-1] = f"{cur.lines[-1]} {s}".strip()  # вторая строка того же заголовка
                    cur.page_end = page.number
                    blank_since_last = False
                    continue
                if _styled_heading(s, styled):
                    start("heading", s, page.number, styled=True)
                    stats.headings += 1
                    blank_since_last = False
                    continue
            lm = LETTER_RE.match(s)
            dm = DASH_RE.match(s)
            if lm or dm:
                marker = lm.group(1) if lm else "-"
                rest = s[(lm or dm).end():]
                start("item", rest, page.number, marker=marker)
                stats.items += 1
                blank_since_last = False
                continue
            # продолжение текста
            if cur is not None and cur.kind == "clause" and cur.number and "." not in cur.number and cur.lines:
                first = cur.lines[0].rstrip()
                if cur.styled and heading_key(s) in styled and not blank_since_last:
                    cur.lines[-1] = f"{cur.lines[-1]} {s}".strip()  # вторая строка нумерованного заголовка
                    cur.page_end = page.number
                    blank_since_last = False
                    continue
                # в DOCX строка — это абзац: «2. Порядок открытия счёта» без точки в конце, за которым идёт абзац с заглавной буквы,
                # это заголовок раздела и первый абзац раздела, а не один пункт
                if (not cur.styled and para_based and len(cur.lines) == 1 and 3 <= len(first) <= 300
                        and not first.endswith((".", ";", ":", ",", "!", "?")) and s[:1].isupper() and "___" not in first and "|" not in first):
                    cur.styled = True
                if cur.styled:
                    start("paragraph", s, page.number)
                    blank_since_last = False
                    continue
            if cur is None or cur.kind in ("heading",) and (blank_since_last or cur.styled):
                start("paragraph", s, page.number)  # обычный текст после оформленного заголовка — уже не заголовок
            else:
                prev = cur.lines[-1] if cur.lines else ""
                if not cur.lines:
                    cur.lines.append(s)
                    cur.page_end = page.number
                    blank_since_last = False
                    continue
                if prev.endswith("-") and s[:1].islower():
                    cur.lines[-1] = prev[:-1] + s
                elif blank_since_last:
                    cur.lines.append("")
                    cur.lines.append(s)
                else:
                    cur.lines[-1] = (prev + " " + s).strip() if prev else s
                cur.page_end = page.number
            blank_since_last = False
    close()
    # пометить преамбулу приказа: всё до первого «ПРИКАЗЫВАЮ», если оно в начале документа, а не в форме приказа внутри приложения
    marker_idx = next((i for i, b in enumerate(blocks) if b.kind == "order_marker"), None)
    first_clause_idx = next((i for i, b in enumerate(blocks) if b.kind == "clause"), None)
    if marker_idx is not None and blocks[marker_idx].attachment is None and (first_clause_idx is None or marker_idx < first_clause_idx):
        for b in blocks[:marker_idx]:
            if b.kind == "paragraph" and not b.attachment:
                b.kind = "preamble"
    # первый раздел без номера стоит до первого пункта («Общие положения» перед п. 1): узнаём его по оформлению и по тому, что сразу за ним идёт пункт
    if styled:
        for i, b in enumerate(blocks[:-1]):
            nxt = blocks[i + 1]
            if b.kind == "paragraph" and nxt.kind == "clause" and nxt.number in ("1", "1.1") and "\n" not in b.text and _styled_heading(b.text, styled):
                b.kind, b.styled = "heading", True
                stats.headings += 1
    _ = in_order_body
    return blocks, stats


def _clause_text_clean(text: str) -> str:
    text = re.sub(r"\n\s*\n", "\n", text)
    return text.strip()


def build_clauses(doc_id: str, raw: RawDocument, doc_title: str = "") -> tuple[list[Clause], StructureStats]:
    blocks, stats = split_blocks(raw)
    clauses: list[Clause] = []
    by_id: dict[str, Clause] = {}
    heading_stack: list[Clause] = []
    last_numbered: dict[Optional[str], dict[str, Clause]] = {}
    last_clause: Optional[Clause] = None
    current_attachment_clause: Optional[Clause] = None
    counters = {"h": 0, "p": 0, "pre": 0, "t": 0}

    def path_for(parent: Optional[Clause]) -> list[str]:
        if parent is None:
            return [doc_title] if doc_title else []
        base = list(parent.path)
        label = parent.title or (f"п. {parent.number}" if parent.number else parent.id)
        if parent.kind == "heading" and parent.text:
            label = parent.text[:80]
        elif parent.number:
            label = f"п. {parent.number} {parent.text[:60]}".strip()
        return base + [label]

    def add(c: Clause) -> None:
        if c.id in by_id:  # нумерация началась заново (несколько приказов или актов в одном файле)
            n = 2
            while f"{c.id}#{n}" in by_id:
                n += 1
            c.id = f"{c.id}#{n}"
        clauses.append(c)
        by_id[c.id] = c
        if c.parent_id and c.parent_id in by_id:
            by_id[c.parent_id].children.append(c.id)

    if stats.numbered < 3:
        stats.fallback = True
        return _fallback_chunks(doc_id, blocks, doc_title), stats

    for b in blocks:
        if b.kind == "order_marker":
            heading_stack = []
            continue
        if b.kind == "attachment":
            counters["h"] += 1
            cid = b.attachment or f"Прил.{counters['h']}"
            c = Clause(id=cid, doc_id=doc_id, kind="heading", title=b.text, text=b.text, level=0,
                       path=[doc_title] if doc_title else [], page_start=b.page_start, page_end=b.page_end, attachment=b.attachment)
            add(c)
            current_attachment_clause = c
            heading_stack = [c]
            last_numbered[b.attachment] = {}
            last_clause = c
            continue
        if b.kind == "heading":
            counters["h"] += 1
            parent = current_attachment_clause if b.attachment else None
            c = Clause(id=f"h{counters['h']}" + (f"@{b.attachment}" if b.attachment else ""), doc_id=doc_id, kind="heading",
                       title=b.text, text=b.text, level=1 if parent is None else 1, parent_id=parent.id if parent else None,
                       path=path_for(parent), page_start=b.page_start, page_end=b.page_end, attachment=b.attachment)
            add(c)
            heading_stack = ([current_attachment_clause] if current_attachment_clause and b.attachment else []) + [c]
            last_clause = c
            continue
        if b.kind == "clause":
            num = b.number or ""
            parts = num.split(".")
            prefix = f"{b.attachment}:" if b.attachment else ""
            numbered = last_numbered.setdefault(b.attachment, {})
            parent: Optional[Clause] = None
            for k in range(len(parts) - 1, 0, -1):
                cand = ".".join(parts[:k])
                if cand in numbered:
                    parent = numbered[cand]
                    break
            if parent is None and heading_stack:
                parent = heading_stack[-1]
            text = _clause_text_clean(b.text)
            kind = "clause"
            if len(parts) == 1 and (b.styled or _is_caps_heading(text) or (len(text) < 90 and not re.search(r"[.;:]$", text) and text.count(" ") <= 8)):
                kind = "heading"
                # раздел «3» — сосед раздела «2», а не его потомок: иначе в путь пунктов попадает название предыдущего раздела
                if parent is not None and parent.kind == "heading" and parent.number and "." not in parent.number:
                    parent = by_id.get(parent.parent_id) if parent.parent_id else None
            c = Clause(id=prefix + num, doc_id=doc_id, number=num, level=len(parts), kind=kind, title=text if kind == "heading" else None,
                       text=text, parent_id=parent.id if parent else None, path=path_for(parent),
                       page_start=b.page_start, page_end=b.page_end, attachment=b.attachment)
            add(c)
            numbered[num] = c
            if kind == "heading":
                heading_stack = ([current_attachment_clause] if current_attachment_clause and b.attachment else []) + [c]
            last_clause = c
            continue
        if b.kind == "item":
            parent = last_clause if last_clause and last_clause.kind in ("clause", "item", "order_item", "paragraph") else last_clause
            if parent is not None and parent.kind == "item" and parent.parent_id in by_id:
                parent = by_id[parent.parent_id]
            marker = b.marker or "-"
            base = (parent.id if parent else "root")
            idx = sum(1 for x in clauses if x.parent_id == base and x.kind == "item") + 1
            cid = f"{base}.{marker}" if marker != "-" else f"{base}.-{idx}"
            if cid in by_id:
                cid = f"{cid}#{idx}"
            c = Clause(id=cid, doc_id=doc_id, kind="item", level=(parent.level + 1 if parent else 1), text=_clause_text_clean(b.text),
                       parent_id=parent.id if parent else None, path=path_for(parent), page_start=b.page_start, page_end=b.page_end,
                       attachment=b.attachment)
            add(c)
            continue
        if b.kind in ("paragraph", "preamble"):
            parent = heading_stack[-1] if heading_stack else None
            key = "pre" if b.kind == "preamble" else "p"
            if b.kind == "paragraph" and parent is not None and parent.number and parent.kind == "heading":
                # абзац нумерованного раздела: ссылка «п. 2, абз. 1» понятнее сквозного «абз. 7»
                idx = sum(1 for x in clauses if x.parent_id == parent.id and x.kind == "paragraph") + 1
                cid = f"{parent.id}.-{idx}"
            else:
                counters[key] += 1
                cid = f"{key}{counters[key]}" + (f"@{b.attachment}" if b.attachment else "")
            c = Clause(id=cid, doc_id=doc_id, kind=b.kind, level=(parent.level + 1 if parent else 1), text=_clause_text_clean(b.text),
                       parent_id=parent.id if parent else None, path=path_for(parent), page_start=b.page_start, page_end=b.page_end,
                       attachment=b.attachment)
            add(c)
            last_clause = c
            continue
    # пункты после «ПРИКАЗЫВАЮ» в приказе — это пункты приказа
    return clauses, stats


def _fallback_chunks(doc_id: str, blocks: list[Block], doc_title: str, target: int = 1800) -> list[Clause]:
    clauses: list[Clause] = []
    buf: list[Block] = []
    size = 0
    heading = ""
    idx = 0

    def flush() -> None:
        nonlocal buf, size, idx
        if not buf:
            return
        idx += 1
        text = "\n".join(_clause_text_clean(b.text) for b in buf)
        clauses.append(Clause(id=f"p{idx}", doc_id=doc_id, kind="paragraph", text=text, level=1,
                              path=[x for x in (doc_title, heading) if x], page_start=buf[0].page_start, page_end=buf[-1].page_end,
                              attachment=buf[0].attachment))
        buf, size = [], 0

    for b in blocks:
        if b.kind == "order_marker":
            continue
        if b.kind in ("heading", "attachment"):
            flush()
            heading = b.text[:80]
            continue
        if size + len(b.text) > target and buf:
            flush()
        buf.append(b)
        size += len(b.text)
    flush()
    return clauses
