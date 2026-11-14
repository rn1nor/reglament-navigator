"""Загрузка файлов в сырой текст по страницам.

Форматы: PDF (PyMuPDF, OCR при отсутствии текстового слоя), DOCX, XLSX (таблицы построчно), HTML, TXT/MD,
RTF (пакет striprtf, без него встроенный разбор), ODT (разбор content.xml), DOC (antiword -> catdoc -> macOS textutil),
сканы-изображения JPG/PNG/TIFF/BMP (через OCR). Кодировка TXT и HTML определяется автоматически (UTF-8, UTF-16, windows-1251 и др.).
OCR-движки по приоритету: tesseract -> macOS Vision (ocrmac) -> EasyOCR (только явно).
Распознанный текст кэшируется по хешу файла в data/ocr_cache, чтобы повторная индексация была мгновенной.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:  # python.org-сборки macOS не видят системные сертификаты: подсказываем urllib сертификаты certifi
    import certifi  # type: ignore

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except Exception:  # noqa: BLE001
    pass

OCR_MODE = os.environ.get("NAVIGATOR_OCR", "auto")  # auto | tesseract | ocrmac | easyocr | off
OCR_CACHE = Path(os.environ.get("NAVIGATOR_OCR_CACHE", Path(__file__).resolve().parent.parent.parent / "data" / "ocr_cache"))
OCR_DEBUG = bool(os.environ.get("NAVIGATOR_OCR_DEBUG"))


class SkipFile(ValueError):
    """Файл прочитан, но документом базы быть не может (нет текста, изображение без распознанного текста).
    В паспорт базы попадает как пропущенный файл с причиной, а не как ошибка чтения."""


@dataclass
class RawPage:
    number: int
    text: str


@dataclass
class RawDocument:
    source_path: str
    file_type: str
    pages: list[RawPage] = field(default_factory=list)
    needs_ocr: bool = False
    ocr_quality: float = 1.0
    source_url: Optional[str] = None
    title_hint: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    ocr_pages: int = 0
    # строки, набранные полужирным по центру (заголовки разделов без номера); ключ — heading_key(строка)
    heading_lines: set[str] = field(default_factory=set)

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pages)


_WS = re.compile(r"[ \t   ]+")


def normalize_text(s: str) -> str:
    s = s.replace("­", "")  # мягкий перенос
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _WS.sub(" ", s)
    s = re.sub(r"[ ]+\n", "\n", s)
    s = re.sub(r"(?<![A-Za-zА-Яа-я])(?:N[º°]|Nº|No\.?|N\s?o\b)\s*(?=\d)", "№ ", s)  # OCR-варианты знака номера
    s = re.sub(r"[\uf0b7\uf0a7\uf0d8\uf076\uf0fc\uf0a8\uf02d\u25aa\u25e6\u2023\u2043]", "•", s)  # символьные маркеры Word/Symbol
    s = re.sub(r"(?m)^[ \t]*•[ \t]*\n[ \t]*(?=\S)", "• ", s)  # маркер на отдельной строке
    return s


def heading_key(line: str) -> str:
    """Ключ сравнения строки с набором оформленных заголовков: без различий в пробелах."""
    return re.sub(r"\s+", " ", line).strip()


def _bold_centered_lines(page) -> set[str]:
    """Строки страницы PDF, целиком набранные полужирным и выровненные по центру текстовой области.
    Так в регламентах оформляют заголовки разделов без номера («Сроки командировки»); в тексте они неотличимы от продолжения пункта."""
    try:
        blocks = page.get_text("dict").get("blocks", [])
    except Exception:  # noqa: BLE001  нестандартный PDF: работаем без сведений об оформлении
        return set()
    lines: list[tuple[str, bool, float, float]] = []
    for b in blocks:
        for ln in b.get("lines", []):
            spans = [sp for sp in ln.get("spans", []) if sp.get("text", "").strip()]
            if not spans:
                continue
            text = "".join(sp.get("text", "") for sp in ln.get("spans", []))
            bold = all((sp.get("flags", 0) & 16) or re.search(r"bold|demi|black|heavy", sp.get("font", ""), re.I) for sp in spans)
            x0, _, x1, _ = ln["bbox"]
            lines.append((text, bool(bold), x0, x1))
    if len(lines) < 3:
        return set()
    left = min(x0 for _, _, x0, _ in lines)
    right = max(x1 for _, _, _, x1 in lines)
    center = (left + right) / 2
    tol = 0.06 * (page.rect.width or (right - left) or 1)
    out: set[str] = set()
    for text, bold, x0, x1 in lines:
        if bold and abs((x0 + x1) / 2 - center) <= tol and x0 > left + 6:
            key = heading_key(normalize_text(text))
            if 3 <= len(key) <= 160:
                out.add(key)
    return out


def text_quality(s: str) -> float:
    """Доля «нормальных» символов среди непробельных: грубая оценка качества OCR/извлечения."""
    chars = [c for c in s if not c.isspace()]
    if not chars:
        return 0.0
    good = sum(1 for c in chars if c.isalnum() or c in ".,;:()«»\"'-–—№%/§")
    base = good / len(chars)
    words = re.findall(r"\w+", s)
    if words:
        mixed = sum(1 for w in words if re.search(r"[а-яё]", w, re.I) and re.search(r"[a-z]", w, re.I))
        base -= min(0.4, 2.0 * mixed / len(words))
    return max(0.0, min(1.0, base))


# ------------------------------------------------------------------ кодировки текстовых файлов
_CYR_COMMON = frozenset("оеаинтсрвлкмдпуяыьгзбчйхжшюцщэфъё ")
_CYR_ENCODINGS = ("cp1251", "koi8-r", "cp866", "iso8859-5", "mac-cyrillic")
_ENC_LABELS = {"cp1251": "windows-1251", "koi8-r": "KOI8-R", "cp866": "DOS (cp866)", "iso8859-5": "ISO-8859-5", "mac-cyrillic": "MacCyrillic",
               "utf-16": "UTF-16", "utf-16-le": "UTF-16", "utf-16-be": "UTF-16", "utf-32": "UTF-32"}


def decode_text(data: bytes, declared: Optional[str] = None) -> tuple[str, str, bool]:
    """Текст файла в неизвестной кодировке: (текст, кодировка, надёжно ли определена).
    Порядок: BOM -> UTF-16 без BOM -> объявленная в файле (meta charset) -> UTF-8 -> однобайтные кириллические по доле частых букв."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "utf-8", True
    if data[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):
        return data.decode("utf-32", errors="replace"), "utf-32", True
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace"), "utf-16", True
    sample = data[:4000]
    if len(sample) >= 40:  # UTF-16 без BOM: старший байт почти всегда 0x00 (латиница, цифры, пробелы) или 0x04 (кириллица)
        le = sum(1 for b in sample[1::2] if b in (0x00, 0x04)) / len(sample[1::2])
        be = sum(1 for b in sample[0::2] if b in (0x00, 0x04)) / len(sample[0::2])
        if max(le, be) > 0.8:
            enc = "utf-16-le" if le >= be else "utf-16-be"
            return data.decode(enc, errors="replace"), enc, True
    if declared:
        try:
            return data.decode(declared), declared.lower(), True
        except (UnicodeDecodeError, LookupError):
            pass
    try:
        return data.decode("utf-8"), "utf-8", True
    except UnicodeDecodeError:
        pass
    best: Optional[tuple[float, str, str]] = None
    for enc in _CYR_ENCODINGS:
        try:
            text = data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        score = sum(1 for c in text if c in _CYR_COMMON) / max(1, len(text))
        if best is None or score > best[0]:
            best = (score, enc, text)
    if best is not None and best[0] >= 0.5:
        return best[2], best[1], True
    try:  # необязательная зависимость: если установлена, спрашиваем её мнение
        from charset_normalizer import from_bytes  # type: ignore

        guess = from_bytes(data).best()
        if guess is not None and guess.encoding:
            return str(guess), guess.encoding, False
    except Exception:  # noqa: BLE001
        pass
    if best is not None:
        return best[2], best[1], False
    return data.decode("utf-8", errors="replace"), "utf-8", False


