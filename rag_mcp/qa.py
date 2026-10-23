"""Growing Q&A base (R10).

Flow: the model *proposes* a Q&A pair (stored as pending, not searchable). Only
after the user confirms is it written to `qa_dir/qa-<id>.json` and indexed into
the `qa` source. The JSON files are the source of truth, so the Q&A base
survives a full index rebuild.
"""

from __future__ import annotations

import json
import re

from .config import QA_SOURCE, Config
from .ingest import Indexer
from .store import Store, now_iso


class QAError(Exception):
    pass


class QABase:
    def __init__(self, cfg: Config, store: Store, indexer: Indexer):
        self.cfg = cfg
        self.store = store
        self.indexer = indexer

    def propose(self, question: str, answer: str, citations: list[str] | None = None) -> int:
        question, answer = question.strip(), answer.strip()
        if not question or not answer:
            raise QAError("Question and answer must not be empty")
        with self.store.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO qa_pending(question, answer, citations, created_at) VALUES(?, ?, ?, ?)",
                (question, answer, json.dumps(citations or [], ensure_ascii=False), now_iso()),
            )
            return int(cur.lastrowid)

    def pending(self) -> list[dict]:
        with self.store.lock:
            rows = self.store.conn.execute("SELECT * FROM qa_pending ORDER BY id").fetchall()
        return [self._row(r) for r in rows]

    def get_pending(self, pending_id: int) -> dict:
        with self.store.lock:
            row = self.store.conn.execute("SELECT * FROM qa_pending WHERE id = ?", (pending_id,)).fetchone()
        if not row:
            raise QAError(f"No pending Q&A proposal with id {pending_id}")
        return self._row(row)

    def reject(self, pending_id: int) -> None:
        self.get_pending(pending_id)
        with self.store.transaction() as conn:
            conn.execute("DELETE FROM qa_pending WHERE id = ?", (pending_id,))

    def approve(self, pending_id: int, via: str) -> dict:
        """Persist a confirmed proposal and index it immediately."""
        p = self.get_pending(pending_id)
        self.cfg.qa_dir.mkdir(parents=True, exist_ok=True)
        entry_id = self._next_id()
        entry = {
            "id": entry_id,
            "question": p["question"],
            "answer": p["answer"],
            "citations": p["citations"],
            "proposed_at": p["created_at"],
            "confirmed_at": now_iso(),
            "confirmed_via": via,
        }
        path = self.cfg.qa_dir / f"qa-{entry_id:06d}.json"
        path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.store.transaction() as conn:
            conn.execute("DELETE FROM qa_pending WHERE id = ?", (pending_id,))
        self.indexer.index([QA_SOURCE], prune=False)
        entry["file"] = str(path)
        return entry

    def entries(self) -> list[dict]:
        if not self.cfg.qa_dir.is_dir():
            return []
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.cfg.qa_dir.glob("qa-*.json"))]

    def delete(self, entry_id: int) -> None:
        path = self.cfg.qa_dir / f"qa-{entry_id:06d}.json"
        if not path.exists():
            raise QAError(f"No Q&A entry {entry_id}")
        path.unlink()
        self.indexer.index([QA_SOURCE], prune=True)

    def _next_id(self) -> int:
        ids = [int(m.group(1)) for p in self.cfg.qa_dir.glob("qa-*.json") if (m := re.match(r"qa-(\d+)", p.stem))]
        return max(ids, default=0) + 1

    @staticmethod
    def _row(r) -> dict:
        return {
            "id": r["id"],
            "question": r["question"],
            "answer": r["answer"],
            "citations": json.loads(r["citations"]),
            "created_at": r["created_at"],
        }
