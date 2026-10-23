import json

import pytest

from rag_mcp.evaluate import is_relevant, load_questions, run_eval
from rag_mcp.qa import QAError


def test_qa_requires_confirmation(indexed_app):
    app = indexed_app
    pid = app.qa.propose(
        "What is the deploy freeze period?",
        "No deployments from December 20 to January 6 except security fixes and rollbacks.",
        ["handbook: deployment.md, Deployment Process > Freeze Periods"],
    )
    # pending proposals are NOT searchable
    assert app.search.search("qa", "deploy freeze", mode="keyword")["returned"] == 0
    assert [p["id"] for p in app.qa.pending()] == [pid]

    entry = app.qa.approve(pid, via="test")
    assert app.qa.pending() == []
    res = app.search.search("qa", "when is the deployment freeze")
    assert res["returned"] == 1
    hit = res["results"][0]
    assert hit["citation"]["source"] == "qa"
    assert hit["citation"]["location"] == f"Q&A #{entry['id']}"
    assert "December 20" in hit["text"] and "Freeze Periods" in hit["text"]
    saved = json.loads((app.cfg.qa_dir / "qa-000001.json").read_text())
    assert saved["confirmed_via"] == "test"


def test_qa_reject_and_errors(indexed_app):
    pid = indexed_app.qa.propose("q?", "a.")
    indexed_app.qa.reject(pid)
    with pytest.raises(QAError):
        indexed_app.qa.approve(pid, via="test")
    with pytest.raises(QAError):
        indexed_app.qa.propose("", "answer")


def test_qa_survives_index_rebuild(indexed_app, make_app):
    pid = indexed_app.qa.propose("Who approves destructive migrations?", "A database owner.")
    indexed_app.qa.approve(pid, via="test")
    indexed_app.close()
    indexed_app.cfg.db_path.unlink()
    for suffix in ("-wal", "-shm"):
        indexed_app.cfg.db_path.with_name(indexed_app.cfg.db_path.name + suffix).unlink(missing_ok=True)
    fresh = make_app()
    report = fresh.indexer.index()
    # extraction JSON is reused after losing the database
    assert report.sources["handbook"].reused_extraction == 6
    assert fresh.search.search("qa", "destructive migrations", mode="keyword")["returned"] == 1


def test_qa_delete(indexed_app):
    e = indexed_app.qa.approve(indexed_app.qa.propose("q one?", "answer one"), via="test")
    indexed_app.qa.delete(e["id"])
    assert indexed_app.search.search("qa", "answer one", mode="keyword")["returned"] == 0


def test_eval_set_loads_and_runs(indexed_app):
    questions = load_questions(indexed_app.cfg.eval_dir / "questions.jsonl")
    assert len(questions) >= 30
    report = run_eval(indexed_app, questions, [{"mode": "hybrid", "rerank": False}, {"mode": "keyword", "rerank": False}], 5)
    for conf in report["configs"]:
        m = conf["metrics"]
        assert 0 <= m["hit@1"] <= m["hit@5"] <= 1
        assert len(conf["questions"]) == len(questions)
    # even the offline fake embeddings must retrieve most expected passages
    assert report["configs"][0]["metrics"]["hit@5"] >= 0.7


def test_is_relevant():
    r = {"citation": {"document": "endpoints/forecasts.md"}, "text": "Signed with HMAC-SHA256  in the header"}
    assert is_relevant(r, [{"document": "forecasts.md", "contains": "hmac-sha256 in the"}])
    assert not is_relevant(r, [{"document": "forecasts.md", "contains": "RSA"}])
    assert not is_relevant(r, [{"document": "stations.md"}])
