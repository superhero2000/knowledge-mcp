"""Client-neutral behaviour: elicitation confirmation, HTTP transports, prompt, structured output, config discovery."""

import asyncio
import os
import socket
import subprocess
import sys
import time

import pytest
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import ElicitResult

try:
    from mcp.client.streamable_http import streamable_http_client
except ImportError:  # older 1.x
    from mcp.client.streamable_http import streamablehttp_client as streamable_http_client

from rag_mcp import server as server_mod
from rag_mcp.app import App
from rag_mcp.cli import main as cli_main
from rag_mcp.config import ConfigError, load_config


def _text(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


def _index(cfg_path):
    app = App.create(load_config(cfg_path))
    app.indexer.index()
    app.close()


def _stdio_params(cfg_path):
    env = {**os.environ, "FAKE_KEY": "test-key"}
    return StdioServerParameters(command=sys.executable, args=["-m", "rag_mcp.server", "--config", str(cfg_path)], env=env)


async def _stdio_run(cfg_path, fn, elicitation_callback=None):
    kwargs = {"elicitation_callback": elicitation_callback} if elicitation_callback else {}
    async with stdio_client(_stdio_params(cfg_path)) as (read, write):
        async with ClientSession(read, write, **kwargs) as session:
            await session.initialize()
            return await fn(session)


def _elicitor(action, save=True, seen=None):
    async def callback(context, params):
        if seen is not None:
            seen.append(params.message)
        return ElicitResult(action=action, content={"save": save} if action == "accept" else None)

    return callback


PROPOSAL = {
    "question": "How long does a rollback take?",
    "answer": "Under five minutes; previous images stay warm for 72 hours.",
    "citations": ["handbook: deployment.md, Deployment Process > Rollback"],
}


@pytest.fixture
def cfg_path(workspace, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "test-key")
    path = workspace / "config.toml"
    _index(path)
    return path


# ---- Q&A confirmation through MCP elicitation (client-independent, R10) ----------------
def test_elicitation_accept_saves(cfg_path):
    seen = []

    async def scenario(session):
        out = _text(await session.call_tool("propose_qa", PROPOSAL))
        found = _text(await session.call_tool("search_qa", {"query": "rollback take", "mode": "keyword"}))
        return out, found

    out, found = asyncio.run(_stdio_run(cfg_path, scenario, _elicitor("accept", True, seen)))
    assert "The user confirmed. Saved as Q&A #1" in out
    assert "Cite as: [qa: qa-000001.json, Q&A #1]" in found
    assert seen and "Under five minutes" in seen[0] and "Deployment Process > Rollback" in seen[0]
    entry = (cfg_path.parent / "data" / "qa" / "qa-000001.json").read_text()
    assert '"confirmed_via": "mcp:elicitation"' in entry


@pytest.mark.parametrize("action,save", [("decline", True), ("accept", False)])
def test_elicitation_decline_discards(cfg_path, action, save):
    async def scenario(session):
        out = _text(await session.call_tool("propose_qa", PROPOSAL))
        found = _text(await session.call_tool("search_qa", {"query": "rollback take", "mode": "keyword"}))
        return out, found

    out, found = asyncio.run(_stdio_run(cfg_path, scenario, _elicitor(action, save)))
    assert "declined" in out and "no results" in found
    app = App.create(load_config(cfg_path))
    assert app.qa.pending() == []
    app.close()


def test_elicitation_cancel_keeps_pending(cfg_path):
    out = asyncio.run(_stdio_run(cfg_path, lambda s: s.call_tool("propose_qa", PROPOSAL), _elicitor("cancel")))
    assert "stays pending" in _text(out)


def test_save_qa_cannot_bypass_elicitation(cfg_path):
    """With an elicitation-capable client, even a direct save_qa call asks the user."""
    async def scenario(session):
        app = App.create(load_config(cfg_path))
        pid = app.qa.propose("q?", "a.")
        app.close()
        return _text(await session.call_tool("save_qa", {"pending_id": pid}))

    assert "declined" in asyncio.run(_stdio_run(cfg_path, scenario, _elicitor("decline")))


def test_client_without_elicitation_falls_back_to_tool_approval(cfg_path):
    async def scenario(session):
        out = _text(await session.call_tool("propose_qa", PROPOSAL))
        pid = int(out.split("pending_id=")[1].split()[0].rstrip(")."))
        saved = _text(await session.call_tool("save_qa", {"pending_id": pid}))
        return out, saved

    out, saved = asyncio.run(_stdio_run(cfg_path, scenario))
    assert "NOT saved yet" in out and "Saved as Q&A #1" in saved
    entry = (cfg_path.parent / "data" / "qa" / "qa-000001.json").read_text()
    assert '"confirmed_via": "mcp:tool-approval"' in entry


def test_strict_elicitation_mode_without_support_requires_cli(workspace, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "test-key")
    path = workspace.write_config()
    path.write_text(path.read_text().replace("cli_confirmation_only = false", 'confirmation = "elicitation"'))
    _index(path)

    async def scenario(session):
        tools = {t.name for t in (await session.list_tools()).tools}
        return tools, _text(await session.call_tool("propose_qa", PROPOSAL))

    tools, out = asyncio.run(_stdio_run(path, scenario))
    assert "save_qa" not in tools
    assert "rag qa approve" in out and "NOT saved" in out


def test_tool_approval_mode_ignores_elicitation(workspace, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "test-key")
    path = workspace.write_config()
    path.write_text(path.read_text().replace("cli_confirmation_only = false", 'confirmation = "tool_approval"'))
    _index(path)
    seen = []
    out = asyncio.run(_stdio_run(path, lambda s: s.call_tool("propose_qa", PROPOSAL), _elicitor("accept", True, seen)))
    assert "NOT saved yet" in _text(out) and seen == []


# ---- structured output and prompt ---------------------------------------------------------
def test_structured_content_and_prompt(cfg_path):
    async def scenario(session):
        res = await session.call_tool("search_handbook", {"query": "rollback", "top_k": 2})
        bad = await session.call_tool("search_handbook", {"query": "  "})
        prompts = (await session.list_prompts()).prompts
        prompt = await session.get_prompt("answer_with_citations", {"question": "How do I roll back?"})
        return res, bad, prompts, prompt

    res, bad, prompts, prompt = asyncio.run(_stdio_run(cfg_path, scenario))
    structured = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    assert structured["source"] == "handbook" and structured["returned"] == 2
    assert structured["results"][0]["citation"]["document"] == "deployment.md"
    assert (getattr(bad, "is_error", None) or getattr(bad, "isError", None)) is True
    assert "answer_with_citations" in {p.name for p in prompts}
    text = prompt.messages[0].content.text
    assert "search_handbook" in text and "search_qa" in text and "How do I roll back?" in text


# ---- HTTP transports ---------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int, proc, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(proc.stderr.read().decode())
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise TimeoutError(f"server did not open port {port}")


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
def test_http_transports(cfg_path, transport):
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "rag_mcp.server", "--config", str(cfg_path), "--transport", transport, "--port", str(port)],
        env={**os.environ, "FAKE_KEY": "test-key"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        _wait_port(port, proc)

        async def scenario():
            if transport == "streamable-http":
                ctx = streamable_http_client(f"http://127.0.0.1:{port}/mcp")
            else:
                ctx = sse_client(f"http://127.0.0.1:{port}/sse")
            async with ctx as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    tools = {t.name for t in (await session.list_tools()).tools}
                    out = _text(await session.call_tool("search_api_docs", {"query": "TW-429-RATE", "mode": "exact"}))
                    return tools, out

        tools, out = asyncio.run(scenario())
        assert {"search_handbook", "search_api_docs", "search_reports", "search_qa"} <= tools
        assert "Cite as: [api_docs: errors.html" in out
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_http_refuses_non_loopback_without_allow_remote(cfg_path, capsys):
    with pytest.raises(SystemExit) as exc:
        server_mod.main(["--config", str(cfg_path), "--transport", "streamable-http", "--host", "0.0.0.0"])
    assert exc.value.code == 2
    assert "refusing to listen on 0.0.0.0" in capsys.readouterr().err


def test_is_loopback():
    assert server_mod._is_loopback("127.0.0.1") and server_mod._is_loopback("::1") and server_mod._is_loopback("localhost")
    assert not server_mod._is_loopback("0.0.0.0") and not server_mod._is_loopback("192.168.1.5")


# ---- config discovery and `rag init` -------------------------------------------------------------
@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    for var in ("KNOWLEDGE_MCP_CONFIG", "RAG_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("APPDATA", str(home / "AppData"))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return home


def test_rag_init_writes_user_config_that_is_found(clean_env, capsys):
    with pytest.raises(ConfigError, match="rag init"):
        load_config()
    with pytest.raises(SystemExit) as exc:
        cli_main(["init"])
    assert exc.value.code == 0
    cfg = load_config()  # found in the per-user folder, regardless of the working directory
    assert "knowledge-mcp" in str(cfg.config_path)
    assert cfg.sources[0].name == "docs" and cfg.qa.confirmation == "auto" and cfg.server.transport == "stdio"
    with pytest.raises(SystemExit) as exc:
        cli_main(["init"])
    assert exc.value.code == 1  # refuses to overwrite


def test_env_var_and_cwd_config_take_precedence(clean_env, workspace, monkeypatch):
    cli_main_exit = pytest.raises(SystemExit)
    with cli_main_exit:
        cli_main(["init"])
    (clean_env.parent / "elsewhere" / "config.toml").write_text((workspace / "config.toml").read_text())
    assert load_config().config_path.parent.name == "elsewhere"
    monkeypatch.setenv("KNOWLEDGE_MCP_CONFIG", str(workspace / "config.toml"))
    assert load_config().config_path == (workspace / "config.toml").resolve()
