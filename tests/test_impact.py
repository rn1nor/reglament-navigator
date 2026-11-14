import datetime as dt
from pathlib import Path

from navigator.ingest.impact import analyze
from navigator.ingest.pipeline import ingest

FIX = Path(__file__).parent / "fixtures"


def test_impact_of_amending_order(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    for name in ("polozhenie_otpusk.txt", "prikaz_staryi.txt"):
        (base / name).write_text((FIX / name).read_text(encoding="utf-8"), encoding="utf-8")
    kb = ingest(base, tmp_path / "kb", today=dt.date(2026, 11, 3))
    assert all(d.status == "active" for d in kb.documents)
    rep = analyze(kb, FIX / "prikaz_izmeneniya.txt", today=dt.date(2026, 11, 3))
    assert any(c.after == "cancelled" for c in rep.status_changes)
    ops = sorted(c.op for c in rep.clause_changes)
    assert ops == ["delete", "insert", "replace", "replace"]  # 3.2 удалён, 2.5 добавлен, 2.2 и 2.4 изменены
    assert "staff" in rep.affected_roles or "all" in rep.affected_roles
    assert not rep.unapplied
