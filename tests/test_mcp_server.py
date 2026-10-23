"""End-to-end: spawn the MCP server over stdio (as MCP clients do) and call its tools."""

import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from rag_mcp.app import App
from rag_mcp.config import load_config


def _text(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def _session_run(config_path, fn):
    env = {**os.environ, "FAKE_KEY": "test-key"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "rag_mcp.server", "--config", str(config_path)], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


def test_mcp_tools_end_to_end(workspace, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "test-key")
    cfg_path = workspace / "config.toml"
    app = App.create(load_config(cfg_path))
    app.indexer.index()
    app.close()

    async def scenario(session):
        out = {}
        tools = (await session.list_tools()).tools
        out["tools"] = {t.name: t for t in tools}
        out["search"] = _text(await session.call_tool("search_handbook", {"query": "how do I roll back", "top_k": 2}))
        out["page2"] = _text(await session.call_tool("search_handbook", {"query": "how do I roll back", "top_k": 2, "offset": 2}))
        out["exact"] = _text(await session.call_tool("search_api_docs", {"query": "TW-429-RATE", "mode": "exact"}))
        out["pdf"] = _text(await session.call_tool("search_reports", {"query": "battery service life seven years", "mode": "keyword", "top_k": 1}))
        out["bad"] = _text(await session.call_tool("search_handbook", {"query": "   "}))
        out["sources"] = _text(await session.call_tool("list_sources", {}))
        chunk_id = int(out["search"].split("chunk_id ")[1].split(")")[0])
        out["context"] = _text(await session.call_tool("get_context", {"chunk_id": chunk_id, "window": 1}))
        out["propose"] = _text(await session.call_tool("propose_qa", {
            "question": "How do I roll back a service?",
            "answer": "Run bw deploy rollback <service> --to <version>; no approval needed.",
            "citations": ["handbook: deployment.md, Deployment Process > Rollback"],
        }))
        out["qa_before"] = _text(await session.call_tool("search_qa", {"query": "roll back service", "mode": "keyword"}))
        pid = int(out["propose"].split("pending_id=")[1].split()[0].rstrip(")."))
        out["save"] = _text(await session.call_tool("save_qa", {"pending_id": pid}))
        out["qa_after"] = _text(await session.call_tool("search_qa", {"query": "roll back service"}))
        return out

    out = asyncio.run(_session_run(cfg_path, scenario))

    # R4: one search tool per source + qa
    names = set(out["tools"])
    assert {"search_handbook", "search_api_docs", "search_reports", "search_qa", "list_sources",
            "get_context", "propose_qa", "save_qa", "reject_qa"} <= names
    tool = out["tools"]["search_handbook"]
    schema = getattr(tool, "input_schema", None) or tool.inputSchema  # SDK 2.x / 1.x
    assert {"query", "mode", "top_k", "offset", "rerank"} <= set(schema["properties"])
    assert "Engineering handbook" in out["tools"]["search_handbook"].description

    # R7 citations, R6 paging
    assert "Cite as: [handbook: deployment.md, Deployment Process > Rollback]" in out["search"]
    assert "offset=2" in out["search"]
    assert "[3]" in out["page2"]
    assert "errors.html" in out["exact"] and "TW-429-RATE" in out["exact"]
    assert "Cite as: [reports: kelp3-field-trial-2025.pdf, p" in out["pdf"]
    assert out["bad"].startswith("ERROR")
    assert '"search_reports"' in out["sources"] and "fake-embed-a" in out["sources"]
    assert "(requested)" in out["context"]

    # R10: nothing saved until save_qa is called
    assert "NOT saved yet" in out["propose"]
    assert "no results" in out["qa_before"]
    assert "Saved as Q&A #1" in out["save"]
    assert "Cite as: [qa: qa-000001.json, Q&A #1]" in out["qa_after"]


def test_cli_only_confirmation_hides_save_tool(workspace, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "test-key")
    cfg_path = workspace.write_config(cli_only=True)

    async def scenario(session):
        tools = {t.name for t in (await session.list_tools()).tools}
        propose = _text(await session.call_tool("propose_qa", {"question": "q?", "answer": "a"}))
        return tools, propose

    tools, propose = asyncio.run(_session_run(cfg_path, scenario))
    assert "save_qa" not in tools and "propose_qa" in tools
    assert "rag qa approve" in propose
