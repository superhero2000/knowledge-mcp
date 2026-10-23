import pytest

from rag_mcp.search import SearchError, format_results


def _docs(res):
    return [r["citation"]["document"] for r in res["results"]]


@pytest.mark.parametrize("mode", ["hybrid", "semantic", "keyword"])
def test_modes_find_rollback(indexed_app, mode):
    res = indexed_app.search.search("handbook", "rollback deploy previous version", mode=mode)
    assert res["mode"] == mode
    assert "deployment.md" in _docs(res)[:3]


def test_exact_mode_and_case_sensitivity(indexed_app):
    res = indexed_app.search.search("api_docs", "TW-404-STATION", mode="exact")
    assert _docs(res) == ["errors.html"]
    assert indexed_app.search.search("api_docs", "tw-404-station", mode="exact")["returned"] == 1
    assert indexed_app.search.search("api_docs", "tw-404-station", mode="exact", case_sensitive=True)["returned"] == 0


def test_every_hit_has_citation(indexed_app):
    for source in ("handbook", "api_docs", "reports"):
        res = indexed_app.search.search(source, "battery error deployment", top_k=10)
        for r in res["results"]:
            c = r["citation"]
            assert c["source"] == source and c["document"] and c["location"]
    res = indexed_app.search.search("reports", "battery service life", mode="keyword")
    top = res["results"][0]["citation"]
    assert top["document"] == "kelp3-field-trial-2025.pdf"
    assert top["page_start"] is not None and top["location"].startswith("p")
    res = indexed_app.search.search("api_docs", "HMAC signature webhooks", mode="keyword")
    assert res["results"][0]["citation"]["location"] == "Endpoint: Forecasts > Threshold Alerts"


def test_top_k_and_paging(indexed_app):
    s = indexed_app.search
    first = s.search("handbook", "incident", mode="hybrid", top_k=4)
    assert first["returned"] == 4 and first["has_more"] and first["next_offset"] == 4
    second = s.search("handbook", "incident", mode="hybrid", top_k=4, offset=first["next_offset"])
    ids1 = {r["chunk_id"] for r in first["results"]}
    ids2 = {r["chunk_id"] for r in second["results"]}
    assert not ids1 & ids2
    assert [r["rank"] for r in second["results"]] == [5, 6, 7, 8]
    # page through everything: no duplicates, ends with has_more False
    seen, offset = [], 0
    while True:
        page = s.search("handbook", "incident", mode="hybrid", top_k=7, offset=offset)
        seen += [r["chunk_id"] for r in page["results"]]
        if not page["has_more"]:
            break
        offset = page["next_offset"]
    assert len(seen) == len(set(seen)) == page["total_candidates"]


def test_top_k_is_capped(indexed_app):
    res = indexed_app.search.search("handbook", "the", top_k=10_000)
    assert res["top_k"] == indexed_app.cfg.search.max_top_k


def test_invalid_input(indexed_app):
    with pytest.raises(SearchError):
        indexed_app.search.search("nope", "x")
    with pytest.raises(SearchError):
        indexed_app.search.search("handbook", "  ")
    with pytest.raises(SearchError):
        indexed_app.search.search("handbook", "x", mode="fuzzy")


def test_hybrid_degrades_to_keyword_when_provider_down(indexed_app, fake_endpoint):
    fake_endpoint.set_failing(True)
    try:
        res = indexed_app.search.search("handbook", "Foghorn paging app", mode="hybrid")
    finally:
        fake_endpoint.set_failing(False)
    assert res["warnings"] and "keyword results only" in res["warnings"][0]
    assert _docs(res)[0] == "oncall.md"


@pytest.mark.parametrize("method", ["llm", "endpoint"])
def test_rerank(make_app, fake_endpoint, method):
    app = make_app(rerank_method=method)
    app.indexer.index()
    fake_endpoint.reset_stats()
    res = app.search.search("api_docs", "Pro plan requests per minute", rerank=True, top_k=3)
    assert res["reranked"]
    assert _docs(res)[0] == "rate-limits.md"
    key = "chat_requests" if method == "llm" else "rerank_requests"
    assert fake_endpoint.stats[key] == 1
    # paging re-uses the cached reranked list: no new model call
    app.search.search("api_docs", "Pro plan requests per minute", rerank=True, top_k=3, offset=3)
    assert fake_endpoint.stats[key] == 1


def test_index_changes_are_picked_up_by_running_engine(indexed_app, workspace):
    s = indexed_app.search
    assert s.search("handbook", "zebra crossing", mode="exact")["returned"] == 0
    (workspace / "docs" / "handbook" / "zebra.md").write_text("# Zebra\n\nThe zebra crossing is outside building B.\n")
    indexed_app.indexer.index(["handbook"])
    assert s.search("handbook", "zebra crossing", mode="exact")["returned"] == 1
    assert s.search("handbook", "zebra crossing building", mode="semantic")["results"][0]["citation"]["document"] == "zebra.md"


def test_format_results_contains_citation_and_paging_hint(indexed_app):
    text = format_results(indexed_app.search.search("handbook", "release train", top_k=2))
    assert "Cite as: [handbook: deployment.md, Deployment Process > Release Trains]" in text
    assert "call again with offset=2" in text


def test_llm_rerank_tolerates_missing_scores(indexed_app, monkeypatch):
    s = indexed_app.search
    monkeypatch.setattr(s.provider, "chat", lambda *a, **k: '{"scores": {"1": 9, "0": 2}}')
    assert s._llm_scores("q", ["a", "b", "c"]) == [2.0, 9.0, -1.0]
    monkeypatch.setattr(s.provider, "chat", lambda *a, **k: 'Sure! {"scores": [1, 5]}')
    assert s._llm_scores("q", ["a", "b", "c"]) == [1.0, 5.0, -1.0]
