from rag_mcp.chunking import chunk_record, chunk_signature
from rag_mcp.config import ChunkingConfig
from rag_mcp.extract import extract_file, run_extraction

from conftest import TESTDATA


def test_markdown_heading_paths_and_front_matter(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text(
        "---\ntitle: My Title\n---\n# Top\nintro\n## Sub A\ntext a\n```\n# not a heading\n```\n### Deep\ndeep text\n## Sub B\ntext b\n"
    )
    doc_type, title, blocks = extract_file(p)
    assert doc_type == "markdown" and title == "My Title"
    paths = [b["heading_path"] for b in blocks]
    assert paths == [["Top"], ["Top", "Sub A"], ["Top", "Sub A", "Deep"], ["Top", "Sub B"]]
    assert "# not a heading" in blocks[1]["text"]


def test_html_sections_and_tables():
    doc_type, title, blocks = extract_file(TESTDATA / "api_docs" / "errors.html")
    assert doc_type == "html" and title.startswith("Tidewatch API")
    server = next(b for b in blocks if b["heading_path"] == ["Error Codes", "Server Errors"])
    assert "TW-503-BUOY-OFFLINE | 503 |" in server["text"]
    assert not any("Home | Docs" in b["text"] for b in blocks), "nav must be skipped"


def test_pdf_pages():
    doc_type, title, blocks = extract_file(TESTDATA / "reports" / "kelp3-field-trial-2025.pdf")
    assert doc_type == "pdf"
    assert title == "Kelp-3 Buoy Field Trial Report 2025"
    assert [b["page"] for b in blocks] == [1, 2, 3, 4, 5, 6]
    assert "seven years" in blocks[2]["text"]


def test_docx_headings_and_tables():
    doc_type, title, blocks = extract_file(TESTDATA / "reports" / "forecast-pipeline-v3-review.docx")
    assert doc_type == "docx" and title == "Architecture Review: Forecast Pipeline v3"
    paths = [tuple(b["heading_path"]) for b in blocks]
    assert ("Architecture Review: Forecast Pipeline v3", "Proposal", "Queue Technology") in paths or (
        "Proposal", "Queue Technology") in paths
    assert any("Autoscale workers" in b["text"] for b in blocks)


def test_extraction_saved_as_json(tmp_path):
    out, rec = run_extraction(TESTDATA / "handbook" / "deployment.md", "handbook", "deployment.md", "abc", tmp_path)
    assert out == tmp_path / "handbook" / "deployment.md.json"
    assert out.exists() and rec["sha256"] == "abc" and rec["blocks"]


def test_chunks_respect_size_headings_and_pages():
    cfg = ChunkingConfig(max_chars=500, overlap_chars=100, min_chars=50)
    _, title, blocks = extract_file(TESTDATA / "reports" / "kelp3-field-trial-2025.pdf")
    chunks = chunk_record({"title": title, "path": "x.pdf", "blocks": blocks}, cfg)
    assert len(chunks) >= 3
    for c in chunks:
        assert len(c["text"]) <= cfg.max_chars * 1.25
        assert c["location"].startswith("p. ") or c["location"].startswith("pp. ")
        assert c["page_start"] <= c["page_end"]
        assert c["embed_text"].startswith(title)
    assert chunks[0]["page_start"] == 1 and chunks[-1]["page_end"] == 6

    _, title, blocks = extract_file(TESTDATA / "handbook" / "deployment.md")
    chunks = chunk_record({"title": title, "path": "deployment.md", "blocks": blocks}, cfg)
    rollback = [c for c in chunks if c["heading"] == "Deployment Process > Rollback"]
    assert rollback and rollback[0]["location"] == "Deployment Process > Rollback"
    assert all("Canary" not in c["text"] or "Canary" in c["heading"] for c in chunks)


def test_long_paragraph_is_split():
    cfg = ChunkingConfig(max_chars=200, overlap_chars=0, min_chars=10)
    text = " ".join(f"Sentence number {i} is here." for i in range(60))
    chunks = chunk_record({"title": "T", "path": "t.txt", "blocks": [{"text": text, "page": None, "heading_path": []}]}, cfg)
    assert len(chunks) > 5 and all(len(c["text"]) <= 250 for c in chunks)
    assert chunks[0]["location"] == "whole document"


def test_chunk_signature_changes_with_settings():
    assert chunk_signature(ChunkingConfig(max_chars=1000)) != chunk_signature(ChunkingConfig(max_chars=1200))
