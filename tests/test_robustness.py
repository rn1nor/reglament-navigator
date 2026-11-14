import datetime as dt
from pathlib import Path

from navigator.ingest.pipeline import ingest
from navigator.llm.base import MockProvider
from navigator.qa.engine import Navigator
from navigator.qa.query import heuristics

FIX = Path(__file__).parent / "fixtures"


def test_invalid_queries_are_handled(tmp_path):
    kb_dir = tmp_path / "kb"
    ingest(FIX, kb_dir, today=dt.date(2026, 11, 3))
    nav = Navigator(kb_dir, provider=MockProvider(), dense="off", reranker="off")
    for q in ("", "   ", "???", "12345", "ыыыыыыыыыыыыыыыыыыыыыыыыыыыыыыы"):
        r = nav.ask(q)
        assert r.outcome == "out_of_scope" and "не обработан" in r.summary and not r.steps
    r = nav.ask("Как работнику подать заявление на отпуск? " * 200, role="staff")  # очень длинный запрос обрезается, но обрабатывается
    assert r.outcome in ("answer", "partial")


def test_corrupted_file_goes_to_audit(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "polozhenie.txt").write_text((FIX / "polozhenie_otpusk.txt").read_text(encoding="utf-8"), encoding="utf-8")
    (src / "broken.pdf").write_bytes(b"%PDF-1.4 this is not really a pdf")
    (src / "broken.docx").write_bytes(b"PK\x03\x04 broken zip")
    kb = ingest(src, tmp_path / "kb", today=dt.date(2026, 11, 3))
    assert len(kb.documents) == 1
    errs = [f for f in kb.audit.findings if f.kind == "read_error"]
    assert len(errs) == 2 and all(f.severity == "error" for f in errs)


def test_cancelled_document_known_only_by_reference(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "prikaz_izmeneniya.txt").write_text((FIX / "prikaz_izmeneniya.txt").read_text(encoding="utf-8"), encoding="utf-8")
    ingest(src, tmp_path / "kb", today=dt.date(2026, 11, 3))  # приказа 15-1 в базе нет, он отменён приказом 215-1
    nav = Navigator(tmp_path / "kb", provider=MockProvider(), dense="off", reranker="off")
    r = nav.ask("Что говорит приказ от 20.01.2023 № 15-1 о согласовании заявлений на отпуск?")
    assert r.outcome == "cancelled_notice" and "215-1" in r.summary


def test_compound_question_is_split():
    qu = heuristics("Сколько длится внутренний заём и какой штраф за просрочку возврата?")
    assert any("штраф" in x for x in qu.reformulations) and any("длится" in x for x in qu.reformulations)
