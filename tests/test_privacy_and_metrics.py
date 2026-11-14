import datetime as dt
from pathlib import Path

from navigator.eval.run import retrieval_metrics
from navigator.ingest.pipeline import ingest
from navigator.llm.base import MockProvider
from navigator.llm.privacy import is_external, kb_is_confidential, set_policy
from navigator.qa.engine import Navigator

FIX = Path(__file__).parent / "fixtures"


class FakeExternal(MockProvider):
    kind = "anthropic"


class FakeLocal(MockProvider):
    kind = "openai_compat"
    base_url = "http://127.0.0.1:8000/v1"


def test_confidential_base_blocks_external_llm(tmp_path, monkeypatch):
    monkeypatch.delenv("NAVIGATOR_ALLOW_EXTERNAL_LLM", raising=False)
    kb_dir = tmp_path / "kb"
    ingest(FIX, kb_dir, today=dt.date(2026, 11, 3))
    assert kb_is_confidential(kb_dir)  # по умолчанию закрытая
    nav = Navigator(kb_dir, provider=FakeExternal(), dense="off", reranker="off")
    assert nav.provider.kind == "mock" and nav.privacy_note
    r = nav.ask("Как работнику подать заявление на ежегодный отпуск?", role="staff")
    assert any("закрытая" in w for w in r.trust.warnings)
    assert not is_external(FakeLocal())  # локальная модель разрешена
    set_policy(kb_dir, confidential=False)
    nav2 = Navigator(kb_dir, provider=FakeExternal(), dense="off", reranker="off")
    assert nav2.provider.kind == "anthropic" and nav2.privacy_note is None


def test_retrieval_metrics(tmp_path):
    kb = ingest(FIX, tmp_path / "kb", today=dt.date(2026, 11, 3))
    pol = next(d for d in kb.documents if d.doc_type == "положение")
    old = next(d for d in kb.documents if d.number == "15-1")
    amend = next(d for d in kb.documents if d.number == "215-1")
    m = retrieval_metrics(kb, [pol.id], [old.id, pol.id, amend.id])
    assert m["recall@1"] == 0.0 and m["recall@3"] == 1.0 and m["mrr@10"] == 0.5
    assert 0 < m["ndcg@10"] < 1
    assert retrieval_metrics(kb, [], [pol.id])["mrr@10"] is None
