# Отчёт о качестве: holdout_v4

Провайдер: mock; эмбеддер: BAAI/bge-m3; реранкер: BAAI/bge-reranker-v2-m3; 2026-11-14T17:38:42

| Метрика | Значение |
|---|---|
| n | 14 |
| retrieval_recall@1 | 1.0 |
| retrieval_recall@3 | 1.0 |
| retrieval_recall@10 | 1.0 |
| retrieval_mrr@10 | 1.0 |
| retrieval_ndcg@10 | 0.824 |
| outcome_accuracy | 0.929 |
| doc_recall@5 | 1.0 |
| cited_doc_precision | 0.9 |
| step_groundedness | 1.0 |
| citation_validity | 1.0 |
| false_answer_rate_E | 0.0 |
| cancelled_leak_D | 0.0 |
| cancelled_notice_D | 0.0 |
| outcome_accuracy_C | 1.0 |
| latency_p50_ms | 4375 |
| latency_p95_ms | 9582 |

## По типам

| Тип | n | Точность исхода | Doc Recall@5 |
|---|---|---|---|
| C (ролевые) | 10 | 1.0 | 1.0 |
| D (ловушки на отменённые) | 1 | 0.0 | 1.0 |
| E (нет в базе) | 3 | 1.0 | None |

## Матрица исходов (ожидание -> факт)

```
{
 "answer": {
  "answer": 10,
  "refuse": 1
 },
 "refuse": {
  "refuse": 3
 }
}
```

## Ошибки

- **H14** [D] ожид=answer факт=refuse top5=True — Что говорит приказ № 76-1 от 12.02.2015 о бланках для открытия ФЛС?
