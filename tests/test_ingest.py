from datetime import date
from pathlib import Path

from navigator.ingest.loaders import load_file
from navigator.ingest.pipeline import build_document, ingest
from navigator.ingest.structure import build_clauses

FIX = Path(__file__).parent / "fixtures"


def test_structure_polozhenie():
    raw = load_file(FIX / "polozhenie_otpusk.txt")
    clauses, stats = build_clauses("d1", raw, doc_title="Положение об отпусках")
    ids = [c.id for c in clauses]
    assert "2.2" in ids and "2.3" in ids and "3.2" in ids
    c22 = next(c for c in clauses if c.id == "2.2")
    assert "14 календарных дней" in c22.text
    assert c22.parent_id == "2"
    assert any(c.kind == "item" and c.parent_id == "2.3" for c in clauses)
    assert any(c.attachment == "Прил.1" and c.number == "1" for c in clauses)
    assert not stats.fallback


def test_metadata_and_relations():
    raw = load_file(FIX / "prikaz_izmeneniya.txt")
    doc = build_document(raw, FIX / "prikaz_izmeneniya.txt")
    assert doc.doc_type == "приказ"
    assert doc.number == "215-1"
    assert doc.date == date(2025, 2, 5)
    assert doc.title.startswith("О внесении изменений")
    types = {r.type for r in doc.relations}
    assert "amends" in types and "cancels" in types
    cancel = next(r for r in doc.relations if r.type == "cancels")
    assert cancel.target_number == "15-1" and cancel.target_date == date(2023, 1, 20)
    amend = next(r for r in doc.relations if r.type == "amends")
    assert amend.approving_number == "101-1"
    ops = sorted(p.op for p in doc.patches)
    assert ops == ["delete", "insert", "replace", "substitute"]


def test_full_pipeline_applies_patches_and_statuses(tmp_path):
    kb = ingest(FIX, tmp_path, today=date(2026, 11, 3))
    pol = next(d for d in kb.documents if d.doc_type == "положение")
    old = next(d for d in kb.documents if d.number == "15-1")
    amend = next(d for d in kb.documents if d.number == "215-1")
    assert pol.date == date(2024, 3, 12)  # дата из «УТВЕРЖДЕНО приказом»
    assert old.status == "cancelled" and amend.id in old.cancelled_by
    assert pol.status == "active" and amend.id in pol.amended_by
    c22 = pol.clause_by_number("2.2")
    assert "10 календарных дней" in c22.text and amend.id in c22.provenance
    assert "14 календарных дней" in (c22.original_text or "")
    assert pol.clause_by_number("2.5") is not None
    assert pol.clause_by_number("3.2").deleted
    assert "5 рабочих дней" in pol.clause_by_number("2.4").text
    assert amend.unapplied_patches == 0
    assert kb.audit is not None and kb.audit.counts["documents"] == 3


def test_section_without_heading_line():
    from navigator.ingest.loaders import RawDocument, RawPage

    text = "РЕГЛАМЕНТ согласования договоров\n1. Общие положения.\n1.1. Настоящий Регламент определяет порядок согласования договоров.\n" + "\n".join(
        f"2.{i}. Договор согласовывается подразделением номер {i} в течение {i} рабочих дней со дня поступления." for i in range(1, 8))
    raw = RawDocument(source_path="x.html", file_type="html", pages=[RawPage(1, text)])
    clauses, stats = build_clauses("d", raw, "Регламент")
    assert not stats.fallback
    assert {c.id for c in clauses} >= {"1.1", "2.1", "2.7"}


def test_styled_headings_split_sections():
    """Заголовки разделов без номера, набранные полужирным по центру, не склеиваются с пунктами и задают раздел."""
    from navigator.ingest.loaders import RawDocument, RawPage

    text = ("РЕГЛАМЕНТ\nкомандирования работников\n\nОбщие положения\n"
            "1. Регламент устанавливает порядок направления работников в служебные командировки.\n"
            "2. Срок командировки определяется работодателем с учётом объёма служебного поручения.\n"
            "Процедура оформления документов\nпо командировке\n"
            "3. Руководитель структурного подразделения принимает решение о направлении работника в командировку.\n"
            "4. Работник представляет документы в группу командирования не позднее чем за 7 рабочих дней до\nкомандировки в\nМФТИ.\n"
            "5. Работник по возвращении из командировки обязан представить отчёт о расходах.\n")
    raw = RawDocument(source_path="x.pdf", file_type="pdf", pages=[RawPage(1, text)],
                      heading_lines={"РЕГЛАМЕНТ", "Общие положения", "Процедура оформления документов", "по командировке"})
    clauses, _ = build_clauses("d1", raw, doc_title="Регламент командирования")
    by_id = {c.id: c for c in clauses}
    heads = [c.text for c in clauses if c.kind == "heading"]
    assert "Общие положения" in heads
    assert "Процедура оформления документов по командировке" in heads  # две строки одного заголовка
    assert by_id["2"].text.endswith("служебного поручения.")  # заголовок не приклеился к пункту
    section = next(c for c in clauses if c.text.startswith("Процедура оформления"))
    assert by_id["3"].parent_id == section.id and by_id["5"].parent_id == section.id
    assert by_id["3"].path[-1].startswith("Процедура оформления")
    assert by_id["4"].text.endswith("МФТИ.")  # перенесённый хвост предложения не стал заголовком
    assert "МФТИ." not in heads