def _encoding_note(doc: RawDocument, enc: str, sure: bool) -> None:
    """Предупреждение о нестандартной кодировке: текст прочитан, но проверяющему стоит об этом знать."""
    name = _ENC_LABELS.get(enc, enc)
    if not sure:
        doc.warnings.append(f"кодировка файла не определена надёжно (принята {name}): возможны искажения текста, сверьтесь с оригиналом")
    elif enc.replace("_", "-").lower() not in ("utf-8", "utf8", "ascii"):
        doc.warnings.append(f"кодировка файла {name} определена автоматически")


# ------------------------------------------------------------------ OCR
_engine_cache: Optional[str] = None
_easy_reader = None


def _ocr_engine() -> Optional[str]:
    global _engine_cache
    if _engine_cache is not None:
        return _engine_cache or None
    engine = ""
    if OCR_MODE != "off":
        if OCR_MODE in ("auto", "tesseract"):
            try:
                import pytesseract  # type: ignore

                pytesseract.get_tesseract_version()
                engine = "tesseract"
            except Exception:  # noqa: BLE001
                engine = ""
        if not engine and OCR_MODE in ("auto", "ocrmac"):
            try:
                import ocrmac  # type: ignore  # noqa: F401

                engine = "ocrmac"
            except Exception:  # noqa: BLE001
                engine = ""
        if not engine and OCR_MODE == "easyocr":  # только явно: на CPU около 40 с на страницу
            try:
                import easyocr  # type: ignore  # noqa: F401

                engine = "easyocr"
            except Exception:  # noqa: BLE001
                engine = ""
    _engine_cache = engine
    return engine or None


