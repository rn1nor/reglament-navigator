"""Проверки исправлений по итогам аудита надёжности: источники базы, форматы и кодировки, паспорт базы, обходчик сайта, API.
Все тесты быстрые: без моделей поиска, без сети (обходчик ходит на локальный сервер), без OCR."""
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import navigator.ingest.loaders as loaders
from navigator.ingest.crawl import crawl, sniff_ext
from navigator.ingest.loaders import SkipFile, decode_text, load_file, rtf_to_text
from navigator.ingest.pipeline import ingest, load_kb, make_doc_id
from navigator.ingest.reindex import get_sources, rebuild, sources_file
from navigator.llm.base import MockProvider
from navigator.qa.engine import Navigator

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parent.parent
TODAY = dt.date(2026, 11, 3)
POLOZHENIE = (FIX / "polozhenie_otpusk.txt").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_ocr(monkeypatch, tmp_path):
    """OCR в тестах выключен, кэш распознавания не трогаем."""
    monkeypatch.setattr(loaders, "_ocr_engine", lambda: None)
    monkeypatch.setattr(loaders, "OCR_CACHE", tmp_path / "ocr_cache")


def _kinds(kb) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in kb.audit.findings:
        out.setdefault(f.kind, []).append(f.message)
    return out


def _built_kb(tmp_path: Path) -> Path:
    kb_dir = tmp_path / "kb"
    ingest(FIX, kb_dir, today=TODAY)
    return kb_dir


# ---------------------------------------------------------------- 1. источники: база не обнуляется молча
def test_ingest_refuses_missing_or_empty_sources_and_keeps_kb(tmp_path):
    kb_dir = _built_kb(tmp_path)
    before = (kb_dir / "kb.json").read_bytes()
    with pytest.raises(FileNotFoundError, match="не найден"):
        ingest(tmp_path / "does_not_exist", kb_dir, today=TODAY)
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "notes.pptx").write_bytes(b"PK\x03\x04 not a document")
    with pytest.raises(FileNotFoundError, match="нет файлов поддерживаемых форматов"):
        ingest(empty, kb_dir, today=TODAY)
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "broken.pdf").write_bytes(b"%PDF-1.4 this is not really a pdf")
    with pytest.raises(ValueError, match="ни один файл не удалось прочитать"):
        ingest(junk, kb_dir, today=TODAY)
    assert (kb_dir / "kb.json").read_bytes() == before  # существующая база не тронута ни в одном случае
    assert not (tmp_path / "new_kb").exists()
    with pytest.raises(FileNotFoundError):
        ingest(tmp_path / "does_not_exist", tmp_path / "new_kb", today=TODAY)
    assert not (tmp_path / "new_kb").exists()  # и пустой каталог базы не создаётся


def test_rebuild_refuses_unavailable_declared_source(tmp_path):
    kb_dir = _built_kb(tmp_path)
    sources_file(kb_dir).write_text(json.dumps([str(tmp_path / "raw_gone")]), encoding="utf-8")
    kb_before, src_before = (kb_dir / "kb.json").read_bytes(), sources_file(kb_dir).read_bytes()
    assert get_sources(kb_dir) == [tmp_path / "raw_gone"]  # недоступный источник не отбрасывается молча
    with pytest.raises(FileNotFoundError, match="источник базы недоступен"):
        rebuild(kb_dir)
    assert (kb_dir / "kb.json").read_bytes() == kb_before and sources_file(kb_dir).read_bytes() == src_before
    assert len(load_kb(kb_dir).documents) == 3


def test_rebuild_with_explicit_sources_keeps_interface_uploads(tmp_path, monkeypatch):
    from navigator import config
    from navigator.index.dense import HashEmbedder

    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr("navigator.ingest.reindex.get_embedder", lambda: HashEmbedder())
    kb_dir = tmp_path / "kb" / "demo"
    main = tmp_path / "raw" / "demo"
    main.mkdir(parents=True)
    (main / "polozhenie.txt").write_text(POLOZHENIE, encoding="utf-8")
    uploads = tmp_path / "raw" / "demo_uploads"  # сюда интерфейс кладёт загруженные файлы
    uploads.mkdir()
    (uploads / "prikaz.txt").write_text((FIX / "prikaz_izmeneniya.txt").read_text(encoding="utf-8"), encoding="utf-8")
    summary = rebuild(kb_dir, [main], with_contradictions=False)  # явный список, как в make rebuild-org
    assert summary["documents"] == 2  # загруженный через интерфейс приказ не выброшен
    assert {Path(d.source_path).name for d in load_kb(kb_dir).documents} == {"polozhenie.txt", "prikaz.txt"}
    assert json.loads(sources_file(kb_dir).read_text(encoding="utf-8")) == [str(main)]  # папка загрузок подхватывается сама, в список не пишется


