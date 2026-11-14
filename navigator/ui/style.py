"""Оформление интерфейса: таблица стилей и HTML-помощники.

Текст документов выводится через экранированный HTML, а не через Markdown: в регламентах встречаются
подчёркивания бланков, знаки «$», «*» и «#», которые разметка исказила бы.
"""
from __future__ import annotations

import html
import re
from typing import Optional

import streamlit as st

LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="262" height="32" viewBox="0 0 262 32">'
    '<circle cx="16" cy="16" r="12.5" fill="none" stroke="#1f4e9c" stroke-width="2.2"/>'
    '<path d="M21.6 10.4l-3.4 7.8-7.8 3.4 3.4-7.8z" fill="#1f4e9c"/><circle cx="16" cy="16" r="1.7" fill="#fff"/>'
    '<text x="38" y="21.3" font-family="-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Arial,sans-serif" font-size="15.5" '
    'font-weight="600" fill="#1b1f24">Навигатор по регламентам</text></svg>'
)

BASE_CSS = """
<style>
:root { --nv-primary:#1f4e9c; --nv-ink:#1b1f24; --nv-muted:#6b7280; --nv-line:#e6eaf0; --nv-tint:#f3f6fb; }
.stMainBlockContainer { padding: 4.2rem 1.5rem 5rem; }
[data-testid="stBottomBlockContainer"] { padding: .5rem 1.5rem 1.4rem; }

/* лента вопросов и ответов */
[data-testid="stChatMessage"] { background: transparent; padding: .15rem 0 .35rem; gap: 0; }
[data-testid^="stChatMessageAvatar"] { display: none; }
.nv-sep { border: 0; border-top: 1px solid var(--nv-line); margin: 1.1rem 0 .9rem; }
.nv-q { font-size: 1.12rem; font-weight: 600; line-height: 1.4; color: var(--nv-ink); scroll-margin-top: 5rem; }
.nv-q-role { font-size: .8rem; color: var(--nv-muted); margin-top: .1rem; }
.nv-summary { font-size: 1.02rem; line-height: 1.6; margin: .15rem 0 .2rem; }

/* подписи разделов ответа */
.nv-label { font-size: .72rem; font-weight: 600; letter-spacing: .07em; text-transform: uppercase; color: var(--nv-muted); margin: .9rem 0 .15rem; }
.nv-label-note { text-transform: none; letter-spacing: 0; font-weight: 400; margin-left: .55rem; }

/* шаги */
.nv-step { display: flex; gap: .75rem; margin: .25rem 0 0; }
.nv-num { flex: 0 0 1.55rem; height: 1.55rem; border-radius: 50%; background: var(--nv-tint); color: var(--nv-primary);
          font-size: .82rem; font-weight: 600; display: flex; align-items: center; justify-content: center; margin-top: .08rem; }
.nv-step-text { line-height: 1.58; min-width: 0; overflow-wrap: anywhere; }
.nv-note { font-size: .8rem; color: var(--nv-muted); }
[class*="st-key-cite-"] { padding-left: 2.3rem; }

/* текст пункта в раскрытой цитате */
.nv-quote { border-left: 3px solid #c9d7ee; padding: .1rem 0 .1rem .85rem; margin: .1rem 0 .35rem; font-size: .93rem; line-height: 1.58; color: #2a3038; overflow-wrap: anywhere; }
.nv-quote mark { background: #fff1b8; color: inherit; padding: 0 .1rem; border-radius: 3px; }
.nv-meta { font-size: .8rem; color: var(--nv-muted); line-height: 1.5; }

/* списки и строки документов */
.nv-list { margin: .1rem 0 .1rem 1.15rem; padding: 0; line-height: 1.55; }
.nv-list li { margin: .12rem 0; }
.nv-doc { display: flex; gap: .55rem; align-items: baseline; margin: .28rem 0; line-height: 1.45; font-size: .95rem; }
.nv-doc a { color: var(--nv-primary); text-decoration: none; white-space: nowrap; }
.nv-doc .nv-meta { margin-left: .15rem; }
.nv-kvs { margin: .8rem 0 .1rem; }
.nv-kv { display: flex; gap: .8rem; align-items: baseline; margin: .2rem 0; line-height: 1.5; }
.nv-kv .k { flex: 0 0 9.2rem; font-size: .72rem; font-weight: 600; letter-spacing: .07em; text-transform: uppercase; color: var(--nv-muted); }
.nv-chip { flex: 0 0 auto; display: inline-block; font-size: .74rem; line-height: 1.25rem; padding: 0 .5rem; border-radius: 999px; white-space: nowrap; }
.nv-green { background: #e6f4ea; color: #1b6e3c; } .nv-red { background: #fdeceb; color: #b42318; }
.nv-orange { background: #fff3e0; color: #a65a05; } .nv-gray { background: #eef1f5; color: #4b5563; }
.nv-blue { background: #e7effc; color: #1f4e9c; }
.nv-check { display: flex; gap: .5rem; font-size: .88rem; line-height: 1.5; margin: .12rem 0; }
.nv-check .ok { color: #1b6e3c; flex: 0 0 1rem; } .nv-check .bad { color: #b45309; flex: 0 0 1rem; font-weight: 700; }
.nv-check .d { color: var(--nv-muted); }
.nv-strike { text-decoration: line-through; color: var(--nv-muted); }

/* стартовый экран */
.nv-hero { text-align: center; margin: 7vh 0 1.1rem; }
.nv-hero h1 { font-size: 2.05rem; font-weight: 650; letter-spacing: -.01em; margin: 0 0 .5rem; padding: 0; }
.nv-hero p { color: #4b5563; font-size: 1.02rem; margin: 0; }
.nv-hero .nv-meta { margin-top: .55rem; }
.st-key-examples button, .st-key-more-examples button, .st-key-pop-examples button {
    justify-content: flex-start; text-align: left; font-weight: 400; line-height: 1.4; }
.st-key-examples button > div, .st-key-more-examples button > div, .st-key-pop-examples button > div { justify-content: flex-start; width: 100%; }
.st-key-examples button p, .st-key-more-examples button p, .st-key-pop-examples button p { text-align: left; }
.st-key-examples button { border: 1px solid var(--nv-line); background: #fff; border-radius: .8rem; padding: .7rem 1rem; min-height: 5.3rem; color: var(--nv-ink); }
.st-key-examples button:hover { border-color: var(--nv-primary); color: var(--nv-primary); background: #fafcff; }
.st-key-more-examples button, .st-key-pop-examples button { min-height: 0; padding: .28rem 0; }

/* текст документа на странице «Документы» */
.nv-h { font-weight: 600; margin: 1rem 0 .3rem; }
.nv-cl { display: flex; gap: .7rem; margin: .32rem 0; line-height: 1.55; font-size: .95rem; }
.nv-cl-id { flex: 0 0 6.6rem; color: var(--nv-muted); font-size: .8rem; padding-top: .16rem; overflow-wrap: anywhere; }
.nv-cl-t { min-width: 0; overflow-wrap: anywhere; }
.nv-edit { color: var(--nv-primary); font-size: .8rem; } .nv-del { color: #b42318; font-size: .8rem; }
.nv-change { display: flex; gap: .7rem; margin: .3rem 0; line-height: 1.5; font-size: .95rem; }
.nv-change .dt { flex: 0 0 5.4rem; color: var(--nv-muted); font-size: .85rem; padding-top: .08rem; }

/* спокойные заголовки страниц */
h3 { font-weight: 650 !important; letter-spacing: -.01em; }
@media (max-width: 640px) {
  .nv-hero { margin-top: 3vh; } .nv-hero h1 { font-size: 1.55rem; }
  .st-key-examples button { min-height: 0; }
  .nv-kv { flex-direction: column; gap: .1rem; } .nv-kv .k { flex-basis: auto; }
  .nv-cl { flex-direction: column; gap: 0; } .nv-cl-id { flex-basis: auto; }
  [class*="st-key-cite-"] { padding-left: .4rem; }
}
</style>
"""


