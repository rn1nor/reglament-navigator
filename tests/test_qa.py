import datetime as dt
from pathlib import Path

import pytest

from navigator.ingest.pipeline import ingest
from navigator.llm.base import MockProvider
from navigator.qa.engine import Navigator

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def nav(tmp_path_factory):
    kb_dir = tmp_path_factory.mktemp("kb")
    ingest(FIX, kb_dir, today=dt.date(2026, 11, 3))
    return Navigator(kb_dir, provider=MockProvider(), dense="hash", reranker="off")


def test_answer_uses_current_edition(nav):
    r = nav.ask("Как работнику подать заявление на ежегодный отпуск и за сколько дней?", role="staff")
    assert r.outcome in ("answer", "partial")
    cited = {(c.doc_id, c.clause_id) for s in r.steps for c in s.citations}
    assert any(cid == "2.2" for _, cid in cited)
    texts = " ".join(s.text for s in r.steps)
    assert "10 календарных дней" in texts and "14 календарных" not in texts
    assert all(c.passed for c in r.trust.checks if c.name.startswith("Цитаты") or c.name.startswith("Текст цитат"))
    assert r.documents and r.documents[0].status == "active"


def test_cancelled_document_notice(nav):
    r = nav.ask("Что говорит приказ от 20.01.2023 № 15-1 о согласовании заявлений на отпуск?")
    assert r.outcome == "cancelled_notice"
    assert "отменён" in r.summary.lower() or "отмен" in r.summary.lower()
    assert any(d.status == "cancelled" for d in r.documents)


def test_refuse_when_not_in_base(nav):
    r = nav.ask("Какой размер суточных при командировке в Казань?", role="staff")
    assert r.outcome == "refuse"
    assert not r.steps
    assert "не найден" in r.summary


def test_out_of_scope(nav):
    r = nav.ask("Какая погода будет завтра в Долгопрудном?")
    assert r.outcome == "out_of_scope"


def test_order_number_without_sign_is_recognized():
    from navigator.qa.query import heuristics

    assert "1465-1" in heuristics("Что говорит приказ 1465-1 о внутреннем заимствовании?").mentioned_documents
    assert "1465-1" in heuristics("Что говорит приказ от 08.07.2020 1465-1?").mentioned_documents
    assert heuristics("Как оформить командировку в 2025-2026 учебном году?").mentioned_documents == []


def test_status_on_date_picks_edition_in_force():
    """Редакция, заменённая позже, считается действовавшей на дату до замены; новая редакция на ту дату ещё не действует."""
    import datetime as dt

    from navigator.qa.timeline import status_on
    from navigator.schemas import Document, KnowledgeBase

    old = Document(id="old", source_path="a", title="Регламент", doc_type="регламент", date=dt.date(2017, 5, 1), status="superseded", superseded_by="new")
    new = Document(id="new", source_path="b", title="Регламент", doc_type="регламент", date=dt.date(2021, 11, 25), status="active")
    kb = KnowledgeBase(documents=[old, new])
    assert status_on(kb, old, dt.date(2019, 12, 31))[0] == "active"
    assert status_on(kb, new, dt.date(2019, 12, 31))[0] == "not_yet"
    assert status_on(kb, old, dt.date(2022, 1, 1))[0] == "superseded"
    assert status_on(kb, new, dt.date(2022, 1, 1))[0] == "active"


def test_unknown_role_does_not_break_answer(nav):
    r = nav.ask("Как студенту оформить академический отпуск?", role="несуществующая роль")
    assert r.outcome != "refuse" or "Техническая ошибка" not in r.summary
    assert r.role in (None, "student")
