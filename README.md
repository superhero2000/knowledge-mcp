# knowledge-mcp

A local RAG knowledge base for **Cline**, exposed as an **MCP server over stdio**.
Pure Python, a single SQLite file, no Docker, no cloud database. Only embedding and
optional chat (reranking) calls go to an OpenAI-compatible model provider.

```
 Cline ──stdio──► rag-mcp (MCP server) ──► SQLite file (chunks, FTS5, vectors, Q&A)
                        │                         ▲
                        │                         │ rag index (manual)
                        ▼                         │
          OpenAI-compatible endpoint     files ─► extraction JSON ─► chunks ─► embeddings
          (OpenAI now, internal later)
```

## Setup (no containers)

Requires Python ≥ 3.10.

```bash
cd knowledge-mcp
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[docx]"             # add ,dev for tests: pip install -e ".[docx,dev]"

cp config.example.toml config.toml   # then edit sources / provider
export OPENAI_API_KEY=sk-...         # Windows: set OPENAI_API_KEY=sk-...

rag check --chat     # verify the provider
rag index -v         # extract, chunk, embed
rag status
rag search handbook "how do I roll back a release?"
```

The shipped `config.toml` points at the synthetic test data in `testdata/` (R3).
Regenerate the PDF/DOCX test files with `python testdata/make_testdata.py`.

## Connect Cline

In Cline → *MCP Servers* → *Configure* (`cline_mcp_settings.json`):

```json
{
  "mcpServers": {
    "knowledge": {
      "command": "/abs/path/knowledge-mcp/.venv/bin/rag-mcp",
      "args": ["--config", "/abs/path/knowledge-mcp/config.toml"],
      "env": { "OPENAI_API_KEY": "sk-..." },
      "autoApprove": ["search_handbook", "search_api_docs", "search_reports", "search_qa",
                      "list_sources", "get_context"],
      "disabled": false
    }
  }
}
```

On Windows use `C:\\path\\knowledge-mcp\\.venv\\Scripts\\rag-mcp.exe`.
Put everything the server needs in `env`: the API key and, behind a corporate proxy,
`HTTPS_PROXY` / `SSL_CERT_FILE`. MCP clients start servers with only a minimal environment.
**Do not auto-approve `save_qa`.** Cline's approval prompt is the user's confirmation (R10).

Optional `.clinerules` snippet:

```
When answering questions about our docs, use the local-rag search tools. Cite every fact
with the "Cite as" string of the passage it came from. If the first page of results is not
enough, page with offset or switch mode (exact for codes/identifiers).
```

## MCP tools

| Tool | Purpose |
|---|---|
| `search_<source>` | One per configured source (R4). Args: `query`, `mode` (`hybrid`\|`semantic`\|`keyword`\|`exact`), `top_k`, `offset`, `rerank`, `case_sensitive` |
| `search_qa` | Search the confirmed Q&A base |
| `get_context` | Full chunk plus its neighbours (`chunk_id`, `window`) |
| `list_sources` | Sources, sizes, embedding model |
| `propose_qa` | Store a Q&A pair as *pending* (not searchable) |
| `save_qa` | Save a pending pair; call only after the user confirms (hidden when `qa.cli_confirmation_only = true`) |
| `reject_qa` | Discard a pending pair |

Each hit looks like this:

```
[1] deployment.md — Deployment Process > Rollback  (score 0.0328, chunk_id 12)
Cite as: [handbook: deployment.md, Deployment Process > Rollback]
To roll back, run `bw deploy rollback <service> --to <version>` ...
...
More results available: call again with offset=5.
```

## CLI

| Command | What it does |
|---|---|
| `rag index [--source S] [--rechunk] [--force-extract] [--no-prune]` | Manual (re)index; unchanged files are skipped |
| `rag status` | Counts per source, model lock state, pending Q&A |
| `rag search SOURCE QUERY [--mode] [--top-k] [--offset] [--rerank] [--json]` | Search from the terminal |
| `rag reembed --switch-model` | Rebuild all vectors with the configured embedding model |
| `rag qa pending \| approve ID \| reject ID \| list \| delete ID` | Review the Q&A base |
| `rag eval [--modes ...] [--top-k 5] [--rerank] [--set k=v] [--baseline run.json] [--label name]` | Measure retrieval quality |
| `rag check [--chat]` | Provider connectivity |
| `rag serve` / `rag-mcp` | Run the MCP server (stdio) |

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

The 39 tests run fully offline against a fake OpenAI-compatible HTTP endpoint
(`tests/fake_openai.py`). That also exercises the config-only provider switch. They cover
extraction for every format, chunking, incremental indexing, the model lock and re-embedding,
all search modes, paging, citations, both rerank methods, the Q&A flow, the evaluation, and
an end-to-end MCP stdio session. They pass with MCP SDK 1.x and 2.x.
`examples/demo_client.py` runs example tool calls through a real stdio MCP session.

## Requirement map

| Req | Where |
|---|---|
| R1 local only | SQLite file + numpy + FTS5; stdio server; `pip install`, no Docker |
| R2 one provider interface | `provider.py` `OpenAICompatibleProvider`; `[provider]` config only |
| R3 non-confidential dev data | `testdata/` (fictional) |
| R4 one tool per source | `server.py` registers `search_<source>` per `[[sources]]` |
| R5 semantic/keyword/exact/hybrid + rerank | `search.py` |
| R6 model controls count + paging | `top_k` (≤ `max_top_k`), `offset`, `next_offset` |
| R7 citations | every hit: source, document, location (pages / heading path) |
| R8 extraction JSON per document | `data/extracted/…json`, reused by `--rechunk` and `reembed` |
| R9 model lock | `store.py` `check_model` / `embedding_lock`; `rag reembed --switch-model` |
| R10 Q&A after confirmation | `propose_qa` → user confirms → `save_qa` / `rag qa approve` |
| R11 manual, incremental re-index | `rag index` (mtime/size → sha256 → chunk signature) |
| R12 measured evaluation | `rag eval`, `eval/questions.jsonl`, `eval/runs/` |

### Path to the LibreChat knowledge service
The core (`App`: store, indexer, search engine, Q&A) is independent of MCP and stdio. A later
shared service can wrap the same `SearchEngine` behind HTTP, swap SQLite/numpy for a server
database, and add access control, without changing extraction, chunking or the eval set.