def test_plain_text_without_style_info_is_unchanged():
    """Без сведений об оформлении строка без номера остаётся продолжением пункта: поведение для DOCX, TXT и сканов прежнее."""
    from navigator.ingest.loaders import RawDocument, RawPage

    text = "1. Первый пункт документа с текстом.\nСроки командировки\n2. Второй пункт документа с текстом.\n3. Третий пункт документа с текстом.\n"
    raw = RawDocument(source_path="x.txt", file_type="txt", pages=[RawPage(1, text)])
    clauses, _ = build_clauses("d1", raw, doc_title="Документ")
    assert not any(c.kind == "heading" and c.text == "Сроки командировки" for c in clauses)


def test_clause_labels_are_readable():
    from navigator.schemas import Clause

    def label(cid, **kw):
        return Clause(id=cid, doc_id="d", text="x", **kw).label

    assert label("3.2", number="3.2") == "п. 3.2"
    assert label("16.-2", kind="item") == "п. 16, абз. 2"
    assert label("2.3.а", kind="item") == "пп. «а» п. 2.3"
    assert label("2.-1", kind="paragraph") == "п. 2, абз. 1"
    assert label("3.-1.-2", kind="item") == "п. 3, абз. 1, перечисление 2"
    assert label("p7.-1", kind="item") == "абз. 7, перечисление 1"
    assert label("6#12.-1", kind="paragraph") == "п. 6, абз. 1"
    assert label("p3@Прил.2", kind="paragraph", attachment="Прил.2") == "Прил. 2, абз. 3"
    assert label("Прил.1:4", number="4", attachment="Прил.1") == "Прил. 1, п. 4"


def test_docx_numbered_section_heading_is_split_from_first_paragraph():
    """В DOCX строка — абзац: «2. Порядок открытия счёта» и следующий абзац с заглавной буквы — заголовок раздела и его первый абзац."""
    from navigator.ingest.loaders import RawDocument, RawPage

    text = ("1. Общие положения\nНастоящий Регламент определяет порядок открытия финансового лицевого счета.\n"
            "1.1. Финансовый лицевой счет открывается для каждого проекта.\n"
            "2. Порядок открытия финансового лицевого счета\nОснованием для открытия счета является договор.\n"
            "2.1. Руководитель проекта направляет служебную записку в финансовый отдел.\n"
            "2.2. Финансовый отдел присваивает номер счета в течение 3 рабочих дней.\n")
    raw = RawDocument(source_path="x.docx", file_type="docx", pages=[RawPage(1, text)])
    clauses, _ = build_clauses("d1", raw, doc_title="Регламент")
    by_id = {c.id: c for c in clauses}
    assert by_id["2"].kind == "heading" and by_id["2"].text == "Порядок открытия финансового лицевого счета"
    assert by_id["2.-1"].kind == "paragraph" and by_id["2.-1"].label == "п. 2, абз. 1"
    assert by_id["2.-1"].text.startswith("Основанием для открытия")
    assert by_id["2.1"].parent_id == "2" and by_id["2.1"].path[-1].startswith("Порядок открытия")


def test_enumeration_inside_clause_and_number_on_separate_line():
    """«1) ... 2) ...» внутри пункта остаются его перечнем; номер раздела на отдельной строке привязывается к заголовку."""
    from navigator.ingest.loaders import RawDocument, RawPage

    text = ("1. ОБЩИЕ ПОЛОЖЕНИЯ\n1.1. Регламент определяет порядок подбора персонала.\n"
            "2.\nТЕРМИНЫ И ОПРЕДЕЛЕНИЯ\n2.1. Заявка – документ, составленный руководителем.\n"
            "2.2. Формат работы с вакансией – способ обработки откликов. Возможны форматы:\n"
            "1) отбор откликов осуществляется самостоятельно;\n2) отбор откликов осуществляется УРП;\n3) УРП проводит телефонное интервью.\n"
            "3.\nПОРЯДОК ПОИСКА И ПОДБОРА ПЕРСОНАЛА\n3.1. Инициатором поиска является руководитель подразделения.\n"
            "3.2. УРП размещает объявление о вакансии в течение одного рабочего дня.\n")
    raw = RawDocument(source_path="x.pdf", file_type="pdf", pages=[RawPage(1, text)])
    clauses, _ = build_clauses("d1", raw, doc_title="Регламент")
    by_id = {c.id: c for c in clauses}
    assert by_id["3"].kind == "heading" and by_id["3"].text == "ПОРЯДОК ПОИСКА И ПОДБОРА ПЕРСОНАЛА"
    assert by_id["3.1"].parent_id == "3" and by_id["3.2"].parent_id == "3"
    items = [c for c in clauses if c.parent_id == "2.2" and c.kind == "item"]
    assert len(items) == 3 and items[2].text.startswith("3) УРП проводит")
    assert items[0].label == "п. 2.2, абз. 1"