def test_ingest_script_exits_nonzero_without_touching_kb(tmp_path):
    kb_dir = _built_kb(tmp_path)
    before = (kb_dir / "kb.json").read_bytes()
    env = dict(os.environ, NAVIGATOR_OCR="off", NAVIGATOR_USE_DENSE="off", NAVIGATOR_USE_RERANKER="off")
    res = subprocess.run([sys.executable, str(ROOT / "scripts" / "ingest.py"), "--src", str(tmp_path / "no_such_dir"), "--out", str(kb_dir)],
                         capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=120)
    assert res.returncode == 2 and "не найден" in res.stderr and "База не изменена" in res.stderr
    assert (kb_dir / "kb.json").read_bytes() == before


# ---------------------------------------------------------------- 2. одноимённые файлы
def test_same_file_name_in_two_folders_gets_distinct_stable_ids(tmp_path):
    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    (src / "b").mkdir()
    (src / "c").mkdir()
    (src / "a" / "good.txt").write_text(POLOZHENIE, encoding="utf-8")
    (src / "b" / "good.txt").write_text((FIX / "prikaz_staryi.txt").read_text(encoding="utf-8"), encoding="utf-8")
    (src / "c" / "copy.txt").write_text(POLOZHENIE, encoding="utf-8")  # тот же файл под другим именем
    kb = ingest(src, tmp_path / "kb", today=TODAY)
    ids = [d.id for d in kb.documents]
    assert len(ids) == 2 and len(set(ids)) == 2  # дубликат по содержимому не стал третьим документом
    assert ids[0] == make_doc_id(src / "a" / "good.txt")  # прежний идентификатор остаётся за первым файлом
    assert ids[1].startswith(ids[0] + "-")
    kinds = _kinds(kb)
    assert "same_name" in kinds and "src/b/good.txt" in kinds["same_name"][0]
    assert "duplicate_file" in kinds and "src/c/copy.txt" in kinds["duplicate_file"][0]
    again = ingest(src, tmp_path / "kb2", today=TODAY)
    assert [d.id for d in again.documents] == ids  # суффикс устойчив между сборками


# ---------------------------------------------------------------- 3. DOC, RTF, ODT без macOS textutil
BS = chr(92)  # обратная косая черта: управляющие слова RTF собираем из частей, чтобы в исходнике не было записей вида «\\uXXXX»


def _rtf_unicode(text: str) -> bytes:
    """Текст в юникодной записи RTF: каждая не-ASCII буква как управляющее слово u с десятичным кодом и запасным символом «?»."""
    return "".join(f"{BS}u{ord(ch)}?" if ord(ch) > 127 else ch for ch in text).encode("ascii")


RTF_CP1251 = (rb"{\rtf1\ansi\ansicpg1251\deff0{\fonttbl{\f0\froman\fcharset204 Times New Roman;}}{\colortbl;\red0\green0\blue0;}"
              rb"{\info{\title \'d1\'ea\'f0\'fb\'f2\'fb\'e9 \'e7\'e0\'e3\'ee\'eb\'ee\'e2\'ee\'ea}}"
              rb"\pard\qc\b \'cf\'ce\'cb\'ce\'c6\'c5\'cd\'c8\'c5 \'ee\'e1 \'ee\'f2\'ef\'f3\'f1\'ea\'e0\'f5\b0\par "
              rb"{\listtext\pard\plain 1.\tab}\'d0\'e0\'e1\'ee\'f2\'ed\'e8\'ea \'ef\'ee\'e4\'e0\'b8\'f2 \'e7\'e0\'ff\'e2\'eb\'e5\'ed\'e8\'e5.\par "
              rb"{\listtext\pard\plain 2.\tab}" + (BS + "uc1").encode("ascii") + _rtf_unicode("Срок — 14 дней.") + rb"\par "
              rb"{\*\generator Riched20 10.0}}")


