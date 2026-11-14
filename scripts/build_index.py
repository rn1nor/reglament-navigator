"""python scripts/build_index.py --kb data/kb/mipt [--dense auto|on|hash|off]"""
import sys
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
import argparse
import time
from pathlib import Path

from navigator.index.dense import get_embedder
from navigator.index.store import HybridIndex, index_dirname
from navigator.ingest.pipeline import load_kb

ap = argparse.ArgumentParser()
ap.add_argument("--kb", required=True)
ap.add_argument("--out", default=None)
ap.add_argument("--dense", default=None)
ap.add_argument("--cache", default=None, help="каталог другого индекса для переиспользования эмбеддингов")
args = ap.parse_args()
kb = load_kb(Path(args.kb))
t = time.time()
emb = get_embedder(args.dense)
out = Path(args.out) if args.out else Path(args.kb) / index_dirname(emb.name)
idx = HybridIndex.build(kb, emb, cache_dir=Path(args.cache) if args.cache else out)
idx.save(out)
print(f"единиц: {len(idx.units)}; эмбеддер: {emb.name}; {time.time()-t:.1f} с; сохранено в {out}")
