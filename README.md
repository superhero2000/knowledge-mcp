# knowledge-mcp

A local RAG knowledge base served over the **Model Context Protocol (MCP)**. It works
with any MCP-compatible client, including Claude Desktop, Claude Code, VS Code (GitHub
Copilot), Cursor, Windsurf, Cline, Continue, Codex CLI, Gemini CLI, LibreChat, the OpenAI
Agents SDK and the MCP Inspector. It uses only the standard MCP protocol, with nothing
specific to any one client.

Pure Python, a single SQLite file, no Docker, no cloud database. Only embedding and
optional chat (reranking) calls go to an OpenAI-compatible model provider.

```
 any MCP client ──stdio or HTTP──► knowledge-mcp ──► SQLite file (chunks, FTS5, vectors, Q&A)
                                        │                    ▲
                                        │                    │ rag index (manual)
                                        ▼                    │
                  OpenAI-compatible endpoint      files ─► extraction JSON ─► chunks ─► embeddings
                  (OpenAI now, internal later)
```

## Install (no containers)

Requires Python ≥ 3.10.

```bash
git clone https://github.com/superhero2000/knowledge-mcp
cd knowledge-mcp
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[docx]"             # add ,dev for tests: pip install -e ".[docx,dev]"
```

Or install it as an isolated tool, without cloning:
`pipx install git+https://github.com/superhero2000/knowledge-mcp`, then add DOCX support with
`pipx inject knowledge-mcp python-docx`. The repository is private, so git needs access to it.

### Configure

```bash
rag init                             # writes a starter config to your per-user config folder
export OPENAI_API_KEY=sk-...         # Windows: set OPENAI_API_KEY=sk-...
rag check --chat                     # verify the provider
rag index -v                         # extract, chunk, embed
rag search docs "how do I ...?"
```

The server finds its config in this order. The order matters because MCP clients start
servers from arbitrary working directories:

1. `--config PATH`
2. `$KNOWLEDGE_MCP_CONFIG` (or the older `$RAG_CONFIG`)
3. `./config.toml` in the working directory
4. The per-user folder: `~/.config/knowledge-mcp/config.toml` (Linux),
   `~/Library/Application Support/knowledge-mcp/config.toml` (macOS),
   `%APPDATA%\knowledge-mcp\config.toml` (Windows)

To try it on the bundled synthetic test data instead, run the commands from the repo folder:
its `config.toml` points at `testdata/` (R3). Regenerate the PDF/DOCX test files with
`python testdata/make_testdata.py`.

## Connect an MCP client

Every client needs the same three things:

| | stdio (client starts the server) | HTTP (client connects to a URL) |
|---|---|---|
| command | `/abs/path/.venv/bin/knowledge-mcp` (Windows: `...\.venv\Scripts\knowledge-mcp.exe`) | start once: `knowledge-mcp --transport streamable-http` |
| args | `["--config", "/abs/path/config.toml"]` (optional, see lookup order) | |
| env | `OPENAI_API_KEY` (and `HTTPS_PROXY` / `SSL_CERT_FILE` behind a proxy) | set in the shell that starts the server |
| URL | | `http://127.0.0.1:8765/mcp` (SSE: `--transport sse`, `http://127.0.0.1:8765/sse`) |

Use **absolute paths**. Pass the API key in the client's `env`: most clients start servers
with only a minimal environment. If you can't run the launcher (`knowledge-mcp`), use the
interpreter instead: command `/abs/path/.venv/bin/python`, args `["-m", "rag_mcp.server", ...]`.

Client config formats change between versions. If a snippet below doesn't match yours,
check your client's MCP documentation: the command, args, env and URL stay the same.

**Claude Desktop** (`claude_desktop_config.json`), **Cursor** (`~/.cursor/mcp.json` or
`.cursor/mcp.json`), **Windsurf** (`~/.codeium/windsurf/mcp_config.json`), **Cline**
(`cline_mcp_settings.json`), **Gemini CLI** (`~/.gemini/settings.json`):

```json
{
  "mcpServers": {
    "knowledge": {
      "command": "/abs/path/knowledge-mcp/.venv/bin/knowledge-mcp",
      "args": ["--config", "/abs/path/config.toml"],
      "env": { "OPENAI_API_KEY": "sk-..." }
    }
  }
}
```