def _ocrmac_text(img) -> str:
    from ocrmac import ocrmac as _om  # type: ignore

    res = _om.OCR(img, language_preference=["ru-RU", "en-US"], recognition_level="accurate").recognize()
    # элементы: (text, confidence, [x, y, w, h]) в нормированных координатах, начало координат внизу слева
    rows: list[tuple[float, float, str]] = []
    for item in res:
        text, _conf, box = item[0], item[1], item[2]
        x, y, w, h = box
        rows.append((round(1.0 - (y + h / 2), 3), x, text))
    rows.sort(key=lambda r: (r[0], r[1]))
    # группируем в строки по близости вертикальной координаты
    lines: list[str] = []
    cur_y: Optional[float] = None
    cur: list[tuple[float, str]] = []
    for y, x, text in rows:
        if cur_y is None or abs(y - cur_y) <= 0.008:
            cur.append((x, text))
            cur_y = y if cur_y is None else (cur_y + y) / 2
        else:
            lines.append(" ".join(t for _, t in sorted(cur)))
            cur, cur_y = [(x, text)], y
    if cur:
        lines.append(" ".join(t for _, t in sorted(cur)))
    return "\n".join(lines)


def _ocr_page(page) -> Optional[str]:
    engine = _ocr_engine()
    if engine is None:
        return None
    try:
        from PIL import Image  # type: ignore

        pix = page.get_pixmap(dpi=200)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        if engine == "tesseract":
            import pytesseract  # type: ignore

            return pytesseract.image_to_string(img, lang="rus+eng")
        if engine == "ocrmac":
            return _ocrmac_text(img)
        global _easy_reader
        if _easy_reader is None:
            import easyocr  # type: ignore

            _easy_reader = easyocr.Reader(["ru", "en"], gpu=False, verbose=False)
        import numpy as np  # type: ignore

        return "\n".join(_easy_reader.readtext(np.asarray(img), detail=0, paragraph=True))
    except Exception as e:  # noqa: BLE001
        if OCR_DEBUG:
            print(f"[ocr:{engine}] {type(e).__name__}: {e}")
        return None


def _cache_key(path: Path) -> str:
    h = hashlib.sha1()
    h.update(path.read_bytes())
    return h.hexdigest()


