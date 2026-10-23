"""MCP server over stdio for Cline.

One search tool per configured source (R4), plus the Q&A base, context
expansion and source listing. The server never re-indexes on its own (R11);
it picks up changes made by `rag index` automatically.
"""

# No `from __future__ import annotations` here: the MCP SDK evaluates the tool
# signatures (which reference config values from closures) at registration time.
import argparse
import json
import logging
import sys
from typing import Annotated, Literal, Optional

from pydantic import Field

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server
try:
    from mcp.types import ToolAnnotations
except ImportError:  # pragma: no cover
    ToolAnnotations = None

from .app import App
from .config import QA_SOURCE, ConfigError, load_config
from .provider import ProviderError
from .qa import QAError
from .search import SearchError, format_results
from .store import ModelLockError

log = logging.getLogger("rag_mcp")

Mode = Literal["hybrid", "semantic", "keyword", "exact"]
_USER_ERRORS = (SearchError, ModelLockError, ProviderError, QAError, ConfigError, ValueError)

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


def _annotations(read_only: bool, title: str):
    if ToolAnnotations is None:
        return None
    # camelCase wire names validate on both SDK 1.x and 2.x
    return ToolAnnotations.model_validate(
        {"title": title, "readOnlyHint": read_only, "destructiveHint": False, "openWorldHint": False}
    )


def build_server(app: App):
    cfg = app.cfg
    server = _Server(
        "local-rag",
        instructions=(
            "Local knowledge search. Use the search_<source> tool matching the question's domain "
            "(call list_sources if unsure; search_qa holds previously confirmed answers). "
            "Cite every fact with the 'Cite as' string of the passage it came from. "
            "When an answer is good and the user may want it again, offer to save it with propose_qa; "
            "only call save_qa after the user explicitly agreed."
        ),
    )
    s = cfg.search

    def make_search(source: str):
        def search(
            query: Annotated[str, Field(description="Natural-language question, keywords, or an exact string")],
            mode: Annotated[Mode, Field(description="hybrid | semantic | keyword | exact")] = s.default_mode,  # type: ignore[assignment]
            top_k: Annotated[int, Field(ge=1, le=s.max_top_k, description="Number of results to return")] = s.default_top_k,
            offset: Annotated[int, Field(ge=0, description="Skip this many results (paging; use next_offset)")] = 0,
            rerank: Annotated[Optional[bool], Field(description="Rerank candidates with the model (default from config)")] = None,
            case_sensitive: Annotated[bool, Field(description="Only for mode=exact")] = False,
        ) -> str:
            try:
                res = app.search.search(source, query, mode, top_k, offset, rerank, case_sensitive)
            except _USER_ERRORS as e:
                return f"ERROR: {e}"
            return format_results(res)

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

    def propose_qa(
        question: Annotated[str, Field(description="The question, phrased generally so it can be found again")],
        answer: Annotated[str, Field(description="The final answer, self-contained")],
        citations: Annotated[list[str], Field(description="'Cite as' strings the answer is based on")] = [],
    ) -> str:
        """Propose a question/answer pair for the Q&A base. Nothing is saved yet: show the proposal to
        the user and ask for explicit confirmation before calling save_qa."""
        try:
            pid = app.qa.propose(question, answer, citations)
        except _USER_ERRORS as e:
            return f"ERROR: {e}"
        how = (
            f"call save_qa(pending_id={pid})"
            if not cfg.qa.cli_confirmation_only
            else f"the user runs `rag qa approve {pid}` in a terminal"
        )
        return (
            f"Proposal stored as pending_id={pid} (NOT saved yet).\n"
            f"Ask the user: 'Save this Q&A to the knowledge base?' Only if they explicitly agree, {how}. "
            f"If they decline, call reject_qa(pending_id={pid})."
        )

    def save_qa(
        pending_id: Annotated[int, Field(description="pending_id returned by propose_qa")],
    ) -> str:
        """Save a proposed Q&A pair permanently. ONLY call this after the user explicitly confirmed
        in the conversation that this exact Q&A should be saved."""
        if cfg.qa.cli_confirmation_only:
            return f"ERROR: saving from the assistant is disabled; the user must run `rag qa approve {pending_id}`."
        try:
            entry = app.qa.approve(pending_id, via="mcp:save_qa")
        except _USER_ERRORS as e:
            return f"ERROR: {e}"
        return f"Saved as Q&A #{entry['id']} and indexed; it is now searchable with search_qa."

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
    if not cfg.qa.cli_confirmation_only:
        server.add_tool(save_qa, name="save_qa", annotations=_annotations(False, "Save confirmed Q&A entry"))
    server.add_tool(reject_qa, name="reject_qa", annotations=_annotations(False, "Reject Q&A proposal"))
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="rag-mcp", description="Local RAG MCP server (stdio)")
    parser.add_argument("--config", help="Path to config.toml (default: $RAG_CONFIG or ./config.toml)")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args(argv)
    # stdout is the MCP channel: all logging must go to stderr.
    logging.basicConfig(stream=sys.stderr, level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        app = App.create(load_config(args.config))
    except ConfigError as e:
        print(f"rag-mcp: {e}", file=sys.stderr)
        sys.exit(2)
    server = build_server(app)
    try:
        server.run("stdio")
    finally:
        app.close()


if __name__ == "__main__":
    main()
