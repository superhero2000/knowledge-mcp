"""SQLite storage: documents, chunks, FTS5 keyword index, vectors, Q&A, model lock.

Vectors are stored as float32 BLOBs next to their chunks and searched with numpy
(exact cosine similarity). That keeps the index a single portable file with no
native extensions, which is plenty for local knowledge bases up to a few hundred
thousand chunks.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,
    path         TEXT NOT NULL,
    title        TEXT,
    doc_type     TEXT,
    sha256       TEXT NOT NULL,
    mtime        REAL,
    size         INTEGER,
    extract_file TEXT,
    chunk_sig    TEXT,
    indexed_at   TEXT,
    UNIQUE (source, path)
);
CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY,
    doc_id       INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    source       TEXT NOT NULL,
    ord          INTEGER NOT NULL,
    text         TEXT NOT NULL,
    heading      TEXT NOT NULL DEFAULT '',
    page_start   INTEGER,
    page_end     INTEGER,
    location     TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    embed_text   TEXT NOT NULL,
    embedding    BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, ord);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, heading, title,
    content='chunks', content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, heading, title) VALUES (new.id, new.text, new.heading, new.title);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, heading, title)
    VALUES ('delete', old.id, old.text, old.heading, old.title);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE OF text, heading, title ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, heading, title)
    VALUES ('delete', old.id, old.text, old.heading, old.title);
    INSERT INTO chunks_fts(rowid, text, heading, title) VALUES (new.id, new.text, new.heading, new.title);
END;

CREATE TABLE IF NOT EXISTS qa_pending (
    id          INTEGER PRIMARY KEY,
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    citations   TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL
);
"""