def _minimal_odt(path: Path) -> None:
    content = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
               '<office:body><office:text>'
               '<text:h text:outline-level="1">ПОЛОЖЕНИЕ об отпусках</text:h>'
               '<text:p>1. Работник подаёт<text:s text:c="2"/>заявление <text:span>не позднее</text:span> чем за 14 дней.</text:p>'
               '<text:list><text:list-item><text:p>2. Заявление согласует руководитель.</text:p></text:list-item></text:list>'
               '<table:table><table:table-row><table:table-cell><text:p>Срок</text:p></table:table-cell>'
               '<table:table-cell><text:p>14 дней</text:p></table:table-cell></table:table-row></table:table>'
               '<text:p>3. Приказ издаётся в течение 3 рабочих дней.</text:p>'
               '</office:text></office:body></office:document-content>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", content)


def test_rtf_is_read_without_external_tools(tmp_path, monkeypatch):
    text = rtf_to_text(RTF_CP1251)
    assert "ПОЛОЖЕНИЕ об отпусках" in text
    assert "1.\tРаботник подаёт заявление." in text  # номер пункта из автонумерации сохранён, «ё» из \'b8
    assert "2.\tСрок — 14 дней." in text  # \uN с пропуском запасного символа
    assert "Скрытый заголовок" not in text and "Times New Roman" not in text and "Riched20" not in text
    monkeypatch.setattr(loaders.shutil, "which", lambda name: None)  # как в контейнере: ни textutil, ни antiword
    p = tmp_path / "polozhenie.rtf"
    p.write_bytes(RTF_CP1251)
    raw = load_file(p)
    assert raw.file_type == "rtf" and "Работник подаёт заявление" in raw.text and not raw.needs_ocr
    western = RTF_CP1251.replace(rb"\ansicpg1251", rb"\ansicpg1252")  # документ объявлен западным, шрифт русский
    assert "Работник" in rtf_to_text(western)
    p.write_bytes(western)
    assert "Работник подаёт заявление" in load_file(p).text
    monkeypatch.setitem(sys.modules, "striprtf.striprtf", None)  # пакета striprtf нет: работает встроенный разбор
    p.write_bytes(RTF_CP1251)
    assert "2. Срок — 14 дней." in load_file(p).text


def test_odt_is_read_with_stdlib(tmp_path, monkeypatch):
    monkeypatch.setattr(loaders.shutil, "which", lambda name: None)
    p = tmp_path / "polozhenie.odt"
    _minimal_odt(p)
    raw = load_file(p)
    lines = [ln for ln in raw.text.splitlines() if ln.strip()]
    assert lines[0] == "ПОЛОЖЕНИЕ об отпусках"
    assert lines[1] == "1. Работник подаёт заявление не позднее чем за 14 дней."  # text:s и вложенный span
    assert "2. Заявление согласует руководитель." in lines and "Срок | 14 дней" in lines
    broken = tmp_path / "broken.odt"
    broken.write_bytes(b"PK\x03\x04 broken zip")
    with pytest.raises(ValueError, match="ODT повреждён"):
        load_file(broken)


def test_doc_without_converter_is_reported_with_true_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(loaders.shutil, "which", lambda name: None)
    src = tmp_path / "src"
    src.mkdir()
    (src / "polozhenie.txt").write_text(POLOZHENIE, encoding="utf-8")
    (src / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 2048)  # настоящий контейнер OLE
    kb = ingest(src, tmp_path / "kb", today=TODAY)
    assert len(kb.documents) == 1 and not any(d.needs_ocr for d in kb.documents)  # пустого «документа со сканом» больше нет
    errs = _kinds(kb)["read_error"]
    assert len(errs) == 1 and "old.doc" in errs[0] and "antiword" in errs[0] and "OCR" not in errs[0]