# ------------------------------------------------------------------ loaders
def load_pdf(path: Path, file_type: str = "pdf") -> RawDocument:
    import pymupdf  # type: ignore

    doc = RawDocument(source_path=str(path), file_type=file_type)
    cache_file = None
    cached: dict = {}
    try:  # распознанный ранее текст используется и там, где OCR-движка нет (например, в чистом окружении)
        cache_file = OCR_CACHE / f"{_cache_key(path)}.json"
        if cache_file.exists():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
        elif _ocr_engine() is not None:
            OCR_CACHE.mkdir(parents=True, exist_ok=True)
        else:
            cache_file = None
    except (OSError, ValueError):
        cache_file, cached = None, {}
    with pymupdf.open(str(path)) as pdf:
        meta_title = (pdf.metadata or {}).get("title") or None
        if meta_title and len(meta_title) > 5 and not re.search(r"\.(docx?|pdf)$", meta_title, re.I):
            doc.title_hint = meta_title.strip()
        empty_pages = 0
        lost_pages = 0   # страницы-сканы, которые некому распознать: их текст в базу не попал
        maybe_lost = 0   # мало текста и есть изображения, а OCR недоступен: часть текста могла остаться картинкой
        for i, page in enumerate(pdf, start=1):
            txt = page.get_text("text") or ""
            try:
                has_images = bool(page.get_images())
            except Exception:  # noqa: BLE001  не у всех форматов, которые открывает PyMuPDF, есть список изображений
                has_images = file_type != "pdf"
            mixed = 25 <= len(txt.strip()) < 700 and has_images and (_ocr_engine() is not None or str(i) in cached)
            if 25 <= len(txt.strip()) < 700 and has_images and not mixed:
                maybe_lost += 1
            if mixed:
                # смешанная страница: текстовый слой только у грифа или штампа, основной текст картинкой
                ocr = cached.get(str(i))
                if ocr is None:
                    ocr = _ocr_page(page)
                    if ocr is not None and cache_file is not None:
                        cached[str(i)] = ocr
                        cache_file.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
                if ocr and len(ocr.strip()) > 1.5 * len(txt.strip()):
                    txt = ocr
                    doc.ocr_pages += 1
            elif len(txt.strip()) < 25:
                ocr = cached.get(str(i))
                if ocr is None:
                    ocr = _ocr_page(page)
                    if ocr is not None and cache_file is not None:
                        cached[str(i)] = ocr
                        cache_file.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
                if ocr is not None and len(ocr.strip()) >= 25:
                    txt = ocr
                    doc.ocr_pages += 1
                else:
                    empty_pages += 1
                    if ocr is None and (has_images or file_type != "pdf"):
                        lost_pages += 1  # пустая страница без изображений потерей не считается
            else:
                doc.heading_lines |= _bold_centered_lines(page)  # только для страниц с текстовым слоем
            doc.pages.append(RawPage(i, normalize_text(txt)))
        if doc.ocr_pages:
            doc.warnings.append(f"страниц распознано OCR ({_ocr_engine()}): {doc.ocr_pages}")
        repaired = bool(getattr(pdf, "is_repaired", False))
        if repaired and not any(pg.text.strip() for pg in doc.pages):
            # PyMuPDF «чинит» обрезанный файл до пустых страниц: это повреждение, а не скан без текстового слоя
            raise ValueError("файл PDF повреждён: структура нарушена, текст не извлекается")
        if repaired:
            doc.warnings.append("у файла PDF нарушения структуры (восстановлен при чтении): текст мог быть прочитан не полностью, сверьтесь с оригиналом")
        if pdf.page_count and empty_pages / pdf.page_count > 0.5:
            doc.needs_ocr = True
            doc.warnings.append(f"{empty_pages}/{pdf.page_count} страниц без текстового слоя: нужен OCR или разбор моделью")
        elif lost_pages:
            # сканом идёт часть страниц, а OCR недоступен или дал сбой: содержимое потеряно, об этом нужно сказать
            doc.warnings.append(f"{lost_pages} из {pdf.page_count} страниц без текста (скан; OCR недоступен или дал сбой): "
                                f"их содержимое в базу не попало")
        if maybe_lost and not doc.needs_ocr:
            doc.warnings.append(f"{maybe_lost} из {pdf.page_count} страниц с изображениями и малым объёмом текста не распознавались (OCR недоступен): "
                                f"часть текста на них могла остаться сканом")
    doc.ocr_quality = text_quality(doc.text)
    return doc


IMAGE_MIN_BYTES = 20_000  # меньше — значок или логотип, а не скан страницы
IMAGE_MIN_TEXT = 200      # меньше символов после OCR — на изображении нет текста документа


def load_image(path: Path) -> RawDocument:
    """Скан в виде изображения (JPG, PNG, TIFF, BMP): распознаётся тем же OCR, что и страницы PDF без текстового слоя.
    Документом становится только изображение с распознанным текстом; остальные попадают в паспорт базы как пропущенные."""
    if path.stat().st_size < IMAGE_MIN_BYTES:
        raise SkipFile("изображение слишком мало для скана документа")
    doc = load_pdf(path, file_type=path.suffix.lstrip(".").lower())
    if len(doc.text.strip()) < IMAGE_MIN_TEXT:
        if _ocr_engine() is None and not doc.ocr_pages:
            raise SkipFile("изображение (скан): для распознавания нужен OCR (tesseract); сохраните скан как PDF или включите OCR")
        raise SkipFile("на изображении не найден текст документа")
    doc.needs_ocr = False
    return doc


