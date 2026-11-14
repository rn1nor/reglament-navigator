"""Streamlit-интерфейс навигатора: «Спросить», «Мой навигатор», «Документы», «Администратору», «Качество».

Запуск: streamlit run navigator/ui/app.py
Навигация сверху, отрисовывается только открытая страница. Настройки (роль, дата, режим администратора) и лента
диалога лежат в st.session_state под своими ключами, поэтому переживают переход между страницами.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import traceback
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional

import streamlit as st
import yaml

from navigator.config import default_kb
from navigator.eval.run import TYPE_NAMES
from navigator.ingest.crawl import crawl
from navigator.ingest.impact import analyze
from navigator.ingest.reindex import get_sources, rebuild, uploads_dir, web_dir
from navigator.qa.engine import Navigator
from navigator.qa.mynav import ReadState, applicable_documents, duties_and_deadlines, recent_changes
from navigator.schemas import ROLE_LABELS, Document, Response
from navigator.ui import style as S
from navigator.ui.preview import render_page

st.set_page_config(page_title="Навигатор по регламентам МФТИ", page_icon="🧭", layout="wide", initial_sidebar_state="collapsed")

KB_DIR = default_kb()
RESULTS_DIR = Path("data/eval/results")
ROLE_OPTIONS = ["не указывать", *[v for k, v in ROLE_LABELS.items() if k != "all"]]
CHAT_HINT = "Например: как оформить служебную командировку?"
WIDE, FEED = 1180, 880  # ширина страниц с таблицами и ленты ответа, px

OUTCOME = {  # исход: подпись, цвет метки, значок
    "answer": ("Ответ найден", "green", "check_circle"),
    "partial": ("Частичный ответ", "orange", "rule"),
    "refuse": ("В документах нет ответа", "gray", "search_off"),
    "clarify": ("Нужно уточнение", "blue", "help"),
    "cancelled_notice": ("Документ отменён или заменён", "red", "history"),
    "out_of_scope": ("Запрос вне области", "gray", "block"),
}
STATUS = {  # статус документа: подпись, цвет метки
    "active": ("действует", "green"), "cancelled": ("отменён", "red"), "superseded": ("предыдущая редакция", "orange"),
    "expired": ("срок истёк", "gray"), "unknown": ("статус не определён", "gray"),
}
RELATION = {"approves": "утверждает", "approved_by": "утверждён документом", "amends": "изменяет", "cancels": "отменяет",
            "replaces": "заменяет", "references": "ссылается на"}
AUDIT_KIND = {"dangling_reference": "ссылка на документ, которого нет в базе", "no_date": "не определена дата", "no_number": "не определён номер",
              "no_numbering": "нет нумерации пунктов", "old_edition": "предыдущая редакция", "low_text_quality": "низкое качество текста",
              "needs_ocr": "скан без текстового слоя", "read_error": "ошибка чтения файла", "unapplied_patches": "поправки не применены",
              "duplicate": "дубликат", "cancelled": "отменённый документ"}
SEVERITY = {"error": "ошибка", "warning": "предупреждение", "info": "справка"}
PATCH_OP = {"replace": "изменён", "insert": "добавлен", "delete": "исключён", "substitute": "заменены слова"}
ROLE_HINTS = [("аспирант", "postgraduate"), ("абитуриент", "applicant"), ("студент", "student"), ("обуча", "student"),
              ("преподав", "teacher"), ("ппс", "teacher"), ("научн", "researcher"), ("ауп", "admin"), ("административ", "admin"),
              ("руковод", "admin"), ("сотрудник", "staff"), ("работник", "staff")]
ABBR = {"мфти", "флс", "нир", "окр", "ниокр", "эб", "цод", "ауп", "ппс", "рф", "фэу", "фэо", "фхд", "гост", "рцни"}
INSTITUTION_RE = re.compile(
    r"(?:федеральн\w+\s+государственн\w+\s+автономн\w+\s+образовательн\w+\s+учреждени\w+\s+высшего\s+образования\s*)?"
    r"[«\"]?Московск\w+\s+физико-техническ\w+\s+институт\w*\s*\(национальн\w+\s+исследовательск\w+\s+университет\)[»\"]?", re.I)


# ============================================================ база и состояние
@st.cache_resource(show_spinner="Загружаю базу документов и модели поиска. Первый запуск может занять несколько минут: модели скачиваются один раз.")
def get_nav() -> Navigator:
    return Navigator(KB_DIR)


try:
    nav = get_nav()
except Exception as e:  # noqa: BLE001  база не собрана или повреждена: объясняем, а не показываем стек
    traceback.print_exc()
    st.error(f"Не удалось открыть базу документов «{KB_DIR}»: {type(e).__name__}. Соберите базу командой "
             f"`python scripts/rebuild.py --kb {KB_DIR} --src data/raw/{KB_DIR.name}` и обновите страницу.")
    st.stop()
kb = nav.kb
docs: dict[str, Document] = {d.id: d for d in kb.documents}

ss = st.session_state
ss.setdefault("history", [])            # [(вопрос, подпись роли, Response)]
ss.setdefault("ui_role", ROLE_OPTIONS[0])
ss.setdefault("ui_use_date", False)
ss.setdefault("ui_date", _dt.date.today())
ss.setdefault("ui_admin", False)
ss.setdefault("ui_admin_name", "")


def sticky(widget: Callable[..., Any], label: str, store: str, **kw: Any) -> Any:
    """Виджет, значение которого хранится под ключом store и не теряется, когда страница с ним не отрисована."""
    wkey = f"w:{store}"
    if wkey not in ss:
        ss[wkey] = ss[store]

    def _save() -> None:
        ss[store] = ss[wkey]

    return widget(label, key=wkey, on_change=_save, **kw)


def role_key(label: Optional[str]) -> Optional[str]:
    return next((k for k, v in ROLE_LABELS.items() if v == label and k != "all"), None)


def role_from_option(option: str) -> Optional[str]:
    """Вариант уточнения («студент», «сотрудник АУП») -> код роли."""
    low = option.lower()
    for key, lab in ROLE_LABELS.items():
        if key != "all" and (low in lab.lower() or lab.lower() in low):
            return key
    return next((key for hint, key in ROLE_HINTS if hint in low), None)


def get_doc(doc_id: Optional[str]) -> Optional[Document]:
    return (docs.get(doc_id) or kb.get(doc_id)) if doc_id else None


def demo_bank() -> dict[str, list[dict[str, Any]]]:
    """Примеры вопросов: файл рядом с базой, файл под имя базы или общий банк."""
    for p in (KB_DIR / "demo_questions.yaml", Path("data/eval") / f"demo_questions_{KB_DIR.name}.yaml", Path("data/eval/demo_questions.yaml")):
        if not p.exists():
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return {}
        if not isinstance(data, dict):
            return {}
        bank = {str(g): [i for i in (items or []) if isinstance(i, dict) and i.get("q")] for g, items in data.items()}
        return {g: items for g, items in bank.items() if items}
    return {}


# ============================================================ названия документов
def nice_title(title: Optional[str]) -> str:
    """Название для показа: без полного официального имени вуза и без набора прописными."""
    t = INSTITUTION_RE.sub("МФТИ", " ".join((title or "").split()))
    t = re.sub(r"(МФТИ|[»\"])\d{1,2}(?=\s|$)", r"\1", t)  # знак сноски, приклеившийся к названию
    letters = [ch for ch in t if ch.isalpha()]
    if letters and all(ch.isupper() for ch in letters):
        low = t.lower()
        t = re.sub(r"[а-яёa-z]+", lambda m: m.group(0).upper() if m.group(0) in ABBR else m.group(0), low)
        t = t[:1].upper() + t[1:]
    else:
        m = re.match(r"^([А-ЯЁ]{4,})(?=\s+[а-яё«])", t)  # «РЕГЛАМЕНТ командирования ...»
        if m:
            t = m.group(1).capitalize() + t[m.end():]
    return t


def approval_note(d: Document) -> str:
    """Чем утверждён акт без собственного номера: «утв. приказом № 5990-1 от 03.11.2026» или год."""
    if d.doc_type in ("приказ", "распоряжение") or d.number:
        return ""
    appr = next((r for r in d.relations if r.type == "approved_by" and r.target_number), None)
    if appr:
        when = f" от {appr.target_date:%d.%m.%Y}" if appr.target_date else ""
        return f"утв. приказом № {appr.target_number}{when}"
    return f"{d.date.year} г." if d.date else ""


def order_head(doc_type: str, number: Optional[str], date_label: str) -> str:
    head = doc_type.capitalize()
    if number:
        head += f" № {number}"
    if date_label:
        head += f" от {date_label}"
    return head


def doc_brief(d: Optional[Document], n: int = 62) -> str:
    """Короткое имя документа для строки источника под шагом."""
    if d is None:
        return "документ"
    if d.doc_type in ("приказ", "распоряжение") and d.number:
        return order_head(d.doc_type, d.number, f"{d.date:%d.%m.%Y}" if d.date else "")
    return S.clip(nice_title(d.title), n) or d.doc_type.capitalize()


def date_text(d: Document) -> str:
    if not d.date:
        return ""
    return f"{d.date.year} г." if any("только год" in w for w in d.warnings) else f"{d.date:%d.%m.%Y}"


def status_text(d: Document) -> str:
    text = STATUS.get(d.status, STATUS["unknown"])[0]
    return text + " (с изменениями)" if d.status == "active" and d.amended_by else text


def reason_text(status: str, reason: Optional[str]) -> str:
    """Причина статуса без повтора слова, которое уже стоит на метке: «отменён: Приказ ...» -> «основание: Приказ ...»."""
    r = (reason or "").strip()
    cut = re.sub(r"^(отмен[её]н\w*|предыдущая\s+редакция|замен[её]н\w*|утратил\w*\s+силу|срок\s+ист[её]к)\s*[:;,]\s*", "", r, flags=re.I)
    if cut != r and status == "cancelled":
        return "основание: " + cut
    return cut


def doc_row_html(doc_type: str, number: Optional[str], date_label: str, title: str, status: str, reason: Optional[str],
                 url: Optional[str], note: str = "", n: int = 118) -> str:
    """Строка документа: метка статуса, реквизиты, название, причина статуса, ссылка на оригинал."""
    text, color = STATUS.get(status, STATUS["unknown"])
    t = nice_title(title)
    if doc_type in ("приказ", "распоряжение") and number:
        name = order_head(doc_type, number, date_label) + (f". {S.clip(t, n)}" if t else "")
    else:
        name = S.clip(t, n) or order_head(doc_type, number, date_label)
    tail = f' <span class="nv-meta">({S.esc(note)})</span>' if note else ""
    why = reason_text(status, reason)
    if why:
        tail += f' <span class="nv-meta">{S.esc(S.clip(why, 120))}</span>'
    href = S.safe_url(url)
    if href:
        tail += f' <a href="{href}" target="_blank" rel="noopener">оригинал</a>'
    return f'<div class="nv-doc">{S.chip(text, color)}<span>{S.esc(name)}{tail}</span></div>'


def card_row_html(dc: Any) -> str:
    d = get_doc(dc.doc_id)
    note = approval_note(d) if d else ""
    return doc_row_html(dc.doc_type, dc.number, "" if note else dc.date_label, dc.title, dc.status, dc.status_reason, dc.source_url, note)


# ============================================================ ответ
def citation_block(c: Any, key: str, name_doc: bool = True, extra: Optional[list[Any]] = None) -> None:
    """Строка источника под шагом: раскрывается в текст пункта, редакцию и страницу оригинала с подсветкой.
    extra: остальные цитаты шага из того же документа (абзацы перечня): показываются внутри того же блока."""
    d = get_doc(c.doc_id)
    cl = d.clause_by_id(c.clause_id) if d else None
    pv_key = f"{key}-pv"
    extra = extra or []
    more = [(d.clause_by_id(x.clause_id) if d else None, x) for x in extra]
    tails = [m.label.replace(f"{cl.label}, ", "") if cl is not None and m is not None and m.label.startswith(cl.label + ", ") else (m.label if m is not None else x.clause_id)
             for m, x in more]
    head = (cl.label if cl else c.clause_id)
    if tails:
        head += " (" + (", ".join(tails) if len(tails) <= 3 else ", ".join(tails[:2]) + f" и ещё {len(tails) - 2}") + ")"
    head += f" · {doc_brief(d)}" if name_doc else ""
    # блок остаётся раскрытым, пока включён показ страницы: иначе перерисовка после переключателя сворачивает его
    with st.expander(S.plain(head), expanded=bool(ss.get(pv_key)), type="compact"):
        if cl is None or d is None:
            st.caption("Пункт не найден в текущей версии базы.")
            return
        section = cl.path[-1] if len(cl.path) > 1 else ""
        S.html_block((f'<div class="nv-meta">{S.esc(S.clip(section, 140))}</div>' if section else "")
                     + f'<div class="nv-quote">{S.mark_quote(cl.text, c.quote)}</div>'
                     + "".join(f'<div class="nv-meta">{S.esc(m.label)}</div><div class="nv-quote">{S.mark_quote(m.text, x.quote)}</div>' for m, x in more if m is not None))
        meta = [S.clip(nice_title(d.title), 150)]
        note = approval_note(d)
        if note:
            meta.append(note)
        if cl.provenance:
            meta.append("в редакции: " + "; ".join(doc_brief(get_doc(p)) for p in cl.provenance))
        if cl.page_start:
            meta.append(f"страница оригинала: {cl.page_start}")
        S.html_block(f'<div class="nv-meta">{S.esc(" · ".join(meta))}</div>')
        if cl.original_text and cl.original_text != cl.text:
            with st.popover("Исходная редакция пункта", type="tertiary"):
                S.html_block(f'<div class="nv-quote">{S.esc(cl.original_text)}</div>')
        is_pdf = bool(d.source_path) and d.source_path.lower().endswith(".pdf") and Path(d.source_path).exists()
        if is_pdf and cl.page_start and st.toggle("Показать страницу оригинала", key=pv_key):
            png = render_page(d.source_path, cl.page_start, c.quote or cl.text[:80])
            if png:
                st.image(png, caption=f"{Path(d.source_path).name}, стр. {cl.page_start}", width="stretch")
            else:
                st.caption("Страницу оригинала показать не удалось.")
        href = S.safe_url(d.source_url)
        if href:
            S.html_block(f'<div class="nv-doc"><a href="{href}" target="_blank" rel="noopener">Открыть оригинал на сайте</a></div>')


def conflict_note(resp: Response) -> Optional[str]:
    """Одна фраза о расхождении между документами по процитированным пунктам."""
    failed = next((c for c in resp.trust.checks if c.name.startswith("Противоречий") and not c.passed), None)
    if failed is None:
        return None
    pairs: list[str] = []
    seen: set[tuple[str, str, str, str]] = set()
    for s in resp.steps:
        for c in s.citations:
            for other in nav.contradictions.get((c.doc_id, c.clause_id), []):
                pair = (c.doc_id, c.clause_id, other["doc_id"], other["clause_id"])
                if pair in seen or (pair[2], pair[3], pair[0], pair[1]) in seen:
                    continue
                seen.add(pair)
                a, b = get_doc(c.doc_id), get_doc(other["doc_id"])
                ca = a.clause_by_id(c.clause_id) if a else None
                cb = b.clause_by_id(other["clause_id"]) if b else None
                pairs.append(f"{ca.label if ca else c.clause_id} ({doc_brief(a)}) и {cb.label if cb else other['clause_id']} ({doc_brief(b)})")
    detail = "; ".join(pairs[:3]) if pairs else failed.detail.replace(" ↔ ", " и ")
    return f"Документы расходятся по этому вопросу, сверьте оба пункта: {detail}."


def render_response(resp: Response, key: str) -> None:
    admin = bool(ss["ui_admin"])
    label, color, icon = OUTCOME.get(resp.outcome, (resp.outcome, "gray", "info"))
    if resp.trace.get("provider") == "validation":
        label, icon = "Запрос не распознан", "edit_off"
    elif resp.trace.get("error"):
        label, color, icon = "Техническая ошибка", "red", "error"
    badges = f":{color}-badge[:material/{icon}: {label}]"
    if resp.trust.verified_by_human:
        badges += f" :blue-badge[:material/verified: Подтверждён сотрудником: {S.md(resp.trust.verified_by_human)}]"
    with st.container(key=f"ans-{key}"):
        st.markdown(badges)
        S.html_block(f'<div class="nv-summary">{S.esc(resp.summary)}</div>')

        notes = [w for w in dict.fromkeys(resp.trust.warnings) if "с иными параметрами" not in w]
        conflict = conflict_note(resp)
        if conflict and not any("асхожд" in w for w in notes):  # движок мог уже описать расхождение своими словами
            notes.insert(0, conflict)
        if notes:
            st.warning("  \n".join(S.md(n[:1].upper() + n[1:]) for n in notes), icon=":material/warning:")

        if resp.clarifying_question:
            st.info(S.md(resp.clarifying_question), icon=":material/help:")
            with st.container(horizontal=True, gap="xsmall"):
                for i, opt in enumerate(resp.clarifying_options):
                    if st.button(S.plain(opt), key=f"{key}-opt{i}"):
                        if getattr(resp, "clarify_kind", None) == "document":
                            # движок понимает «Документ: <название>» и ищет ответ только в выбранном документе
                            ss["pending"] = {"q": f"{resp.question} Документ: {opt}", "role": resp.role}
                        else:
                            rk = role_from_option(opt)
                            ss["pending"] = {"q": resp.question if rk else f"{resp.question} Уточнение: {opt}", "role": rk or resp.role}
                        st.rerun()

        if resp.steps:
            procedure = getattr(resp, "answer_kind", None) == "procedure"
            S.label("Порядок действий" if procedure else "Основание", getattr(resp, "procedure_source", None) if procedure else None)
            for i, s in enumerate(resp.steps, 1):
                part = '<div class="nv-note">подтверждено документом частично</div>' if s.verdict == "partial" else ""
                S.html_block(f'<div class="nv-step"><div class="nv-num">{i}</div><div class="nv-step-text">{S.esc(s.text)}{part}</div></div>')
                with st.container(key=f"cite-{key}-{i}", gap=None):
                    # одна строка источника на документ: пункт и его абзацы раскрываются вместе
                    groups: list[list[Any]] = []
                    for c in s.citations:
                        if groups and groups[-1][0].doc_id == c.doc_id:
                            groups[-1].append(c)
                        else:
                            groups.append([c])
                    for j, group in enumerate(groups):
                        citation_block(group[0], f"{key}-s{i}c{j}", extra=group[1:])
        if resp.deadlines:
            S.label("Сроки")
            S.bullets(resp.deadlines)
        if resp.conditions:
            S.label("Условия и исключения")
            S.bullets(resp.conditions)
        if resp.not_covered:
            S.label("Не найдено в документах базы")
            S.bullets(resp.not_covered)
        pairs = [(k, v) for k, v in (("Куда обращаться", resp.responsible), ("Формы", ", ".join(resp.forms))) if v]
        if pairs:
            S.html_block('<div class="nv-kvs">' + "".join(f'<div class="nv-kv"><span class="k">{k}</span><span>{S.esc(v)}</span></div>' for k, v in pairs) + "</div>")
        cards = resp.documents or resp.checked_documents
        if cards:
            S.label("Документы" if resp.documents else "Проверенные документы")
            S.html_block("".join(card_row_html(dc) for dc in cards))

        checks = resp.trust.checks
        if checks:
            passed = sum(1 for c in checks if c.passed)
            total_ms = resp.timings_ms.get("total") or sum(v for k, v in resp.timings_ms.items() if k != "total")
            head = f"Проверки: {passed} из {len(checks)} пройдено"
            if total_ms and total_ms >= 100:
                head += f" · ответ за {total_ms / 1000:.1f} с".replace(".", ",")
            with st.expander(head, type="compact"):
                rows = "".join(f'<div class="nv-check"><span class="{"ok" if c.passed else "bad"}">{"✓" if c.passed else "!"}</span>'
                               f'<span>{S.esc(c.name)}<span class="d"> · {S.esc(c.detail.replace(" ↔ ", " и "))}</span></span></div>' for c in checks)
                if resp.trust.dropped_steps:
                    rows += '<div class="nv-label">Отброшено как неподтверждённое документами</div>'
                    rows += "".join(f'<div class="nv-check nv-strike">{S.esc(x)}</div>' for x in resp.trust.dropped_steps)
                S.html_block(rows)
        if admin:
            with st.expander("Трасса поиска", type="compact"):
                st.json({k: resp.trace.get(k) for k in ("provider", "embedder", "reranker", "queries", "method", "decision", "hits") if k in resp.trace}, expanded=False)
                st.caption(f"Время, мс: {resp.timings_ms}")
            if resp.outcome in ("answer", "partial") and resp.steps and not resp.trust.verified_by_human:
                if st.button("Подтвердить ответ как правильный", key=f"{key}-approve", icon=":material/verified:"):
                    try:
                        nav.approve(resp, approved_by=ss["ui_admin_name"] or "администратор")
                        st.success("Ответ сохранён как проверенный. Он будет выдаваться на похожие вопросы и устареет автоматически, если процитированные пункты изменятся.")
                    except ValueError as e:  # ответ без опоры на пункты подтверждать нельзя
                        st.error(f"Ответ не сохранён: {e}")


def render_turn(question: str, role_label: str, resp: Optional[Response], key: str) -> None:
    with st.chat_message("user", avatar=":material/person:"):
        role_line = f'<div class="nv-q-role">роль: {S.esc(role_label)}</div>' if role_label else ""
        S.html_block(f'<div class="nv-q" id="nv-turn-{key}">{S.esc(question)}</div>{role_line}')
    if resp is not None:
        with st.chat_message("assistant", avatar=":material/explore:"):
            render_response(resp, key)


# ============================================================ страница «Спросить»
def ask_example(question: str, role: Optional[str]) -> None:
    ss["pending"] = {"q": question, "role": role or role_key(ss["ui_role"])}


def settings_popover() -> None:
    chosen = ss["ui_role"]
    title = "Роль: " + ("не указана" if chosen == ROLE_OPTIONS[0] else chosen)
    if ss["ui_use_date"]:
        title += f" · на {ss['ui_date']:%d.%m.%Y}"
    with st.popover(title, icon=":material/tune:", type="tertiary", key="ask-settings"):
        sticky(st.selectbox, "Кто вы", "ui_role", options=ROLE_OPTIONS, help="Роль уточняет, какие документы к вам применимы")
        sticky(st.toggle, "Ответ на другую дату", "ui_use_date", help="Статусы документов и сроки считаются на выбранную дату")
        if ss["ui_use_date"]:
            sticky(st.date_input, "Дата", "ui_date", format="DD.MM.YYYY")


def example_buttons(items: list[dict[str, Any]], prefix: str) -> None:
    for i, item in enumerate(items):
        st.button(S.plain(item["q"]), key=f"{prefix}-{i}", on_click=ask_example, args=(item["q"], item.get("role") or None), type="tertiary")


def more_examples(bank: dict[str, list[dict[str, Any]]], skip: int, prefix: str) -> None:
    """Остальные примеры: остаток первой группы и другие группы банка."""
    groups = {g: (items[skip:] if n == 0 else items) for n, (g, items) in enumerate(bank.items())}
    groups = {g: items for g, items in groups.items() if items}
    if not groups:
        st.caption("Других примеров нет.")
        return
    names = list(groups)
    group = st.selectbox("Тема", names, key=f"{prefix}-group", label_visibility="collapsed") if len(names) > 1 else names[0]
    example_buttons(groups[group], f"{prefix}-{names.index(group)}")


def hero(bank: dict[str, list[dict[str, Any]]]) -> None:
    facts = [f"В базе документов: {len(kb.documents)}"]
    facts.append("база закрытая, внешние модели не используются" if nav.confidential else "база открытая")
    if nav.provider.kind == "mock":
        facts.append("шаги приводятся дословно по тексту документов")
    S.html_block('<div class="nv-hero"><h1>Навигатор по регламентам</h1>'
                 "<p>Ответ строится только по пунктам документов базы. Каждый шаг можно проверить по первоисточнику.</p>"
                 f'<div class="nv-meta">{S.esc(" · ".join(facts))}</div></div>')
    with st.container(horizontal=True, horizontal_alignment="center"):
        settings_popover()
    first = next(iter(bank.values()), [])[:6]
    if first:
        with st.container(key="examples"):
            for a in range(0, len(first), 2):
                for col, (i, item) in zip(st.columns(2), enumerate(first[a:a + 2], start=a)):
                    col.button(S.plain(item["q"]), key=f"ex-{i}", on_click=ask_example, args=(item["q"], item.get("role") or None), width="stretch", wrap=True)
        with st.container(horizontal=True, horizontal_alignment="center"):
            with st.expander("Ещё примеры", type="compact"):
                with st.container(key="more-examples", gap=None):
                    more_examples(bank, len(first), "more")


def toolbar(bank: dict[str, list[dict[str, Any]]]) -> None:
    with st.container(horizontal=True, vertical_alignment="center", gap="xsmall"):
        settings_popover()
        if bank:
            with st.popover("Примеры вопросов", icon=":material/lightbulb:", type="tertiary", key="ask-examples"):
                with st.container(key="pop-examples", gap=None):
                    more_examples(bank, 0, "pop")
        st.space("stretch")
        st.button("Новый диалог", icon=":material/add_comment:", type="tertiary", key="ask-clear", on_click=lambda: ss.update(history=[]))


def page_ask() -> None:
    S.page(FEED)
    entered = ss.get("page") != "ask"  # пришли с другой страницы: покажем начало последнего ответа, а не его конец
    ss["page"] = "ask"
    ss["ask_visits"] = ss.get("ask_visits", 0) + int(entered)
    history: list[tuple[str, str, Response]] = ss["history"]
    pending = ss.pop("pending", None)
    typed = st.chat_input(CHAT_HINT, key="ask-input", max_chars=2000)
    question = (pending or {}).get("q") or typed
    q_role = (pending or {}).get("role") if pending else role_key(ss["ui_role"])
    bank = demo_bank()
    if not history and not question:
        hero(bank)
        return
    toolbar(bank)
    for i, (q, r_label, resp) in enumerate(history):
        if i:
            S.html_block('<hr class="nv-sep">')
        render_turn(q, r_label, resp, f"h{i}")
    if question:
        r_label = ROLE_LABELS.get(q_role, str(q_role)) if q_role else ""
        if history:
            S.html_block('<hr class="nv-sep">')
        render_turn(question, r_label, None, f"h{len(history)}")
        with st.chat_message("assistant", avatar=":material/explore:"):
            with st.spinner("Ищу пункты документов и проверяю каждый шаг…"):
                try:
                    resp = nav.ask(question, role=q_role if q_role in ROLE_LABELS else None, as_of=ss["ui_date"] if ss["ui_use_date"] else None)
                except Exception as e:  # noqa: BLE001  движок сам ловит ошибки; это страховка, чтобы лента не пропала
                    traceback.print_exc()
                    resp = Response(outcome="refuse", question=question, as_of=_dt.date.today(), trace={"error": f"{type(e).__name__}: {e}"},
                                    summary=f"Техническая ошибка при обработке запроса ({type(e).__name__}). Ответ не сформирован, чтобы не выдать непроверенные сведения. Повторите запрос.")
            render_response(resp, f"h{len(history)}")
        history.append((question, r_label, resp))
    if history and (question or entered):
        S.scroll_to(f"nv-turn-h{len(history) - 1}", f"{len(history)}:{ss['ask_visits']}")


# ============================================================ страница «Мой навигатор»
def page_my() -> None:
    S.page(WIDE)
    ss["page"] = "my"
    st.subheader("Мой навигатор")
    st.caption("Документы, которые относятся к вашей роли, изменения в них, требования и сроки.")
    roles = ROLE_OPTIONS[1:]
    if "ui_my_role" not in ss:
        ss["ui_my_role"] = ss["ui_role"] if ss["ui_role"] in roles else roles[-1]
    c1, c2 = st.columns([1, 1.5], gap="large")
    with c1:
        my_label = sticky(st.selectbox, "Роль", "ui_my_role", options=roles)
    with c2:
        days = st.slider("Показывать изменения за последние, дней", 30, 730, 180, step=30)
    my_role = role_key(my_label) or "staff"
    read_state = ReadState(KB_DIR / "read_state.json")
    app_docs = applicable_documents(kb, my_role)
    changes = recent_changes(kb, app_docs, _dt.date.today() - _dt.timedelta(days=days))
    m1, m2, m3 = st.columns(3)
    m1.metric("Применимых документов", len(app_docs))
    m2.metric("Не отмечены как прочитанные", len(read_state.unread(my_role, app_docs)))
    m3.metric(f"Изменений за {days} дн.", len(changes))

    S.label("Изменения и новые документы")
    if changes:
        rows = []
        for ch in changes[:30]:
            note = ch["kind"]
            if ch.get("order"):  # у нового документа пояснение повторяло бы название
                note = f"{ch['kind']} ({doc_brief(ch['order'])}): {S.clip(ch['detail'], 140)}"
            rows.append(f'<div class="nv-change"><span class="dt">{ch["date"]:%d.%m.%Y}</span><span>{S.esc(S.clip(nice_title(ch["doc"].title), 120))}'
                        f' <span class="nv-meta">{S.esc(note)}</span></span></div>')
        S.html_block("".join(rows))
    else:
        st.caption("За выбранный период изменений нет.")

    S.label("Документы и отметки об ознакомлении")
    if not app_docs:
        st.caption("Для этой роли в базе нет применимых документов.")
    with st.container(gap=None):
        for d in app_docs:
            meta = ", ".join(x for x in (order_head(d.doc_type, d.number, date_text(d)) if d.number else d.doc_type, approval_note(d)) if x)
            line = f"{S.md(S.clip(nice_title(d.title), 110))} :gray[· {S.md(meta)}]"
            url = S.safe_url(d.source_url)
            if url:
                line += f" · [оригинал]({url})"
            was = read_state.is_read(my_role, d.id)
            now = st.checkbox(line, value=was, key=f"read-{my_role}-{d.id}")
            if now != was:
                read_state.mark(my_role, d.id, now)

    S.label("Требования и сроки из применимых документов")
    rows = duties_and_deadlines(app_docs)
    if rows:
        st.dataframe([{"документ": S.clip(nice_title(r["doc"].title), 70), "пункт": r["clause"].label, "срок": "; ".join(r["deadlines"]), "требование": r["text"]} for r in rows],
                     hide_index=True, column_config={"документ": st.column_config.TextColumn(width="medium"), "пункт": st.column_config.TextColumn(width="small"),
                                                     "срок": st.column_config.TextColumn(width="medium"), "требование": st.column_config.TextColumn(width="large")})
    else:
        st.caption("Требований со сроками в применимых документах не найдено.")


# ============================================================ страница «Документы»
def document_view(d: Document) -> None:
    st.markdown(f"#### {S.md(nice_title(d.title) or d.id)}")
    text, color = STATUS.get(d.status, STATUS["unknown"])
    head = ", ".join(x for x in (order_head(d.doc_type, d.number, date_text(d)) if d.number else d.doc_type, approval_note(d)) if x)
    roles = ", ".join(ROLE_LABELS.get(r, r) for r in d.roles)
    line = f'<div class="nv-doc">{S.chip(status_text(d), color)}<span>{S.esc(head)} <span class="nv-meta">· роли: {S.esc(roles)}</span>'
    if d.status_reason:
        line += f' <span class="nv-meta">· {S.esc(reason_text(d.status, d.status_reason))}</span>'
    href = S.safe_url(d.source_url)
    if href:
        line += f' <a href="{href}" target="_blank" rel="noopener">оригинал</a>'
    S.html_block(line + "</span></div>")
    if d.relations:
        rel = []
        for r in d.relations[:20]:
            target = get_doc(r.target_doc_id)
            rel.append(f"{RELATION.get(r.type, r.type)}: {doc_brief(target) if target else S.clip(r.target_ref, 70)}")
        S.html_block(f'<div class="nv-meta">Связи: {S.esc("; ".join(dict.fromkeys(rel)))}</div>')
    if d.warnings:
        S.html_block(f'<div class="nv-meta">Замечания разбора: {S.esc("; ".join(d.warnings[:4]))}</div>')
    if d.source_path.lower().endswith(".pdf") and Path(d.source_path).exists():
        with st.expander("Страницы оригинала", type="compact"):
            pg = st.number_input("Страница", min_value=1, max_value=max(1, d.pages), value=1, key=f"doc-page-{d.id}")
            png = render_page(d.source_path, int(pg))
            if png:
                st.image(png, width="stretch")
            else:
                st.caption("Страницу показать не удалось.")
    rows = []
    for c in d.clauses:
        if c.kind == "heading":
            rows.append(f'<div class="nv-h">{S.esc(c.text)}</div>')
            continue
        body = S.esc(c.text[:700] + ("…" if len(c.text) > 700 else ""))
        note = ""
        if c.deleted:
            body = f'<span class="nv-strike">{body}</span>'
            note = f'<div class="nv-del">исключён: {S.esc(", ".join(doc_brief(get_doc(p)) for p in c.provenance))}</div>'
        elif c.provenance:
            note = f'<div class="nv-edit">в редакции: {S.esc(", ".join(doc_brief(get_doc(p)) for p in c.provenance))}</div>'
        rows.append(f'<div class="nv-cl"><span class="nv-cl-id">{S.esc(c.label)}</span><div class="nv-cl-t">{body}{note}</div></div>')
    S.html_block("".join(rows) or '<div class="nv-meta">В документе не выделено ни одного пункта.</div>')


def page_docs() -> None:
    S.page(WIDE)
    ss["page"] = "docs"
    st.subheader("Документы")
    st.caption(f"В базе {len(kb.documents)} документов, собрана {kb.built_at[:16].replace('T', ' ')}. Отметьте строку слева, чтобы открыть документ.")
    f1, f2, f3, f4 = st.columns([1, 1, 1.2, 1.6])
    st_filter = f1.multiselect("Статус", sorted({d.status for d in kb.documents}), format_func=lambda s: STATUS.get(s, (s, ""))[0], placeholder="Все")
    type_filter = f2.multiselect("Тип", sorted({d.doc_type for d in kb.documents}), placeholder="Все")
    topic_filter = f3.multiselect("Тема", sorted({t for d in kb.documents for t in d.topics}), placeholder="Все")
    text_filter = f4.text_input("Поиск по названию", "", placeholder="Например: командирование")
    shown: list[Document] = []
    for d in kb.documents:
        if st_filter and d.status not in st_filter:
            continue
        if type_filter and d.doc_type not in type_filter:
            continue
        if topic_filter and not (set(topic_filter) & set(d.topics)):
            continue
        if text_filter and text_filter.lower() not in d.title.lower():
            continue
        shown.append(d)
    rows = [{"статус": status_text(d), "тип": d.doc_type, "номер": d.number or "", "дата": date_text(d), "название": S.clip(nice_title(d.title), 120),
             "темы": ", ".join(d.topics), "владелец": d.owner_unit or "", "пунктов": sum(1 for c in d.clauses if c.kind != "heading"),
             "роли": ", ".join(ROLE_LABELS.get(r, r) for r in d.roles), "изменён": len(d.amended_by)} for d in shown]
    if not rows:
        st.info("Под выбранные фильтры не попал ни один документ.")
        return
    table_key = "docs-" + str(abs(hash((tuple(st_filter), tuple(type_filter), tuple(topic_filter), text_filter))))
    opened = bool((ss.get(table_key) or {}).get("selection", {}).get("rows"))  # документ открыт: таблицу сжимаем, чтобы текст был виден сразу
    event = st.dataframe(rows, hide_index=True, height=min(240 if opened else 560, 36 * len(rows) + 42), on_select="rerun", selection_mode="single-row", key=table_key)
    picked = event.selection.rows if event is not None and hasattr(event, "selection") else []
    if picked and picked[0] < len(shown):
        st.divider()
        document_view(shown[picked[0]])


# ============================================================ страница «Администратору»
def tab_sources() -> None:
    st.caption("Добавьте файлы или ссылки и нажмите «Переиндексировать базу»: документы разберутся, индекс досчитается только для новых пунктов, паспорт базы обновится.")
    default_raw = Path("data/raw") / KB_DIR.name
    current = get_sources(KB_DIR, default_raw=default_raw)
    st.markdown("Текущие источники: " + (", ".join(f"`{p}`" for p in current) if current else "не заданы"))
    cu, cw = st.columns(2, gap="large")
    with cu:
        ups = st.file_uploader("Файлы регламентов и приказов", type=["pdf", "docx", "doc", "rtf", "odt", "xlsx", "html", "htm", "txt"], accept_multiple_files=True, key="src-upload")
        if ups:
            udir = uploads_dir(KB_DIR)
            udir.mkdir(parents=True, exist_ok=True)
            saved = 0
            for f in ups:
                target = udir / Path(f.name).name
                if not target.exists() or target.stat().st_size != len(f.getbuffer()):
                    target.write_bytes(f.getbuffer())
                    saved += 1
            st.info(f"Сохранено новых файлов: {saved}, папка `{udir}`. Нажмите «Переиндексировать базу».")
    with cw:
        urls = st.text_area("Ссылки: документы, страницы или разделы сайта, по одной в строке", placeholder="https://mipt.ru/sveden/document/", height=100)
        depth = st.select_slider("Глубина обхода раздела", options=[0, 1, 2], value=1, help="0: только указанная страница и файлы с неё; 1: плюс страницы того же раздела")
        if st.button("Скачать по ссылкам") and urls.strip():
            with st.spinner("Обхожу сайт и скачиваю документы…"):
                res = crawl([u.strip() for u in urls.splitlines() if u.strip()], web_dir(KB_DIR), max_depth=int(depth), progress=None)
            st.info(res.summary())
            if res.errors:
                st.warning("Не всё удалось скачать: " + "; ".join(S.md(e) for e in res.errors[:3]))
    if st.button("Переиндексировать базу", type="primary", icon=":material/sync:"):
        log = st.empty()
        lines: list[str] = []

        def _progress(msg: str) -> None:
            lines.append(msg)
            log.code("\n".join(lines[-8:]))

        try:
            with st.spinner("Разбираю документы, строю индекс и ищу противоречия…"):
                summary = rebuild(KB_DIR, get_sources(KB_DIR, default_raw=default_raw), progress=_progress)
        except (FileNotFoundError, ValueError) as e:  # источник недоступен или пуст: база остаётся прежней
            st.error(f"База не пересобрана и не изменена: {e}")
            return
        ss["rebuild_note"] = (f"База пересобрана за {summary['seconds']} с: документов {summary['documents']}, пунктов {summary['units']}, "
                              f"кандидатов в противоречия {summary['contradictions']}, находок аудита {summary['findings']}.")
        st.cache_resource.clear()
        st.rerun()
    note = ss.pop("rebuild_note", None)
    if note:
        st.success(note)


def tab_passport() -> None:
    if not kb.audit:
        st.caption("Паспорт появится после пересборки базы.")
        return
    counts = kb.audit.counts
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Документов", counts.get("documents", 0))
    c2.metric("Действуют", counts.get("status:active", 0))
    c3.metric("Отменены или заменены", counts.get("status:cancelled", 0) + counts.get("status:superseded", 0))
    c4.metric("Ссылок на отсутствующие", counts.get("dangling_refs", 0))
    c5.metric("Сканы без текста", sum(1 for f in kb.audit.findings if f.kind == "needs_ocr"))
    kinds = Counter(f.kind for f in kb.audit.findings)
    S.label("Находки аудита", ", ".join(f"{AUDIT_KIND.get(k, k)}: {v}" for k, v in kinds.most_common()) or "нет")
    sev = st.multiselect("Серьёзность", list(SEVERITY), default=["error", "warning"], format_func=lambda s: SEVERITY[s], label_visibility="collapsed", placeholder="Любая серьёзность")
    found = [{"находка": AUDIT_KIND.get(f.kind, f.kind), "серьёзность": SEVERITY.get(f.severity, f.severity),
              "документ": doc_brief(get_doc(f.doc_id)) if get_doc(f.doc_id) else (f.doc_id or ""), "сообщение": f.message}
             for f in kb.audit.findings if not sev or f.severity in sev]
    if found:
        st.dataframe(found, hide_index=True, height=260)
    else:
        st.caption("Находок выбранной серьёзности нет.")
    S.label("Поправки, которые не удалось применить автоматически")
    unapplied = [{"приказ": doc_brief(d), "операция": PATCH_OP.get(p.op, p.op), "пункт": p.clause_number, "цель": S.clip(p.target_ref, 60), "причина": p.note or ""}
                 for d in kb.documents for p in d.patches if not p.applied]
    if unapplied:
        st.dataframe(unapplied, hide_index=True)
    else:
        st.caption("Все найденные поправки применены.")
    S.label("Документы, на которые ссылаются, но которых нет в базе")
    if kb.stubs:
        st.dataframe([{"документ": s.short_name or s.title, "статус": STATUS.get(s.status, STATUS["unknown"])[0]} for s in kb.stubs], hide_index=True, height=220)
    else:
        st.caption("Таких документов нет.")


def tab_contradictions() -> None:
    st.caption("Кандидаты на проверку: пункты разных действующих документов с похожим смыслом и разными числами.")
    path = KB_DIR / "contradictions.json"
    if not path.exists():
        st.caption("Список ещё не построен. Он появится после пересборки базы на вкладке «Источники».")
        return
    try:
        pairs = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st.error("Файл со списком противоречий повреждён. Пересоберите базу на вкладке «Источники».")
        return
    if not pairs:
        st.caption("Кандидатов в противоречия не найдено.")
        return
    for n, pr in enumerate(pairs[:25]):
        a, b = pr["a"], pr["b"]
        da, db = get_doc(a.get("doc_id")), get_doc(b.get("doc_id"))
        ca = da.clause_by_id(a["clause_id"]) if da else None
        cb = db.clause_by_id(b["clause_id"]) if db else None
        la, lb = (ca.label if ca else a["clause_id"]), (cb.label if cb else b["clause_id"])
        with st.expander(S.plain(f"{la}, {doc_brief(da) if da else a['doc'][:55]} и {lb}, {doc_brief(db) if db else b['doc'][:55]} · сходство {pr['similarity']:.2f}"), expanded=n == 0):
            col_a, col_b = st.columns(2, gap="large")
            for col, side, d, lab in ((col_a, a, da, la), (col_b, b, db, lb)):
                with col:
                    S.html_block(f'<div class="nv-meta">{S.esc(doc_brief(d, 90) if d else side["doc"])} · {S.esc(lab)} · числа: {S.esc(", ".join(side["numbers"]))}</div>'
                                 f'<div class="nv-quote">{S.esc(side["text"])}</div>')


def tab_impact() -> None:
    st.caption("Загрузите приказ: система покажет, что он отменяет и меняет, какие пункты и роли затрагивает. База при этом не изменяется.")
    up = st.file_uploader("Файл приказа (PDF, DOCX, TXT)", type=["pdf", "docx", "txt", "doc", "rtf"], key="impact-upload")
    if up is None:
        return
    tmp = Path("data") / "uploads"
    tmp.mkdir(parents=True, exist_ok=True)
    fpath = tmp / Path(up.name).name
    stamp = (fpath.name, up.size, kb.built_at)
    if ss.get("impact_stamp") != stamp:
        fpath.write_bytes(up.getbuffer())
        with st.spinner("Разбираю приказ и сопоставляю с базой…"):
            try:
                ss["impact_report"] = analyze(kb, fpath)
            except Exception as e:  # noqa: BLE001  нечитаемый файл не должен ронять страницу
                traceback.print_exc()
                st.error(f"Не удалось разобрать файл «{fpath.name}»: {type(e).__name__}. Проверьте, что это текстовый PDF, DOCX или TXT.")
                return
        ss["impact_stamp"] = stamp
    rep = ss["impact_report"]
    st.success(re.sub(r"(?<=роли: )[a-z, ]+", lambda m: ", ".join(ROLE_LABELS.get(x.strip(), x.strip()) for x in m.group(0).split(",")), rep.summary))
    if rep.relations:
        S.label("Связи нового документа")
        S.bullets([f"{RELATION.get(r['type'], r['type'])}: {S.clip(r['target'], 90)}" + ("" if r["resolved"] else " (нет в базе)") for r in rep.relations[:12]])
    if rep.status_changes:
        S.label("Изменение статусов")
        st.dataframe([{"документ": S.clip(nice_title(c.title), 80), "было": STATUS.get(c.before, (c.before,))[0], "стало": STATUS.get(c.after, (c.after,))[0],
                       "причина": S.clip(c.reason, 140)} for c in rep.status_changes], hide_index=True)
    if rep.clause_changes:
        S.label("Изменение пунктов")
    for n, ch in enumerate(rep.clause_changes[:20]):
        with st.expander(S.plain(f"{ch.clause}: {PATCH_OP.get(ch.op, ch.op)} · {S.clip(nice_title(ch.title), 70)}"), expanded=n < 3):
            col_a, col_b = st.columns(2, gap="large")
            with col_a:
                S.html_block(f'<div class="nv-meta">Было</div><div class="nv-quote">{S.esc(ch.before or "пункта не было")}</div>')
            with col_b:
                S.html_block(f'<div class="nv-meta">Стало</div><div class="nv-quote">{S.esc(ch.after or "пункт исключён")}</div>')
    if rep.unapplied:
        st.warning("Не удалось применить автоматически: " + "; ".join(S.md(f"{PATCH_OP.get(u['op'], u['op'])} п. {u['clause'] or ''} ({u['note']})") for u in rep.unapplied[:5]))


def tab_approved() -> None:
    table = nav.approved.status_table(kb)
    if not table:
        st.caption("Проверенных ответов пока нет. В режиме администратора под ответом появляется кнопка «Подтвердить ответ как правильный».")
        return
    st.dataframe(table, hide_index=True)
    if ss["ui_admin"]:
        rm = st.selectbox("Удалить проверенный ответ", [""] + [f"{r['id']} · {r['вопрос'][:60]}" for r in table])
        if rm and st.button("Удалить"):
            nav.approved.remove(rm.split(" · ")[0])
            st.rerun()
    else:
        st.caption("Удаление доступно в режиме администратора.")


def page_admin() -> None:
    S.page(WIDE)
    ss["page"] = "admin"
    st.subheader("Администратору")
    c1, c2 = st.columns([1, 1.6], gap="large", vertical_alignment="center")
    with c1:
        sticky(st.toggle, "Режим администратора", "ui_admin", help="Под ответами появляются кнопка подтверждения и трасса поиска")
    with c2:
        if ss["ui_admin"]:
            sticky(st.text_input, "Ваше имя и подразделение", "ui_admin_name", placeholder="Иванова И.И., Управление кадров", label_visibility="collapsed")
    if nav.privacy_note:
        st.info(S.md(nav.privacy_note[:1].upper() + nav.privacy_note[1:]), icon=":material/lock:")
    t_src, t_pass, t_con, t_imp, t_app = st.tabs(["Источники", "Паспорт базы", "Противоречия", "Влияние приказа", "Проверенные ответы"])
    with t_src:
        tab_sources()
    with t_pass:
        tab_passport()
    with t_con:
        tab_contradictions()
    with t_imp:
        tab_impact()
    with t_app:
        tab_approved()


# ============================================================ страница «Качество»
QUALITY_COLUMNS = [("retrieval_recall@1", "нужный документ первым"), ("doc_recall@5", "документ в топ-5"), ("outcome_accuracy", "верный исход"),
                   ("false_answer_rate_E", "ложные ответы"), ("over_refusal_A", "лишние отказы"), ("step_groundedness", "обоснованность шагов"),
                   ("citation_validity", "валидность цитат"), ("latency_p50_ms", "медиана, мс")]


def page_quality() -> None:
    S.page(WIDE)
    ss["page"] = "quality"
    st.subheader("Качество")
    search = nav.embedder.name.split("/")[-1] if nav.use_dense else "BM25"
    rerank = getattr(nav.reranker, "name", "") or ""
    model = "без языковой модели, шаги дословно из документов" if nav.provider.kind == "mock" else nav.provider.kind
    st.caption(f"Проверка на размеченных вопросах. Сейчас работает: поиск {search}" + (f" + {rerank.split('/')[-1]}" if rerank else " без реранкера") + f"; ответ: {model}.")
    runs = sorted(RESULTS_DIR.glob("*.json")) if RESULTS_DIR.exists() else []
    aggs: dict[str, dict[str, Any]] = {}
    for p in runs:
        try:
            aggs[p.stem] = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    if not aggs:
        st.info("Результатов пока нет. Запустите оценку: `python scripts/run_eval.py --label имя_прогона`.")
        return
    st.dataframe([{"прогон": name, "вопросов": a.get("n"), "поиск": f"{str(a.get('embedder', '')).split('/')[-1]} + {str(a.get('reranker') or 'без реранкера').split('/')[-1]}",
                   **{title: (a.get(k) if k.endswith("_ms") else pct(a.get(k), "")) for k, title in QUALITY_COLUMNS}} for name, a in aggs.items()], hide_index=True)
    names = list(aggs)
    preferred = next((n for n in ("org_final", "final_bge_rerank") if n in aggs), names[-1])
    sel = st.selectbox("Подробности прогона", names, index=names.index(preferred))
    a = aggs[sel]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Верный исход", pct(a.get("outcome_accuracy")))
    m2.metric("Нужный документ первым", pct(a.get("retrieval_recall@1")))
    m3.metric("Ложные ответы", pct(a.get("false_answer_rate_E")))
    m4.metric("Медиана времени", f"{a['latency_p50_ms'] / 1000:.1f} с".replace(".", ",") if a.get("latency_p50_ms") else "нет данных")
    left, right = st.columns(2, gap="large")
    with left:
        S.label("По типам вопросов")
        st.dataframe([{"тип": f"{t}: {TYPE_NAMES.get(t, t)}", "вопросов": v.get("n"), "верный исход": pct(v.get("outcome_accuracy"), ""),
                       "документ в топ-5": pct(v.get("doc_recall@5"), "")} for t, v in a.get("by_type", {}).items()], hide_index=True)
    with right:
        S.label("Исходы: ожидалось и получено")
        st.dataframe([{"ожидалось": OUTCOME.get(exp, (exp,))[0], "получено": OUTCOME.get(got, (got,))[0], "вопросов": n}
                      for exp, row in a.get("confusion", {}).items() for got, n in row.items()], hide_index=True)
    jl = RESULTS_DIR / f"{sel}.jsonl"
    if jl.exists():
        S.label("По вопросам")
        items = []
        for line in jl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            items.append({"id": r["id"], "тип": r["type"], "вопрос": r["question"][:90], "ожидалось": OUTCOME.get(r["expected"], (r["expected"],))[0],
                          "получено": OUTCOME.get(r["outcome"], (r["outcome"],))[0], "верно": "да" if r["outcome_ok"] else "нет",
                          "документ в топ-5": {True: "да", False: "нет", None: ""}[r["gold_hit_top5"]],
                          "уверенность": round(r["gate"], 2) if r.get("gate") is not None else None, "мс": r["latency_ms"]})
        st.dataframe(items, hide_index=True, height=400)


def pct(value: Optional[float], empty: str = "нет данных") -> str:
    return empty if value is None else f"{value * 100:.0f}%"


# ============================================================ навигация
def safe(fn: Callable[[], None]) -> Callable[[], None]:
    """Ошибка на странице показывается понятной фразой, стек уходит в журнал сервера."""

    def run() -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            st.error(f"Страницу не удалось показать целиком: {type(e).__name__}. Обновите страницу; если ошибка повторяется, сообщите администратору.")

    run.__name__ = fn.__name__
    return run


st.logo(S.LOGO_SVG, size="large")
pages = [
    st.Page(safe(page_ask), title="Спросить", icon=":material/chat:", url_path="ask", default=True),
    st.Page(safe(page_my), title="Мой навигатор", icon=":material/person_pin:", url_path="my"),
    st.Page(safe(page_docs), title="Документы", icon=":material/description:", url_path="docs"),
    st.Page(safe(page_admin), title="Администратору", icon=":material/admin_panel_settings:", url_path="admin"),
    st.Page(safe(page_quality), title="Качество", icon=":material/monitoring:", url_path="quality"),
]
st.navigation(pages, position="top").run()
