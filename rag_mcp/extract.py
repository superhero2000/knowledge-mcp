"""Text extraction. Output is saved as one JSON file per document (R8).

Re-chunking and re-embedding read these JSON files and never re-run extraction.
Each extraction is a list of *blocks*: a piece of text with its location
(page number for PDFs, heading path for structured documents).
"""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

from .store import now_iso

EXTRACTION_SCHEMA = 1
SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt", ".text", ".rst", ".pdf", ".html", ".htm", ".docx"}


class ExtractionError(Exception):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_file(path: Path, qa: bool = False) -> tuple[str, str, list[dict]]:
    """Return (doc_type, title, blocks)."""
    if qa:
        return ("qa",) + _extract_qa(path)
    suffix = path.suffix.lower()
    if suffix in (".md", ".markdown"):
        return ("markdown",) + _extract_markdown(path.read_text(encoding="utf-8", errors="replace"), path)
    if suffix in (".txt", ".text", ".rst"):
        text = path.read_text(encoding="utf-8", errors="replace")
        return "text", path.stem, [{"text": text, "page": None, "heading_path": []}]
    if suffix == ".pdf":
        return ("pdf",) + _extract_pdf(path)
    if suffix in (".html", ".htm"):
        return ("html",) + _extract_html(path.read_text(encoding="utf-8", errors="replace"), path)
    if suffix == ".docx":
        return ("docx",) + _extract_docx(path)
    raise ExtractionError(f"Unsupported file type: {path.suffix}")


# ---- markdown -----------------------------------------------------------------
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _sections_from_lines(lines: list[str], heading_of) -> tuple[str | None, list[dict]]:
    """Group lines into blocks by heading. `heading_of(line, in_fence)` returns (level, text) or None."""
    blocks: list[dict] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    first_title = None
    in_fence = False

    def flush():
        text = "\n".join(buf).strip()
        if text:
            blocks.append({"text": text, "page": None, "heading_path": [h for _, h in stack]})
        buf.clear()

    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        h = None if in_fence else heading_of(line)
        if h:
            flush()
            level, title = h
            if first_title is None:
                first_title = title
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
        else:
            buf.append(line)
    flush()
    return first_title, blocks


def _extract_markdown(text: str, path: Path) -> tuple[str, list[dict]]:
    body = text
    title = None
    if body.startswith("---\n"):  # YAML front matter
        end = body.find("\n---", 4)
        if end != -1:
            fm = body[4:end]
            m = re.search(r"^title:\s*(.+)$", fm, re.M)
            if m:
                title = m.group(1).strip().strip("'\"")
            body = body[end + 4 :]

    def heading_of(line: str):
        m = _HEADING_RE.match(line)
        return (len(m.group(1)), m.group(2).strip()) if m else None

    first, blocks = _sections_from_lines(body.splitlines(), heading_of)
    return title or first or path.stem, blocks


# ---- pdf --------------------------------------------------------------------
def _extract_pdf(path: Path) -> tuple[str, list[dict]]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
    except Exception as e:
        raise ExtractionError(f"Cannot read PDF: {e}") from e
    blocks = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        text = re.sub(r"[ \t]+\n", "\n", text).strip()
        if text:
            blocks.append({"text": text, "page": i, "heading_path": []})
    title = None
    try:
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title).strip()
    except Exception:
        pass
    if not title and blocks:
        title = blocks[0]["text"].splitlines()[0][:120]
    return title or path.stem, blocks