def _docx_heading_like(para) -> bool:
    """Абзац DOCX оформлен как заголовок: по центру и полужирный либо по центру и начинается с номера раздела."""
    try:
        from docx.enum.text import WD_ALIGN_PARAGRAPH  # type: ignore

        align = para.alignment
        if align is None and para.style is not None:
            align = para.style.paragraph_format.alignment
        if align != WD_ALIGN_PARAGRAPH.CENTER:
            return False
        runs = [r for r in para.runs if r.text.strip()]
        style_bold = bool(para.style is not None and para.style.font is not None and para.style.font.bold)
        bold = bool(runs) and all(r.bold or (r.bold is None and style_bold) for r in runs)
        return bold or bool(re.match(r"^\s*\d{1,2}\.\s*[А-ЯЁ]", para.text))
    except Exception:  # noqa: BLE001  нестандартные стили: работаем без сведений об оформлении
        return False


def load_docx(path: Path) -> RawDocument:
    import docx  # type: ignore
    from docx.oxml.ns import qn  # type: ignore

    d = docx.Document(str(path))
    doc = RawDocument(source_path=str(path), file_type="docx")
    pages: list[list[str]] = [[]]
    body = d.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            para = docx.text.paragraph.Paragraph(child, d)
            if child.findall(".//" + qn("w:br") + "[@" + qn("w:type") + "='page']"):
                pages.append([])
            t = para.text
            pages[-1].append(t if t.strip() else "")
            if t.strip() and len(t) <= 200 and _docx_heading_like(para):
                for line in normalize_text(t).splitlines():
                    if line.strip():
                        doc.heading_lines.add(heading_key(line))
        elif tag == "tbl":
            table = docx.table.Table(child, d)
            for row in table.rows:
                cells: list[str] = []
                for cell in row.cells:
                    ct = " ".join(cell.text.split())
                    if not cells or cells[-1] != ct:
                        cells.append(ct)
                pages[-1].append(" | ".join(cells))
            pages[-1].append("")
    core_title = (d.core_properties.title or "").strip()
    if len(core_title) > 5:
        doc.title_hint = core_title
    for i, lines in enumerate(pages, start=1):
        doc.pages.append(RawPage(i, normalize_text("\n".join(lines))))
    doc.ocr_quality = text_quality(doc.text)
    return doc


# ------------------------------------------------------------------ RTF / ODT / DOC
# Группы RTF, содержимое которых не является текстом документа (таблицы шрифтов и стилей, картинки, колонтитулы, служебные поля).
# Группы \listtext и \pntext сюда намеренно не входят: в них лежат номера пунктов автонумерации, без которых не восстановить структуру.
_RTF_DESTINATIONS = frozenset((
    "aftncn aftnsep aftnsepc annotation atnauthor atndate atnicn atnid atnparent atnref atntime atrfend atrfstart author background "
    "bkmkend bkmkstart blipuid buptim category colorschememapping colortbl comment company creatim datafield datastore defchp defpap "
    "do doccomm docvar dptxbxtext ebcend ebcstart factoidname falt fchars ffdeftext ffentrymcr ffexitmcr ffformat ffhelptext ffl "
    "ffname ffstattext file filetbl fldinst fldtype fname fontemb fontfile fonttbl footer footerf footerl footerr footnote "
    "formfield ftncn ftnsep ftnsepc g generator gridtbl header headerf headerl headerr hl hlfr hlinkbase hlloc hlsrc hsv htmltag "
    "info keycode keywords latentstyles lchars levelnumbers leveltext lfolevel linkval list listlevel listname listoverride "
    "listoverridetable listpicture liststylename listtable lsdlockedexcept manager mmath mmathPr nesttableprops nextfile nonesttables "
    "objalias objclass objdata object objname objsect objtime oldcprops oldpprops oldsprops oldtprops oleclsid operator panose "
    "password passwordhash pgp pgptbl picprop pict pn pnseclvl printim private propname protend protstart protusertbl pxe result "
    "revtbl revtim rsidtbl rxe shp shpgrp shpinst shppict shprslt shptxt sn sp staticval stylesheet subject sv svb tc template "
    "themedata title txe ud upr userprops wgrffmtfilter windowcaption writereservation writereservhash xe xform xmlattrname "
    "xmlattrvalue xmlclose xmlname xmlnstbl xmlopen").split())
