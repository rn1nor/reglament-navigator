"""FastAPI: /ask, /approve, /approved, /documents, /documents/{id}, /navigator/{role}, /contradictions, /audit, /health.
Запуск: uvicorn navigator.api.app:app --reload

Надёжность: база загружается один раз под блокировкой и перечитывается, когда kb.json изменился на диске (пересборка из интерфейса
или скриптом); /health отвечает всегда и сообщает причину, если база не загрузилась; остальные эндпойнты в этом случае отдают 503
с понятным текстом; неверные параметры запроса дают 422, а не «техническую ошибку» в теле ответа."""
from __future__ import annotations

import datetime as _dt
import json
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from ..config import default_kb
from ..qa.approved import approval_problems
from ..qa.engine import Navigator, card
from ..schemas import ROLE_LABELS, Response

app = FastAPI(title="Навигатор по регламентам", version="0.2.0")

RELOAD_GRACE_S = 120.0  # сколько ждать, пока пересборка допишет индекс после kb.json, прежде чем перечитать базу в любом случае
_lock = threading.RLock()
_nav: Optional[Navigator] = None
_nav_dir: Optional[Path] = None
_nav_mtime: Optional[float] = None
_nav_error: Optional[str] = None       # почему база не загружена или не перечитана
_failed_mtime: Optional[float] = None  # версия kb.json, которую уже не удалось загрузить: не повторяем попытку на каждом запросе


def _kb_mtime(kb_dir: Path) -> Optional[float]:
    try:
        return (Path(kb_dir) / "kb.json").stat().st_mtime
    except OSError:
        return None


def _index_written(n: Navigator, kb_mtime: float) -> bool:
    """Пересборка сначала пишет kb.json, затем индекс. Пока индекс старше базы, плотный индекс ещё считается другим процессом."""
    if not n.use_dense or "hash" in n.embedder.name:
        return True  # индекс BM25 Navigator пересоберёт сам за секунды
    try:
        return (Path(n.index_dir) / "meta.json").stat().st_mtime >= kb_mtime
    except OSError:
        return False


def _short(e: BaseException) -> str:
    text = " ".join(str(e).split())
    return f"{type(e).__name__}: {text[:300]}" if text else type(e).__name__


def nav() -> Navigator:
    """Загруженная база. Первый вызов загружает её (один раз, под блокировкой); после пересборки базы на диске перечитывает.
    Если база не загружается, поднимает 503 с причиной."""
    global _nav, _nav_dir, _nav_mtime, _nav_error, _failed_mtime
    kb_dir = default_kb()
    mtime = _kb_mtime(kb_dir)
    current = _nav
    if current is not None and _nav_dir == kb_dir and (mtime == _nav_mtime or mtime is None):
        return current  # быстрый путь без блокировки; mtime None: kb.json заменяется прямо сейчас, работаем на загруженной базе
    with _lock:
        mtime = _kb_mtime(kb_dir)
        loaded = _nav is not None and _nav_dir == kb_dir
        if loaded and (mtime == _nav_mtime or mtime is None or mtime == _failed_mtime):
            return _nav  # type: ignore[return-value]
        if loaded and not _index_written(_nav, mtime) and time.time() - mtime < RELOAD_GRACE_S:  # type: ignore[arg-type]
            return _nav  # type: ignore[return-value]  # индекс новой базы ещё пишется: отвечаем по прежней, перечитаем позже
        if not loaded and mtime is not None and mtime == _failed_mtime:
            raise HTTPException(503, detail=f"база знаний недоступна: {_nav_error}")
        try:
            fresh = Navigator(kb_dir)
        except Exception as e:  # noqa: BLE001  повреждённая или отсутствующая база не должна давать голый 500
            _failed_mtime = mtime
            _nav_error = _short(e)
            if loaded:
                return _nav  # type: ignore[return-value]  # новая версия не читается: продолжаем отвечать по прежней, /health сообщит причину
            raise HTTPException(503, detail=f"база знаний недоступна: {_nav_error}. Проверьте {kb_dir}/kb.json или пересоберите базу.") from e
        _nav, _nav_dir, _nav_mtime, _nav_error, _failed_mtime = fresh, kb_dir, mtime, None, None
        return fresh


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Непредвиденная ошибка: понятный JSON вместо голого «Internal Server Error» (стек остаётся в журнале сервера)."""
    return JSONResponse(status_code=500, content={"detail": f"внутренняя ошибка сервиса ({type(exc).__name__}); запрос не выполнен, повторите его позже"})


def _role_key(value: Optional[str]) -> Optional[str]:
    """Ключ роли по ключу или русскому названию; None для пустого значения; ValueError для неизвестного."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("роль задаётся строкой")
    v = value.strip()
    if not v:
        return None
    if v in ROLE_LABELS:
        return v
    by_label = {label.lower(): key for key, label in ROLE_LABELS.items()}
    if v.lower() in by_label:
        return by_label[v.lower()]
    raise ValueError(f"неизвестная роль «{v[:40]}»; допустимые значения: " + ", ".join(ROLE_LABELS))


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    role: Optional[str] = None
    as_of: Optional[_dt.date] = None

    @field_validator("role", mode="before")
    @classmethod
    def _check_role(cls, v):  # noqa: ANN001
        return _role_key(v)