class ModelLockError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, db_path: Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.create_function("ulower", 1, lambda s: s.lower() if s else s, deterministic=True)
        self.conn.executescript(SCHEMA)
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", SCHEMA_VERSION)
        self._vec_cache: dict[str, tuple[int, np.ndarray, np.ndarray]] = {}

    def close(self) -> None:
        self.conn.close()

    # ---- transactions -------------------------------------------------
    def transaction(self):
        store = self

        class _Tx:
            def __enter__(self):
                store.lock.acquire()
                store.conn.execute("BEGIN")
                return store.conn

            def __exit__(self, exc_type, exc, tb):
                try:
                    if exc_type is None:
                        store.conn.execute("COMMIT")
                        store._bump_version()
                    else:
                        store.conn.execute("ROLLBACK")
                finally:
                    store.lock.release()
                return False

        return _Tx()

    def _bump_version(self) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('index_version', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
        )

    def index_version(self) -> int:
        return int(self.get_meta("index_version") or 0)

    # ---- meta ---------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ---- model lock (R9) ----------------------------------------------
    def model_lock(self) -> dict | None:
        raw = self.get_meta("embedding_lock")
        return json.loads(raw) if raw else None

    def check_model(self, model: str, dim: int | None = None) -> None:
        """Raise unless `model` (and `dim`) match the model that built the index."""
        lock = self.model_lock()
        if lock is None:
            return
        if lock["model"] != model:
            raise ModelLockError(
                f"Index is locked to embedding model '{lock['model']}' (dim {lock['dim']}), "
                f"but the config requests '{model}'. Vectors from two models cannot share one index. "
                "Either restore the configured model, or rebuild all vectors with "
                "`rag reembed --switch-model` (no re-extraction or re-chunking needed)."
            )
        if dim is not None and lock["dim"] != dim:
            raise ModelLockError(
                f"Index is locked to {lock['dim']}-dimensional vectors from '{model}', but the provider "
                f"returned {dim} dimensions. Check provider.embedding_dimensions or run "
                "`rag reembed --switch-model`."
            )

    def set_model_lock(self, model: str, dim: int, base_url: str) -> None:
        self.set_meta(
            "embedding_lock",
            json.dumps({"model": model, "dim": dim, "base_url": base_url, "locked_at": now_iso()}),
        )

    def clear_model_lock(self) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM meta WHERE key = 'embedding_lock'")

    # ---- documents & chunks -------------------------------------------
    def get_document(self, source: str, path: str) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(
                "SELECT * FROM documents WHERE source = ? AND path = ?", (source, path)
            ).fetchone()

    def list_documents(self, source: str | None = None) -> list[sqlite3.Row]:
        with self.lock:
            if source:
                return self.conn.execute(
                    "SELECT * FROM documents WHERE source = ? ORDER BY path", (source,)
                ).fetchall()
            return self.conn.execute("SELECT * FROM documents ORDER BY source, path").fetchall()

    def upsert_document(self, conn: sqlite3.Connection, **fields: Any) -> int:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{k} = excluded.{k}" for k in fields if k not in ("source", "path"))
        conn.execute(
            f"INSERT INTO documents({cols}) VALUES({marks}) "
            f"ON CONFLICT(source, path) DO UPDATE SET {updates}",
            tuple(fields.values()),
        )
        row = conn.execute(
            "SELECT id FROM documents WHERE source = ? AND path = ?", (fields["source"], fields["path"])
        ).fetchone()
        return int(row["id"])

    def delete_chunks(self, conn: sqlite3.Connection, doc_id: int) -> None:
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))

    def delete_document(self, conn: sqlite3.Connection, doc_id: int) -> None:
        self.delete_chunks(conn, doc_id)
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))

    def insert_chunks(self, conn: sqlite3.Connection, doc_id: int, source: str, title: str, chunks: Iterable[dict]) -> None:
        for c in chunks:
            conn.execute(
                "INSERT INTO chunks(doc_id, source, ord, text, heading, page_start, page_end, location, title, embed_text) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doc_id, source, c["ord"], c["text"], c["heading"], c.get("page_start"),
                    c.get("page_end"), c["location"], title or "", c["embed_text"],
                ),
            )

    def chunks_missing_embeddings(self, source: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT id, embed_text FROM chunks WHERE embedding IS NULL"
        args: tuple = ()
        if source:
            q += " AND source = ?"
            args = (source,)
        with self.lock:
            return self.conn.execute(q + " ORDER BY id", args).fetchall()

    def set_embeddings(self, ids: list[int], vectors: np.ndarray) -> None:
        with self.transaction() as conn:
            conn.executemany(
                "UPDATE chunks SET embedding = ? WHERE id = ?",
                [(vectors[i].astype(np.float32).tobytes(), cid) for i, cid in enumerate(ids)],
            )

    def clear_all_embeddings(self) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE chunks SET embedding = NULL")

    def get_chunks(self, ids: list[int]) -> dict[int, sqlite3.Row]:
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        with self.lock:
            rows = self.conn.execute(
                f"SELECT c.id, c.doc_id, c.source, c.ord, c.text, c.heading, c.page_start, c.page_end, "
                f"c.location, d.path, d.title, d.doc_type FROM chunks c JOIN documents d ON d.id = c.doc_id "
                f"WHERE c.id IN ({marks})",
                ids,
            ).fetchall()
        return {r["id"]: r for r in rows}

    def neighbours(self, chunk_id: int, window: int) -> list[sqlite3.Row]:
        with self.lock:
            row = self.conn.execute("SELECT doc_id, ord FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
            if not row:
                return []
            ids = [
                r["id"]
                for r in self.conn.execute(
                    "SELECT id FROM chunks WHERE doc_id = ? AND ord BETWEEN ? AND ? ORDER BY ord",
                    (row["doc_id"], row["ord"] - window, row["ord"] + window),
                )
            ]
        got = self.get_chunks(ids)
        return [got[i] for i in ids]

    # ---- vector matrix cache --------------------------------------------
    def vectors(self, source: str) -> tuple[np.ndarray, np.ndarray]:
        """Return (chunk_ids, matrix) for a source; cached until the index changes."""
        version = self.index_version()
        cached = self._vec_cache.get(source)
        if cached and cached[0] == version:
            return cached[1], cached[2]
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, embedding FROM chunks WHERE source = ? AND embedding IS NOT NULL ORDER BY id",
                (source,),
            ).fetchall()
        if rows:
            ids = np.array([r["id"] for r in rows], dtype=np.int64)
            mat = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        else:
            ids = np.zeros(0, dtype=np.int64)
            mat = np.zeros((0, 0), dtype=np.float32)
        self._vec_cache[source] = (version, ids, mat)
        return ids, mat

    # ---- stats ----------------------------------------------------------
    def stats(self) -> dict[str, dict[str, int]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT d.source AS source, COUNT(DISTINCT d.id) AS docs, COUNT(c.id) AS chunks, "
                "SUM(CASE WHEN c.embedding IS NULL AND c.id IS NOT NULL THEN 1 ELSE 0 END) AS unembedded "
                "FROM documents d LEFT JOIN chunks c ON c.doc_id = d.id GROUP BY d.source"
            ).fetchall()
        return {r["source"]: {"documents": r["docs"], "chunks": r["chunks"], "unembedded": r["unembedded"] or 0} for r in rows}
