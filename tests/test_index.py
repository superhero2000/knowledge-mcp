import json
import time

import pytest

from rag_mcp.store import ModelLockError


def test_full_index_creates_docs_chunks_vectors_and_json(indexed_app):
    app = indexed_app
    stats = app.store.stats()
    assert stats["handbook"]["documents"] == 6
    assert stats["api_docs"]["documents"] == 6
    assert stats["reports"]["documents"] == 2
    assert all(s["unembedded"] == 0 for s in stats.values())
    # R8: one JSON per document
    jsons = list(app.cfg.extract_dir.rglob("*.json"))
    assert len(jsons) == 14
    rec = json.loads((app.cfg.extract_dir / "api_docs" / "endpoints" / "forecasts.md.json").read_text())
    assert rec["source"] == "api_docs" and rec["blocks"]


def test_reindex_skips_unchanged(indexed_app, fake_endpoint):
    fake_endpoint.reset_stats()
    report = indexed_app.indexer.index()
    for name in ("handbook", "api_docs", "reports"):
        r = report.sources[name]
        assert r.unchanged == r.scanned and r.extracted == 0 and r.chunked == 0
    assert report.embedded == 0
    assert fake_endpoint.stats["embedding_requests"] == 0


def test_changed_added_and_deleted_files(indexed_app, workspace):
    hb = workspace / "docs" / "handbook"
    p = hb / "oncall.md"
    p.write_text(p.read_text() + "\n## Pager Etiquette\n\nAlways write a short note when you snooze a page.\n")
    (hb / "new-page.md").write_text("# Coffee\n\nThe espresso machine is descaled every Friday.\n")
    (hb / "code-review.md").unlink()
    report = indexed_app.indexer.index(["handbook"])
    r = report.sources["handbook"]
    assert (r.extracted, r.chunked, r.removed, r.unchanged) == (2, 2, 1, 4)
    assert indexed_app.store.get_document("handbook", "code-review.md") is None
    assert not (indexed_app.cfg.extract_dir / "handbook" / "code-review.md.json").exists()
    res = indexed_app.search.search("handbook", "espresso machine descaled", mode="keyword")
    assert res["results"][0]["citation"]["document"] == "new-page.md"


def test_touch_without_content_change_is_skipped(indexed_app, workspace):
    p = workspace / "docs" / "handbook" / "oncall.md"
    time.sleep(0.01)
    p.write_text(p.read_text())  # new mtime, same content
    r = indexed_app.indexer.index(["handbook"]).sources["handbook"]
    assert r.unchanged == 6 and r.extracted == 0


def test_rechunk_reuses_extraction_and_unchanged_vectors(make_app, fake_endpoint):
    app = make_app()
    app.indexer.index()
    fake_endpoint.reset_stats()
    # Changing chunk settings: re-chunk from JSON, never re-extract (R8).
    app2 = make_app()
    app2.cfg.chunking.max_chars = 400
    report = app2.indexer.index()
    for name in ("handbook", "api_docs", "reports"):
        r = report.sources[name]
        assert r.extracted == 0 and r.reused_extraction == r.scanned and r.chunked == r.scanned
    assert report.embedded > 0
    # explicit --rechunk with identical settings re-embeds nothing (chunk texts unchanged)
    fake_endpoint.reset_stats()
    report = app2.indexer.index(rechunk=True)
    assert report.embedded == 0 and fake_endpoint.stats["embedding_requests"] == 0


def test_model_lock_blocks_second_model(make_app):
    app = make_app()
    app.indexer.index()
    assert app.store.model_lock()["model"] == "fake-embed-a"
    other = make_app(model="fake-embed-b")
    with pytest.raises(ModelLockError, match="locked to embedding model 'fake-embed-a'"):
        other.indexer.index()
    with pytest.raises(ModelLockError):
        other.search.search("handbook", "rollback", mode="semantic")
    # keyword / exact search do not need the embedding model and keep working
    assert other.search.search("handbook", "rollback", mode="keyword")["results"]


def test_model_lock_checks_dimensions(make_app, workspace):
    app = make_app()
    app.indexer.index()
    lock = app.store.model_lock()
    app.store.set_model_lock(lock["model"], 999, lock["base_url"])
    (workspace / "docs" / "handbook" / "extra.md").write_text("# Extra\n\nsomething new\n")
    with pytest.raises(ModelLockError, match="999"):
        app.indexer.index(["handbook"])


def test_reembed_switches_model(make_app, fake_endpoint):
    make_app().indexer.index()
    other = make_app(model="fake-embed-small")
    n = other.indexer.reembed_all()
    assert n == sum(s["chunks"] for s in other.store.stats().values())
    lock = other.store.model_lock()
    assert lock["model"] == "fake-embed-small" and lock["dim"] == 64
    res = other.search.search("handbook", "roll back a deployment", mode="semantic")
    assert res["results"]


def test_reembed_probe_protects_index_when_provider_down(make_app, fake_endpoint):
    make_app().indexer.index()
    other = make_app(model="fake-embed-b")
    fake_endpoint.set_failing(True)
    try:
        with pytest.raises(Exception):
            other.indexer.reembed_all()
    finally:
        fake_endpoint.set_failing(False)
    assert other.store.model_lock()["model"] == "fake-embed-a"
    assert all(s["unembedded"] == 0 for s in other.store.stats().values())