_RTF_SPECIAL = {"par": "\n", "sect": "\n", "page": "\f", "line": "\n", "tab": "\t", "emdash": "—", "endash": "–", "emspace": " ", "enspace": " ",
                "qmspace": " ", "bullet": "•", "lquote": "‘", "rquote": "’", "ldblquote": "“", "rdblquote": "”", "row": "\n", "nestrow": "\n",
                "cell": " | ", "nestcell": " | "}
_RTF_TOKEN = re.compile(r"\\([a-z]{1,32})(-?\d{1,10})?[ ]?|\\'([0-9a-f]{2})|\\([^a-z])|([{}])|[\r\n]+|(.)", re.I | re.S)


def rtf_to_text(data: bytes) -> str:
    r"""Текст RTF без внешних зависимостей: управляющие слова, группы-назначения, \'hh в кодовой странице документа, \uN.
    Абзац (\par) = строка, разрыв страницы = \f, ячейки таблицы разделены « | »."""
    import codecs

    src = data.decode("latin-1")
    cp = "cp1252"
    m = re.search(r"\\ansicpg(\d+)", src)
    if m:
        cp = f"cp{m.group(1)}"
    try:
        codecs.lookup(cp)
    except LookupError:
        cp = "cp1252"
    if cp == "cp1252" and re.search(r"\\fcharset204(?!\d)", src):
        cp = "cp1251"  # документ объявлен западным, но шрифты русские: байты текста в windows-1251
    stack: list[tuple[int, bool]] = []
    ignorable = False
    ucskip, curskip = 1, 0
    out: list[str] = []
    pending = bytearray()

    def flush() -> None:
        if pending:
            out.append(pending.decode(cp, errors="replace"))
            pending.clear()

    for tok in _RTF_TOKEN.finditer(src):
        word, arg, hexv, char, brace, tchar = tok.groups()
        if brace:
            flush()
            curskip = 0
            if brace == "{":
                stack.append((ucskip, ignorable))
            elif stack:
                ucskip, ignorable = stack.pop()
        elif char:
            curskip = 0
            if char == "*":
                ignorable = True
            elif ignorable:
                continue
            elif char == "~":
                flush()
                out.append("\u00a0")
            elif char in "{}\\":
                flush()
                out.append(char)
            elif char == "_":
                flush()
                out.append("-")
            elif char in "\r\n":
                flush()
                out.append("\n")
        elif word:
            curskip = 0
            if word in _RTF_DESTINATIONS:
                ignorable = True
            elif ignorable:
                continue
            elif word in _RTF_SPECIAL:
                flush()
                out.append(_RTF_SPECIAL[word])
            elif word == "uc":
                ucskip = int(arg) if arg else 1
            elif word == "u":
                code = int(arg) if arg else 0
                if code < 0:
                    code += 0x10000
                flush()
                out.append(chr(code))
                curskip = ucskip
        elif hexv:
            if curskip > 0:
                curskip -= 1
            elif not ignorable:
                pending.append(int(hexv, 16))
        elif tchar:
            if curskip > 0:
                curskip -= 1
            elif not ignorable:
                flush()
                out.append(tchar)
    flush()
    text = "".join(out)
    try:  # суррогатные пары, записанные двумя \uN
        text = text.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        text = text.encode("utf-8", "ignore").decode("utf-8", "ignore")
    lines = [re.sub(r"(\s*\|\s*)+$", "", ln).rstrip() for ln in text.replace("\r", "").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")


def _cyrillic(text: str) -> int:
    return sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch in "ёЁ")


def _rtf_text(path: Path) -> str:
    """RTF: пакет striprtf, если он установлен; встроенный разбор, если пакета нет, он не справился
    или потерял кириллицу (документ объявлен западным, а шрифты русские)."""
    data = path.read_bytes()
    packaged = ""
    try:
        from striprtf.striprtf import rtf_to_text as _strip  # type: ignore

        packaged = _strip(data.decode("latin-1"), errors="ignore")
    except ImportError:
        pass
    except Exception:  # noqa: BLE001  нестандартный RTF
        packaged = ""
    try:
        builtin = rtf_to_text(data)
    except Exception:  # noqa: BLE001
        builtin = ""
    if packaged.strip() and _cyrillic(packaged) >= 0.8 * _cyrillic(builtin):
        return packaged
    return builtin if builtin.strip() else packaged


_ODF_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_ODF_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_ODF_OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"


