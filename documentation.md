# knowledge-mcp: Technical Documentation

This document describes how knowledge-mcp is built (architecture) and how it is secured
(security). For installation and client setup, see [README.md](README.md).

**Version described:** `main` after the security review of 2026-10-23.

---

## Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
   - [Components](#21-components)
   - [Data flow: indexing](#22-data-flow-indexing)
   - [Data flow: a search request](#23-data-flow-a-search-request)
   - [Storage](#24-storage)
   - [The MCP layer](#25-the-mcp-layer)
   - [Configuration](#26-configuration)
   - [Design decisions](#27-design-decisions)
3. [Security](#3-security)
   - [Scope and trust model](#31-scope-and-trust-model)
   - [Network egress: only the configured endpoint](#32-network-egress-only-the-configured-endpoint)
   - [Network ingress: who can reach the server](#33-network-ingress-who-can-reach-the-server)
   - [Data handling](#34-data-handling)
   - [Secrets](#35-secrets)
   - [Input validation and injection](#36-input-validation-and-injection)
   - [Human confirmation for writes](#37-human-confirmation-for-writes)
   - [Security review: findings and fixes](#38-security-review-findings-and-fixes)
   - [Verifying the egress guarantee yourself](#39-verifying-the-egress-guarantee-yourself)
   - [Residual risks and recommendations](#310-residual-risks-and-recommendations)

---

## 1. Overview

knowledge-mcp is a local retrieval-augmented generation (RAG) knowledge base. It indexes
local documents and exposes search over them as tools via the **Model Context Protocol
(MCP)**, so that any MCP-compatible AI client can answer questions with citations.

**What runs where:**

| Runs locally (your machine) | Runs remotely (the model provider) |
|---|---|
| Document scanning and text extraction | Computing embeddings (vectors) for chunks and queries |
| Chunking | Optional: scoring passages for reranking (chat model or `/rerank` endpoint) |
| SQLite database (keyword index, vectors, metadata) | |
| All search, ranking, paging and citation logic | |
| The MCP server (stdio or localhost HTTP) | |
| The Q&A base and the evaluation harness | |

The provider is any **OpenAI-compatible** API, reached through one configurable endpoint
(`provider.base_url`). It is OpenAI during development and an internal endpoint in production.

**Codebase:** about 2,600 lines of Python in `rag_mcp/`, 67 automated tests in `tests/`.

---

## 2. Architecture

```
                      ┌────────────────────────── knowledge-mcp (one Python process) ──────────────────────────┐
 MCP client           │                                                                                         │
 (Claude, VS Code, ──►│  server.py ── MCP layer: tools, prompt, transports (stdio / HTTP / SSE)                 │
  Cursor, …)          │      │                                                                                  │
                      │      ▼                                                                                  │
 rag CLI ────────────►│  cli.py ──► app.py (App: wires the components below together)                           │
                      │               │                                                                         │
                      │   ┌───────────┼───────────────┬──────────────────┬──────────────┐                      │
                      │   ▼           ▼               ▼                  ▼              ▼                      │
                      │ ingest.py   search.py        qa.py           evaluate.py    config.py                  │
                      │ (indexer)   (search engine)  (Q&A base)      (R12 eval)     (TOML config)              │
                      │   │  │          │   │                                                                   │
                      │   │  ▼          │   ▼                                                                   │
                      │   │ extract.py  │  store.py ──────────► SQLite file (data/rag.sqlite)                  │
                      │   │ chunking.py │                                                                       │
                      │   ▼             ▼                                                                       │
                      │  provider.py (the ONLY outbound network code) ──egress guard──┐                         │
                      └───────────────────────────────────────────────────────────────┼─────────────────────────┘
                                                                                      ▼
                                                              configured endpoint (provider.base_url)
```

### 2.1 Components

| Module | Responsibility |
|---|---|
| `config.py` | Loads and validates `config.toml`; finds it (`--config`, `$KNOWLEDGE_MCP_CONFIG`, `./config.toml`, per-user folder); rejects unknown keys and invalid source names |
| `provider.py` | The single model-provider interface (R2): embeddings, chat (for LLM reranking), `/rerank` endpoint. Contains the **egress guard** |
| `extract.py` | Turns files into text *blocks* with locations: Markdown (heading paths), HTML (headings, tables), PDF (pages, via `pypdf`), DOCX (heading styles, via `python-docx`), TXT. Saves one JSON per document (R8) |
| `chunking.py` | Splits blocks into chunks that never cross a heading and prefer page boundaries; each chunk gets a citation `location` (`p. 4`, `pp. 4-5`, `Deployment > Rollback`) |
| `ingest.py` | The indexer behind `rag index`: scan → extract → chunk → embed, skipping unchanged work (R11); prunes deleted files; enforces the model lock (R9) |
| `store.py` | All SQLite access: schema, documents, chunks, FTS5 keyword index, vectors, model lock, statistics |
| `search.py` | Search modes (semantic, keyword, exact, hybrid via Reciprocal Rank Fusion), optional reranking, paging, result formatting with citations (R5–R7) |
| `qa.py` | The Q&A base: pending proposals, confirmed entries as JSON files, indexing them as source `qa` (R10) |
| `evaluate.py` | The retrieval evaluation: hit@1, hit@k, MRR, latency per configuration (R12) |
| `app.py` | Creates one shared instance of each component (`App`) for the CLI and the server |
| `server.py` | The MCP server: registers tools and the prompt, handles Q&A confirmation via elicitation, runs the chosen transport |
| `cli.py` | The `rag` command: `init`, `index`, `status`, `search`, `reembed`, `qa`, `eval`, `check`, `serve` |

All components share **one** `Store` (database connection) and **one** `Provider`. The CLI
and the MCP server call the same methods, so `rag search` and the `search_<source>` tool
behave identically.

### 2.2 Data flow: indexing

`rag index` is the only way the index changes (R11). The server never indexes on its own.

```
for each configured source:
  scan folder (include/exclude globs; skip hidden files and symlinks that leave the folder)
  for each file:
    unchanged? (mtime+size, else sha256; and same chunk settings)    → skip
    extraction JSON exists with the same sha256?                      → reuse it (no re-extraction)
    otherwise                                                          → extract → write JSON
    chunk the extraction → replace the document's chunks in SQLite
       (vectors of chunks whose text did not change are kept)
  files that disappeared                                               → removed from the index
embed every chunk that has no vector yet (batched; checks the model lock first)
```

### 2.3 Data flow: a search request

```
client → tools/call search_handbook {query, mode, top_k, offset, rerank}
  → SDK validates arguments against the schema (types, enum, min/max)
  → SearchEngine.search("handbook", …)
       semantic: embed the query (provider) → cosine similarity against the source's vectors (numpy)
       keyword:  SQLite FTS5 BM25 query (tokens quoted, OR-joined)
       exact:    substring match (Unicode case-folding)
       hybrid:   Reciprocal Rank Fusion of semantic + keyword
       rerank:   top candidates rescored by the chat model or the /rerank endpoint (provider)
     ranking cached per (source, query, options, index version) → stable, cheap paging
  → result: text with "Cite as: [source: document, location]" per hit + structuredContent JSON
```

If the embedding endpoint is unreachable, hybrid search falls back to keyword results and
says so in a warning.

### 2.4 Storage

Everything lives under the `storage` paths in the config (default `data/`):

| Path | Content | Rebuildable? |
|---|---|---|
| `data/rag.sqlite` | documents, chunks, FTS5 index, vectors (float32 BLOBs), model lock, pending Q&A | Yes, from the files + extraction JSON |
| `data/extracted/<source>/<path>.json` | one extraction per document (R8) | Yes, from the source files |
| `data/qa/qa-NNNNNN.json` | confirmed Q&A entries (source of truth) | **No**: back these up |
| `eval/runs/*.json` | evaluation results with a config snapshot | No, but optional |

SQLite schema (`store.py`):

| Table | Purpose |
|---|---|
| `meta` | schema version, index version counter, embedding model lock |
| `documents` | one row per indexed file: source, relative path, title, type, sha256, mtime, size, chunk signature |
| `chunks` | text, heading, page range, citation location, embedding input text, vector |
| `chunks_fts` | FTS5 keyword index over chunk text/heading/title, kept in sync by triggers |
| `qa_pending` | Q&A proposals awaiting confirmation |

SQLite runs in WAL mode, so `rag index` can write while a running server reads; the server
notices index changes through the index version counter and reloads its vector cache.

### 2.5 The MCP layer

Built on the official MCP Python SDK; compatible with SDK 1.x (`FastMCP`) and 2.x (`MCPServer`).

| Feature | Name | Notes |
|---|---|---|
| tool (one per source) | `search_<source>` | generated from `[[sources]]` at startup; read-only |
| tool | `search_qa` | the confirmed Q&A base; read-only |
| tool | `list_sources`, `get_context` | read-only |
| tool | `propose_qa`, `save_qa`, `reject_qa` | write to the Q&A base; confirmation required (§3.7) |
| prompt | `answer_with_citations` | instruction template for clients that support prompts |

Tools are plain Python functions; their type hints (`Annotated`, `Literal`, `Field(ge=…, le=…)`)
become the JSON input schema that clients see and the SDK enforces. Read-only tools carry
`readOnlyHint` annotations.

**Transports:** `stdio` (default; the client starts the server as a subprocess),
`streamable-http` (`/mcp`) and `sse` (`/sse`), bound to `127.0.0.1:8765` by default.

### 2.6 Configuration

One TOML file, with sections `storage`, `provider`, `chunking`, `search`, `rerank`, `qa`,
`server` and one `[[sources]]` block per source. Unknown keys are rejected, so typos fail
loudly. Relative paths resolve against the config file's folder. Switching from OpenAI to an
internal endpoint changes only `[provider]` (R2).

### 2.7 Design decisions

| Decision | Why |
|---|---|
| Vectors in SQLite BLOBs + numpy brute-force search | One portable file, no native extensions, exact results; fast enough for up to a few hundred thousand chunks |
| FTS5 with external-content triggers | Works on old SQLite versions shipped with Windows/macOS Python |
| Extraction saved as JSON | Re-chunking and model switches never re-run extraction (R8) |
| Model lock in `meta` | Vectors from two embedding models must never be compared (R9) |
| Q&A entries as JSON files | User-created content survives deleting and rebuilding the database |
| Hybrid via Reciprocal Rank Fusion | Robust without tuning score scales; weights adjustable and measured by `rag eval` |

---

## 3. Security

### 3.1 Scope and trust model

knowledge-mcp is a **single-user, local** tool. Multi-user access control is out of scope.

| Trusted | Not trusted |
|---|---|
| The user running the process | **Document contents** (may contain anything, including text written to manipulate an AI) |
| The config file and the environment variables | **Requests from the MCP client / model** (arguments are validated; writes need human confirmation) |
| The configured provider endpoint (it receives your text) | **Any other network host** |
| The Python packages installed in the virtual environment | Responses that try to redirect traffic elsewhere |

The central guarantee is: **document content, queries and the API key are only ever sent to
the configured endpoint (`provider.base_url`)**.

### 3.2 Network egress: only the configured endpoint

#### Where outbound traffic can originate

The security review checked every module for network code (HTTP clients, sockets, URL
fetching, subprocesses, dynamic code execution). Result:

| Component | Outbound network activity |
|---|---|
| `rag_mcp/provider.py` | **Yes**: embeddings, chat, rerank. The only outbound code in the project |
| `extract.py` (pypdf, python-docx, stdlib HTML parser) | None. The HTML parser only reads text; it never fetches images, scripts or links |
| `store.py` (SQLite), `search.py` (numpy) | None |
| `server.py` (MCP SDK) | Inbound only (see §3.3). OpenTelemetry hooks in the SDK are inert: only `opentelemetry-api` is installed, no SDK or exporter |
| `cli.py`, `qa.py`, `ingest.py`, `evaluate.py`, `config.py` | None (they call `provider.py` indirectly) |
| No `subprocess`, `os.system`, `eval`, `exec`, `pickle` or YAML loading in `rag_mcp/` | |

#### How the guarantee is enforced: the egress guard

All provider traffic goes through one `httpx.Client` created in `OpenAICompatibleProvider`.
That client has a **request hook** (`egress_guard` in `provider.py`):

```python
self._http = httpx.Client(
    verify=verify, timeout=cfg.timeout_s, event_hooks={"request": [egress_guard(cfg.base_url)]}
)
```

- **Before every request:** the hook compares the request's origin (scheme, host, port) with the configured `base_url`.
- **On a mismatch:** it raises `ProviderError`, so nothing is sent, and logs an error naming the blocked destination.
- **Coverage:** httpx runs request hooks for **every** request it sends, including each hop of a redirect. The OpenAI SDK and the rerank call both use this client.

This closes the following paths:

| Attack / mistake | Without the guard | With the guard |
|---|---|---|
| Endpoint answers `307/308` redirect to another host | The OpenAI SDK follows redirects and **re-sends the request body (your document text)** to the new host | Blocked before sending |
| Redirect to `http://` on the same host (TLS downgrade) | Followed | Blocked (scheme differs) |
| Redirect to another port on the same host | Followed | Blocked (port differs) |
| Look-alike host (`api.openai.com.evil.example`) | Followed | Blocked (exact host match) |
| `rerank.endpoint_path` set to a full URL | (never left base_url: it is appended to it) | Still stays on `base_url` |

Tests: `tests/test_security.py` (a second fake server plays "another host" and must receive
zero requests while the endpoint redirects embed, chat and rerank calls to it).

#### What the guarantee does *not* cover

- **Proxies:** if `HTTPS_PROXY`/`HTTP_PROXY` is set, httpx connects to that proxy, which opens
  a TLS tunnel to the endpoint. The proxy sees the destination host name, but the TLS
  encryption is end-to-end with the endpoint. Check proxy variables in environments where this
  matters. Clients pass only the variables you list in their `env`.
- **DNS:** resolving the endpoint's host name contacts your configured DNS resolver.
- **Installing packages** (`pip install`) contacts the package index. That's a setup step, not runtime.
- **Who you configure:** `provider.base_url` (or `RAG_BASE_URL`) *defines* the allowed
  endpoint. Whoever can edit the config or environment can change it (see §3.1).

### 3.3 Network ingress: who can reach the server

| Transport | Exposure |
|---|---|
| `stdio` (default) | No network port. Only the client process that started the server can talk to it |
| `streamable-http`, `sse` | Listens on `127.0.0.1:8765` by default. **No authentication** |

Protections for the HTTP transports:

1. **Loopback by default:** `server.host` defaults to `127.0.0.1`.
2. **Remote binding needs explicit opt-in:** `_is_loopback()` checks the address. Anything
   not loopback (including `0.0.0.0` and the empty string, which means "all interfaces") is
   refused unless `--allow-remote` / `server.allow_remote = true` is set.
3. **DNS-rebinding protection:** for localhost binds, the MCP SDK checks the `Host` and
   `Origin` headers. A malicious website open in your browser can't use DNS tricks to call
   the local server.

Exposing the server beyond localhost should only happen behind a reverse proxy that adds
authentication and TLS.

### 3.4 Data handling

| Data | Where it goes |
|---|---|
| Full chunk text (prefixed with title and heading) | Sent to the endpoint for embedding during `rag index` |
| Search queries | Sent to the endpoint for embedding (semantic/hybrid modes) |
| Top candidate passages (up to `rerank.candidates`, 900 chars each) | Sent to the endpoint only when reranking is used |
| Everything else (files, extractions, database, Q&A) | Stays on disk locally |

- **Only configured folders are read.** Scanning resolves every file and **skips symlinks
  that point outside the source folder**, so a stray link (for example to `~/.ssh`) can't pull
  unrelated files into the index and send them to the endpoint. Hidden files and folders
  (starting with `.`) are skipped too.
- **Development uses synthetic data only (R3).** `testdata/` is fictional.
- **Data at rest is not encrypted.** The database and extraction JSON contain the full text of
  your documents in plain form, protected only by file-system permissions (see §3.10).

### 3.5 Secrets

- **Where the API key comes from:** it is read from the environment variable named in
  `provider.api_key_env` (default `OPENAI_API_KEY`). It is never written to the config file,
  the database, logs or error messages.
- **Where it is sent:** only in the `Authorization` header to the configured endpoint, which
  the egress guard enforces. On a cross-origin redirect it would also be stripped by httpx,
  but the guard blocks such a request first.
- **In the repository:** `config.toml` contains only the *name* of the variable. `data/` and
  evaluation runs are excluded by `.gitignore`.
- **In MCP clients:** prefer client features that prompt for the key or read it from a
  secret store (for example VS Code `inputs` with `"password": true`) over pasting it into a
  JSON config.
- **TLS:** certificate verification is on by default. Use `provider.ca_bundle` for a corporate
  CA. Setting `verify_ssl = false` logs a warning at startup and should never be used in
  production.

### 3.6 Input validation and injection

| Input | Protection |
|---|---|
| Tool arguments from the model | JSON schema from type hints; the SDK rejects wrong types, unknown enum values and out-of-range numbers (`top_k ≤ max_top_k`, `window ≤ 5`) before any code runs |
| SQL | All queries use parameters (`?`). The only interpolated SQL fragments are fixed column lists and placeholder strings built in code |
| FTS5 query syntax | Query text is reduced to `\w+` tokens, each quoted, so FTS operators in user input have no effect |
| File paths | Paths come from scanning configured folders, never from the model. Extraction JSON paths are built from the source name (validated `[a-z][a-z0-9_]*`) and the scanned relative path |
| Config | Unknown keys and invalid values are rejected; source names are validated and `qa` is reserved |
| Documents | Parsed as data only; no macros, scripts or external references are executed or fetched |

**Prompt injection (indirect).** Search results contain document text, which the model reads.
A document could contain instructions aimed at the model ("ignore previous instructions…").
The server limits the damage:
- every tool is read-only except the Q&A tools
- Q&A writes need human confirmation (§3.7)
- no tool can make network requests, run commands or read arbitrary files

Treat answers based on untrusted documents with appropriate care.

### 3.7 Human confirmation for writes

The only state the model can change is the Q&A base (R10). `qa.confirmation` controls how
the user confirms:

| Mode | Enforcement |
|---|---|
| `auto` (default) | If the client supports MCP **elicitation**, the server shows the user a confirmation dialog itself. Both `propose_qa` and a direct `save_qa` call trigger it, so the model can't skip the step. Otherwise the client's tool-approval prompt for `save_qa` is the confirmation. |
| `elicitation` | Only elicitation; without client support, only `rag qa approve <id>` in a terminal |
| `tool_approval` | The client's approval of `save_qa`; don't auto-approve that tool |
| `cli` | Only `rag qa approve <id>`; `save_qa` is not registered at all |

Each entry records how it was confirmed (`confirmed_via`). Pending proposals are not
searchable.

### 3.8 Security review: findings and fixes

A full review of the codebase was performed on 2026-10-23. (The `/security-review` command
reviews pending branch changes; since everything was already merged, the whole codebase was
reviewed instead.) Findings:

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | The OpenAI SDK follows HTTP redirects by default; a `307/308` from the endpoint (or a misconfigured gateway) would re-send document text to another host | **High** for the "only the endpoint" requirement | **Fixed**: egress guard on every request (§3.2); tests added |
| 2 | `_is_loopback("")` returned `True`, but an empty host binds to all interfaces, so `host = ""` in the config exposed the unauthenticated HTTP server without `--allow-remote` (and disabled DNS-rebinding protection) | **Medium** | **Fixed**: empty host is not loopback; test added |
| 3 | Symlinks inside a source folder pointing elsewhere (for example `~/.ssh`) were indexed, and their content sent to the endpoint | **Medium** | **Fixed**: files resolving outside the source folder are skipped with a warning; test added |
| 4 | `verify_ssl = false` disabled TLS verification silently | Low | **Fixed**: a warning is logged |
| 5 | HTTP transports have no authentication | Accepted (design) | Loopback-only by default, explicit opt-in for remote, DNS-rebinding protection; documented |
| 6 | Data at rest is unencrypted | Accepted (design) | Documented; rely on disk encryption and file permissions (§3.10) |
| 7 | Indirect prompt injection through document text | Accepted (inherent to RAG) | Read-only tools, confirmed writes, no network/command tools (§3.6) |

### 3.9 Verifying the egress guarantee yourself

**1. Automated tests** (offline; a second local server must receive nothing):

```bash
pytest -q tests/test_security.py
```

**2. Trace a real run.** A tiny `sitecustomize.py` logs every DNS lookup, socket connection
and HTTP request of the process and its subprocesses:

```python
# save as /tmp/egress/sitecustomize.py
import os, socket
LOG = os.environ["EGRESS_LOG"]
def w(*a): open(LOG, "a").write(" ".join(map(str, a)) + "\n")
_c, _g = socket.socket.connect, socket.getaddrinfo
socket.socket.connect = lambda s, a: (w("connect", a[:2]) if s.family in (2, 10) else None, _c(s, a))[1]
socket.getaddrinfo = lambda h, *a, **k: (w("dns", h), _g(h, *a, **k))[1]
import httpx
_s = httpx.Client.send
httpx.Client.send = lambda self, r, *a, **k: (w("http", r.method, r.url.host), _s(self, r, *a, **k))[1]
```

```bash
EGRESS_LOG=/tmp/egress/log PYTHONPATH=/tmp/egress:. sh -c '
  rag check --chat && rag search reports "battery" --mode semantic && rag search reports "anchors" --rerank
  python examples/demo_client.py'
sort /tmp/egress/log | uniq -c
```

**Result of this check during the review** (live run against OpenAI from the cloud
development environment, 4 processes including the MCP server subprocess):

```
9  http POST api.openai.com            ← every HTTP request went to the configured endpoint
9  connect ('127.0.0.1', <port>)       ← the environment's mandatory local HTTPS proxy
9  dns 127.0.0.1                       ← lookup of that proxy address
```

No other host or address was contacted. On a machine without a proxy, the `connect` lines
show the endpoint's IP address instead.

**3. At the operating-system level**, you can watch connections independently of Python:
- Linux: `strace -f -e trace=connect rag search …`
- macOS: Little Snitch / LuLu
- Windows: Resource Monitor → Network

For hard enforcement, add a host firewall rule that only allows the endpoint's address for the
Python executable of the virtual environment.

### 3.10 Residual risks and recommendations

| Risk | Recommendation |
|---|---|
| Config or environment changed by someone else redirects the "allowed" endpoint | Protect `config.toml` and the client config files with file permissions; review `RAG_BASE_URL` / proxy variables in shared environments |
| Plain-text data at rest (`data/`) | Keep `data/` on an encrypted disk; restrict permissions (`chmod 700 data`); include `data/qa/` in backups |
| HTTP transport exposed beyond localhost | Prefer stdio; if remote access is needed, use a reverse proxy with authentication and TLS |
| Untrusted documents | Index only sources you trust; review answers that act on retrieved instructions |
| Confidential data and an external provider | Only use confidential sources once `base_url` points to the approved internal endpoint (R3) |
| Malformed or huge PDFs (parser resource use) | Index only expected file types and folders; `rag index` reports and skips files that fail to extract |
| Dependency vulnerabilities | Pin versions for production and scan them regularly (for example `pip-audit`) |
