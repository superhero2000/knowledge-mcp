"""Chunking of extraction records into citeable chunks.

Chunks never cross a heading boundary; for paged documents (PDF) they may span
consecutive pages and record the page range. Every chunk carries a human-readable
`location` used in citations (R7).
"""

from __future__ import annotations

import hashlib
import re

from .config import ChunkingConfig

CHUNKER_VERSION = "2"
_SENT_RE = re.compile(r"(?<=[.!?])\s+")


def chunk_signature(cfg: ChunkingConfig) -> str:
    raw = f"{CHUNKER_VERSION}:{cfg.max_chars}:{cfg.overlap_chars}:{cfg.min_chars}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _split_long(text: str, max_chars: int) -> list[str]:
    """Split one oversized paragraph on sentence boundaries, then hard-split."""
    if len(text) <= max_chars:
        return [text]
    out, cur = [], ""
    for sent in _SENT_RE.split(text):
        while len(sent) > max_chars:
            if cur:
                out.append(cur)
                cur = ""
            out.append(sent[:max_chars])
            sent = sent[max_chars:]
        if cur and len(cur) + 1 + len(sent) > max_chars:
            out.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}" if cur else sent
    if cur:
        out.append(cur)
    return out


def _units(blocks: list[dict], max_chars: int):
    """Yield (text, page, heading_tuple) paragraph units."""
    for b in blocks:
        heading = tuple(b.get("heading_path") or ())
        page = b.get("page")
        for para in re.split(r"\n\s*\n", b["text"]):
            para = para.strip()
            if not para:
                continue
            for piece in _split_long(para, max_chars):
                yield piece, page, heading


def format_location(heading: tuple, page_start, page_end) -> str:
    parts = []
    if page_start is not None:
        parts.append(f"p. {page_start}" if page_start == page_end else f"pp. {page_start}-{page_end}")
    if heading:
        parts.append(" > ".join(heading))
    return ", ".join(parts) or "whole document"


def chunk_record(record: dict, cfg: ChunkingConfig) -> list[dict]:
    title = record.get("title") or record["path"]
    chunks: list[dict] = []
    cur: list[tuple[str, object]] = []  # (text, page)
    cur_heading: tuple | None = None

    def emit():
        if not cur:
            return
        text = "\n\n".join(t for t, _ in cur)
        pages = [p for _, p in cur if p is not None]
        ps, pe = (min(pages), max(pages)) if pages else (None, None)
        heading = cur_heading or ()
        heading_str = " > ".join(heading)
        # A small contextual header improves retrieval of short or generic chunks.
        header = title if not heading_str else f"{title} — {heading_str}"
        chunks.append(
            {
                "ord": len(chunks),
                "text": text,
                "heading": heading_str,
                "page_start": ps,
                "page_end": pe,
                "location": format_location(heading, ps, pe),
                "embed_text": f"{header}\n\n{text}",
            }
        )

    for text, page, heading in _units(record.get("blocks", []), cfg.max_chars):
        size = sum(len(t) + 2 for t, _ in cur)
        if cur and heading != cur_heading:
            emit()
            cur = []
        elif cur and page is not None and cur[-1][1] is not None and page != cur[-1][1] and size >= cfg.min_chars:
            # Prefer page boundaries in paged documents: keeps page citations precise.
            emit()
            cur = []
        elif cur and size + len(text) > cfg.max_chars:
            emit()
            # Carry trailing paragraphs as overlap when they fit the overlap budget.
            carry, total = [], 0
            for t, p in reversed(cur):
                if total + len(t) > cfg.overlap_chars:
                    break
                carry.insert(0, (t, p))
                total += len(t)
            cur = carry
        cur_heading = heading
        cur.append((text, page))
    emit()

    # Merge tiny trailing chunks into their predecessor within the same section.
    merged: list[dict] = []
    for c in chunks:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and len(c["text"]) < cfg.min_chars
            and prev["heading"] == c["heading"]
            and len(prev["text"]) + len(c["text"]) <= cfg.max_chars * 1.25
        ):
            prev["text"] += "\n\n" + c["text"]
            prev["embed_text"] += "\n\n" + c["text"]
            if c["page_end"] is not None:
                prev["page_end"] = c["page_end"]
                prev["location"] = format_location(
                    tuple(prev["heading"].split(" > ")) if prev["heading"] else (), prev["page_start"], prev["page_end"]
                )
            continue
        merged.append(c)
    for i, c in enumerate(merged):
        c["ord"] = i
    return merged
