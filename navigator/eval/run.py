"""Прогон тест-набора и метрики: полнота поиска, исходы по типам, ложные ответы, переотказ, утечка отменённых, обоснованность."""
from __future__ import annotations

import datetime as _dt
import json
import statistics
import time
from pathlib import Path
from typing import Any, Optional

import yaml

from ..schemas import Document, KnowledgeBase, Response

TYPE_NAMES = {"A": "прямые", "B": "многодокументные / редакции", "C": "ролевые", "D": "ловушки на отменённые", "E": "нет в базе", "F": "неоднозначные"}
ANSWERED = {"answer", "partial"}


def load_testset(path: Path) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return data["items"] if isinstance(data, dict) else data


def match_doc(gold: str, d: Document) -> bool:
    g = gold.lower().replace("ё", "е")
    if g in d.id.lower() or g in d.title.lower().replace("ё", "е"):
        return True
    num = (d.number or "").lower().replace("-", "_").replace("/", "_")
    return bool(num) and g.replace("-", "_") == num


def ranked_docs(resp: Response, k: int = 10) -> list[str]:
    """Ранжированный список документов на выходе поиска (после реранкера), из трассы."""
    out: list[str] = []
    for h in resp.trace.get("hits", []):
        if h["doc_id"] not in out:
            out.append(h["doc_id"])
        if len(out) >= k:
            break
    return out


def retrieval_metrics(kb: KnowledgeBase, gold: list[str], ranked: list[str]) -> dict[str, Optional[float]]:
    """Recall@k, MRR@10, nDCG@10 по документам (бинарная релевантность). None, если у вопроса нет эталона."""
    if not gold:
        return {"recall@1": None, "recall@3": None, "recall@5": None, "recall@10": None, "mrr@10": None, "ndcg@10": None}
    import math

    docs = {d.id: d for d in kb.documents}
    rel = [1 if (i in docs and any(match_doc(g, docs[i]) for g in gold)) else 0 for i in ranked[:10]]
    first = next((r for r, v in enumerate(rel, start=1) if v), None)
    n_rel_total = max(1, min(10, sum(1 for d in kb.documents if any(match_doc(g, d) for g in gold))))
    dcg = sum(v / math.log2(r + 1) for r, v in enumerate(rel, start=1))
    idcg = sum(1 / math.log2(r + 1) for r in range(1, n_rel_total + 1))
    return {
        "recall@1": float(bool(first and first <= 1)), "recall@3": float(bool(first and first <= 3)),
        "recall@5": float(bool(first and first <= 5)), "recall@10": float(bool(first and first <= 10)),
        "mrr@10": (1.0 / first) if first else 0.0, "ndcg@10": round(dcg / idcg, 4) if idcg else 0.0,
    }


def evaluate_item(kb: KnowledgeBase, item: dict, resp: Response, latency_ms: int) -> dict[str, Any]:
    gold = item.get("gold_docs") or []
    top5 = [c.doc_id for c in resp.checked_documents[:5]]
    cited = list(dict.fromkeys(c.doc_id for s in resp.steps for c in s.citations))
    docs = {d.id: d for d in kb.documents}
    gold_hit_top5 = any(any(match_doc(g, docs[i]) for g in gold) for i in top5 if i in docs) if gold else None
    gold_hit_cited = any(any(match_doc(g, docs[i]) for g in gold) for i in cited if i in docs) if gold and cited else None
    cited_precision = (sum(1 for i in cited if i in docs and any(match_doc(g, docs[i]) for g in gold)) / len(cited)) if gold and cited else None
    expected = item.get("expected_outcome", "answer")
    ok_outcome = resp.outcome == expected or (expected == "answer" and resp.outcome == "partial" and item.get("accept_partial", True))
    inactive_cited = [i for i in cited if i in docs and docs[i].status != "active"]
    supported = sum(1 for s in resp.steps if s.verdict == "supported")
    checks = {c.name: c.passed for c in resp.trust.checks}
    return {
        "id": item["id"], "type": item.get("type", "A"), "question": item["question"], "expected": expected, "outcome": resp.outcome,
        "outcome_ok": ok_outcome, "gold_hit_top5": gold_hit_top5, "gold_hit_cited": gold_hit_cited, "cited_precision": cited_precision,
        "cited": cited, "top5": top5, "inactive_cited": inactive_cited, "steps": len(resp.steps), "supported": supported,
        "dropped": len(resp.trust.dropped_steps), "citations_valid": checks.get("Текст цитат совпадает с документом", True) and checks.get("Цитаты указывают на переданные пункты", True),
        "gate": resp.trace.get("decision", {}).get("gate_value"), "latency_ms": latency_ms, "summary": resp.summary[:300],
        "clarify": resp.clarifying_question,
        **retrieval_metrics(kb, gold, ranked_docs(resp)),
    }


