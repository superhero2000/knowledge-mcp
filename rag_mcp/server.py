"""knowledge-mcp: an MCP server usable from any MCP-compatible client.

Transports: stdio (default; the client starts the server), streamable HTTP and
SSE (the client connects to a URL). Tools: one search tool per configured source
(R4), the Q&A base, context expansion and source listing; plus a prompt that
asks the model to answer with citations. The server never re-indexes on its own
(R11); it picks up changes made by `rag index` automatically.
"""

# No `from __future__ import annotations` here: the MCP SDK evaluates the tool
# signatures (which reference config values from closures) at registration time.
import argparse
import asyncio
import inspect
import ipaddress
import json
import logging
import sys
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import Context
    from mcp.server.fastmcp import FastMCP as _Server
from mcp.types import CallToolResult, TextContent

try:
    from mcp.types import ToolAnnotations
except ImportError:  # pragma: no cover
    ToolAnnotations = None

from .app import App
from .config import QA_SOURCE, TRANSPORTS, ConfigError, load_config
from .provider import ProviderError
from .qa import QAError
from .search import SearchError, format_results
from .store import ModelLockError

log = logging.getLogger("rag_mcp")

SERVER_NAME = "knowledge"
Mode = Literal["hybrid", "semantic", "keyword", "exact"]
_USER_ERRORS = (SearchError, ModelLockError, ProviderError, QAError, ConfigError, ValueError)

INSTRUCTIONS = (
    "Local knowledge search with citations. Use the search_<source> tool matching the question's "
    "domain (call list_sources if unsure; search_qa holds answers the user previously confirmed). "
    "Cite every fact with the 'Cite as' string of the passage it came from. "
    "When an answer is worth keeping, you may offer to save it with propose_qa; the user must confirm "
    "before anything is saved."
)

SEARCH_DOC = """Search the '{name}' knowledge source. {description}

Returns passages with a citation each (source, document, location = page range or heading path).
Modes:
- hybrid (default): semantic + keyword fused; best general choice.
- semantic: meaning-based, good for paraphrased or conceptual questions.
- keyword: BM25 term matching, good for distinctive words and names.
- exact: literal substring match, for error codes, identifiers, config keys, quoted phrases.
Control the number of results with top_k (1-{max_top_k}). If the answer is not in the results and
has_more is indicated, call again with offset=<next_offset> to page further, or rephrase / switch mode.
Set rerank=true for higher precision on hard queries (slower, uses the chat model).
Always cite facts as [source: document, location] exactly as given in 'Cite as'."""


class SaveConfirmation(BaseModel):
    save: bool = Field(default=True, description="Save this question and answer to the knowledge base")


def _annotations(read_only: bool, title: str):
    if ToolAnnotations is None:
        return None
    # camelCase wire names validate on both SDK 1.x and 2.x
    return ToolAnnotations.model_validate(
        {"title": title, "readOnlyHint": read_only, "destructiveHint": False, "openWorldHint": False}
    )


def client_supports_elicitation(ctx) -> bool:
    """True if the connected client declared the MCP elicitation capability."""
    try:
        session = ctx.session
    except Exception:
        return False
    caps = getattr(session, "client_capabilities", None)  # SDK 2.x
    if callable(caps):
        caps = caps()
    if caps is None:  # SDK 1.x
        params = getattr(session, "client_params", None)
        caps = getattr(params, "capabilities", None)
    return getattr(caps, "elicitation", None) is not None


