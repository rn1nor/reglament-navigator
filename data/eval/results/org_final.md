# Отчёт о качестве: org_v4

Провайдер: mock; эмбеддер: BAAI/bge-m3; реранкер: BAAI/bge-reranker-v2-m3; 2026-11-14T17:37:10

| Метрика | Значение |
|---|---|
| n | 42 |
| retrieval_recall@1 | 1.0 |
| retrieval_recall@3 | 1.0 |
| retrieval_recall@10 | 1.0 |
| retrieval_mrr@10 | 1.0 |
| retrieval_ndcg@10 | 0.872 |
| outcome_accuracy | 1.0 |
| doc_recall@5 | 1.0 |
| cited_doc_precision | 0.984 |
| step_groundedness | 1.0 |
| citation_validity | 1.0 |
| false_answer_rate_E | 0.0 |
| over_refusal_A | 0.0 |
| cancelled_leak_D | 0.0 |
| cancelled_notice_D | 0.667 |
| clarify_rate_F | 1.0 |
| outcome_accuracy_C | 1.0 |
| latency_p50_ms | 3501 |
| latency_p95_ms | 7555 |

## По типам

| Тип | n | Точность исхода | Doc Recall@5 |
|---|---|---|---|
| A (прямые) | 22 | 1.0 | 1.0 |
| B (многодокументные / редакции) | 3 | 1.0 | 1.0 |
| C (ролевые) | 5 | 1.0 | 1.0 |
| D (ловушки на отменённые) | 3 | 1.0 | 1.0 |
| E (нет в базе) | 8 | 1.0 | None |
| F (неоднозначные) | 1 | 1.0 | None |

## Матрица исходов (ожидание -> факт)

```
{
 "answer": {
  "answer": 30
 },
 "cancelled_notice": {
  "cancelled_notice": 2
 },
 "refuse": {
  "refuse": 8
 },
 "out_of_scope": {
  "out_of_scope": 1
 },
 "clarify": {
  "clarify": 1
 }
}
```

## Ошибки

