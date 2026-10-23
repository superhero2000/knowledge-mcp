"""Talk to the MCP server exactly like Cline does (stdio) and run a few example calls.

Usage:  python examples/demo_client.py [--config config.toml]
Requires an indexed knowledge base (`rag index`) and the provider API key in the environment.
"""

import argparse
import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

EXAMPLES = [
    ("search_handbook", {"query": "I got paged three times last night, can I start late?", "top_k": 2}),
    ("search_api_docs", {"query": "TW-403-BLOCKED", "mode": "exact"}),
    ("search_api_docs", {"query": "how should my client react to rate limiting", "mode": "semantic", "top_k": 2}),
    ("search_reports", {"query": "what went wrong with the anchors during the trial", "top_k": 1, "rerank": True}),
    ("search_handbook", {"query": "security", "mode": "keyword", "top_k": 2, "offset": 2}),
]


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def main(config: str) -> None:
    # Pass our environment through (API key, proxy settings); the SDK default passes only a minimal set.
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "rag_mcp.server", "--config", config], env=dict(os.environ)
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            print("Tools:", ", ".join(t.name for t in tools), "\n")
            for name, args in EXAMPLES:
                print(f"=== {name}({args})")
                print(text_of(await session.call_tool(name, args)), "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.toml")
    asyncio.run(main(ap.parse_args().config))