def build_server(app: App):
    cfg = app.cfg
    server = _Server(SERVER_NAME, instructions=INSTRUCTIONS)
    s = cfg.search
    confirmation = cfg.qa.confirmation

    def make_search(source: str):
        def search(
            query: Annotated[str, Field(description="Natural-language question, keywords, or an exact string")],
            mode: Annotated[Mode, Field(description="hybrid | semantic | keyword | exact")] = s.default_mode,  # type: ignore[assignment]
            top_k: Annotated[int, Field(ge=1, le=s.max_top_k, description="Number of results to return")] = s.default_top_k,
            offset: Annotated[int, Field(ge=0, description="Skip this many results (paging; use next_offset)")] = 0,
            rerank: Annotated[Optional[bool], Field(description="Rerank candidates with the model (default from config)")] = None,
            case_sensitive: Annotated[bool, Field(description="Only for mode=exact")] = False,
        ) -> CallToolResult:
            try:
                res = app.search.search(source, query, mode, top_k, offset, rerank, case_sensitive)
            except _USER_ERRORS as e:
                return CallToolResult(content=[TextContent(type="text", text=f"ERROR: {e}")], isError=True)
            # Text for the model, structured JSON for clients that consume structuredContent.
            return CallToolResult(content=[TextContent(type="text", text=format_results(res))], structuredContent=res)

        return search

    for src in cfg.sources:
        server.add_tool(
            make_search(src.name),
            name=f"search_{src.name}",
            description=SEARCH_DOC.format(name=src.name, description=src.description, max_top_k=s.max_top_k),
            annotations=_annotations(True, f"Search {src.name}"),
        )
    server.add_tool(
        make_search(QA_SOURCE),
        name=f"search_{QA_SOURCE}",
        description=SEARCH_DOC.format(
            name=QA_SOURCE,
            description="Previously answered questions that the user confirmed as correct. "
            "Check it first for recurring questions; each entry lists its original sources.",
            max_top_k=s.max_top_k,
        ),
        annotations=_annotations(True, "Search confirmed Q&A"),
    )

    def list_sources() -> str:
        """List the knowledge sources, what they contain, their size and the embedding model in use."""
        stats = app.store.stats()
        out = []
        for src in cfg.sources:
            st = stats.get(src.name, {})
            out.append(
                {"tool": f"search_{src.name}", "source": src.name, "description": src.description,
                 "documents": st.get("documents", 0), "chunks": st.get("chunks", 0)}
            )
        st = stats.get(QA_SOURCE, {})
        out.append({"tool": "search_qa", "source": QA_SOURCE, "description": "Confirmed Q&A base",
                    "documents": st.get("documents", 0), "chunks": st.get("chunks", 0)})
        lock = app.store.model_lock()
        return json.dumps({"sources": out, "embedding_model": lock["model"] if lock else None}, indent=2)

    def get_context(
        chunk_id: Annotated[int, Field(description="chunk_id from a search result")],
        window: Annotated[int, Field(ge=0, le=5, description="Neighbouring chunks before/after to include")] = 1,
    ) -> str:
        """Fetch a chunk in full together with its neighbouring chunks from the same document."""
        rows = app.store.neighbours(chunk_id, window)
        if not rows:
            return f"ERROR: chunk {chunk_id} not found"
        parts = []
        for r in rows:
            marker = " (requested)" if r["id"] == chunk_id else ""
            parts.append(
                f"--- chunk_id {r['id']}{marker} | Cite as: [{r['source']}: {r['path']}, {r['location']}]\n{r['text']}"
            )
        return "\n\n".join(parts)

    async def ask_user(ctx, pending: dict) -> str | None:
        """Ask the user through MCP elicitation. Returns 'save', 'decline', 'cancel' or None (not possible)."""
        if confirmation not in ("auto", "elicitation") or not client_supports_elicitation(ctx):
            return None
        message = (
            "Save this entry to the knowledge base?\n\n"
            f"Q: {pending['question']}\nA: {pending['answer']}"
            + (f"\nSources: {'; '.join(pending['citations'])}" if pending["citations"] else "")
        )
        try:
            result = await ctx.elicit(message=message, schema=SaveConfirmation)
        except Exception as e:  # client advertised elicitation but failed: fall back
            log.warning("Elicitation failed: %s", e)
            return None
        if result.action == "accept":
            return "save" if getattr(result.data, "save", False) else "decline"
        return "decline" if result.action == "decline" else "cancel"

    async def confirm_and_save(pid: int, ctx, via_tool: bool) -> str:
        answer = await ask_user(ctx, app.qa.get_pending(pid))
        if answer == "save":
            entry = await asyncio.to_thread(app.qa.approve, pid, "mcp:elicitation")
            return f"The user confirmed. Saved as Q&A #{entry['id']}; it is now searchable with search_qa."
        if answer == "decline":
            app.qa.reject(pid)
            return "The user declined. The proposal was discarded; do not save it."
        if answer == "cancel":
            return f"The user dismissed the confirmation. Proposal pending_id={pid} stays pending (not saved)."
        # No elicitation available.
        if confirmation == "cli" or confirmation == "elicitation":
            return (
                f"Proposal stored as pending_id={pid} (NOT saved). This setup only accepts confirmation "
                f"outside the conversation: the user can run `rag qa approve {pid}` in a terminal."
            )
        if via_tool:  # tool_approval: the client's approval of this save_qa call is the confirmation
            entry = await asyncio.to_thread(app.qa.approve, pid, "mcp:tool-approval")
            return f"Saved as Q&A #{entry['id']}; it is now searchable with search_qa."
        return (
            f"Proposal stored as pending_id={pid} (NOT saved yet).\n"
            f"Ask the user: 'Save this Q&A to the knowledge base?' Only if they explicitly agree, "
            f"call save_qa(pending_id={pid}). If they decline, call reject_qa(pending_id={pid})."
        )

    async def propose_qa(
        question: Annotated[str, Field(description="The question, phrased generally so it can be found again")],
        answer: Annotated[str, Field(description="The final answer, self-contained")],
        ctx: Context,
        citations: Annotated[list[str], Field(description="'Cite as' strings the answer is based on")] = [],
    ) -> str:
        """Propose a question/answer pair for the Q&A base. The user is asked to confirm (directly by the
        server if the client supports it); nothing is saved without their confirmation."""
        try:
            pid = app.qa.propose(question, answer, citations)
            return await confirm_and_save(pid, ctx, via_tool=False)
        except _USER_ERRORS as e:
            return f"ERROR: {e}"

    async def save_qa(
        pending_id: Annotated[int, Field(description="pending_id returned by propose_qa")],
        ctx: Context,
    ) -> str:
        """Save a proposed Q&A pair permanently. ONLY call this after the user explicitly confirmed
        in the conversation that this exact Q&A should be saved."""
        try:
            return await confirm_and_save(pending_id, ctx, via_tool=True)
        except _USER_ERRORS as e:
            return f"ERROR: {e}"

    def reject_qa(pending_id: Annotated[int, Field(description="pending_id returned by propose_qa")]) -> str:
        """Discard a proposed Q&A pair the user did not want to save."""
        try:
            app.qa.reject(pending_id)
        except _USER_ERRORS as e:
            return f"ERROR: {e}"
        return f"Proposal {pending_id} discarded."

    server.add_tool(list_sources, name="list_sources", annotations=_annotations(True, "List sources"))
    server.add_tool(get_context, name="get_context", annotations=_annotations(True, "Get chunk context"))
    server.add_tool(propose_qa, name="propose_qa", annotations=_annotations(False, "Propose Q&A entry"))
    if confirmation in ("auto", "tool_approval"):
        server.add_tool(save_qa, name="save_qa", annotations=_annotations(False, "Save confirmed Q&A entry"))
    server.add_tool(reject_qa, name="reject_qa", annotations=_annotations(False, "Reject Q&A proposal"))

    @server.prompt(
        name="answer_with_citations",
        description="Answer a question from the local knowledge sources, citing every fact.",
    )
    def answer_with_citations(question: str) -> str:
        names = ", ".join(f"search_{n}" for n in cfg.all_search_sources())
        return (
            f"Answer the following question using the knowledge tools ({names}).\n"
            "Pick the source(s) that match the question; check search_qa for previously confirmed answers. "
            "Use mode=exact for error codes and identifiers. If the first results are not enough, page with "
            "offset or rephrase. Cite every fact with the 'Cite as' string of its passage, and say clearly if "
            "the sources do not contain the answer.\n\n"
            f"Question: {question}"
        )

    return server


