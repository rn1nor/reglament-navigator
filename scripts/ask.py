"""python scripts/ask.py "как уйти в академический отпуск" [--role student] [--kb data/kb/mipt] [--json] [--no-rerank] [--dense hash]"""
import sys
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
import argparse
import datetime as dt
import json
from pathlib import Path

from navigator.qa.engine import Navigator
from navigator.schemas import ROLE_LABELS

ap = argparse.ArgumentParser()
ap.add_argument("question")
ap.add_argument("--role", default=None)
ap.add_argument("--as-of", default=None)
ap.add_argument("--kb", default=None, help="каталог базы; по умолчанию как в интерфейсе (NAVIGATOR_KB или data/kb/org)")
ap.add_argument("--json", action="store_true")
ap.add_argument("--no-rerank", action="store_true")
ap.add_argument("--dense", default=None)
ap.add_argument("--index", default=None)
ap.add_argument("--trace", action="store_true")
args = ap.parse_args()
from navigator.config import default_kb  # noqa: E402

nav = Navigator(Path(args.kb) if args.kb else default_kb(), index_dir=args.index, dense=args.dense, reranker="off" if args.no_rerank else None)
resp = nav.ask(args.question, role=args.role, as_of=dt.date.fromisoformat(args.as_of) if args.as_of else None)
if args.json:
    print(resp.model_dump_json(indent=1))
    raise SystemExit
print(f"ИСХОД: {resp.outcome}   роль: {ROLE_LABELS.get(resp.role or '', '—')}   на дату: {resp.as_of}   провайдер: {resp.trace['provider']}   {resp.timings_ms}")
print(f"\n{resp.summary}\n")
if resp.clarifying_question:
    print("УТОЧНЕНИЕ:", resp.clarifying_question, resp.clarifying_options)
if resp.steps:
    print("ПОРЯДОК ДЕЙСТВИЙ" + (f" (раздел «{resp.procedure_source}»)" if resp.procedure_source else "") + ":" if resp.answer_kind == "procedure" else "ОСНОВАНИЕ:")
for i, s in enumerate(resp.steps, 1):
    cites = "; ".join(f"{c.clause_label or c.clause_id}, {c.doc_title or c.doc_id}" for c in s.citations[:2])
    print(f"{i}. {s.text}\n     [{s.verdict}] {cites}" + (f" и ещё {len(s.citations) - 2}" if len(s.citations) > 2 else ""))
if resp.conditions:
    print("\nУсловия:", *[f"\n - {c}" for c in resp.conditions])
if resp.not_covered:
    print("\nНе регламентировано в базе:", *[f"\n - {c}" for c in resp.not_covered])
if resp.responsible or resp.forms or resp.deadlines:
    print(f"\nКуда/кто: {resp.responsible or '—'} | формы: {resp.forms or '—'} | сроки: {resp.deadlines or '—'}")
print("\nДокументы:")
for d in (resp.documents or resp.checked_documents):
    print(f" - [{d.status}] {d.doc_type} {d.number or ''} {d.date or ''} {d.title[:90]}")
print("\nКарточка доверия:")
for c in resp.trust.checks:
    print(f" {'✔' if c.passed else '✖'} {c.name}: {c.detail}")
for w in resp.trust.warnings:
    print(f" ! {w}")
if args.trace:
    print("\nTRACE:", json.dumps({k: resp.trace[k] for k in ('queries', 'method', 'decision', 'hits')}, ensure_ascii=False, indent=1))
