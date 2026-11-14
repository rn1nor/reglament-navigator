"""Конвейер индексации: файлы -> документы с пунктами -> связи -> граф -> статусы -> аудит -> kb.json."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

from ..schemas import AuditFinding, Document, KnowledgeBase, Relation
from .compound import split_compound
from .graph import DocumentGraph
from .loaders import IMAGE_EXTENSIONS, RawDocument, SkipFile, load_file
from .metadata import detect_owner_unit, detect_roles, detect_topics, detect_type, extract_effective, extract_header, extract_relations, extract_title
from .patches import extract_patches
from .structure import build_clauses

SUPPORTED = {".pdf", ".docx", ".doc", ".rtf", ".odt", ".xlsx", ".xlsm", ".html", ".htm", ".txt", ".md", *IMAGE_EXTENSIONS}
# служебные файлы источников: в базу не идут и в паспорте не упоминаются
SERVICE_NAMES = {"manifest.json", "thumbs.db", "desktop.ini", "readme.md", "readme.txt"}
SERVICE_EXT = {".log", ".tmp", ".part", ".crdownload", ".lock", ".broken", ".gitkeep"}
UNSUPPORTED_HINTS = {
    ".xls": "сохраните таблицу как XLSX", ".ppt": "сохраните презентацию как PDF", ".pptx": "сохраните презентацию как PDF",
    ".djvu": "сохраните документ как PDF", ".zip": "распакуйте архив в папку источника", ".rar": "распакуйте архив в папку источника",
    ".7z": "распакуйте архив в папку источника", ".bin": "тип файла не определён: переименуйте с верным расширением",
    ".php": "тип файла не определён: переименуйте с верным расширением", "": "файл без расширения: переименуйте с верным расширением",
    ".gif": "изображение GIF не распознаётся: сохраните скан как PDF, JPG или PNG", ".heic": "сохраните скан как PDF, JPG или PNG",
    ".webp": "сохраните скан как PDF, JPG или PNG",
}
MAX_SKIP_FINDINGS = 40


def make_doc_id(path: Path) -> str:
    base = re.sub(r"[^\w]+", "_", path.stem, flags=re.U).strip("_").lower()[:48]
    h = hashlib.sha1(str(path.name).encode("utf-8")).hexdigest()[:6]
    return f"{base}-{h}"


def build_document(raw: RawDocument, path: Path, url: Optional[str] = None, listing_title: Optional[str] = None,
                   doc_id: Optional[str] = None, part: str = "main") -> Document:
    text = raw.text
    doc_id = doc_id or make_doc_id(path)
    doc_type = detect_type(text, path.name if part == "main" else "")
    if part == "attachment" and doc_type in ("приказ", "распоряжение"):
        doc_type = "положение"
    header = extract_header(text, doc_type, path.name if part == "main" else "")
    number, dt = header.number, header.date
    title = extract_title(text, doc_type, path.name, hint=raw.title_hint or listing_title)
    if doc_type in ("приказ", "распоряжение") and not re.match(r"^(О|Об)\s", title, re.I) and listing_title and re.match(r"^(О|Об)\s", listing_title):
        title = listing_title
    if part == "attachment" and listing_title and not re.match(r"^(О|Об|Приказ)\b", listing_title) and (len(title) < 12 or title.lower().startswith(("приложение", "утвержден"))):
        title = listing_title
    doc = Document(
        id=doc_id, source_path=str(path), source_url=url, file_type=raw.file_type, title=title, doc_type=doc_type,
        number=number, date=dt, pages=len(raw.pages), ocr_quality=round(raw.ocr_quality, 3), needs_ocr=raw.needs_ocr,
        text_hash=hashlib.sha1(re.sub(r"\s+", " ", text).strip().encode("utf-8")).hexdigest(), warnings=list(raw.warnings),
    )
    doc.effective_from, doc.effective_to = extract_effective(text, dt)
    if header.date_partial:
        doc.warnings.append(f"в грифе указан только год ({dt.year}): точная дата не определена")
    if header.source:
        doc.warnings.append(f"номер и дата: {header.source}")
    doc.roles = detect_roles(text, title)  # type: ignore[assignment]
    doc.topics = detect_topics(text, title)
    doc.owner_unit = detect_owner_unit(text)
    clauses, stats = build_clauses(doc_id, raw, doc_title=title)
    doc.clauses = clauses
    if stats.fallback:
        doc.warnings.append("нумерация не распознана: абзацные чанки")
    doc.relations = extract_relations(doc, clauses)
    if header.approved_number and not any(r.type == "approved_by" and r.target_number == header.approved_number for r in doc.relations):
        doc.relations.insert(0, Relation(type="approved_by", source_doc_id=doc.id, target_ref=f"утверждён приказом № {header.approved_number}",
                                         target_kind="приказ", target_number=header.approved_number, target_date=header.approved_date,
                                         clause_id=clauses[0].id if clauses else None, confidence=0.95))
    if doc_type in ("приказ", "распоряжение") or any(r.type == "amends" for r in doc.relations):
        doc.patches = extract_patches(doc, clauses)
    return doc


def build_documents(raw: RawDocument, path: Path, url: Optional[str] = None, listing_title: Optional[str] = None,
                    base_id: Optional[str] = None) -> list[Document]:
    """Один файл может содержать приказ и утверждённые им акты: возвращаем список логических документов.
    base_id задаёт идентификатор, когда имя файла уже занято одноимённым файлом из другой папки."""
    parts = split_compound(raw)
    base_id = base_id or make_doc_id(path)
    docs: list[Document] = []
    main = build_document(parts[0][0], path, url=url, listing_title=listing_title if len(parts) == 1 else None, doc_id=base_id, part="main")
    docs.append(main)
    for n, (sub, _role) in enumerate(parts[1:], start=1):
        att = build_document(sub, path, url=url, listing_title=listing_title, doc_id=f"{base_id}-att{n}", part="attachment")
        att.approved_by = main.id
        appr = next((r for r in att.relations if r.type == "approved_by" and r.target_number), None)
        if appr and not main.number:
            main.number = appr.target_number  # первая страница приказа отсутствует, номер виден в грифе приложения
        if main.date is None and appr and appr.target_number == main.number and appr.target_date:
            main.date = appr.target_date
        att.relations = [r for r in att.relations if not (r.type == "approved_by" and r.target_number and r.target_number == main.number)]
        if main.effective_from and (att.date is None or any("только год" in w for w in att.warnings)):
            att.date = main.effective_from
            att.warnings = [w for w in att.warnings if "только год" not in w]
        if main.date and any("только год" in w for w in att.warnings) and att.date and att.date.year == main.date.year:
            att.date = main.date  # в грифе только год, точная дата есть у утверждающего приказа
            att.warnings = [w for w in att.warnings if "только год" not in w]
        att.effective_from = att.effective_from or main.effective_from
        att.date = att.date or main.date
        if main.date is None and att.date:
            main.date = att.date
        att.warnings.append(f"выделен из файла приказа {main.number or ''} как приложение {n}")
        att.relations.insert(0, Relation(type="approved_by", source_doc_id=att.id, target_doc_id=main.id, target_ref=f"приложение к приказу {main.number or ''}",
                                         target_kind="приказ", target_number=main.number, target_date=main.date, confidence=1.0, method="manual"))
        main.relations.append(Relation(type="approves", source_doc_id=main.id, target_doc_id=att.id, target_ref=att.title[:200], target_kind=att.doc_type,
                                       target_title=att.title, confidence=1.0, method="manual"))
        docs.append(att)
    return docs


def _is_service(p: Path, root: Optional[Path] = None) -> bool:
    """Скрытые и служебные файлы (в том числе временные файлы Word «~$...») документами не считаются."""
    rel = p.relative_to(root).parts if root is not None else (p.name,)
    if any(part.startswith(".") for part in rel) or p.name.startswith("~$"):
        return True
    return p.name.lower() in SERVICE_NAMES or p.suffix.lower() in SERVICE_EXT


def iter_files(src: Path) -> list[Path]:
    if src.is_file():
        return [src]
    return sorted(p for p in src.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED and not _is_service(p, src))


def unsupported_files(src: Path) -> list[Path]:
    """Файлы источника, которые конвейер не умеет читать: о каждом сообщается в паспорте базы."""
    if not src.is_dir():
        return []
    return sorted(p for p in src.rglob("*") if p.is_file() and p.suffix.lower() not in SUPPORTED and not _is_service(p, src))


def display_path(path: Path, root: Optional[Path] = None) -> str:
    """Путь для сообщений: относительно папки-источника (с её именем), без абсолютных путей машины."""
    if root is not None and root.is_dir():
        try:
            return str(Path(root.name) / path.relative_to(root))
        except ValueError:
            pass
    return path.name


def _error_text(e: Exception, path: Path, shown: str) -> str:
    """Причина для паспорта базы: наши сообщения как есть, чужие исключения с именем класса; абсолютные пути заменяются относительными."""
    msg = str(e).strip() or type(e).__name__
    if not (isinstance(e, (ValueError, RuntimeError)) and re.search(r"[а-яё]", msg, re.I)):
        msg = f"{type(e).__name__}: {msg}"
    for full in {str(path), str(path.resolve()), str(path.absolute())}:
        msg = msg.replace(full, shown)
    return msg.replace(str(path.resolve().parent) + os.sep, "").replace(str(path.parent) + os.sep, "")


def _reject_reason(raw: RawDocument) -> Optional[str]:
    """Почему прочитанный файл не может быть документом базы (пустой, без слов, нечитаемый мусор). None, если всё в порядке."""
    if raw.needs_ocr:
        return None  # скан без текста остаётся в базе с пометкой «нужен OCR»
    body = re.sub(r"(?m)^Лист: .*$", "", raw.text).strip()
    if not body:
        return "в таблице нет данных" if raw.file_type in ("xlsx", "xlsm") else "файл пуст: текст не извлекается"
    control = sum(1 for ch in body if ord(ch) < 32 and ch not in "\t\n\r\f")
    if control > 0.02 * len(body):
        return "файл не похож на текст: в нём двоичные данные (проверьте формат и расширение файла)"
    if len(re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", body)) < 3:
        return "в файле нет связного текста"
    if raw.ocr_quality < 0.4:
        return f"текст не читается (доля осмысленных символов {raw.ocr_quality:.2f}): файл повреждён или в неизвестной кодировке"
    return None


def ingest(src: Path | list[Path], out_dir: Path, today: Optional[date] = None, progress: Optional[Callable[[str], None]] = None,
           enrich: Optional[Callable[[Document, RawDocument], None]] = None,
           transcribe: Optional[Callable[[Path, RawDocument], RawDocument]] = None, allow_empty: bool = False) -> KnowledgeBase:
    """Собирает базу из источников и сохраняет kb.json. Существующая база не трогается, если источники недоступны,
    в них нет файлов поддерживаемых форматов или ни один файл не прочитан (allow_empty=True отключает последнюю проверку)."""
    sources = [Path(x) for x in (src if isinstance(src, (list, tuple)) else [src])]
    out_dir = Path(out_dir)
    missing = [str(s_) for s_ in sources if not s_.exists()]
    if missing or not sources:
        raise FileNotFoundError("источник документов не найден: " + (", ".join(missing) or "список источников пуст")
                                + ". База не изменена. Проверьте путь; публичные документы mipt.ru скачиваются командой make fetch.")
    manifest: dict = {}
    files: list[tuple[Path, Path]] = []  # (файл, его источник)
    notes: list[AuditFinding] = []  # находки паспорта базы, не привязанные к прочитанному документу
    skipped = 0

    def skip(kind: str, message: str, severity: str = "warning") -> None:
        nonlocal skipped
        skipped += 1
        if skipped <= MAX_SKIP_FINDINGS:
            notes.append(AuditFinding(kind=kind, severity=severity, doc_id=None, message=message))  # type: ignore[arg-type]

    for s_ in sources:
        mp = s_ / "manifest.json" if s_.is_dir() else None
        if mp and mp.exists():
            try:
                loaded = json.loads(mp.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("ожидался объект")
                manifest.update({k: v for k, v in loaded.items() if isinstance(v, dict)})
            except (ValueError, OSError):
                notes.append(AuditFinding(kind="manifest_error", severity="warning", doc_id=None,
                                          message=f"{display_path(mp, s_)} повреждён: названия и ссылки на оригиналы из него не использованы"))
        files.extend((f, s_) for f in iter_files(s_))
        for u in unsupported_files(s_):
            ext = u.suffix.lower()
            hint = UNSUPPORTED_HINTS.get(ext, "поддерживаются PDF, DOCX, DOC, RTF, ODT, XLSX, HTML, TXT и сканы JPG, PNG, TIFF")
            skip("unsupported_file", f"файл пропущен: {display_path(u, s_)} (формат {ext or 'без расширения'} не поддерживается; {hint})")
    if not files:
        raise FileNotFoundError("в источниках нет файлов поддерживаемых форматов: " + ", ".join(str(s_) for s_ in sources) + ". База не изменена.")
    out_dir.mkdir(parents=True, exist_ok=True)
    docs: list[Document] = []
    read_errors: list[tuple[str, str]] = []
    used_ids: dict[str, str] = {}
    seen_bytes: dict[str, str] = {}
    for path, root in files:
        shown = display_path(path, root)
        try:
            digest = hashlib.sha1(path.read_bytes()).hexdigest()
            if digest in seen_bytes:
                skip("duplicate_file", f"файл пропущен: {shown} совпадает с {seen_bytes[digest]} байт в байт", severity="info")
                continue
            raw = load_file(path)
            reason = _reject_reason(raw)
            if reason:
                raise SkipFile(reason) if raw.text.strip() else ValueError(reason)
            seen_bytes[digest] = shown
        except SkipFile as e:
            skip("skipped_file", f"файл пропущен: {shown} ({e})")
            if progress:
                progress(f"-- {shown}: {e}")
            continue
        except Exception as e:  # noqa: BLE001  повреждённый или нечитаемый файл не останавливает индексацию
            read_errors.append((shown, _error_text(e, path, shown)))
            if progress:
                progress(f"!! {shown}: {_error_text(e, path, shown)}")
            continue
        entry = manifest.get(path.name, {})
        if raw.needs_ocr and transcribe is not None:
            try:
                raw = transcribe(path, raw)
            except Exception as e:  # noqa: BLE001
                raw.warnings.append(f"расшифровка скана не удалась: {e}")
        base_id = make_doc_id(path)
        if base_id in used_ids:
            # одноимённый файл из другой папки: прежний идентификатор остаётся за первым, второй получает устойчивый суффикс от пути
            first = used_ids[base_id]
            suffix = hashlib.sha1(shown.encode("utf-8")).hexdigest()[:4]
            cand, n = f"{base_id}-{suffix}", 2
            while cand in used_ids:
                cand, n = f"{base_id}-{suffix}{n}", n + 1
            base_id = cand
            notes.append(AuditFinding(kind="same_name", severity="warning", doc_id=base_id,
                                      message=f"файл {shown} называется так же, как {first}, но содержит другой текст: документу присвоен отдельный идентификатор"))
        used_ids[base_id] = shown
        for doc in build_documents(raw, path, url=entry.get("url"), listing_title=entry.get("title"), base_id=base_id):
            if enrich is not None:
                try:
                    enrich(doc, raw)
                except Exception as e:  # noqa: BLE001
                    doc.warnings.append(f"LLM-экстракция не удалась: {e}")
            docs.append(doc)
            if progress:
                progress(f"ok {path.name}: {doc.doc_type} | {doc.title[:70]} | пунктов {len(doc.clauses)} | связей {len(doc.relations)} | патчей {len(doc.patches)}")
    if skipped > MAX_SKIP_FINDINGS:
        notes.append(AuditFinding(kind="skipped_file", severity="warning", doc_id=None, message=f"и ещё пропущено файлов: {skipped - MAX_SKIP_FINDINGS}"))
    if not docs and not allow_empty:
        first = "; ".join(f"{n}: {e}" for n, e in read_errors[:3]) or "; ".join(f.message for f in notes[:3])
        raise ValueError(f"ни один файл не удалось прочитать ({first}). База не изменена.")
    graph = DocumentGraph(docs, today=today)
    graph.resolve()
    graph.apply_patches()
    graph.compute_statuses()
    audit = graph.audit(read_errors, extra=notes)
    kb = KnowledgeBase(documents=docs, built_at=datetime.now().isoformat(timespec="seconds"), audit=audit, stubs=list(graph.stubs.values()))
    save_kb(kb, out_dir)
    return kb


def save_kb(kb: KnowledgeBase, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "kb.json"
    tmp = out_dir / "kb.json.tmp"
    tmp.write_text(kb.model_dump_json(indent=1), encoding="utf-8")
    os.replace(tmp, p)  # атомарно: читатели не увидят наполовину записанный файл
    return p


def load_kb(kb_dir: Path) -> KnowledgeBase:
    p = Path(kb_dir) / "kb.json" if Path(kb_dir).is_dir() else Path(kb_dir)
    return KnowledgeBase.model_validate_json(p.read_text(encoding="utf-8"))
