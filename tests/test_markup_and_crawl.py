import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from navigator.ingest.crawl import crawl
from navigator.ingest.metadata import detect_owner_unit, detect_topics


def test_topics_and_owner():
    text = ("ПОЛОЖЕНИЕ о служебных командировках работников. 1. Работник направляется в командировку приказом. "
            "2. Авансовый отчёт представляется в бухгалтерию в течение трёх рабочих дней. Суточные выплачиваются по нормам. "
            "Контроль за исполнением настоящего приказа возложить на начальника финансово-экономического управления.")
    topics = detect_topics(text, "Положение о служебных командировках работников")
    assert topics and topics[0] == "Командировки"
    assert detect_owner_unit(text).startswith("начальника финансово-экономического управления")


def test_crawl_local_site(tmp_path):
    site = tmp_path / "site"
    (site / "docs").mkdir(parents=True)
    (site / "docs" / "polozhenie.txt").write_text("x", encoding="utf-8")
    pdf = site / "docs" / "prikaz_o_komandirovkah.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"0" * 4000)  # размер важнее содержимого для фильтра
    (site / "index.html").write_text('<html><head><title>Документы</title></head><body>'
                                     '<a href="docs/prikaz_o_komandirovkah.pdf">Приказ о командировках</a>'
                                     '<a href="docs/otchet_o_samoobsledovanii.pdf">Отчет о самообследовании</a>'
                                     '<a href="reglament.html">Регламент договоров</a></body></html>', encoding="utf-8")
    body = "".join(f"<p>2.{i}. Договор согласовывается в течение {i} рабочих дней.</p>" for i in range(1, 120))
    (site / "reglament.html").write_text(f"<html><head><title>Регламент</title></head><body><h1>РЕГЛАМЕНТ согласования договоров</h1>{body}</body></html>", encoding="utf-8")
    handler = partial(SimpleHTTPRequestHandler, directory=str(site))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        res = crawl([f"http://127.0.0.1:{port}/index.html"], tmp_path / "out", max_depth=1, delay=0.0)
    finally:
        srv.shutdown()
    files = {s["file"] for s in res.saved}
    assert any(f.endswith(".pdf") and "komandirovkah" in f for f in files)
    assert any(f.endswith(".html") for f in files)  # страница-регламент сохранена как документ
    assert not any("samoobsled" in f for f in files)
    assert (tmp_path / "out" / "manifest.json").exists()
