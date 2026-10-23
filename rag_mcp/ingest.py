"""Indexing pipeline: scan -> extract (JSON) -> chunk -> embed.

`rag index` is the only way the index changes (R11). For each file it decides
the cheapest work needed:

* unchanged file (mtime/size, else sha256) + same chunking settings -> skipped
* file unchanged but extraction JSON present -> extraction reused (R8)
* chunking settings changed (or --rechunk) -> re-chunked from JSON, no extraction
* chunks without vectors -> embedded (also resumes interrupted runs)
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .chunking import chunk_record, chunk_signature
from .config import QA_SOURCE, Config
from .extract import (
    SUPPORTED_SUFFIXES,
    ExtractionError,
    extraction_path,
    load_extraction,
    run_extraction,
    sha256_file,
)
from .provider import Provider
from .store import Store, now_iso

log = logging.getLogger(__name__)
EMBED_WRITE_BATCH = 256


@dataclass
class SourceReport:
    scanned: int = 0
    unchanged: int = 0
    extracted: int = 0
    reused_extraction: int = 0
    chunked: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class IndexReport:
    sources: dict[str, SourceReport] = field(default_factory=dict)
    embedded: int = 0

    def summary(self) -> str:
        lines = []
        for name, r in self.sources.items():
            lines.append(
                f"{name:<14} scanned={r.scanned} unchanged={r.unchanged} extracted={r.extracted} "
                f"reused_extraction={r.reused_extraction} chunked={r.chunked} removed={r.removed} errors={len(r.errors)}"
            )
            lines += [f"    ! {e}" for e in r.errors]
        lines.append(f"embedded chunks: {self.embedded}")
        return "\n".join(lines)


class Indexer:
    def __init__(self, cfg: Config, store: Store, provider: Provider, progress: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.store = store
        self.provider = provider
        self.progress = progress or (lambda msg: log.info(msg))

    # ---- scanning ---------------------------------------------------------
    def scan(self, source: str) -> list[tuple[Path, str]]:
        if source == QA_SOURCE:
            root, include, exclude, suffixes = self.cfg.qa_dir, ["*.json"], [], {".json"}
        else:
            sc = self.cfg.source(source)
            root, include, exclude, suffixes = sc.path, sc.include, sc.exclude, SUPPORTED_SUFFIXES
        if not root.is_dir():
            return []
        found: dict[str, Path] = {}
        for pattern in include:
            for p in root.glob(pattern):
                if not p.is_file() or p.suffix.lower() not in suffixes:
                    continue
                rel = p.relative_to(root).as_posix()
                if any(part.startswith(".") for part in Path(rel).parts):
                    continue
                if any(fnmatch.fnmatch(rel, ex) for ex in exclude):
                    continue
                found[rel] = p
        return [(found[k], k) for k in sorted(found)]

    # ---- indexing ---------------------------------------------------------
    def index(
        self,
        sources: list[str] | None = None,
        *,
        force_extract: bool = False,
        rechunk: bool = False,
        prune: bool = True,
        embed: bool = True,
    ) -> IndexReport:
        # Fail fast if the configured embedding model conflicts with the index (R9).
        self.store.check_model(self.provider.embedding_model)
        names = sources or self.cfg.all_search_sources()
        report = IndexReport()
        for name in names:
            if name != QA_SOURCE:
                self.cfg.source(name)  # validates the name
            report.sources[name] = self._index_source(name, force_extract, rechunk, prune)
        if prune and not sources:
            self._remove_orphan_sources(report)
        if embed:
            report.embedded = self.embed_missing()
        return report

    def _index_source(self, source: str, force_extract: bool, rechunk: bool, prune: bool) -> SourceReport:
        rep = SourceReport()
        sig = chunk_signature(self.cfg.chunking)
        files = self.scan(source)
        seen: set[str] = set()
        for path, rel in files:
            rep.scanned += 1
            seen.add(rel)
            try:
                self._index_file(source, path, rel, sig, force_extract, rechunk, rep)
            except (ExtractionError, OSError, ValueError, KeyError) as e:
                rep.errors.append(f"{rel}: {e}")
                log.warning("Failed to index %s/%s: %s", source, rel, e)
        if prune:
            for doc in self.store.list_documents(source):
                if doc["path"] not in seen:
                    with self.store.transaction() as conn:
                        self.store.delete_document(conn, doc["id"])
                    if doc["extract_file"]:
                        Path(doc["extract_file"]).unlink(missing_ok=True)
                    rep.removed += 1
                    self.progress(f"[{source}] removed {doc['path']}")
        return rep

    def _index_file(self, source, path: Path, rel: str, sig: str, force_extract: bool, rechunk: bool, rep: SourceReport):
        st = path.stat()
        doc = self.store.get_document(source, rel)
        if doc and not force_extract and doc["mtime"] == st.st_mtime and doc["size"] == st.st_size:
            sha = doc["sha256"]
        else:
            sha = sha256_file(path)

        content_unchanged = doc is not None and doc["sha256"] == sha
        if content_unchanged and doc["chunk_sig"] == sig and not rechunk and not force_extract:
            if doc["mtime"] != st.st_mtime or doc["size"] != st.st_size:
                with self.store.transaction() as conn:
                    conn.execute("UPDATE documents SET mtime = ?, size = ? WHERE id = ?", (st.st_mtime, st.st_size, doc["id"]))
            rep.unchanged += 1
            return

        xpath = extraction_path(self.cfg.extract_dir, source, rel)
        record = None if force_extract else load_extraction(xpath)
        if record is not None and record.get("sha256") == sha:
            rep.reused_extraction += 1
        else:
            xpath, record = run_extraction(path, source, rel, sha, self.cfg.extract_dir, qa=(source == QA_SOURCE))
            rep.extracted += 1
            self.progress(f"[{source}] extracted {rel}")

        chunks = chunk_record(record, self.cfg.chunking)
        with self.store.transaction() as conn:
            doc_id = self.store.upsert_document(
                conn,
                source=source,
                path=rel,
                title=record.get("title"),
                doc_type=record.get("doc_type"),
                sha256=sha,
                mtime=st.st_mtime,
                size=st.st_size,
                extract_file=str(xpath),
                chunk_sig=sig,
                indexed_at=now_iso(),
            )
            # Keep vectors of chunks whose text did not change (saves re-embedding cost).
            old_vectors = {
                r["embed_text"]: r["embedding"]
                for r in conn.execute(
                    "SELECT embed_text, embedding FROM chunks WHERE doc_id = ? AND embedding IS NOT NULL", (doc_id,)
                )
            }
            self.store.delete_chunks(conn, doc_id)
            self.store.insert_chunks(conn, doc_id, source, record.get("title") or rel, chunks)
            conn.executemany(
                "UPDATE chunks SET embedding = ? WHERE doc_id = ? AND embed_text = ?",
                [(vec, doc_id, text) for text, vec in old_vectors.items()],
            )
        rep.chunked += 1
        self.progress(f"[{source}] chunked {rel} -> {len(chunks)} chunks")

    def _remove_orphan_sources(self, report: IndexReport) -> None:
        known = set(self.cfg.all_search_sources())
        for doc in self.store.list_documents():
            if doc["source"] not in known:
                with self.store.transaction() as conn:
                    self.store.delete_document(conn, doc["id"])
                report.sources.setdefault(doc["source"], SourceReport()).removed += 1

    # ---- embedding --------------------------------------------------------
    def embed_missing(self) -> int:
        rows = self.store.chunks_missing_embeddings()
        if not rows:
            return 0
        model = self.provider.embedding_model
        self.store.check_model(model)
        done = 0
        for i in range(0, len(rows), EMBED_WRITE_BATCH):
            batch = rows[i : i + EMBED_WRITE_BATCH]
            vectors = self.provider.embed([r["embed_text"] for r in batch])
            dim = int(vectors.shape[1])
            self.store.check_model(model, dim)
            if self.store.model_lock() is None:
                self.store.set_model_lock(model, dim, self.cfg.provider.base_url)
            self.store.set_embeddings([r["id"] for r in batch], vectors)
            done += len(batch)
            self.progress(f"embedded {done}/{len(rows)} chunks")
        return done

    def reembed_all(self) -> int:
        """Switch the index to the configured embedding model (R9).

        Probes the provider first so a misconfigured endpoint does not wipe vectors.
        Re-uses existing chunks: no extraction, no re-chunking.
        """
        probe = self.provider.embed(["model switch probe"])
        self.progress(f"provider OK: {self.provider.embedding_model} returns {probe.shape[1]} dimensions")
        self.store.clear_all_embeddings()
        self.store.clear_model_lock()
        return self.embed_missing()