def test_doc_is_converted_with_antiword_then_catdoc(tmp_path, monkeypatch):
    """В контейнере DOC читается утилитой antiword (или catdoc): проверяем порядок и параметры вызова без самих утилит."""
    calls: list[list[str]] = []

    class Done:
        def __init__(self, out: str):
            self.returncode, self.stdout = (0, out.encode("utf-8")) if out else (1, b"")

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return Done("ПОЛОЖЕНИЕ об отпусках\n1. Работник подаёт заявление не позднее чем за 14 дней.\n" if cmd[0] == available[-1] else "")

    doc = tmp_path / "old.doc"
    doc.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 2048)
    monkeypatch.setattr(loaders.subprocess, "run", fake_run)
    available = ["antiword"]
    monkeypatch.setattr(loaders.shutil, "which", lambda name: f"/usr/bin/{name}" if name in available else None)
    raw = load_file(doc)
    assert "Работник подаёт заявление" in raw.text and raw.file_type == "doc" and not raw.needs_ocr
    assert calls == [["antiword", "-m", "UTF-8.txt", "-w", "0", str(doc)]]  # без переноса строк: абзац остаётся одной строкой
    calls.clear()
    available = ["antiword", "catdoc"]  # antiword не справился: пробуем catdoc
    raw = load_file(doc)
    assert "Работник подаёт заявление" in raw.text and [c[0] for c in calls] == ["antiword", "catdoc"] and "-w" in calls[1]
    calls.clear()
    available = ["antiword"]
    monkeypatch.setattr(loaders.subprocess, "run", lambda cmd, **kw: Done(""))  # утилита есть, но текст не извлекла
    with pytest.raises(ValueError, match="DOC не прочитан"):
        load_file(doc)


def test_doc_extension_with_other_content_is_detected_by_signature(tmp_path, monkeypatch):
    import docx

    monkeypatch.setattr(loaders.shutil, "which", lambda name: None)
    as_rtf = tmp_path / "really_rtf.doc"
    as_rtf.write_bytes(RTF_CP1251)
    raw = load_file(as_rtf)
    assert "Работник подаёт заявление" in raw.text and any("на самом деле RTF" in w for w in raw.warnings)
    d = docx.Document()
    d.add_paragraph("ПОЛОЖЕНИЕ о командировках")
    d.add_paragraph("1. Работник направляется в командировку приказом ректора.")
    as_docx = tmp_path / "really_docx.doc"
    d.save(str(as_docx.with_suffix(".docx")))
    as_docx.with_suffix(".docx").rename(as_docx)
    raw = load_file(as_docx)
    assert "направляется в командировку" in raw.text and any("на самом деле DOCX" in w for w in raw.warnings)


# ---------------------------------------------------------------- 4. кодировки
def test_text_encodings_are_detected(tmp_path):
    for enc, label in (("cp1251", "windows-1251"), ("koi8-r", "KOI8-R"), ("utf-16", "UTF-16"), ("utf-16-le", "UTF-16")):
        p = tmp_path / f"polozhenie_{enc}.txt"
        p.write_bytes(POLOZHENIE.encode(enc, errors="replace"))  # в KOI8-R нет знака «№»: он заменится на «?»
        raw = load_file(p)
        assert "ПОЛОЖЕНИЕ" in raw.text and "календарных дней" in raw.text, enc
        assert raw.ocr_quality > 0.9, enc
        assert any(label in w for w in raw.warnings), (enc, raw.warnings)
    plain = tmp_path / "utf8.txt"
    plain.write_text(POLOZHENIE, encoding="utf-8")
    assert load_file(plain).warnings == []  # обычный UTF-8 без предупреждений
    bom = tmp_path / "bom.txt"
    bom.write_bytes(b"\xef\xbb\xbf" + POLOZHENIE.encode("utf-8"))
    assert load_file(bom).text.lstrip().startswith(POLOZHENIE.lstrip()[:20]) and load_file(bom).warnings == []
    text, enc, sure = decode_text("Регламент согласования договоров".encode("cp1251"))
    assert text == "Регламент согласования договоров" and enc == "cp1251" and sure


def test_html_in_cp1251_is_read(tmp_path):
    body = "".join(f"<p>2.{i}. Договор согласовывается в течение {i} рабочих дней.</p>" for i in range(1, 6))
    html = f'<html><head><meta http-equiv="Content-Type" content="text/html; charset=windows-1251"><title>Регламент</title></head><body><h1>РЕГЛАМЕНТ согласования договоров</h1>{body}</body></html>'
    for name, data in (("declared.html", html.encode("cp1251")), ("undeclared.html", html.replace("charset=windows-1251", "").encode("cp1251"))):
        p = tmp_path / name
        p.write_bytes(data)
        raw = load_file(p)
        assert "РЕГЛАМЕНТ согласования договоров" in raw.text and "рабочих дней" in raw.text, name
        assert raw.title_hint == "Регламент" and any("windows-1251" in w for w in raw.warnings), name