def _is_loopback(host: str) -> bool:
    # Note: an empty host is NOT loopback; binding to "" listens on every interface.
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def run_server(server, transport: str, host: str, port: int) -> None:
    if transport == "stdio":
        server.run("stdio")
        return
    params = inspect.signature(server.run).parameters
    if "host" in params or any(p.kind is p.VAR_KEYWORD for p in params.values()):
        try:  # SDK 2.x takes host/port per run
            server.run(transport, host=host, port=port)
            return
        except TypeError:
            pass
    server.settings.host = host  # SDK 1.x keeps them in settings
    server.settings.port = port
    server.run(transport)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="knowledge-mcp", description="knowledge-mcp: local RAG MCP server")
    parser.add_argument("--config", help="Path to config.toml (default: $KNOWLEDGE_MCP_CONFIG, ./config.toml, "
                                         "then the per-user config folder)")
    parser.add_argument("--transport", choices=TRANSPORTS, help="Override server.transport (default stdio)")
    parser.add_argument("--host", help="HTTP bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, help="HTTP port (default 8765)")
    parser.add_argument("--allow-remote", action="store_true",
                        help="Allow binding HTTP to a non-loopback address (no authentication!)")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args(argv)
    # With stdio, stdout is the MCP channel: all logging must go to stderr.
    logging.basicConfig(stream=sys.stderr, level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"knowledge-mcp: {e}", file=sys.stderr)
        sys.exit(2)
    transport = args.transport or cfg.server.transport
    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    if transport != "stdio" and not _is_loopback(host) and not (args.allow_remote or cfg.server.allow_remote):
        print(f"knowledge-mcp: refusing to listen on {host}: the HTTP transport has no authentication. "
              "Use a loopback address, or pass --allow-remote behind an authenticating proxy.", file=sys.stderr)
        sys.exit(2)
    app = App.create(cfg)
    server = build_server(app)
    if transport != "stdio":
        path = "/mcp" if transport == "streamable-http" else "/sse"
        print(f"knowledge-mcp: serving {transport} on http://{host}:{port}{path}", file=sys.stderr)
    try:
        run_server(server, transport, host, port)
    finally:
        app.close()


if __name__ == "__main__":
    main()
