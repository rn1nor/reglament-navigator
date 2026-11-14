"""Скачивает публичные локальные нормативные акты МФТИ со страниц раскрытия информации mipt.ru.

Использование: python scripts/fetch_mipt_docs.py [--out data/raw/mipt] [--limit 120]
Пишет manifest.json: файл -> url, заголовок ссылки, страница-источник.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from html import unescape
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx

LISTING_PAGES = [
    ("https://mipt.ru/sveden/document/", None),
    ("https://mipt.ru/sveden/paid_edu/", None),
    ("https://mipt.ru/sveden/employees/", None),
    ("https://mipt.ru/sveden/vacant/", None),
    ("https://mipt.ru/sveden/education/", None),
    ("https://mipt.ru/sveden/grants/", None),
    ("https://mipt.ru/sveden/common/", None),
    ("https://mipt.ru/sveden/inter/", None),
    ("https://mipt.ru/sveden/struct/", 40),  # положения о подразделениях: берём ограниченно
]

KEEP = re.compile(
    r"полож|правил|регламент|порядок|приказ|устав|инструкц|коллективн|кодекс|изменен|договор|"
    r"положение|стипенд|отпуск|перевод|отчисл|восстанов|практик|оплат|стоимост|режим|норм|"
    r"polozh|pravil|reglament|poryadok|prikaz|ustav|instrukc|kollektiv|kodeks|izmenen|dogovor|stipend|otpusk|perevod|praktik|oplat|rezhim",
    re.I,
)
DROP = re.compile(r"самообслед|otchet|отчет|отчёт|доход|dohod|план\s+фин|plan_fin|pfhd|бюджет|smeta|смета|licen|лиценз|akkred|аккред|svidetel|свидетел", re.I)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; reglament-navigator/0.1; research)"}


def slugify(s: str) -> str:
    s = unquote(s)
    s = re.sub(r"[^\w\-.]+", "_", s, flags=re.U)
    return s.strip("_")[:120]


def extract_links(html: str, base: str) -> list[tuple[str, str]]:
    out = []
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, flags=re.S | re.I):
        href, inner = m.group(1), m.group(2)
        if not re.search(r"\.(pdf|docx?)(\?|$)", href, re.I):
            continue
        text = re.sub(r"<[^>]+>", " ", unescape(inner))
        text = re.sub(r"\s+", " ", text).strip()
        out.append((urljoin(base, href), text))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/raw/mipt")
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--max-mb", type=float, default=30.0)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    manifest: dict[str, dict] = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    client = httpx.Client(headers=HEADERS, timeout=60, follow_redirects=True)
    seen_urls = {v["url"] for v in manifest.values()}
    total = len(manifest)
    for page_url, per_page_limit in LISTING_PAGES:
        try:
            r = client.get(page_url)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            print(f"!! {page_url}: {e}", file=sys.stderr)
            continue
        links = extract_links(r.text, page_url)
        picked = 0
        for url, text in links:
            if total >= args.limit:
                break
            if per_page_limit is not None and picked >= per_page_limit:
                break
            name = Path(urlparse(url).path).name
            hay = f"{text} {name}"
            if DROP.search(hay) or not KEEP.search(hay):
                continue
            if url in seen_urls:
                continue
            try:
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    size = int(resp.headers.get("content-length") or 0)
                    if size and size > args.max_mb * 1024 * 1024:
                        print(f"-- skip big {url} ({size/1e6:.1f} MB)")
                        continue
                    ext = Path(urlparse(url).path).suffix.lower() or ".pdf"
                    h = hashlib.sha1(url.encode()).hexdigest()[:8]
                    fname = f"{slugify(Path(name).stem)}_{h}{ext}"
                    data = b"".join(resp.iter_bytes())
            except Exception as e:  # noqa: BLE001
                print(f"!! {url}: {e}", file=sys.stderr)
                continue
            if len(data) < 2000:
                continue
            (out / fname).write_bytes(data)
            manifest[fname] = {"url": url, "title": text, "listing": page_url, "bytes": len(data)}
            seen_urls.add(url)
            picked += 1
            total += 1
            print(f"ok {fname} <- {text[:70]}")
            time.sleep(0.3)
        print(f"== {page_url}: picked {picked}, total {total}")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"manifest: {manifest_path} ({len(manifest)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
