import datetime as dt
from pathlib import Path

import pytest

from navigator.ingest.pipeline import ingest
from navigator.llm.base import MockProvider
from navigator.qa.engine import Navigator

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture()
def nav(tmp_path):
    kb_dir = tmp_path / "kb"
    ingest(FIX, kb_dir, today=dt.date(2026, 11, 3))
    return Navigator(kb_dir, provider=MockProvider(), dense="hash", reranker="off")


def test_approved_answer_is_served_and_invalidated(nav):
    q = "Как работнику подать заявление на ежегодный отпуск и за сколько дней?"
    r1 = nav.ask(q, role="staff")
    assert r1.steps
    nav.approve(r1, approved_by="Иванова И.И., Управление кадров")
    r2 = nav.ask("Как подать заявление на ежегодный отпуск работнику и за сколько дней до начала?", role="staff")
    assert r2.trace.get("provider") == "approved"
    assert r2.trust.verified_by_human and "Иванова" in r2.trust.verified_by_human
    # меняем текст процитированного пункта в базе: подтверждённый ответ должен устареть
    kb = nav.kb
    pol = next(d for d in kb.documents if d.doc_type == "положение")
    c22 = pol.clause_by_number("2.2")
    c22.text = c22.text.replace("10 календарных дней", "7 календарных дней")
    rows = nav.approved.status_table(kb)
    assert rows and rows[0]["статус"] == "требует перепроверки"
    r3 = nav.ask(q, role="staff")
    assert r3.trace.get("provider") != "approved"
    assert any("устарел" in w for w in r3.trust.warnings)


def test_deadline_computed_from_question_date(nav):
    r = nav.ask("Хочу уйти в ежегодный отпуск с 01.12.2026, когда подать заявление?", role="staff")
    assert r.outcome in ("answer", "partial")
    assert any("21.11.2026" in d for d in r.deadlines), r.deadlines  # 10 календарных дней до 01.12.2026
