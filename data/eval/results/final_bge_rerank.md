# Отчёт о качестве: final_bge_rerank

Провайдер: mock; эмбеддер: BAAI/bge-m3; реранкер: BAAI/bge-reranker-v2-m3; 2026-11-14T14:15:37

| Метрика | Значение |
|---|---|
| n | 42 |
| retrieval_recall@1 | 0.806 |
| retrieval_recall@3 | 0.935 |
| retrieval_recall@10 | 1.0 |
| retrieval_mrr@10 | 0.885 |
| retrieval_ndcg@10 | 0.687 |
| outcome_accuracy | 0.881 |
| doc_recall@5 | 1.0 |
| cited_doc_precision | 0.902 |
| step_groundedness | 1.0 |
| citation_validity | 1.0 |
| false_answer_rate_E | 0.1 |
| over_refusal_A | 0.05 |
| cancelled_leak_D | 0.0 |
| cancelled_notice_D | 0.0 |
| clarify_rate_F | 0.0 |
| outcome_accuracy_C | 0.75 |
| latency_p50_ms | 3863 |
| latency_p95_ms | 6630 |

## По типам

| Тип | n | Точность исхода | Doc Recall@5 |
|---|---|---|---|
| A (прямые) | 20 | 0.95 | 1.0 |
| B (многодокументные / редакции) | 4 | 1.0 | 1.0 |
| C (ролевые) | 4 | 0.75 | 1.0 |
| D (ловушки на отменённые) | 2 | 1.0 | 1.0 |
| E (нет в базе) | 10 | 0.9 | 1.0 |
| F (неоднозначные) | 2 | 0.0 | 1.0 |

## Матрица исходов (ожидание -> факт)

```
{
 "answer": {
  "answer": 27,
  "refuse": 1
 },
 "clarify": {
  "refuse": 1,
  "answer": 2
 },
 "refuse": {
  "refuse": 9,
  "answer": 1
 },
 "out_of_scope": {
  "out_of_scope": 1
 }
}
```

## Ошибки

- **A03** [A] ожид=answer факт=refuse top5=True — Как выйти из академического отпуска досрочно?
- **C01** [C] ожид=clarify факт=refuse top5=True — Как оформить отпуск?
- **E10** [E] ожид=refuse факт=answer top5=True — Какой порядок выплаты материальной помощи сотрудникам?
- **F01** [F] ожид=clarify факт=answer top5=True — Какие у меня права и обязанности по правилам внутреннего распорядка?
- **F02** [F] ожид=clarify факт=answer top5=None — Какой срок подачи заявления?
