"""python scripts/impact.py --kb data/kb/mipt --new path/to/new_order.pdf [--questions data/eval/testset_mipt.yaml]
Показывает, что изменит новый приказ: статусы, пункты, роли, вопросы с другим ответом."""
import sys
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
import argparse
import json
from pathlib import Path

from navigator.eval.run import load_testset
from navigator.ingest.impact import analyze
from navigator.ingest.pipeline import load_kb

ap = argparse.ArgumentParser()
ap.add_argument("--kb", default="data/kb/mipt")
ap.add_argument("--new", required=True)
ap.add_argument("--questions", default=None)
ap.add_argument("--dense", default="hash")
args = ap.parse_args()
kb = load_kb(Path(args.kb))
factory = None
questions = None
if args.questions:
    from navigator.index.dense import get_embedder
    from navigator.index.rerank import get_reranker
    from navigator.index.store import HybridIndex
    from navigator.llm.base import get_provider
    from navigator.qa.engine import Navigator

    questions = load_testset(Path(args.questions))

    def factory(kb_obj):  # noqa: E306
        nav = Navigator.__new__(Navigator)
        nav.kb = kb_obj
        nav.embedder = get_embedder(args.dense)
        nav.index = HybridIndex.build(kb_obj, nav.embedder)
        nav.provider = get_provider()
        nav.reranker = get_reranker()
        return nav

rep = analyze(kb, Path(args.new), questions=questions, navigator_factory=factory)
print(rep.summary)
print(json.dumps(rep.model_dump(mode="json"), ensure_ascii=False, indent=1)[:6000])