def odt_to_text(path: Path) -> str:
    r"""Текст ODT стандартной библиотекой: content.xml внутри zip. Абзац = строка, строки таблиц через « | », разрыв страницы = \f."""
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(str(path)) as z:
        root = ET.fromstring(z.read("content.xml"))
    body = root.find(f"{_ODF_OFFICE}body")
    area = body.find(f"{_ODF_OFFICE}text") if body is not None else None
    if area is None:
        area = body if body is not None else root
    lines: list[str] = []

    def inline(el) -> str:
        parts = [el.text or ""]
        for ch in el:
            tag = ch.tag
            if tag == f"{_ODF_TEXT}s":
                parts.append(" " * int(ch.get(f"{_ODF_TEXT}c", "1") or 1))
            elif tag == f"{_ODF_TEXT}tab":
                parts.append("\t")
            elif tag == f"{_ODF_TEXT}line-break":
                parts.append("\n")
            elif tag == f"{_ODF_TEXT}soft-page-break":
                parts.append("\f")
            elif tag in (f"{_ODF_TEXT}note", f"{_ODF_OFFICE}annotation"):
                pass  # сноски и примечания рецензента в текст нормы не входят
            else:
                parts.append(inline(ch))
            parts.append(ch.tail or "")
        return "".join(parts)

    def walk(el) -> None:
        for ch in el:
            tag = ch.tag
            if tag in (f"{_ODF_TEXT}p", f"{_ODF_TEXT}h"):
                lines.append(inline(ch))
            elif tag == f"{_ODF_TABLE}table":
                for row in ch.iter(f"{_ODF_TABLE}table-row"):
                    cells = []
                    for cell in row.findall(f"{_ODF_TABLE}table-cell"):
                        ct = " ".join(" ".join(inline(par).split()) for par in cell.iter() if par.tag in (f"{_ODF_TEXT}p", f"{_ODF_TEXT}h"))
                        if ct and (not cells or cells[-1] != ct):
                            cells.append(ct)
                    if cells:
                        lines.append(" | ".join(cells))
                lines.append("")
            elif tag == f"{_ODF_TEXT}soft-page-break":
                lines.append("\f")
            elif tag in (f"{_ODF_TEXT}tracked-changes", f"{_ODF_OFFICE}forms", f"{_ODF_TEXT}sequence-decls"):
                continue
            else:
                walk(ch)  # разделы, списки и элементы списков

    walk(area)
    return "\n".join(lines)


