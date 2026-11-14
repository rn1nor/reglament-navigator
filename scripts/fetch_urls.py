"""Скачивает документы по списку ссылок (организаторы могут выдать «источники» ссылками).

python scripts/fetch_urls.py --list urls.txt --out data/raw/org
Строка файла: URL [| заголовок]. HTML-страницы сохраняются как .html, файлы — как есть. Пишет manifest.json.
"""
import sys
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; reglament-navigator/0.1)"}


def slugify(s: str) -> str:
    s = re.sub(r"[^\w\-.]+", "_", unquote(s), flags=re.U)
    return s.strip("_")[:100] or "doc"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    mpath = out / "manifest.json"
    manifest = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {}
    client = httpx.Client(headers=HEADERS, timeout=90, follow_redirects=True)
    for line in Path(args.list).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        url, _, title = line.partition("|")
        url, title = url.strip(), title.strip()
        try:
            r = client.get(url)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            print(f"!! {url}: {e}")
            continue
        ctype = r.headers.get("content-type", "").lower()
        name = Path(urlparse(url).path).name or "index"
        ext = Path(name).suffix.lower()
        if "html" in ctype and ext not in (".pdf", ".docx", ".doc", ".xlsx"):
            ext = ".html"
        elif not ext:
            ext = ".pdf" if "pdf" in ctype else ".bin"
        h = hashlib.sha1(url.encode()).hexdigest()[:8]
        fname = f"{slugify(Path(name).stem)}_{h}{ext}"
        (out / fname).write_bytes(r.content)
        manifest[fname] = {"url": url, "title": title or name, "bytes": len(r.content)}
        print(f"ok {fname} <- {url}")
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"manifest: {mpath} ({len(manifest)} файлов)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