def page(max_width: int) -> None:
    """Стили и ширина рабочей области страницы (лента ответа уже, таблицы шире)."""
    st.html(BASE_CSS + f"<style>.stMainBlockContainer, [data-testid='stBottomBlockContainer'] {{ max-width: calc({max_width}px + 3rem); }}</style>")


def scroll_to(anchor: str, token: str) -> None:
    """Показать начало нового ответа. Лента с закреплённым полем ввода сама прокручивается в самый низ,
    и у длинного ответа на экране остаётся только его конец. Сценарий выполняется один раз на значение token."""
    st.html(
        "<script>(() => {"
        f'if (window.__nvScroll === "{token}") return; window.__nvScroll = "{token}";'
        f'const go = () => {{ const el = document.getElementById("{anchor}"); if (el) el.scrollIntoView({{block: "start"}}); }};'
        "[60, 250, 600].forEach(t => setTimeout(go, t));"
        "})();</script>",
        unsafe_allow_javascript=True,
    )


def esc(text: Optional[str]) -> str:
    """Текст для вставки в HTML: без интерпретации разметки, с сохранением переносов строк."""
    return html.escape(str(text or ""), quote=False).replace("\n", "<br>")


def md(text: Optional[str]) -> str:
    """Текст для вставки в Markdown-строку Streamlit (подписи виджетов, плашки): служебные символы экранируются."""
    flat = " ".join(str(text or "").split())
    return re.sub(r"([\\`*_\[\]<>$~|#])", r"\\\1", flat)