@app.get("/health")
def health() -> dict:
    """Отвечает всегда: status ok, либо degraded с причиной, если база не загрузилась или её новая версия не читается."""
    kb_dir = default_kb()
    try:
        n = nav()
    except HTTPException as e:
        return {"status": "degraded", "ready": False, "reason": str(e.detail), "kb": str(kb_dir)}
    out = {"status": "ok", "ready": True, "kb": str(kb_dir), "built_at": n.kb.built_at, "documents": len(n.kb.documents), "units": len(n.index.units),
           "provider": n.provider.kind, "embedder": n.embedder.name if n.use_dense else "bm25-only", "reranker": getattr(n.reranker, "name", None)}
    warnings = [w for w in (_nav_error and f"новая версия базы не загружена: {_nav_error}", getattr(n.approved, "warning", None)) if w]
    if _nav_error:
        out["status"] = "degraded"
        out["reason"] = warnings[0]
    if warnings:
        out["warnings"] = warnings
    return out


@app.post("/ask", response_model=Response)
def ask(req: AskRequest) -> Response:
    return nav().ask(req.question, role=req.role, as_of=req.as_of)


@app.get("/documents")
def documents(status: Optional[str] = None, doc_type: Optional[str] = None) -> list[dict]:
    out = []
    for d in nav().kb.documents:
        if status and d.status != status:
            continue
        if doc_type and d.doc_type != doc_type:
            continue
        c = card(d).model_dump(mode="json")
        c.update({"clauses": len(d.clauses), "roles": d.roles, "approved_by": d.approved_by, "pages": d.pages})
        out.append(c)
    return out


@app.get("/documents/{doc_id}")
def document(doc_id: str) -> dict:
    d = nav().kb.get(doc_id)
    if d is None:
        raise HTTPException(404, "документ не найден")
    return d.model_dump(mode="json")


class ApproveRequest(BaseModel):
    response: Response
    approved_by: str


@app.post("/approve")
def approve(req: ApproveRequest) -> dict:
    """Сохраняет ответ как проверенный. Принимается только ответ с шагами, каждый шаг которого ссылается на существующий пункт базы
    и содержит дословную цитату из него; подпись проверяющего обязательна."""
    n = nav()
    problems = approval_problems(req.response, n.kb, req.approved_by)
    if problems:
        raise HTTPException(422, detail="ответ нельзя подтвердить: " + "; ".join(problems[:5]))
    entry = n.approve(req.response, req.approved_by.strip())
    return {"id": entry.id, "approved_at": entry.approved_at}


@app.get("/approved")
def approved() -> list[dict]:
    n = nav()
    return n.approved.status_table(n.kb)


@app.get("/contradictions")
def contradictions() -> list[dict]:
    p = default_kb() / "contradictions.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except (ValueError, OSError):
        return []  # повреждённый файл: список появится после пересборки базы
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


@app.get("/navigator/{role}")
def my_navigator(role: str, days: int = Query(180, ge=0, le=3650)) -> dict:
    from ..qa.mynav import applicable_documents, duties_and_deadlines, recent_changes

    try:
        key = _role_key(role)
    except ValueError as e:
        raise HTTPException(422, detail=str(e)) from e
    if key is None:
        raise HTTPException(422, detail="роль не указана; допустимые значения: " + ", ".join(ROLE_LABELS))
    n = nav()
    docs_ = applicable_documents(n.kb, key)
    changes = recent_changes(n.kb, docs_, _dt.date.today() - _dt.timedelta(days=days))
    return {
        "role": key,
        "documents": [card(d).model_dump(mode="json") for d in docs_],
        "changes": [{"date": c["date"].isoformat(), "kind": c["kind"], "doc_id": c["doc"].id, "title": c["doc"].title, "detail": c["detail"]} for c in changes],
        "duties": [{"doc_id": r["doc"].id, "clause_id": r["clause"].id, "deadlines": r["deadlines"], "text": r["text"]} for r in duties_and_deadlines(docs_)],
    }


@app.get("/audit")
def audit() -> dict:
    kb = nav().kb
    return {"audit": kb.audit.model_dump(mode="json") if kb.audit else None, "stubs": [s.model_dump(mode="json") for s in kb.stubs], "built_at": kb.built_at}