def aggregate(rows: list[dict]) -> dict[str, Any]:
    def share(xs: list[bool]) -> Optional[float]:
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 3) if xs else None

    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)
    out: dict[str, Any] = {"n": len(rows), "outcome_accuracy": share([r["outcome_ok"] for r in rows])}
    out["doc_recall@5"] = share([r["gold_hit_top5"] for r in rows if r["gold_hit_top5"] is not None])
    for m in ("recall@1", "recall@3", "recall@5", "recall@10", "mrr@10", "ndcg@10"):
        vals = [r[m] for r in rows if r.get(m) is not None]
        out[f"retrieval_{m}"] = round(sum(vals) / len(vals), 3) if vals else None
    out["cited_doc_precision"] = share([r["cited_precision"] for r in rows if r["cited_precision"] is not None])
    kept = sum(r["steps"] for r in rows)
    out["step_groundedness"] = round(sum(r["supported"] for r in rows) / kept, 3) if kept else None
    out["citation_validity"] = share([r["citations_valid"] for r in rows if r["steps"]])
    if "E" in by_type:
        out["false_answer_rate_E"] = share([r["outcome"] in ANSWERED for r in by_type["E"]])
    if "A" in by_type:
        out["over_refusal_A"] = share([r["outcome"] == "refuse" for r in by_type["A"]])
    if "D" in by_type:
        out["cancelled_leak_D"] = share([bool(r["inactive_cited"]) for r in by_type["D"]])
        out["cancelled_notice_D"] = share([r["outcome"] == "cancelled_notice" for r in by_type["D"]])
    if "F" in by_type:
        out["clarify_rate_F"] = share([r["outcome"] == "clarify" for r in by_type["F"]])
    if "C" in by_type:
        out["outcome_accuracy_C"] = share([r["outcome_ok"] for r in by_type["C"]])
    lat = [r["latency_ms"] for r in rows]
    out["latency_p50_ms"] = int(statistics.median(lat)) if lat else None
    out["latency_p95_ms"] = int(sorted(lat)[max(0, int(len(lat) * 0.95) - 1)]) if lat else None
    out["by_type"] = {t: {"n": len(rs), "outcome_accuracy": share([r["outcome_ok"] for r in rs]), "doc_recall@5": share([r["gold_hit_top5"] for r in rs if r["gold_hit_top5"] is not None])} for t, rs in sorted(by_type.items())}
    out["confusion"] = {}
    for r in rows:
        out["confusion"].setdefault(r["expected"], {}).setdefault(r["outcome"], 0)
        out["confusion"][r["expected"]][r["outcome"]] += 1
    return out


def run(nav, testset: Path, out_dir: Path, label: str = "run") -> dict[str, Any]:
    items = load_testset(testset)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for it in items:
        t = time.perf_counter()
        resp = nav.ask(it["question"], role=it.get("role"))
        ms = int((time.perf_counter() - t) * 1000)
        rows.append(evaluate_item(nav.kb, it, resp, ms))
        print(f"{it['id']:>4} {it.get('type','A')} ожид={it.get('expected_outcome','answer'):<16} факт={resp.outcome:<16} top5={'✔' if rows[-1]['gold_hit_top5'] else ('—' if rows[-1]['gold_hit_top5'] is None else '✖')} {ms} мс | {it['question'][:60]}")
    agg = aggregate(rows)
    agg["label"] = label
    agg["provider"] = nav.provider.kind
    agg["embedder"] = nav.embedder.name if getattr(nav, "use_dense", True) else "bm25-only"
    agg["reranker"] = getattr(nav.reranker, "name", None)
    agg["run_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    (out_dir / f"{label}.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    (out_dir / f"{label}.json").write_text(json.dumps(agg, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / f"{label}.md").write_text(report_md(agg, rows), encoding="utf-8")
    return agg


def report_md(agg: dict, rows: list[dict]) -> str:
    lines = [f"# Отчёт о качестве: {agg['label']}", "", f"Провайдер: {agg['provider']}; эмбеддер: {agg['embedder']}; реранкер: {agg['reranker']}; {agg['run_at']}", "",
             "| Метрика | Значение |", "|---|---|"]
    for k in ("n", "retrieval_recall@1", "retrieval_recall@3", "retrieval_recall@10", "retrieval_mrr@10", "retrieval_ndcg@10", "outcome_accuracy", "doc_recall@5", "cited_doc_precision", "step_groundedness", "citation_validity", "false_answer_rate_E",
              "over_refusal_A", "cancelled_leak_D", "cancelled_notice_D", "clarify_rate_F", "outcome_accuracy_C", "latency_p50_ms", "latency_p95_ms"):
        if k in agg and agg[k] is not None:
            lines.append(f"| {k} | {agg[k]} |")
    lines += ["", "## По типам", "", "| Тип | n | Точность исхода | Doc Recall@5 |", "|---|---|---|---|"]
    for t, v in agg["by_type"].items():
        lines.append(f"| {t} ({TYPE_NAMES.get(t, t)}) | {v['n']} | {v['outcome_accuracy']} | {v['doc_recall@5']} |")
    lines += ["", "## Матрица исходов (ожидание -> факт)", "", "```", json.dumps(agg["confusion"], ensure_ascii=False, indent=1), "```", "", "## Ошибки", ""]
    for r in rows:
        if not r["outcome_ok"] or r["gold_hit_top5"] is False:
            lines.append(f"- **{r['id']}** [{r['type']}] ожид={r['expected']} факт={r['outcome']} top5={r['gold_hit_top5']} — {r['question']}")
    return "\n".join(lines) + "\n"
