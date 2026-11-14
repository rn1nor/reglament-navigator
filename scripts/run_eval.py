"""python scripts/run_eval.py --testset data/eval/testset_mipt.yaml --kb data/kb/mipt --label baseline [--dense hash] [--no-rerank]"""
import sys
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
import argparse
from pathlib import Path

from navigator.eval.run import run
from navigator.qa.engine import Navigator

ap = argparse.ArgumentParser()
ap.add_argument("--testset", default="data/eval/testset_mipt.yaml")
ap.add_argument("--kb", default="data/kb/mipt")
ap.add_argument("--out", default="data/eval/results")
ap.add_argument("--label", default="run")
ap.add_argument("--dense", default=None)
ap.add_argument("--index", default=None)
ap.add_argument("--no-rerank", action="store_true")
ap.add_argument("--bm25-only", action="store_true", help="базовая линия: только BM25, без плотного поиска и реранкера")
args = ap.parse_args()
nav = Navigator(Path(args.kb), index_dir=args.index, dense="off" if args.bm25_only else args.dense, reranker="off" if (args.no_rerank or args.bm25_only) else None)
agg = run(nav, Path(args.testset), Path(args.out), label=args.label)
print("\n== ИТОГО ==")
for k, v in agg.items():
    if k not in ("by_type", "confusion"):
        print(f"{k}: {v}")
print("по типам:", agg["by_type"])
print("матрица:", agg["confusion"])
