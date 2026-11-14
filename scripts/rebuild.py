"""python scripts/rebuild.py --kb data/kb/org --src data/raw/org [data/raw/org_web ...]
Полная пересборка базы: разбор, индекс (с кэшем эмбеддингов), противоречия. Источники запоминаются в kb/sources.json."""
import sys
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
import argparse
from pathlib import Path

from navigator.ingest.reindex import get_sources, rebuild

ap = argparse.ArgumentParser()
ap.add_argument("--kb", required=True)
ap.add_argument("--src", nargs="*", default=None)
args = ap.parse_args()
kb_dir = Path(args.kb)
sources = [Path(s) for s in args.src] if args.src else get_sources(kb_dir, default_raw=Path("data/raw") / kb_dir.name)
print("источники:", [str(s) for s in sources])
try:
    print(rebuild(kb_dir, sources, progress=print))  # папки загрузок из интерфейса и с сайта добавляются к списку автоматически
except (FileNotFoundError, ValueError) as e:  # нет источника или в нём нет читаемых документов: существующая база не тронута
    print(f"!! {e}", file=sys.stderr)
    sys.exit(2)