# ---- html -------------------------------------------------------------------
class _HTMLText(HTMLParser):
    SKIP = {"script", "style", "nav", "footer", "head", "noscript"}
    BLOCK = {"p", "div", "li", "tr", "br", "pre", "table", "ul", "ol", "section", "article", "dd", "dt"}

    def __init__(self):
        super().__init__()
        self.lines: list[str] = []
        self.cur: list[str] = []
        self.skip = 0
        self.heading_level = 0
        self.title = None
        self.in_title = False

    def _newline(self):
        line = "".join(self.cur).strip()
        line = re.sub(r"\s+", " ", line)
        if line:
            self.lines.append(line)
        self.cur = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag in self.SKIP:
            self.skip += 1
        elif re.fullmatch(r"h[1-6]", tag):
            self._newline()
            self.heading_level = int(tag[1])
        elif tag in self.BLOCK:
            self._newline()
        elif tag in ("td", "th") and "".join(self.cur).strip():
            self.cur.append(" | ")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif re.fullmatch(r"h[1-6]", tag) and self.heading_level:
            text = re.sub(r"\s+", " ", "".join(self.cur)).strip()
            self.cur = []
            if text:
                self.lines.append("\x00H%d %s" % (self.heading_level, text))
            self.heading_level = 0
        elif tag in self.BLOCK:
            self._newline()

    def handle_data(self, data):
        if self.in_title:
            self.title = (self.title or "") + data.strip()
        if not self.skip:
            self.cur.append(data)


def _extract_html(text: str, path: Path) -> tuple[str, list[dict]]:
    p = _HTMLText()
    p.feed(text)
    p._newline()

    def heading_of(line: str):
        if line.startswith("\x00H"):
            return int(line[2]), line[4:]
        return None

    first, blocks = _sections_from_lines(p.lines, heading_of)
    return p.title or first or path.stem, blocks


# ---- docx -------------------------------------------------------------------
def _extract_docx(path: Path) -> tuple[str, list[dict]]:
    try:
        import docx  # python-docx
    except ImportError as e:
        raise ExtractionError("python-docx is not installed (pip install 'knowledge-mcp[docx]')") from e
    d = docx.Document(str(path))
    lines = []
    for para in d.paragraphs:
        style = (para.style.name or "") if para.style is not None else ""
        m = re.match(r"Heading (\d)", style)
        if m and para.text.strip():
            lines.append("\x00H%s %s" % (m.group(1), para.text.strip()))
        elif style == "Title" and para.text.strip():
            lines.append("\x00H1 " + para.text.strip())
        else:
            lines.append(para.text)
    for table in d.tables:
        for row in table.rows:
            lines.append(" | ".join(c.text.strip() for c in row.cells))

    def heading_of(line: str):
        if line.startswith("\x00H"):
            return int(line[2]), line[4:]
        return None

    first, blocks = _sections_from_lines(lines, heading_of)
    title = (d.core_properties.title or "").strip() or first or path.stem
    return title, blocks


# ---- confirmed Q&A entries ------------------------------------------------------
def qa_text(entry: dict) -> str:
    text = f"Question: {entry['question']}\n\nAnswer: {entry['answer']}"
    if entry.get("citations"):
        text += "\n\nSources: " + "; ".join(entry["citations"])
    return text


def _extract_qa(path: Path) -> tuple[str, list[dict]]:
    entry = json.loads(path.read_text(encoding="utf-8"))
    return entry["question"], [{"text": qa_text(entry), "page": None, "heading_path": [f"Q&A #{entry['id']}"]}]


# ---- JSON persistence ---------------------------------------------------------
def extraction_path(extract_dir: Path, source: str, rel_path: str) -> Path:
    return extract_dir / source / (rel_path + ".json")


def run_extraction(
    path: Path, source: str, rel_path: str, sha256: str, extract_dir: Path, qa: bool = False
) -> tuple[Path, dict]:
    doc_type, title, blocks = extract_file(path, qa=qa)
    record = {
        "schema": EXTRACTION_SCHEMA,
        "source": source,
        "path": rel_path,
        "sha256": sha256,
        "doc_type": doc_type,
        "title": title,
        "extracted_at": now_iso(),
        "blocks": blocks,
    }
    out = extraction_path(extract_dir, source, rel_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)
    return out, record


def load_extraction(path: Path) -> dict | None:
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if rec.get("schema") == EXTRACTION_SCHEMA else None
