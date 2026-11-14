"""Пересборка базы из её источников: папки с файлами, загрузки из интерфейса, выгрузки с сайта. Используется кнопкой «Переиндексировать» и скриптами."""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Callable, Optional

from .. import config
from ..index.dense import get_embedder
from ..index.store import HybridIndex, index_dirname
from .contradictions import find_contradictions, save_contradictions
from .pipeline import ingest


def sources_file(kb_dir: Path) -> Path:
    return Path(kb_dir) / "sources.json"


def _resolve(p: Path) -> Path:
    """Относительный путь источника: от текущего каталога, а если там его нет — от корня проекта (запуск из другой папки)."""
    p = Path(p)
    if p.is_absolute() or p.exists():
        return p
    alt = Path(config.ROOT) / p
    return alt if alt.exists() else p


def _same(a: Path, b: Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a) == str(b)


def _portable(p: Path) -> str:
    """Путь для sources.json: относительно корня проекта, если источник лежит внутри него (файл переносим между машинами)."""
    try:
        return str(Path(p).resolve().relative_to(Path(config.ROOT).resolve()))
    except (ValueError, OSError):
        return str(p)


def declared_sources(kb_dir: Path) -> list[Path]:
    """Источники, записанные в sources.json базы (как записаны). Повреждённый файл считается отсутствующим."""
    sf = sources_file(kb_dir)
    if not sf.exists():
        return []
    try:
        data = json.loads(sf.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    return [Path(x) for x in data if isinstance(x, str) and x.strip()] if isinstance(data, list) else []


def get_sources(kb_dir: Path, default_raw: Optional[Path] = None) -> list[Path]:
    """Источники базы: записанные в sources.json (иначе папка по умолчанию, если она есть) и папки загрузок из интерфейса и с сайта.
    Записанный, но недоступный источник остаётся в списке: rebuild() откажется пересобирать базу без него, а не обнулит её молча."""
    kb_dir = Path(kb_dir)
    src = [_resolve(p) for p in declared_sources(kb_dir)]
    if not src and default_raw is not None and _resolve(Path(default_raw)).exists():
        src = [_resolve(Path(default_raw))]
    for extra in (uploads_dir(kb_dir), web_dir(kb_dir)):
        if extra.exists() and not any(_same(extra, p) for p in src):
            src.append(extra)
    return src


def set_sources(kb_dir: Path, sources: list[Path]) -> None:
    Path(kb_dir).mkdir(parents=True, exist_ok=True)
    sources_file(kb_dir).write_text(json.dumps([_portable(p) for p in sources], ensure_ascii=False, indent=1), encoding="utf-8")


def uploads_dir(kb_dir: Path) -> Path:
    return Path(config.RAW_DIR) / f"{Path(kb_dir).name}_uploads"


def web_dir(kb_dir: Path) -> Path:
    return Path(config.RAW_DIR) / f"{Path(kb_dir).name}_web"


def rebuild(kb_dir: Path, sources: Optional[list[Path]] = None, progress: Optional[Callable[[str], None]] = None,
            with_contradictions: bool = True) -> dict:
    """ingest -> индекс (эмбеддинги неизменившихся пунктов из кэша) -> противоречия. Возвращает сводку.
    Если источник недоступен или в источниках нет документов, поднимается FileNotFoundError / ValueError, а существующая база
    и её sources.json остаются нетронутыми. Папки загрузок из интерфейса и с сайта добавляются всегда, в том числе к явному списку."""
    kb_dir = Path(kb_dir)
    listed = [_resolve(Path(s)) for s in sources] if sources else get_sources(kb_dir)
    extras = [extra for extra in (uploads_dir(kb_dir), web_dir(kb_dir)) if extra.exists()]
    for extra in extras:
        if not any(_same(extra, p) for p in listed):
            listed.append(extra)
    missing = [str(p) for p in listed if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "источник базы недоступен: " + ", ".join(missing) + f". База {kb_dir} не изменена. Верните папку на место "
            f"(публичные документы mipt.ru скачиваются командой make fetch) или уберите её из {sources_file(kb_dir)}.")
    if not listed:
        raise FileNotFoundError(f"у базы {kb_dir} нет источников: положите документы в папку data/raw/{kb_dir.name} или загрузите их через интерфейс. База не изменена.")
    t0 = _dt.datetime.now()
    kb = ingest(listed, kb_dir, progress=progress)  # сам проверяет, что в источниках есть читаемые документы, и не трогает базу, если их нет
    # автоматически подхватываемые папки загрузок в sources.json не пишем: они добавляются при каждой пересборке сами
    set_sources(kb_dir, [p for p in listed if not any(_same(p, e) for e in extras)] or listed)
    from ..llm.privacy import policy_path, set_policy

    if not policy_path(kb_dir).exists():
        set_policy(kb_dir, confidential=True, note="по умолчанию база закрытая: внешние LLM API не используются")
    emb = get_embedder()
    idx_dir = kb_dir / index_dirname(emb.name)
    index = HybridIndex.build(kb, emb, cache_dir=idx_dir)
    index.save(idx_dir)
    n_pairs = None
    if with_contradictions:
        pairs = find_contradictions(kb, index)
        save_contradictions(pairs, kb_dir)
        n_pairs = len(pairs)
    return {
        "documents": len(kb.documents), "units": len(index.units), "contradictions": n_pairs,
        "findings": len(kb.audit.findings) if kb.audit else 0, "seconds": round((_dt.datetime.now() - t0).total_seconds(), 1),
        "sources": [_portable(s) for s in listed],
    }
