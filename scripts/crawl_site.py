"""python scripts/crawl_site.py --url https://mipt.ru/sveden/document/ [--depth 1] [--out data/raw/org_web] [--max-pages 60] [--all]
Обходит раздел сайта (в пределах того же пути), скачивает регламенты и страницы с их текстом; затем: scripts/ingest.py --src data/raw/org_web ..."""
import sys
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
import argparse
from pathlib import Path

from navigator.ingest.crawl import crawl

ap = argparse.ArgumentParser()
ap.add_argument("--url", required=True, nargs="+")
ap.add_argument("--out", default="data/raw/org_web")
ap.add_argument("--depth", type=int, default=1)
ap.add_argument("--max-pages", type=int, default=60)
ap.add_argument("--max-files", type=int, default=300)
ap.add_argument("--all", action="store_true", help="брать все файлы, а не только похожие на регламенты по названию")
args = ap.parse_args()
res = crawl(args.url, Path(args.out), max_depth=args.depth, max_pages=args.max_pages, max_files=args.max_files, keep_all_files=args.all, progress=print)
print(res.summary())
for e in res.errors[:10]:
    print("!!", e)
