"""Примеры работы системы для проверяющих: прогоняет набор вопросов и сохраняет ответы в examples/.

python scripts/make_examples.py --kb data/kb/mipt_demo [--out examples]
Создаёт examples/README.md (сводка: вопрос, исход, короткий ответ, шаги с цитатами, карточка доверия) и examples/<id>.json (полный ответ с трассой).
"""
import sys
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
import argparse
import datetime as dt
from pathlib import Path

from navigator.qa.engine import Navigator
from navigator.schemas import ROLE_LABELS

EXAMPLES_ORG = [
    ("ex01_komandirovka", "staff", "Как оформить служебную командировку?"),
    ("ex02_sutochnye", "staff", "Сколько составляют суточные при командировке по России по регламенту и по приказу о бюджете на 2026 год?"),
    ("ex03_vacancy_long", "admin", "Я руководитель лаборатории, нам нужен новый инженер в штат. С чего начать, кто должен согласовать служебную записку и сколько ждать публикации вакансии?"),
    ("ex04_zaem_compound", "admin", "Сколько длится внутренний заём и какой штраф, если не вернуть его вовремя?"),
    ("ex05_fls_edition", "researcher", "Как открыть финансовый лицевой счет для научно-исследовательской работы?"),
    ("ex06_smeta", "admin", "Как согласовать и утвердить смету доходов и расходов подразделения?"),
    ("ex07_techsupport", "staff", "У меня не работает корпоративная почта, куда писать и когда ответят?"),
    ("ex08_obshchezhitie", "staff", "Мне 45 лет, я сотрудник МФТИ, прописан в Твери. Могу ли я заселиться в общежитие?"),
    ("ex09_cancelled_order", None, "Что говорит приказ от 08.07.2020 № 1465-1 о внутреннем заимствовании?"),
    ("ex10_not_in_base", "staff", "Как оформить ежегодный оплачиваемый отпуск?"),
    ("ex11_missing_order", None, "Что говорит приказ № 9999-1 от 01.01.2024 о премировании сотрудников?"),
    ("ex12_invalid_query", None, "???"),
]
EXAMPLES = [
    ("ex01_academ", "student", "Как студенту оформить академический отпуск?"),
    ("ex02_deadline", "student", "Хочу уйти в академический отпуск с 01.02.2027, когда подавать заявление и когда выйдет приказ?"),
    ("ex03_budget", "student", "Как перейти с платного обучения на бюджет?"),
    ("ex04_restore", "student", "Как восстановиться после отчисления?"),
    ("ex05_amended", "student", "В какой срок издаётся приказ об академическом отпуске после решения комиссии?"),
    ("ex06_cancelled", None, "Что говорит приказ № 3680-1 от 23.09.2024 об изменении графика учебного процесса?"),
    ("ex07_missing_doc", None, "Что говорит приказ № 9999-1 от 01.01.2020 о порядке командировок?"),
    ("ex08_not_in_base", "staff", "Как оформить служебную командировку и получить суточные?"),
    ("ex09_out_of_scope", None, "Какая погода будет в Долгопрудном на выходных?"),
    ("ex10_role_ambiguous", None, "Как оформить отпуск?"),
    ("ex11_collective", "staff", "Какие гарантии и льготы предусмотрены работникам коллективным договором?"),
    ("ex12_admission", "applicant", "Какие документы подавать при поступлении в бакалавриат?"),
]
OUTCOME_RU = {"answer": "ответ", "partial": "частичный ответ", "refuse": "отказ", "clarify": "уточнение", "cancelled_notice": "документ отменён",
              "out_of_scope": "вне области / некорректный запрос"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", default="data/kb/org")
    ap.add_argument("--out", default="examples")
    ap.add_argument("--set", default="auto", choices=["auto", "org", "mipt"], help="набор вопросов: org — документы организаторов, mipt — публичная демо-база")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    nav = Navigator(Path(args.kb))
    qset = args.set if args.set != "auto" else ("org" if Path(args.kb).name == "org" else "mipt")
    examples = EXAMPLES_ORG if qset == "org" else EXAMPLES
    lines = [
        "# Примеры работы",
        "",
        f"База: `{args.kb}` ({len(nav.kb.documents)} документов). Режим модели: `{nav.provider.kind}`"
        + (" (без модели: краткий ответ и шаги собираются из дословных положений документов, для вопросов «как сделать» в порядке раздела с процедурой)." if nav.provider.kind == "mock" else "."),
        f"Поиск: {nav.embedder.name} + {getattr(nav.reranker, 'name', 'без реранкера')}. Сгенерировано {dt.datetime.now():%d.%m.%Y %H:%M} скриптом `scripts/make_examples.py`.",
        "",
        "| № | Вопрос | Роль | Исход | Документов | Шагов | Время, мс |",
        "|---|---|---|---|---|---|---|",
    ]
    details = []
    for ex_id, role, q in examples:
        resp = nav.ask(q, role=role)
        (out / f"{ex_id}.json").write_text(resp.model_dump_json(indent=1), encoding="utf-8")
        lines.append(f"| {ex_id} | {q} | {ROLE_LABELS.get(role, '—') if role else '—'} | {OUTCOME_RU.get(resp.outcome, resp.outcome)} | {len(resp.documents or resp.checked_documents)} | {len(resp.steps)} | {resp.timings_ms.get('total', sum(resp.timings_ms.values()))} |")
        d = [f"## {ex_id}: {q}", "", f"Роль: {ROLE_LABELS.get(role, '—') if role else 'не указана'} · Исход: **{OUTCOME_RU.get(resp.outcome, resp.outcome)}**", "", f"**Краткий ответ.** {resp.summary}", ""]
        if resp.clarifying_question:
            d += [f"Уточняющий вопрос: {resp.clarifying_question} Варианты: {'; '.join(resp.clarifying_options)}", ""]
        if resp.steps:
            d += ["**Порядок действий" + (f" (раздел «{resp.procedure_source}»)" if resp.procedure_source else "") + ":**" if resp.answer_kind == "procedure" else "**Основание:**", ""]
        for i, s in enumerate(resp.steps, 1):
            cites = "; ".join(f"{c.clause_label or c.clause_id}, {c.doc_title or c.doc_id}" + (f", стр. {c.page}" if c.page else "") for c in s.citations[:3])
            more = f" и ещё {len(s.citations) - 3}" if len(s.citations) > 3 else ""
            d.append(f"{i}. {s.text}  \n   Источник: {cites}{more}")
        if resp.steps:
            d.append("")
        if resp.not_covered:
            d += ["**Не найдено в документах базы:**"] + [f"- {x}" for x in resp.not_covered] + [""]
        if resp.deadlines:
            d += ["**Сроки:**"] + [f"- {x}" for x in resp.deadlines] + [""]
        docs_ = resp.documents or resp.checked_documents
        if docs_:
            d += ["**Документы:**"] + [f"- [{x.status}] {x.doc_type} {x.number or ''} {x.date_label} {x.title[:110]}" + (f" — {x.status_reason}" if x.status_reason else "") for x in docs_] + [""]
        d += ["**Карточка доверия:**"] + [f"- {'✔' if c.passed else '✖'} {c.name}: {c.detail}" for c in resp.trust.checks]
        d += [f"- ! {w}" for w in resp.trust.warnings]
        d += ["", f"Полный ответ с трассой: `{ex_id}.json`", ""]
        details.extend(d)
        print(f"{ex_id}: {resp.outcome} ({len(resp.steps)} шагов)")
    (out / "README.md").write_text("\n".join(lines + [""] + details), encoding="utf-8")
    print(f"готово: {out / 'README.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
