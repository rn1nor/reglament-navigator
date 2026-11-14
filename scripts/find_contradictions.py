"""python scripts/find_contradictions.py --kb data/kb/mipt [--threshold 0.8]
Ищет пары действующих пунктов разных документов с похожим смыслом и разными числами; результат в kb/contradictions.json."""
import sys
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
import argparse
from pathlib import Path

from navigator.index.dense import get_embedder
from navigator.index.store import HybridIndex, index_dirname
from navigator.ingest.contradictions import find_contradictions, save_contradictions
from navigator.ingest.pipeline import load_kb

ap = argparse.ArgumentParser()
ap.add_argument("--kb", default="data/kb/mipt")
ap.add_argument("--threshold", type=float, default=0.80)
args = ap.parse_args()
kb = load_kb(Path(args.kb))
emb = get_embedder()
idx_dir = Path(args.kb) / index_dirname(emb.name)
index = HybridIndex.load(idx_dir, emb) if (idx_dir / "units.json").exists() else HybridIndex.build(kb, emb)
pairs = find_contradictions(kb, index, threshold=args.threshold)
p = save_contradictions(pairs, Path(args.kb))
print(f"кандидатов в противоречия: {len(pairs)} -> {p}")
for pr in pairs[:10]:
    print(f"\n[{pr['similarity']}] {pr['a']['doc'][:60]} {pr['a']['clause_id']} {pr['a']['numbers']}\n   vs {pr['b']['doc'][:60]} {pr['b']['clause_id']} {pr['b']['numbers']}")