# ---------------------------------------------------------------- 5. частично отсканированные и повреждённые PDF
def _make_pdf(path: Path, pages: list) -> None:
    """pages: строка — страница с текстом, "IMG" — страница-скан (только изображение), None — пустая страница."""
    import pymupdf

    doc = pymupdf.open()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 60), False)
    pix.clear_with(200)
    for item in pages:
        page = doc.new_page()
        if item == "IMG":
            page.insert_image(pymupdf.Rect(50, 50, 500, 700), pixmap=pix)
        elif item:
            page.insert_textbox(pymupdf.Rect(50, 50, 550, 780), item, fontsize=11)
    doc.save(str(path))
    doc.close()


def test_partially_scanned_pdf_reports_lost_pages(tmp_path):
    text = "1. The employee submits a request to the department head within three working days of the event. " * 3
    src = tmp_path / "src"
    src.mkdir()
    _make_pdf(src / "partial.pdf", [text, text, text, "IMG", "IMG", None])
    raw = load_file(src / "partial.pdf")
    assert not raw.needs_ocr and len(raw.pages) == 6
    lost = [w for w in raw.warnings if "страниц без текста" in w]
    assert len(lost) == 1 and lost[0].startswith("2 из 6")  # пустая страница без изображения потерей не считается
    kb = ingest(src, tmp_path / "kb", today=TODAY)
    found = _kinds(kb).get("partial_ocr", [])
    assert len(found) == 1 and "2 из 6" in found[0]
    _make_pdf(src / "scan.pdf", ["IMG", "IMG", "IMG"])
    assert load_file(src / "scan.pdf").needs_ocr  # полностью отсканированный документ по-прежнему помечается «нужен OCR»


