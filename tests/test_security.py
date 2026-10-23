"""Security properties: egress only to the configured endpoint, safe scanning, safe HTTP binding."""

import os

import pytest
from fake_openai import FakeOpenAI

from rag_mcp import server as server_mod
from rag_mcp.config import ProviderConfig
from rag_mcp.provider import OpenAICompatibleProvider, ProviderError, egress_guard


@pytest.fixture
def other_host():
    """A second server standing in for 'any other host'. It must never receive a request."""
    with FakeOpenAI() as srv:
        yield srv


def _provider(base_url, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "test-key")
    return OpenAICompatibleProvider(ProviderConfig(base_url=base_url, api_key_env="FAKE_KEY",
                                                   embedding_model="fake-embed-a", max_retries=0, timeout_s=5))


def test_requests_reach_the_configured_endpoint(fake_endpoint, monkeypatch):
    v = _provider(fake_endpoint.base_url, monkeypatch).embed(["hello"])
    assert v.shape == (1, 256)


@pytest.mark.parametrize("call", ["embed", "chat", "rerank"])
def test_redirect_to_another_host_is_blocked(fake_endpoint, other_host, monkeypatch, call):
    p = _provider(fake_endpoint.base_url, monkeypatch)
    other_origin = other_host.base_url.rsplit("/v1", 1)[0]
    fake_endpoint.set_redirect(other_origin)
    other_host.reset_stats()
    try:
        with pytest.raises(ProviderError):
            if call == "embed":
                p.embed(["CONFIDENTIAL document text"])
            elif call == "chat":
                p.chat([{"role": "user", "content": "CONFIDENTIAL"}])
            else:
                p.rerank("q", ["CONFIDENTIAL"], model="m", path="/rerank")
    finally:
        fake_endpoint.set_redirect(None)
    assert other_host.stats.get("requests", 0) == 0, "document text reached another host"


def test_rerank_path_cannot_point_elsewhere(fake_endpoint, other_host, monkeypatch):
    p = _provider(fake_endpoint.base_url, monkeypatch)
    other_host.reset_stats()
    fake_endpoint.reset_stats()
    # An absolute URL in endpoint_path is appended under base_url, so it still goes to the endpoint.
    p.rerank("q", ["x"], model="m", path=other_host.base_url + "/rerank")
    assert fake_endpoint.stats["rerank_requests"] == 1
    assert other_host.stats.get("requests", 0) == 0


@pytest.mark.parametrize("url,allowed", [
    ("https://api.openai.com/v1/embeddings", True),
    ("https://API.openai.com:443/v1/x", True),
    ("http://api.openai.com/v1/embeddings", False),      # scheme downgrade
    ("https://api.openai.com:8443/v1", False),           # other port
    ("https://api.openai.com.evil.example/v1", False),   # look-alike host
    ("https://10.0.0.5/v1", False),
])
def test_egress_guard_origin_matching(url, allowed):
    import httpx
    check = egress_guard("https://api.openai.com/v1")
    req = httpx.Request("POST", url)
    if allowed:
        check(req)
    else:
        with pytest.raises(ProviderError):
            check(req)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_symlinks_out_of_a_source_are_not_indexed(make_app, workspace, tmp_path):
    secret_dir = tmp_path / "outside"
    secret_dir.mkdir()
    (secret_dir / "id_rsa.md").write_text("# key\n\nSECRET-PRIVATE-KEY-MATERIAL\n")
    hb = workspace / "docs" / "handbook"
    try:
        os.symlink(secret_dir / "id_rsa.md", hb / "leak.md")
        os.symlink(secret_dir, hb / "leakdir")
    except OSError:
        pytest.skip("symlinks not permitted")
    (hb / "inside.md").write_text("# Inside\n\nharmless\n")
    os.symlink(hb / "inside.md", hb / "alias.md")  # a link that stays inside the source is fine
    app = make_app()
    app.indexer.index(["handbook"])
    paths = {d["path"] for d in app.store.list_documents("handbook")}
    assert "leak.md" not in paths and not any(p.startswith("leakdir") for p in paths)
    assert {"inside.md", "alias.md"} <= paths
    assert app.search.search("handbook", "SECRET-PRIVATE-KEY-MATERIAL", mode="exact")["returned"] == 0


def test_empty_host_is_not_loopback(workspace, monkeypatch, capsys):
    assert not server_mod._is_loopback("")
    monkeypatch.setenv("FAKE_KEY", "test-key")
    cfg = workspace / "config.toml"
    cfg.write_text(cfg.read_text() + '\n[server]\ntransport = "streamable-http"\nhost = ""\n')
    with pytest.raises(SystemExit) as exc:
        server_mod.main(["--config", str(cfg)])
    assert exc.value.code == 2 and "refusing to listen" in capsys.readouterr().err