**Claude Code**:

```bash
claude mcp add knowledge --env OPENAI_API_KEY=sk-... -- /abs/path/.venv/bin/knowledge-mcp --config /abs/path/config.toml
# or, with the server already running over HTTP:
claude mcp add --transport http knowledge http://127.0.0.1:8765/mcp
```

**VS Code / GitHub Copilot** (`.vscode/mcp.json`). This format can prompt for the key
instead of storing it:

```json
{
  "inputs": [{ "type": "promptString", "id": "openai-key", "description": "OpenAI API key", "password": true }],
  "servers": {
    "knowledge": {
      "type": "stdio",
      "command": "/abs/path/knowledge-mcp/.venv/bin/knowledge-mcp",
      "args": ["--config", "/abs/path/config.toml"],
      "env": { "OPENAI_API_KEY": "${input:openai-key}" }
    }
  }
}
```

**Codex CLI** (`~/.codex/config.toml`):

```toml
[mcp_servers.knowledge]
command = "/abs/path/knowledge-mcp/.venv/bin/knowledge-mcp"
args = ["--config", "/abs/path/config.toml"]
env = { OPENAI_API_KEY = "sk-..." }
```

**LibreChat** (`librechat.yaml`):

```yaml
mcpServers:
  knowledge:
    type: streamable-http
    url: http://127.0.0.1:8765/mcp
```

**OpenAI Agents SDK** (Python):

```python
from agents.mcp import MCPServerStdio
knowledge = MCPServerStdio(params={"command": "/abs/path/.venv/bin/knowledge-mcp",
                                   "args": ["--config", "/abs/path/config.toml"]})
```

**MCP Inspector** (to debug the server):
`npx @modelcontextprotocol/inspector /abs/path/.venv/bin/knowledge-mcp --config /abs/path/config.toml`

### HTTP transport and security

For the architecture and the full security model (including how outbound traffic is restricted to
the configured endpoint), see [documentation.md](documentation.md).


`knowledge-mcp --transport streamable-http [--host 127.0.0.1] [--port 8765]` serves at `/mcp`.
`--transport sse` serves at `/sse`. You can also set this in the `[server]` section of the
config. **The HTTP transport has no authentication.** It binds to `127.0.0.1`, and the server
refuses other addresses unless you pass `--allow-remote` (or set `server.allow_remote = true`).
Only do that behind an authenticating reverse proxy. Prefer stdio for a single user.

## MCP features

| Feature | Name | Purpose |
|---|---|---|
| tool | `search_<source>` | One per configured source (R4). Args: `query`, `mode` (`hybrid`\|`semantic`\|`keyword`\|`exact`), `top_k`, `offset`, `rerank`, `case_sensitive`. Returns text for the model plus `structuredContent` (JSON) |
| tool | `search_qa` | Search the confirmed Q&A base |
| tool | `get_context` | Full chunk plus its neighbours (`chunk_id`, `window`) |
| tool | `list_sources` | Sources, sizes, embedding model |
| tool | `propose_qa` | Propose a Q&A pair; the user is asked to confirm (see below) |
| tool | `save_qa` | Save a pending pair after confirmation (only in `auto` / `tool_approval` mode) |
| tool | `reject_qa` | Discard a pending pair |
| prompt | `answer_with_citations` | Ready-made instruction to answer a question with cited sources (shown as a slash command or prompt template in many clients) |

Search and listing tools are annotated `readOnlyHint`, so clients that support annotations
can auto-approve them safely. Each hit looks like this:

```
[1] deployment.md — Deployment Process > Rollback  (score 0.0328, chunk_id 12)
Cite as: [handbook: deployment.md, Deployment Process > Rollback]
To roll back, run `bw deploy rollback <service> --to <version>` ...
...
More results available: call again with offset=5.
```

### Confirming Q&A entries in any client (R10)

`qa.confirmation` in the config decides how the user confirms a proposed Q&A entry:

| Mode | Behaviour |
|---|---|
| `auto` (default) | If the client supports **MCP elicitation**, the server shows the user a "Save this entry?" dialog itself, so the model can't skip it. Otherwise the client's tool-approval prompt for `save_qa` is the confirmation. |
| `elicitation` | Only through elicitation. Clients without it get a pending entry for the user to approve with `rag qa approve <id>`. |
| `tool_approval` | The client's approval of the `save_qa` call is the confirmation. Don't auto-approve `save_qa` in your client. |
| `cli` | Only `rag qa approve <id>` in a terminal; `save_qa` is not offered. |

Each saved entry records how it was confirmed (`confirmed_via`).

## CLI

| Command | What it does |
|---|---|
| `rag init [--path P] [--force]` | Write a starter config (default: the per-user config folder) |
| `rag index [--source S] [--rechunk] [--force-extract] [--no-prune]` | Manual (re)index; unchanged files are skipped |
| `rag status` | Counts per source, model lock state, pending Q&A |
| `rag search SOURCE QUERY [--mode] [--top-k] [--offset] [--rerank] [--json]` | Search from the terminal |
| `rag reembed --switch-model` | Rebuild all vectors with the configured embedding model |
| `rag qa pending \| approve ID \| reject ID \| list \| delete ID` | Review the Q&A base |
| `rag eval [--modes ...] [--top-k 5] [--rerank] [--set k=v] [--baseline run.json] [--label name]` | Measure retrieval quality |
| `rag check [--chat]` | Provider connectivity |
| `knowledge-mcp [--transport stdio\|streamable-http\|sse] [--host] [--port]` | Run the MCP server (also `rag serve ...`, `rag-mcp`, `python -m rag_mcp.server`) |

## Switching to the internal endpoint (R2)

Change only the `[provider]` block. No code changes:

```toml
[provider]
base_url        = "https://llm-gateway.internal.example/v1"
api_key_env     = "INTERNAL_LLM_API_KEY"
embedding_model = "bge-m3"
chat_model      = "llama-3.3-70b-instruct"
ca_bundle       = "/etc/ssl/certs/corp-ca.pem"
```

(The env vars `RAG_BASE_URL`, `RAG_EMBEDDING_MODEL`, `RAG_CHAT_MODEL` and `RAG_API_KEY_ENV` also override these.)
If the embedding model changes, the **model lock** refuses to index or search semantically
until you run `rag reembed --switch-model`. That re-embeds the existing chunks: no
re-extraction and no re-chunking. A probe call runs first, so a broken endpoint never
wipes your vectors. For reranking through a `/rerank` endpoint (vLLM, TEI, Jina or
Cohere style), set `rerank.method = "endpoint"` and `rerank.model`.

## How it works

* **Extraction** (`rag_mcp/extract.py`) handles Markdown (heading paths, front matter), HTML
  (headings, tables; nav/script skipped), PDF (per page, via pypdf), DOCX (heading styles,
  tables) and TXT. Output is one JSON per document in `data/extracted/<source>/<path>.json` (R8).
* **Chunking** (`rag_mcp/chunking.py`) never crosses headings and prefers page boundaries in
  PDFs. Every chunk gets a `location` (`p. 4`, `pp. 4-5`, `Deployment > Rollback`) and is
  embedded with a small title/heading header.
* **Incremental indexing** (`rag_mcp/ingest.py`) skips a file when mtime/size are unchanged,
  else compares sha256. Unchanged extraction JSON is reused. Changed chunk settings cause a
  re-chunk only. Vectors of unchanged chunk texts are kept. Deleted files are pruned.
* **Storage** (`rag_mcp/store.py`) is one SQLite file: documents, chunks, an FTS5 index
  (BM25, porter stemming) and float32 vectors searched with numpy (exact cosine). It needs
  no native extension. A running server notices `rag index` updates automatically.
* **Search** (`rag_mcp/search.py`) offers four modes. `semantic` is cosine similarity,
  `keyword` is FTS5 BM25, `exact` is a substring match (Unicode case-folding), and `hybrid`
  fuses semantic and keyword with Reciprocal Rank Fusion. Optional reranking uses the LLM
  or a `/rerank` endpoint. Rankings are cached, so paging is stable and cheap. If
  embeddings are unreachable, hybrid falls back to keyword search with a warning.
