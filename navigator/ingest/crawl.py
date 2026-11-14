"""Загрузка источников по ссылкам: отдельные документы, страницы и обход разделов сайта (например, mipt.ru/sveden/document/).

Собирает файлы регламентов (PDF, DOCX, DOC, RTF, XLSX) и HTML-страницы, похожие на нормативные акты, в папку с manifest.json,
которую затем читает обычный ingest. Обход вежливый: одна область сайта, ограничение глубины и числа страниц, пауза между запросами.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urljoin, urlparse

import httpx

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; reglament-navigator/0.2; +internal use)"}
DOC_EXT = re.compile(r"\.(pdf|docx?|rtf|xlsx?|odt)(\?|$)", re.I)
# ссылки на выгрузку без расширения в адресе: /download?id=5, getfile.php?id=7, /files/123
DOWNLOAD_HINT = re.compile(r"(download|getfile|get_file|attachment|file\.php|/files?/|/docs?/\d|export=download)", re.I)
SAVE_EXT = {".pdf", ".docx", ".doc", ".rtf", ".odt", ".xlsx", ".xlsm", ".txt"}  # что умеет читать конвейер индексации
CTYPE_EXT = {
    "application/pdf": ".pdf", "application/msword": ".doc", "application/rtf": ".rtf", "text/rtf": ".rtf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.oasis.opendocument.text": ".odt", "text/plain": ".txt",
}


def sniff_ext(data: bytes, ctype: str = "", url_ext: str = "") -> Optional[str]:
    """Расширение файла по сигнатуре и Content-Type, а не по адресу ссылки. None — тип не определён или не поддерживается;
    ".html" — по ссылке пришла HTML-страница (например, страница ошибки вместо документа)."""
    head = data[:8]
    ctype = (ctype or "").split(";")[0].strip().lower()
    url_ext = (url_ext or "").lower()
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"{\\rtf"):
        return ".rtf"
    if head.startswith(b"\xd0\xcf\x11\xe0"):  # контейнер OLE: старые DOC и XLS
        return ".doc" if url_ext in ("", ".doc", ".bin", ".php") and "excel" not in ctype and "spreadsheet" not in ctype else None
    if head.startswith(b"PK\x03\x04"):
        try:
            names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        except zipfile.BadZipFile:
            return None
        if "word/document.xml" in names:
            return ".docx"
        if "xl/workbook.xml" in names:
            return ".xlsm" if url_ext == ".xlsm" else ".xlsx"
        if "content.xml" in names and "mimetype" in names:
            return ".odt"
        return None  # презентация или просто архив
    probe = data[:400].lstrip().lower()
    if probe.startswith((b"<!doctype html", b"<html")) or b"<head" in probe or "html" in ctype:
        return ".html"
    if ctype in CTYPE_EXT:
        return CTYPE_EXT[ctype]
    if url_ext == ".txt" or ctype.startswith("text/plain"):
        return ".txt"
    return None
KEEP_HINT = re.compile(
    r"полож|правил|регламент|порядок|приказ|устав|инструкц|коллективн|кодекс|изменен|договор|стипенд|отпуск|перевод|отчисл|восстанов|практик|"
    r"оплат|стоимост|режим|норм|команд|отчет|отчёт|закуп|polozh|pravil|reglament|poryadok|prikaz|ustav|instrukc|kollektiv|kodeks|izmenen|dogovor",
    re.I,
)
DROP_HINT = re.compile(r"самообслед|otchet_o_samoobsled|доход|dohod|план\s+фин|plan_fin|pfhd|смета|smeta|licen|лиценз|akkred|аккред|svidetel|свидетел|фото|foto|news|новост", re.I)
REG_PAGE = re.compile(r"(ПОЛОЖЕНИЕ|РЕГЛАМЕНТ|ПОРЯДОК|ПРАВИЛА|ПРИКАЗ|ИНСТРУКЦИЯ|Положение о|Регламент|Порядок|Правила|Приказываю|ПРИКАЗЫВАЮ)")


@dataclass
class CrawlResult:
    saved: list[dict] = field(default_factory=list)
    visited_pages: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    manifest_path: Optional[Path] = None
    unsupported: list[str] = field(default_factory=list)  # ссылки, по которым пришёл файл неподдерживаемого или неопределённого типа

    def summary(self) -> str:
        extra = f"; неподдерживаемых файлов: {len(self.unsupported)}" if self.unsupported else ""
        return (f"страниц просмотрено: {self.visited_pages}; файлов и страниц сохранено: {len(self.saved)}; пропущено: {self.skipped}; "
                f"ошибок: {len(self.errors)}{extra}")


def _slug(s: str) -> str:
    s = re.sub(r"[^\w\-.]+", "_", unquote(s), flags=re.U)
    return s.strip("_")[:100] or "doc"


def _links(html: str, base: str) -> list[tuple[str, str]]:
    out = []
    for m in re.finditer(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', html, flags=re.S | re.I):
        href, inner = m.group(1), m.group(2)
        if href.startswith(("mailto:", "javascript:", "tel:")):
            continue
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(inner))).strip()
        out.append((urljoin(base, href), text))
    return out


def _page_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", unescape(m.group(1))).strip()[:200] if m else ""


def crawl(start_urls: list[str], out_dir: Path, max_depth: int = 1, max_pages: int = 60, max_files: int = 300, keep_all_files: bool = False,
          save_pages: bool = True, delay: float = 0.3, progress: Optional[Callable[[str], None]] = None) -> CrawlResult:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    mpath = out_dir / "manifest.json"
    manifest: dict = {}
    if mpath.exists():
        try:
            loaded = json.loads(mpath.read_text(encoding="utf-8"))
            manifest = {k: v for k, v in loaded.items() if isinstance(v, dict)} if isinstance(loaded, dict) else {}
        except (ValueError, OSError):  # повреждённый список прошлых загрузок не мешает новой: начинаем с пустого, старый откладываем
            mpath.replace(mpath.with_name("manifest.json.broken"))
    known_urls = {v.get("url") for v in manifest.values()}
    res = CrawlResult(manifest_path=mpath)
    client = httpx.Client(headers=HEADERS, timeout=60, follow_redirects=True)
    queue: list[tuple[str, int]] = [(u.strip(), 0) for u in start_urls if u.strip()]
    seen_pages: set[str] = set()
    domains = {urlparse(u).netloc for u, _ in queue}
    path_prefixes = {urlparse(u).path.rsplit("/", 1)[0] for u, _ in queue}

    def save_bytes(url: str, data: bytes, title: str, listing: str, ext: str) -> None:
        h = hashlib.sha1(url.encode()).hexdigest()[:8]
        name = Path(urlparse(url).path).name or "index"
        fname = f"{_slug(Path(name).stem)}_{h}{ext}"
        (out_dir / fname).write_bytes(data)
        manifest[fname] = {"url": url, "title": title or name, "listing": listing, "bytes": len(data)}
        known_urls.add(url)
        res.saved.append({"file": fname, "url": url, "title": title})
        if progress:
            progress(f"ok {fname} <- {title[:60] or url}")

    while queue and res.visited_pages < max_pages and len(res.saved) < max_files:
        url, depth = queue.pop(0)
        if url in seen_pages:
            continue
        seen_pages.add(url)
        try:
            r = client.get(url)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            res.errors.append(f"{url}: {e}")
            continue
        ctype = r.headers.get("content-type", "").lower()
        time.sleep(delay)
        kind = sniff_ext(r.content, ctype, Path(urlparse(url).path).suffix)
        if kind != ".html":
            # прямая ссылка на файл: тип определяем по содержимому и Content-Type, а не по адресу
            if url in known_urls:
                res.skipped += 1
                continue
            if kind not in SAVE_EXT:
                res.unsupported.append(f"{url}: тип файла не поддерживается или не определён ({ctype.split(';')[0] or 'Content-Type не указан'})")
                continue
            save_bytes(url, r.content, Path(urlparse(url).path).stem, url, kind)
            continue
        res.visited_pages += 1
        html = r.text
        title = _page_title(html)
        links = _links(html, url)
        if progress:
            progress(f"== {url} ({len(links)} ссылок)")
        # страница сама может быть регламентом (опубликован текстом)
        text_len = len(re.sub(r"<[^>]+>", " ", html))
        if save_pages and depth > 0 and REG_PAGE.search(html) and text_len > 2500 and url not in known_urls:
            save_bytes(url, r.content, title, url, ".html")
        for href, text in links:
            if len(res.saved) >= max_files:
                break
            pu = urlparse(href)
            if pu.scheme not in ("http", "https") or pu.netloc not in domains:
                continue
            by_ext = bool(DOC_EXT.search(href))
            # ссылка на выгрузку без расширения в адресе: берём, если по тексту ссылки это похоже на нормативный документ
            by_hint = not by_ext and bool(DOWNLOAD_HINT.search(href)) and bool(KEEP_HINT.search(text)) and href not in seen_pages
            if by_ext or by_hint:
                if href in known_urls:
                    res.skipped += 1
                    continue
                hay = f"{text} {Path(pu.path).name}"
                if not keep_all_files and (DROP_HINT.search(hay) or not KEEP_HINT.search(hay)):
                    res.skipped += 1
                    continue
                try:
                    seen_pages.add(href)
                    fr = client.get(href)
                    fr.raise_for_status()
                    if len(fr.content) < 2000 or len(fr.content) > 40 * 1024 * 1024:
                        res.skipped += 1
                        continue
                    fkind = sniff_ext(fr.content, fr.headers.get("content-type", ""), Path(pu.path).suffix)
                    if fkind == ".html":
                        if by_ext:
                            res.errors.append(f"{href}: вместо документа пришла HTML-страница")
                        elif depth < max_depth:
                            queue.append((href, depth + 1))  # это обычная страница: обойдём её в общем порядке
                            seen_pages.discard(href)
                        continue
                    if fkind not in SAVE_EXT:
                        res.unsupported.append(f"{href}: тип файла не поддерживается или не определён "
                                               f"({fr.headers.get('content-type', '').split(';')[0] or 'Content-Type не указан'})")
                        continue
                    save_bytes(href, fr.content, text, url, fkind)
                    time.sleep(delay)
                except Exception as e:  # noqa: BLE001
                    res.errors.append(f"{href}: {e}")
            elif depth < max_depth and href not in seen_pages and any(pu.path.startswith(p) for p in path_prefixes) and not re.search(r"\.(jpg|png|gif|css|js|ico|svg|zip|mp4)(\?|$)", href, re.I):
                queue.append((href, depth + 1))
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return res
