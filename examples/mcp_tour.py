"""A short scripted MCP session (stdio, like Cline) used for demos.

Usage:
    python examples/mcp_tour.py tools                 # list the tools the server exposes
    python examples/mcp_tour.py call TOOL '{"k": "v"}'  # call one tool with JSON arguments
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def main(argv: list[str]) -> None:
    config = os.environ.get("RAG_CONFIG", "config.toml")
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "rag_mcp.server", "--config", config], env=dict(os.environ)
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            info = getattr(init, "server_info", None) or getattr(init, "serverInfo", None)
            if argv[0] == "tools":
                print(f"Connected to MCP server '{info.name}' over stdio\n")
                for t in (await session.list_tools()).tools:
                    first = (t.description or "").strip().splitlines()[0]
                    print(f"  {t.name:<16} {first[:88]}")
            elif argv[0] == "call":
                args = json.loads(argv[2]) if len(argv) > 2 else {}
                print(text_of(await session.call_tool(argv[1], args)))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["tools"]))
