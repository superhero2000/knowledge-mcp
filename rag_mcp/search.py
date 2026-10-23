"""Search: semantic, keyword (BM25), exact (substring) and hybrid (RRF) + optional rerank.

Results are ranked once per (source, query, mode, options, index version) and the
ranking is cached, so paging with `offset` is cheap and stable (R6).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np

from .config import SEARCH_MODES, Config
from .provider import Provider, ProviderError
from .store import ModelLockError, Store

log = logging.getLogger(__name__)
MAX_POOL = 1000

_STOPWORDS = set(
    """a an and are as at be but by can do does for from how i if in into is it its of on or should so
    that the their then there these this to was what when where which who why will with you your
    der die das und oder ist sind ein eine einer wie was wer wo wann warum mit von für auf im in zu den dem des""".split()
)


class SearchError(Exception):
    pass


class _LRU(OrderedDict):
    def __init__(self, size: int):
        super().__init__()
        self.size = size

    def get_(self, key):
        if key in self:
            self.move_to_end(key)
            return self[key]
        return None

    def put(self, key, value):
        self[key] = value
        self.move_to_end(key)
        while len(self) > self.size:
            self.popitem(last=False)


@dataclass
class Ranked:
    ids: list[int]
    scores: list[float]
    reranked: bool
    warnings: list[str]


def fts_query(query: str) -> str | None:
    tokens = re.findall(r"\w+", query.lower())
    if not tokens:
        return None
    content = [t for t in tokens if t not in _STOPWORDS] or tokens
    seen: list[str] = []
    for t in content:
        if t not in seen:
            seen.append(t)
    return " OR ".join(f'"{t}"' for t in seen)


class SearchEngine:
    def __init__(self, cfg: Config, store: Store, provider: Provider):
        self.cfg = cfg
        self.store = store
        self.provider = provider
        self._qvec_cache = _LRU(256)
        self._rank_cache = _LRU(128)

    # ---- public API ---------------------------------------------------------
    def search(
        self,
        source: str,
        query: str,
        mode: str | None = None,
        top_k: int | None = None,
        offset: int = 0,
        rerank: bool | None = None,
        case_sensitive: bool = False,
    ) -> dict:
        t0 = time.perf_counter()
        if source not in self.cfg.all_search_sources():
            raise SearchError(f"Unknown source '{source}'")
        query = (query or "").strip()
        if not query:
            raise SearchError("Query must not be empty")
        mode = mode or self.cfg.search.default_mode
        if mode not in SEARCH_MODES:
            raise SearchError(f"mode must be one of {', '.join(SEARCH_MODES)}")
        top_k = int(top_k or self.cfg.search.default_top_k)
        top_k = max(1, min(top_k, self.cfg.search.max_top_k))
        offset = max(0, int(offset or 0))
        rerank = self.cfg.rerank.enabled if rerank is None else bool(rerank)

        ranked = self._ranked(source, query, mode, offset + top_k, rerank, case_sensitive)
        page_ids = ranked.ids[offset : offset + top_k]
        page_scores = ranked.scores[offset : offset + top_k]
        rows = self.store.get_chunks(page_ids)
        results = []
        limit = self.cfg.search.max_snippet_chars
        for i, (cid, score) in enumerate(zip(page_ids, page_scores)):
            r = rows.get(cid)
            if r is None:
                continue
            text = r["text"]
            if len(text) > limit:
                text = text[:limit] + " …[truncated; use get_context for the full chunk]"
            results.append(
                {
                    "rank": offset + i + 1,
                    "chunk_id": cid,
                    "score": round(float(score), 4),
                    "citation": {
                        "source": r["source"],
                        "document": r["path"],
                        "title": r["title"],
                        "location": r["location"],
                        "page_start": r["page_start"],
                        "page_end": r["page_end"],
                        "heading_path": r["heading"],
                    },
                    "text": text,
                }
            )
        total = len(ranked.ids)
        next_offset = offset + len(page_ids)
        has_more = next_offset < total
        return {
            "source": source,
            "query": query,
            "mode": mode,
            "reranked": ranked.reranked,
            "offset": offset,
            "top_k": top_k,
            "returned": len(results),
            "total_candidates": total,
            "has_more": has_more,
            "next_offset": next_offset if has_more else None,
            "results": results,
            "warnings": ranked.warnings,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }

    # ---- ranking --------------------------------------------------------------
    def _ranked(self, source, query, mode, needed, rerank, case_sensitive) -> Ranked:
        pool = min(MAX_POOL, max(self.cfg.search.candidate_pool, needed))
        key = (source, query, mode, rerank, case_sensitive, self.store.index_version(), self.provider.embedding_model)
        cached = self._rank_cache.get_(key)
        if cached and (len(cached[1].ids) >= needed or cached[0] >= MAX_POOL or len(cached[1].ids) < cached[0]):
            return cached[1]

        warnings: list[str] = []
        if mode == "semantic":
            pairs = self._semantic(source, query, pool)
        elif mode == "keyword":
            pairs = self._keyword(source, query, pool)
        elif mode == "exact":
            pairs = self._exact(source, query, pool, case_sensitive)
        else:
            try:
                sem = self._semantic(source, query, pool)
            except ProviderError as e:
                warnings.append(f"Semantic search unavailable, keyword results only: {e}")
                sem = []
            kw = self._keyword(source, query, pool)
            pairs = self._rrf([(sem, self.cfg.search.semantic_weight), (kw, self.cfg.search.keyword_weight)], pool)

        ids = [p[0] for p in pairs]
        scores = [p[1] for p in pairs]
        reranked = False
        if rerank and ids:
            try:
                ids, scores = self._rerank(query, ids, scores)
                reranked = True
            except (ProviderError, ValueError) as e:
                warnings.append(f"Reranking failed, original order kept: {e}")
        result = Ranked(ids, scores, reranked, warnings)
        self._rank_cache.put(key, (pool, result))
        return result

    def _query_vector(self, query: str) -> np.ndarray:
        key = (self.provider.embedding_model, query)
        v = self._qvec_cache.get_(key)
        if v is None:
            v = self.provider.embed([query])[0]
            self._qvec_cache.put(key, v)
        return v

    def _semantic(self, source, query, limit) -> list[tuple[int, float]]:
        self.store.check_model(self.provider.embedding_model)
        ids, mat = self.store.vectors(source)
        if len(ids) == 0:
            return []
        q = self._query_vector(query)
        if q.shape[0] != mat.shape[1]:
            raise ModelLockError(
                f"Query vector has {q.shape[0]} dimensions but the index has {mat.shape[1]}; "
                "check provider.embedding_dimensions or run `rag reembed --switch-model`."
            )
        sims = mat @ q
        k = min(limit, len(sims))
        top = np.argpartition(-sims, k - 1)[:k]
        top = top[np.argsort(-sims[top])]
        return [(int(ids[i]), float(sims[i])) for i in top]

    def _keyword(self, source, query, limit) -> list[tuple[int, float]]:
        expr = fts_query(query)
        if not expr:
            return []
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT c.id AS id, bm25(chunks_fts, 1.0, 0.6, 0.3) AS s FROM chunks_fts "
                "JOIN chunks c ON c.id = chunks_fts.rowid "
                "WHERE chunks_fts MATCH ? AND c.source = ? ORDER BY s LIMIT ?",
                (expr, source, limit),
            ).fetchall()
        return [(r["id"], -float(r["s"])) for r in rows]

    def _exact(self, source, query, limit, case_sensitive) -> list[tuple[int, float]]:
        needle = query if case_sensitive else query.lower()
        col = "text || ' ' || heading" if case_sensitive else "ulower(text || ' ' || heading)"
        with self.store.lock:
            rows = self.store.conn.execute(
                f"SELECT id, text, heading FROM chunks WHERE source = ? AND instr({col}, ?) > 0 ORDER BY id",
                (source, needle),
            ).fetchall()
        scored = []
        for r in rows:
            hay = r["text"] + " " + r["heading"]
            hay = hay if case_sensitive else hay.lower()
            scored.append((r["id"], float(hay.count(needle))))
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:limit]

    def _rrf(self, lists: list[tuple[list[tuple[int, float]], float]], limit: int) -> list[tuple[int, float]]:
        k = self.cfg.search.rrf_k
        fused: dict[int, float] = {}
        for pairs, weight in lists:
            for rank, (cid, _) in enumerate(pairs):
                fused[cid] = fused.get(cid, 0.0) + weight / (k + rank + 1)
        return sorted(fused.items(), key=lambda x: (-x[1], x[0]))[:limit]

    # ---- reranking --------------------------------------------------------------
    def _rerank(self, query: str, ids: list[int], scores: list[float]) -> tuple[list[int], list[float]]:
        n = min(self.cfg.rerank.candidates, len(ids))
        head, rest = ids[:n], ids[n:]
        rows = self.store.get_chunks(head)
        texts = [f"{rows[i]['title']} — {rows[i]['location']}\n{rows[i]['text']}" for i in head]
        rc = self.cfg.rerank
        if rc.method == "endpoint":
            new_scores = self.provider.rerank(query, texts, model=rc.model, path=rc.endpoint_path)
        else:
            new_scores = self._llm_scores(query, texts)
        order = sorted(range(n), key=lambda i: (-new_scores[i], i))
        new_ids = [head[i] for i in order] + rest
        # Reranked items keep their rerank score; the tail keeps a score below all of them.
        floor = min(new_scores) if new_scores else 0.0
        new_sc = [float(new_scores[i]) for i in order] + [floor - 1.0 for _ in rest]
        return new_ids, new_sc

    def _llm_scores(self, query: str, texts: list[str]) -> list[float]:
        passages = "\n\n".join(f"[{i}] {t[:900]}" for i, t in enumerate(texts))
        messages = [
            {
                "role": "system",
                "content": "You judge search relevance. For each passage, give a score from 0 (irrelevant) "
                "to 10 (directly answers the query). Respond with JSON only, keyed by passage number: "
                '{"scores": {"0": <score>, "1": <score>, ...}}',
            },
            {"role": "user", "content": f"Query: {query}\n\nPassages:\n{passages}"},
        ]
        raw = self.provider.chat(messages, json_mode=True, model=self.cfg.rerank.model or None)
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise ValueError("reranker returned no JSON")
        scores = json.loads(m.group(0)).get("scores")
        if isinstance(scores, list):
            scores = {str(i): v for i, v in enumerate(scores)}
        if not isinstance(scores, dict) or not scores:
            raise ValueError("reranker returned no scores")
        # Passages the model skipped sink below every scored passage but keep their relative order.
        return [float(scores.get(str(i), -1.0)) for i in range(len(texts))]


def format_results(res: dict) -> str:
    """Compact text rendering for the MCP client (LLM-friendly)."""
    head = (
        f"Source: {res['source']} | mode: {res['mode']}{' + rerank' if res['reranked'] else ''} | "
        f"results {res['offset'] + 1}-{res['offset'] + res['returned']} of {res['total_candidates']} candidates"
    )
    if not res["results"]:
        head = f"Source: {res['source']} | mode: {res['mode']} | no results for this query" + (
            f" at offset {res['offset']}" if res["offset"] else ""
        )
    lines = [head]
    for w in res["warnings"]:
        lines.append(f"WARNING: {w}")
    for r in res["results"]:
        c = r["citation"]
        lines.append("")
        lines.append(f"[{r['rank']}] {c['document']} — {c['location']}  (score {r['score']}, chunk_id {r['chunk_id']})")
        lines.append(f"Cite as: [{c['source']}: {c['document']}, {c['location']}]")
        lines.append(r["text"])
    lines.append("")
    if res["has_more"]:
        lines.append(f"More results available: call again with offset={res['next_offset']}.")
    else:
        lines.append("No further results.")
    return "\n".join(lines)