def _run_tool(cmd: list[str], timeout: int = 120) -> Optional[str]:
    """Вывод внешнего конвертера или None, если его нет, он завершился с ошибкой или завис."""
    try:
        res = subprocess.run(cmd, check=False, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0 or not res.stdout.strip():
        return None
    return res.stdout.decode("utf-8", errors="replace")


def _textutil(path: Path) -> Optional[str]:
    if shutil.which("textutil") is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.txt"
        try:
            subprocess.run(["textutil", "-convert", "txt", "-output", str(out), str(path)], check=False, capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.read_text(encoding="utf-8", errors="ignore") if out.exists() else None


def _doc_text(path: Path) -> str:
    """DOC (Word 97–2003): antiword, затем catdoc, затем macOS textutil. Без переноса строк: абзац = строка."""
    tried = []
    if shutil.which("antiword"):
        tried.append("antiword")
        text = _run_tool(["antiword", "-m", "UTF-8.txt", "-w", "0", str(path)])
        if text and text.strip():
            return text
    if shutil.which("catdoc"):
        tried.append("catdoc")
        text = _run_tool(["catdoc", "-w", "-s", "cp1251", "-d", "utf-8", str(path)])  # 8-битные документы считаем русскими
        if text and text.strip():
            return text
    text = _textutil(path)
    if text is not None:
        tried.append("textutil")
        if text.strip():
            return text
    if not tried:
        raise RuntimeError("для формата DOC нужна утилита antiword или catdoc (на macOS подходит системная textutil): "
                           "установите её или сохраните файл как DOCX или PDF")
    raise ValueError(f"файл DOC не прочитан ({', '.join(tried)} не извлекли текст): файл повреждён или защищён; сохраните его как DOCX или PDF")


def load_legacy_office(path: Path) -> RawDocument:
    """DOC / RTF / ODT. Формат определяется по сигнатуре, а не по расширению: под именем .doc часто лежит RTF или DOCX."""
    ext = path.suffix.lstrip(".").lower()
    head = path.read_bytes()[:8]
    if head.startswith(b"PK\x03\x04") and ext != "odt":
        try:
            with zipfile.ZipFile(str(path)) as z:
                names = set(z.namelist())
        except zipfile.BadZipFile:
            names = set()
        if "word/document.xml" in names:
            doc = load_docx(path)
            doc.warnings.append(f"файл с расширением .{ext} на самом деле DOCX")
            return doc
        if "content.xml" in names:
            ext = "odt"
    kind = "rtf" if head.startswith(b"{\\rtf") else "odt" if ext == "odt" else "doc"
    doc = RawDocument(source_path=str(path), file_type=ext)
    if kind != ext:
        doc.warnings.append(f"файл с расширением .{ext} на самом деле {kind.upper()}")
    if kind == "rtf":
        text = _rtf_text(path)
        if not text.strip():
            text = _textutil(path) or ""
        if not text.strip():
            raise ValueError("файл RTF не содержит текста или повреждён")
    elif kind == "odt":
        try:
            text = odt_to_text(path)
        except (zipfile.BadZipFile, KeyError, SyntaxError) as e:
            text = _textutil(path) or ""
            if not text.strip():
                raise ValueError(f"файл ODT повреждён: {type(e).__name__}") from e
        if not text.strip():
            raise ValueError("файл ODT не содержит текста")
    else:
        if head.lstrip()[:1] == b"<" or head.lower().startswith((b"<html", b"<!doc")):
            html = load_html(path)  # HTML-страница, сохранённая с расширением .doc
            html.file_type = ext
            html.warnings.append(f"файл с расширением .{ext} на самом деле HTML")
            return html
        text = _doc_text(path)
    # разрыв страницы конвертеры ставят как \f
    for i, chunk in enumerate(text.split("\f") or [""], start=1):
        doc.pages.append(RawPage(i, normalize_text(chunk)))
    doc.ocr_quality = text_quality(doc.text)
    return doc


def load_xlsx(path: Path) -> RawDocument:
    import openpyxl  # type: ignore

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    doc = RawDocument(source_path=str(path), file_type="xlsx")
    for i, ws in enumerate(wb.worksheets, start=1):
        lines = [f"Лист: {ws.title}"]
        for row in ws.iter_rows(values_only=True):
            cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
            if cells:
                lines.append(" | ".join(cells))
        doc.pages.append(RawPage(i, normalize_text("\n".join(lines))))
    doc.ocr_quality = text_quality(doc.text)
    return doc


def load_html(path: Path) -> RawDocument:
    from bs4 import BeautifulSoup  # type: ignore

    data = path.read_bytes()
    declared = re.search(rb"""charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", data[:4096])
    html, enc, sure = decode_text(data, declared.group(1).decode("ascii", "ignore") if declared else None)
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        t.decompose()
    title = soup.title.get_text(strip=True) if soup.title else None
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = main.get_text("\n")
    doc = RawDocument(source_path=str(path), file_type="html", title_hint=title)
    _encoding_note(doc, enc, sure)
    doc.pages.append(RawPage(1, normalize_text(re.sub(r"\n{3,}", "\n\n", text))))
    doc.ocr_quality = text_quality(doc.text)
    return doc


def load_txt(path: Path) -> RawDocument:
    text, enc, sure = decode_text(path.read_bytes())
    doc = RawDocument(source_path=str(path), file_type=path.suffix.lstrip(".") or "txt")
    _encoding_note(doc, enc, sure)
    doc.pages.append(RawPage(1, normalize_text(text.replace("\x00", ""))))
    doc.ocr_quality = text_quality(doc.text)
    return doc


LOADERS = {
    ".pdf": load_pdf,
    ".docx": load_docx,
    ".doc": load_legacy_office,
    ".rtf": load_legacy_office,
    ".odt": load_legacy_office,
    ".xlsx": load_xlsx,
    ".xlsm": load_xlsx,
    ".html": load_html,
    ".htm": load_html,
    ".txt": load_txt,
    ".md": load_txt,
    ".jpg": load_image,
    ".jpeg": load_image,
    ".png": load_image,
    ".tif": load_image,
    ".tiff": load_image,
    ".bmp": load_image,
}
IMAGE_EXTENSIONS = frozenset((".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"))


def load_file(path: str | Path) -> RawDocument:
    path = Path(path)
    loader = LOADERS.get(path.suffix.lower())
    if loader is None:
        raise ValueError(f"Неподдерживаемый формат: {path.suffix} ({path.name})")
    return loader(path)