def plain(text: Optional[str]) -> str:
    """Текст для подписей, где разметка не нужна совсем."""
    return re.sub(r"\s+", " ", re.sub(r"[\\`*_\[\]<>$~|#]", " ", str(text or ""))).strip()


def clip(text: Optional[str], n: int) -> str:
    """Обрезка по границе слова с многоточием."""
    flat = " ".join(str(text or "").split())
    if len(flat) <= n:
        return flat
    cut = flat[:n].rsplit(" ", 1)[0].rstrip(" ,;:(«")
    return (cut or flat[:n]) + "…"


def html_block(body: str) -> None:
    st.markdown(body, unsafe_allow_html=True)


def label(text: str, note: Optional[str] = None) -> None:
    """Малая подпись раздела ответа."""
    extra = f'<span class="nv-label-note">{esc(note)}</span>' if note else ""
    html_block(f'<div class="nv-label">{esc(text)}{extra}</div>')


def chip(text: str, color: str) -> str:
    return f'<span class="nv-chip nv-{color}">{esc(text)}</span>'


def bullets(items: list[str]) -> None:
    html_block('<ul class="nv-list">' + "".join(f"<li>{esc(x)}</li>" for x in items) + "</ul>")


def safe_url(url: Optional[str]) -> Optional[str]:
    """Только http(s)-ссылки попадают в href."""
    u = (url or "").strip()
    return html.escape(u, quote=True) if re.match(r"^https?://", u, re.I) else None


def mark_quote(text: str, quote: Optional[str]) -> str:
    """Текст пункта с подсвеченной цитатой, на которую опирается шаг."""
    q = (quote or "").strip()
    pos = text.find(q) if len(q) >= 12 else -1
    if pos < 0:
        return esc(text)
    end = pos + len(q)
    while end < len(text) and (text[end].isalnum() or text[end] in "-"):  # цитата обрезана по длине: доводим подсветку до конца слова
        end += 1
    return esc(text[:pos]) + "<mark>" + esc(text[pos:end]) + "</mark>" + esc(text[end:])
