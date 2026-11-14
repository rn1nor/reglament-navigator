"""Проверенные ответы: сотрудник подразделения подтверждает ответ, похожие вопросы получают его с именем и датой проверки.
Ответ автоматически устаревает, если любой процитированный пункт изменился, удалён или документ перестал действовать."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

import numpy as np
from pydantic import BaseModel, Field

from .. import config
from ..index.dense import Embedder
from ..schemas import KnowledgeBase, Response

SIM_THRESHOLD = 0.90


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class ApprovedCitation(BaseModel):
    doc_id: str
    clause_id: str
    text_sha1: str
    doc_status: str


class ApprovedEntry(BaseModel):
    id: str
    question: str
    role: Optional[str] = None
    approved_by: str
    approved_at: str
    response: dict[str, Any]
    citations: list[ApprovedCitation] = Field(default_factory=list)
    embedding: list[float] = Field(default_factory=list)
    embedder: str = ""


def approval_problems(response: Response, kb: KnowledgeBase, approved_by: str) -> list[str]:
    """Почему ответ нельзя сохранить как проверенный. Пустой список — можно.
    Подтверждается только ответ с шагами, каждый шаг которого опирается на существующий пункт базы и дословную цитату из него."""
    from .verify import quote_matches

    problems: list[str] = []
    if not (approved_by or "").strip():
        problems.append("не указано, кто подтверждает ответ (approved_by)")
    if response.outcome not in ("answer", "partial"):
        problems.append(f"подтвердить можно только ответ или частичный ответ, а не исход «{response.outcome}»")
    if not response.steps:
        problems.append("в ответе нет шагов со ссылками на пункты документов")
    for i, step in enumerate(response.steps, 1):
        if not step.citations:
            problems.append(f"шаг {i} не содержит ссылки на пункт документа")
            continue
        for c in step.citations:
            d = kb.get(c.doc_id)
            cl = d.clause_by_id(c.clause_id) if d else None
            if d is None or d.id.startswith("stub:"):
                problems.append(f"шаг {i}: документа «{c.doc_id}» нет в базе")
            elif cl is None or cl.deleted:
                problems.append(f"шаг {i}: пункта «{c.clause_id}» нет в документе «{d.short_name[:60]}»")
            elif not (c.quote or "").strip() or quote_matches(c.quote, cl.text) < config.QUOTE_MATCH_THRESHOLD:
                problems.append(f"шаг {i}: цитата не совпадает с текстом пункта {cl.label}")
    return problems


class ApprovedStore:
    """Хранилище проверенных ответов в approved.json. Файл общий для интерфейса и API (разные процессы и контейнеры):
    перед каждым обращением перечитывается, если изменился на диске; запись атомарная.
    Повреждённый файл не роняет запуск: он откладывается в approved.json.broken, хранилище считается пустым."""

    def __init__(self, path: Path, embedder: Embedder):
        self.path = Path(path)
        self.embedder = embedder
        self.entries: list[ApprovedEntry] = []
        self.warning: Optional[str] = None
        self._mtime: Optional[float] = None
        self._lock = threading.RLock()
        self._load()

    def _disk_mtime(self) -> Optional[float]:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    def _load(self) -> None:
        with self._lock:
            self._mtime = self._disk_mtime()
            if self._mtime is None:
                self.entries = []
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    raise ValueError("ожидался список записей")
                entries = [ApprovedEntry.model_validate(e) for e in data]
            except Exception as e:  # noqa: BLE001  битый JSON или записи неверного вида
                broken = self.path.with_name(self.path.name + ".broken")
                try:
                    os.replace(self.path, broken)
                except OSError:
                    pass
                self.entries = []
                self._mtime = self._disk_mtime()
                self.warning = f"файл проверенных ответов повреждён ({type(e).__name__}) и отложен в {broken.name}: хранилище пусто"
                print(f"[approved] {self.warning}")
                return
            self.entries = entries
            changed = False
            for e in self.entries:
                if e.embedder != self.embedder.name or not e.embedding:
                    e.embedding = self.embedder.encode([e.question], is_query=True)[0].tolist()
                    e.embedder = self.embedder.name
                    changed = True
            if changed:
                self._save()

    def refresh(self) -> None:
        """Подхватить изменения, сделанные другим процессом (интерфейс и API работают с одним файлом)."""
        if self._disk_mtime() != self._mtime:
            self._load()

    def _save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps([e.model_dump(mode="json") for e in self.entries], ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)  # атомарно: другой процесс не прочитает наполовину записанный файл
            self._mtime = self._disk_mtime()

    # ------------------------------------------------------------------
    def add(self, response: Response, kb: KnowledgeBase, approved_by: str) -> ApprovedEntry:
        problems = approval_problems(response, kb, approved_by)
        if problems:
            raise ValueError("ответ нельзя подтвердить: " + "; ".join(problems[:5]))
        cits: list[ApprovedCitation] = []
        for s in response.steps:
            for c in s.citations:
                d = kb.get(c.doc_id)
                cl = d.clause_by_id(c.clause_id) if d else None
                cits.append(ApprovedCitation(doc_id=c.doc_id, clause_id=c.clause_id, text_sha1=_sha(cl.text if cl else ""), doc_status=d.status if d else "unknown"))
        payload = response.model_dump(mode="json")
        payload.pop("trace", None)
        entry = ApprovedEntry(id=uuid.uuid4().hex[:10], question=response.question, role=response.role, approved_by=approved_by.strip(),
                              approved_at=_dt.datetime.now().isoformat(timespec="seconds"), response=payload, citations=cits,
                              embedding=self.embedder.encode([response.question], is_query=True)[0].tolist(), embedder=self.embedder.name)
        with self._lock:
            self.refresh()  # не затереть подтверждения, сделанные тем временем другим процессом
            self.entries = [e for e in self.entries if not (e.question.strip().lower() == entry.question.strip().lower() and e.role == entry.role)]
            self.entries.append(entry)
            self._save()
        return entry

    def remove(self, entry_id: str) -> None:
        with self._lock:
            self.refresh()
            self.entries = [e for e in self.entries if e.id != entry_id]
            self._save()

    def match(self, question: str, role: Optional[str]) -> Optional[tuple[ApprovedEntry, float]]:
        self.refresh()
        if not self.entries:
            return None
        q = self.embedder.encode([question], is_query=True)[0]
        best, best_sim = None, 0.0
        for e in self.entries:
            if e.role and role and e.role != role:
                continue
            v = np.asarray(e.embedding, dtype=np.float32)
            if v.shape != q.shape:
                continue
            sim = float(v @ q / (np.linalg.norm(v) * np.linalg.norm(q) + 1e-9))
            if sim > best_sim:
                best, best_sim = e, sim
        if best is not None and best_sim >= SIM_THRESHOLD:
            return best, best_sim
        return None

    def validate(self, entry: ApprovedEntry, kb: KnowledgeBase) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if not entry.citations:
            reasons.append("в подтверждённом ответе нет ссылок на пункты документов")
        for c in entry.citations:
            d = kb.get(c.doc_id)
            if d is None:
                reasons.append(f"документ {c.doc_id} исчез из базы")
                continue
            if d.status != "active":
                reasons.append(f"{d.short_name[:60]}: {d.status_reason or d.status}")
            cl = d.clause_by_id(c.clause_id)
            if cl is None or cl.deleted:
                reasons.append(f"{d.short_name[:60]}: пункт {c.clause_id} удалён")
            elif _sha(cl.text) != c.text_sha1:
                reasons.append(f"{d.short_name[:60]}: пункт {c.clause_id} изменён" + (f" ({', '.join(cl.provenance)})" if cl.provenance else ""))
        return (not reasons), reasons

    def status_table(self, kb: KnowledgeBase) -> list[dict[str, Any]]:
        self.refresh()
        rows = []
        for e in self.entries:
            ok, reasons = self.validate(e, kb)
            rows.append({"id": e.id, "вопрос": e.question, "роль": e.role or "—", "кто": e.approved_by, "когда": e.approved_at[:16].replace("T", " "),
                         "статус": "действует" if ok else "требует перепроверки", "причина": "; ".join(reasons)[:200]})
        return rows
