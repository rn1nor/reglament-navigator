"""Превью страницы оригинала с подсветкой процитированного фрагмента (PyMuPDF)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional


def _needles(quote: str) -> list[str]:
    q = " ".join(quote.split())
    out = []
    if len(q) > 70:
        out.append(q[:70])
    out.append(q[:40])
    for sep in (".", ";", ","):
        head = q.split(sep)[0].strip()
        if 12 <= len(head) <= 120:
            out.append(head)
    return list(dict.fromkeys(x for x in out if len(x) >= 12))


@lru_cache(maxsize=256)
def render_page(pdf_path: str, page_no: int, highlight: str = "", zoom: float = 1.6) -> Optional[bytes]:
    try:
        import pymupdf  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    p = Path(pdf_path)
    if not p.exists() or p.suffix.lower() != ".pdf":
        return None
    try:
        with pymupdf.open(str(p)) as doc:
            if page_no < 1 or page_no > doc.page_count:
                return None
            page = doc[page_no - 1]
            if highlight:
                for needle in _needles(highlight):
                    rects = page.search_for(needle)
                    if rects:
                        for r in rects[:6]:
                            annot = page.add_highlight_annot(r)
                            annot.set_colors(stroke=(1.0, 0.9, 0.3))
                            annot.update()
                        break
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            return pix.tobytes("png")
    except Exception:  # noqa: BLE001
        return None