def test_truncated_pdf_is_a_read_error_not_a_scan(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "polozhenie.txt").write_text(POLOZHENIE, encoding="utf-8")
    good = tmp_path / "good.pdf"
    _make_pdf(good, ["1. The employee submits a request to the department head. " * 5] * 4)
    data = good.read_bytes()
    (src / "truncated.pdf").write_bytes(data[: len(data) // 2])
    kb = ingest(src, tmp_path / "kb", today=TODAY)
    assert [Path(d.source_path).name for d in kb.documents] == ["polozhenie.txt"]
    kinds = _kinds(kb)
    assert "needs_ocr" not in kinds and len(kinds["read_error"]) == 1 and "truncated.pdf" in kinds["read_error"][0]


# ---------------------------------------------------------------- 6. файлы, которые конвейер не берёт
def _noise_png(path: Path, side: int) -> None:
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, side, side, os.urandom(side * side * 3), False)
    pix.save(str(path))


def test_unsupported_and_skipped_files_are_listed_in_audit(tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "polozhenie.txt").write_text(POLOZHENIE, encoding="utf-8")
    (src / "slides.pptx").write_bytes(b"PK\x03\x04 presentation")
    (src / "sub" / "download_6041518b.bin").write_bytes(b"\x00\x01\x02 unknown")
    (src / "table.xls").write_bytes(b"\xd0\xcf\x11\xe0 old excel")
    _noise_png(src / "icon.png", 16)  # значок: слишком мал для скана
    _noise_png(src / "scan.png", 220)  # скан-картинка, а OCR недоступен
    for service in (".DS_Store", "manifest.json", "fetch.log", "~$polozhenie.docx"):
        (src / service).write_text("{}" if service.endswith(".json") else "service", encoding="utf-8")
    kb = ingest(src, tmp_path / "kb", today=TODAY)
    assert len(kb.documents) == 1
    kinds = _kinds(kb)
    unsupported = " ".join(kinds["unsupported_file"])
    assert len(kinds["unsupported_file"]) == 3
    assert "src/slides.pptx" in unsupported and "src/sub/download_6041518b.bin" in unsupported and "src/table.xls" in unsupported
    assert "сохраните таблицу как XLSX" in unsupported
    skipped = " ".join(kinds["skipped_file"])
    assert "icon.png (изображение слишком мало" in skipped and "scan.png" in skipped and "нужен OCR" in skipped
    assert "read_error" not in kinds  # служебные и временные файлы не считаются ни документами, ни ошибками
    assert kb.audit.counts["skipped_files"] == 5
    with pytest.raises(SkipFile):
        load_file(src / "icon.png")


def _docx_bytes(tmp_path: Path) -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph("ПОЛОЖЕНИЕ о командировках")
    for i in range(1, 40):
        d.add_paragraph(f"{i}. Работник направляется в командировку приказом ректора не позднее чем за {i} дней.")
    p = tmp_path / "tmp_docx_source.docx"
    d.save(str(p))
    return p.read_bytes()


def test_crawler_detects_file_type_by_content(tmp_path):
    docx_data = _docx_bytes(tmp_path)
    pdf_data = b"%PDF-1.4\n" + b"0" * 4000
    pptx_path = tmp_path / "slides.pptx"
    with zipfile.ZipFile(pptx_path, "w") as z:
        z.writestr("ppt/presentation.xml", "<p/>" * 800)
        z.writestr("[Content_Types].xml", "<Types/>")
    error_page = ("<html><head><title>404</title></head><body>" + "<p>Страница не найдена</p>" * 120 + "</body></html>").encode("utf-8")
    index = ('<html><head><title>Документы</title></head><body>'
             '<a href="/download?id=5">Положение о командировках</a>'
             '<a href="/getfile.php?id=7">Приказ о командировках</a>'
             '<a href="/files/9">Регламент закупок (презентация)</a>'
             '<a href="/docs/missing.pdf">Порядок оплаты</a></body></html>').encode()
    routes = {
        "/index.html": ("text/html; charset=utf-8", index),
        "/download?id=5": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", docx_data),
        "/getfile.php?id=7": ("application/octet-stream", pdf_data),  # тип в заголовке не указан: решает сигнатура
        "/files/9": ("application/octet-stream", pptx_path.read_bytes()),
        "/docs/missing.pdf": ("text/html; charset=utf-8", error_page),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            ctype, body = routes.get(self.path, ("text/plain", b""))
            self.send_response(200 if self.path in routes else 404)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        res = crawl([f"http://127.0.0.1:{srv.server_address[1]}/index.html"], tmp_path / "out", max_depth=1, delay=0.0)
    finally:
        srv.shutdown()
    files = sorted(s["file"] for s in res.saved)
    assert len(files) == 2 and files[0].endswith(".docx") and files[1].endswith(".pdf")  # не .bin и не .php
    assert len(res.unsupported) == 1 and "/files/9" in res.unsupported[0]
    assert any("missing.pdf" in e and "HTML" in e for e in res.errors)
    assert "неподдерживаемых файлов: 1" in res.summary()
    kb = ingest(tmp_path / "out", tmp_path / "kb", today=TODAY)  # сохранённый DOCX читается конвейером
    assert any(d.file_type == "docx" and d.source_url.endswith("/download?id=5") for d in kb.documents)
    assert sniff_ext(b"{\\rtf1 text", "application/octet-stream") == ".rtf" and sniff_ext(b"\x00\x01", "application/octet-stream") is None


# ---------------------------------------------------------------- 7. пустые и мусорные файлы, повреждённый manifest.json
def test_empty_and_junk_files_do_not_become_documents(tmp_path):
    import openpyxl

    src = tmp_path / "src"
    src.mkdir()
    (src / "polozhenie.txt").write_text(POLOZHENIE, encoding="utf-8")
    wb = openpyxl.Workbook()
    wb.active.title = "Пустой"
    wb.create_sheet("Тоже пустой")
    wb.save(str(src / "empty.xlsx"))
    (src / "garbage.txt").write_bytes(bytes(range(256)) * 40)
    (src / "numbers.txt").write_text("12345 67890\n111 222 333\n", encoding="utf-8")
    (src / "blank.txt").write_text("   \n\n", encoding="utf-8")
    (src / "manifest.json").write_text("{ this is not json", encoding="utf-8")
    kb = ingest(src, tmp_path / "kb", today=TODAY)
    assert [Path(d.source_path).name for d in kb.documents] == ["polozhenie.txt"]
    kinds = _kinds(kb)
    assert "manifest_error" in kinds and "src/manifest.json" in kinds["manifest_error"][0]
    messages = " ".join(m for ms in kinds.values() for m in ms)
    for name in ("empty.xlsx", "garbage.txt", "numbers.txt", "blank.txt"):
        assert name in messages, name
    assert "в таблице нет данных" in messages
    assert str(tmp_path) not in kb.audit.model_dump_json()  # в паспорте базы нет абсолютных путей машины


# ---------------------------------------------------------------- 8–11. API
@pytest.fixture()
def api(tmp_path, monkeypatch):
    """Клиент API на базе из фикстур, без моделей поиска. Состояние модуля сбрасывается перед каждым тестом."""
    from fastapi.testclient import TestClient

    import navigator.api.app as app_module

    kb_dir = _built_kb(tmp_path)
    monkeypatch.setenv("NAVIGATOR_KB", str(kb_dir))
    created: list[Path] = []

    def light(kb):
        created.append(Path(kb))
        return Navigator(kb, provider=MockProvider(), dense="hash", reranker="off")

    monkeypatch.setattr(app_module, "Navigator", light)
    for name in ("_nav", "_nav_dir", "_nav_mtime", "_nav_error", "_failed_mtime"):
        monkeypatch.setattr(app_module, name, None)
    client = TestClient(app_module.app, raise_server_exceptions=False)
    client.kb_dir, client.created, client.module = kb_dir, created, app_module  # type: ignore[attr-defined]
    return client


QUESTION = "Как работнику подать заявление на ежегодный отпуск и за сколько дней?"


def test_api_rejects_unknown_role_with_422(api):
    r = api.post("/ask", json={"question": QUESTION, "role": "директор"})
    assert r.status_code == 422 and "неизвестная роль" in r.text and "staff" in r.text
    assert api.post("/ask", json={"question": QUESTION, "role": "staff"}).json()["role"] == "staff"
    assert api.post("/ask", json={"question": QUESTION, "role": "Студент"}).json()["role"] == "student"  # русское название роли
    ok = api.post("/ask", json={"question": QUESTION, "role": ""})
    assert ok.status_code == 200 and "Техническая ошибка" not in ok.json()["summary"]
    assert api.post("/ask", json={"question": QUESTION, "as_of": "вчера"}).status_code == 422
    assert api.get("/navigator/директор").status_code == 422
    assert api.get("/navigator/staff").status_code == 200 and api.get("/navigator/staff?days=-5").status_code == 422


def test_api_health_always_answers_and_other_endpoints_return_503(api, tmp_path, monkeypatch):
    monkeypatch.setenv("NAVIGATOR_KB", str(tmp_path / "no_such_kb"))
    h = api.get("/health")
    assert h.status_code == 200 and h.json()["status"] == "degraded" and h.json()["ready"] is False and "база знаний недоступна" in h.json()["reason"]
    for call in (lambda: api.post("/ask", json={"question": QUESTION}), lambda: api.get("/documents"), lambda: api.get("/audit"), lambda: api.get("/approved")):
        r = call()
        assert r.status_code == 503 and "база знаний недоступна" in r.json()["detail"]
    bad = tmp_path / "bad_kb"
    bad.mkdir()
    (bad / "kb.json").write_text("{ broken", encoding="utf-8")
    monkeypatch.setenv("NAVIGATOR_KB", str(bad))
    assert api.get("/health").json()["status"] == "degraded" and api.get("/documents").status_code == 503
    monkeypatch.setenv("NAVIGATOR_KB", str(api.kb_dir))
    h = api.get("/health")
    assert h.status_code == 200 and h.json()["status"] == "ok" and h.json()["documents"] == 3 and h.json()["ready"] is True


def test_api_survives_broken_auxiliary_files(api):
    (api.kb_dir / "approved.json").write_text("{not json", encoding="utf-8")
    (api.kb_dir / "contradictions.json").write_text("[{", encoding="utf-8")
    h = api.get("/health")
    assert h.status_code == 200 and h.json()["status"] == "ok" and any("повреждён" in w for w in h.json()["warnings"])
    assert api.post("/ask", json={"question": QUESTION, "role": "staff"}).status_code == 200
    assert api.get("/approved").json() == [] and api.get("/contradictions").json() == []
    assert (api.kb_dir / "approved.json.broken").exists() and (api.kb_dir / "contradictions.json.broken").exists()
    (api.kb_dir / "contradictions.json").write_text(json.dumps([{"a": 1}, "мусор"]), encoding="utf-8")  # записи неверного вида
    from navigator.ingest.contradictions import load_contradictions

    assert load_contradictions(api.kb_dir) == {}


def test_api_loads_navigator_once_and_reloads_after_rebuild(api, tmp_path):
    def slow(kb):
        time.sleep(0.2)
        api.created.append(Path(kb))
        return Navigator(kb, provider=MockProvider(), dense="hash", reranker="off")

    api.module.Navigator = slow  # возвращается фикстурой monkeypatch
    threads = [threading.Thread(target=api.module.nav) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(api.created) == 1  # восемь одновременных первых запросов загрузили базу один раз
    assert api.get("/health").json()["documents"] == 3 and len(api.created) == 1
    two = tmp_path / "two"
    two.mkdir()
    for name in ("polozhenie_otpusk.txt", "prikaz_izmeneniya.txt"):
        (two / name).write_text((FIX / name).read_text(encoding="utf-8"), encoding="utf-8")
    ingest(two, api.kb_dir, today=TODAY)  # базу пересобрал другой процесс
    stamp = time.time() + 5
    os.utime(api.kb_dir / "kb.json", (stamp, stamp))
    assert api.get("/health").json()["documents"] == 2 and len(api.created) == 2  # база перечитана без перезапуска сервиса
    (api.kb_dir / "kb.json").write_text("{ broken", encoding="utf-8")  # новая версия не читается: отвечаем по прежней и сообщаем об этом
    h = api.get("/health").json()
    assert h["status"] == "degraded" and h["documents"] == 2 and "не загружена" in h["reason"]
    assert api.post("/ask", json={"question": QUESTION, "role": "staff"}).status_code == 200


def test_api_approve_requires_grounded_answer_and_signature(api):
    fake = {"outcome": "answer", "question": QUESTION, "as_of": "2026-11-03", "summary": "ВЫДУМАННЫЙ ОТВЕТ: заявление подавать не нужно."}
    r = api.post("/approve", json={"response": fake, "approved_by": "Иванова И.И."})
    assert r.status_code == 422 and "нет шагов" in r.json()["detail"]
    real = api.post("/ask", json={"question": QUESTION, "role": "staff"}).json()
    assert real["steps"]
    assert api.post("/approve", json={"response": real, "approved_by": "  "}).status_code == 422  # без подписи
    forged = json.loads(json.dumps(real))
    forged["steps"][0]["citations"][0]["clause_id"] = "99.9"
    r = api.post("/approve", json={"response": forged, "approved_by": "Иванова И.И."})
    assert r.status_code == 422 and "99.9" in r.json()["detail"]
    forged = json.loads(json.dumps(real))
    forged["steps"][0]["citations"][0]["quote"] = "текст, которого в пункте нет вовсе"
    assert api.post("/approve", json={"response": forged, "approved_by": "Иванова И.И."}).status_code == 422
    refusal = api.post("/ask", json={"question": "Какой размер повышенной стипендии и как её получить?"}).json()
    assert api.post("/approve", json={"response": refusal, "approved_by": "Иванова И.И."}).status_code == 422
    assert api.get("/approved").json() == []  # ничего из отклонённого не сохранилось
    ok = api.post("/approve", json={"response": real, "approved_by": "Иванова И.И., Управление кадров"})
    assert ok.status_code == 200 and ok.json()["id"]
    rows = api.get("/approved").json()
    assert len(rows) == 1 and rows[0]["статус"] == "действует" and rows[0]["кто"].startswith("Иванова")


def test_approved_store_is_shared_between_processes(tmp_path):
    from navigator.index.dense import HashEmbedder
    from navigator.qa.approved import ApprovedStore

    kb_dir = _built_kb(tmp_path)
    nav = Navigator(kb_dir, provider=MockProvider(), dense="hash", reranker="off")
    answer = nav.ask(QUESTION, role="staff")
    assert answer.steps
    other = ApprovedStore(kb_dir / "approved.json", HashEmbedder())  # второй процесс: интерфейс рядом с API
    assert other.entries == []
    nav.approve(answer, approved_by="Иванова И.И., Управление кадров")
    assert len(other.status_table(nav.kb)) == 1  # подтверждение, сделанное другим процессом, подхвачено
    assert other.match(QUESTION, "staff") is not None
    other.remove(other.entries[0].id)
    time.sleep(0.01)
    assert nav.ask(QUESTION, role="staff").trace.get("provider") != "approved"  # и удаление тоже
    with pytest.raises(ValueError, match="нельзя подтвердить"):
        nav.approved.add(answer.model_copy(update={"steps": []}), nav.kb, "Иванова И.И.")