* **Q&A base** (`rag_mcp/qa.py`): proposals are pending in SQLite. Confirmed entries are written
  to `data/qa/qa-NNNNNN.json` (the source of truth, survives an index rebuild) and indexed as source `qa`.
* **Server** (`rag_mcp/server.py`) is built on the official MCP Python SDK (works with 1.x and 2.x).
  It supports stdio, streamable HTTP and SSE, tool annotations, structured tool output, a prompt,
  and elicitation for Q&A confirmation.

## Evaluation (R12)

`eval/questions.jsonl` holds 40 questions over the test data. Each one names the passage that
must be retrieved (document plus a snippet it must contain). Each run reports hit@1, hit@k,
MRR@k and latency per mode, plus per-source breakdowns and misses, and saves everything with
a config snapshot to `eval/runs/`. Compare a tuning experiment against a baseline:

```bash
rag eval --label baseline
rag eval --modes hybrid --set search.semantic_weight=2 --baseline eval/runs/<ts>-baseline.json
rag eval --modes hybrid --rerank --baseline eval/runs/<ts>-baseline.json
```

Measured on the test data with `text-embedding-3-small` (top_k = 5):

| Config | hit@1 | hit@5 | MRR@5 |
|---|---|---|---|
| hybrid (page-aware chunking) | 0.925 | 1.000 | 0.958 |
| hybrid, before page-aware chunking | 0.875 | 1.000 | 0.933 |
| hybrid + `semantic_weight=2` (old chunking) | 0.950 | 1.000 | 0.975 |
| hybrid + LLM rerank (old chunking) | 0.950 | 1.000 | 0.975 |
| semantic | 1.000 | 1.000 | 1.000 |
| keyword | 0.800 | 0.950 | 0.868 |

The synthetic set is small and easy for embeddings. **Build an eval set from your real
documents and real questions** before changing defaults. Note: `mean_ms` for semantic looks
tiny when it runs after hybrid, because query embeddings are cached in-process.

## Tests

```bash
pip install -e ".[docx,dev]"
pytest -q
```

The 67 tests run fully offline against a fake OpenAI-compatible HTTP endpoint
(`tests/fake_openai.py`). That also exercises the config-only provider switch. They cover
extraction for every format, chunking, incremental indexing, the model lock and re-embedding,
all search modes, paging, citations, both rerank methods, the evaluation, and real MCP
sessions over stdio, streamable HTTP and SSE. The MCP tests include every Q&A confirmation
mode, with clients that do and don't support elicitation, plus structured output, the
prompt, and config discovery. Security tests check that no request ever reaches a host
other than the configured endpoint. They pass with MCP SDK 1.x and 2.x.
`examples/demo_client.py` runs example tool calls through a real stdio MCP session.

## Requirement map

| Req | Where |
|---|---|
| R1 local only | SQLite file + numpy + FTS5; stdio (or localhost HTTP) server; `pip install`, no Docker |
| R2 one provider interface | `provider.py` `OpenAICompatibleProvider`; `[provider]` config only |
| R3 non-confidential dev data | `testdata/` (fictional) |
| R4 one tool per source | `server.py` registers `search_<source>` per `[[sources]]` |
| R5 semantic/keyword/exact/hybrid + rerank | `search.py` |
| R6 model controls count + paging | `top_k` (≤ `max_top_k`), `offset`, `next_offset` |
| R7 citations | every hit: source, document, location (pages / heading path) |
| R8 extraction JSON per document | `data/extracted/…json`, reused by `--rechunk` and `reembed` |
| R9 model lock | `store.py` `check_model` / `embedding_lock`; `rag reembed --switch-model` |
| R10 Q&A after confirmation | `propose_qa` → user confirms (MCP elicitation, tool approval or `rag qa approve`) |
| R11 manual, incremental re-index | `rag index` (mtime/size → sha256 → chunk signature) |
| R12 measured evaluation | `rag eval`, `eval/questions.jsonl`, `eval/runs/` |

### Path to the LibreChat knowledge service
LibreChat can already connect over streamable HTTP for a single user. The core (`App`: store,
indexer, search engine, Q&A) is independent of the MCP transport. A later shared service can
add authentication and access control in front of the same server, swap SQLite/numpy for a
server database, and keep extraction, chunking and the eval set unchanged.
