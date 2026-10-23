"""Run the demo commands for real on a fresh copy of the project and record their output."""

import json
import os
import shutil
import subprocess
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
HERE = Path(os.environ.get("VIDEO_WORKDIR", "/tmp/rag-demo-video"))
HERE.mkdir(parents=True, exist_ok=True)
WS = HERE / "knowledge-mcp"  # fresh demo workspace

if WS.exists():
    shutil.rmtree(WS)
WS.mkdir()
for name in ("testdata", "eval", "examples"):
    shutil.copytree(PROJECT / name, WS / name, ignore=shutil.ignore_patterns("runs", "__pycache__"))
shutil.copy(PROJECT / "config.toml", WS / "config.toml")
shutil.copy(PROJECT / "README.md", WS / "README.md")

ENV = {**os.environ, "OPENAI_API_KEY": "injected-by-environment-secret", "PYTHONPATH": str(PROJECT),
       "RAG_CONFIG": str(WS / "config.toml"), "COLUMNS": "110"}

scenes = []


def title(heading, sub="", bullets=None, req=""):
    scenes.append({"type": "title", "heading": heading, "sub": sub, "bullets": bullets or [], "req": req})


def cmd(caption, command, req="", shown=None, max_lines=34, highlight=None, stdin=None, cut=None, cwd=WS):
    r = subprocess.run(command, shell=True, cwd=cwd, env=ENV, capture_output=True, text=True, input=stdin)
    out = (r.stdout + r.stderr).rstrip("\n")
    out = "\n".join(l for l in out.splitlines() if "WARNING rag_mcp.provider" not in l and "notice]" not in l)
    out = out.replace(str(HERE) + "/", "~/")
    if cut:
        out = cut(out)
    lines = out.splitlines()
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + [f"… ({len(lines) - max_lines + 1} more lines)"]
    scenes.append({"type": "cmd", "caption": caption, "req": req, "cmd": shown or command,
                   "out": "\n".join(lines), "highlight": highlight or [], "exit": r.returncode})
    print(f"[{r.returncode}] {shown or command}  ({len(lines)} lines)")


def shorten_results(max_text_lines):
    """Keep each search hit's header + first N text lines, so a frame stays readable."""
    def f(out):
        res, n = [], None
        for line in out.splitlines():
            if line.startswith("[") and "chunk_id" in line:
                n = 0
                res.append(line)
                continue
            if n is not None and line.startswith("Cite as:"):
                res.append(line)
                continue
            if n is not None and line and not line.startswith(("More results", "No further")):
                n += 1
                if n == max_text_lines + 1:
                    res.append("    …")
                if n > max_text_lines:
                    continue
            res.append(line)
        return "\n".join(res)
    return f


title("knowledge-mcp", "A local RAG knowledge base for Cline, served over MCP",
      ["Python only · one SQLite file · no Docker",
       "Only embeddings / optional reranking call the model provider",
       "Every answer comes with a citation"])

title("Architecture", "Everything runs on the developer machine except model inference",
      ["files  →  extraction JSON  →  chunks  →  embeddings  →  SQLite (FTS5 + vectors)",
       "Cline  ⇄  rag-mcp (stdio)  →  search_<source> tools with citations",
       "Provider: OpenAI today, internal OpenAI-compatible endpoint tomorrow (config only)"],
      req="R1 · R2")

cmd("The provider is configured in one place: config.toml [provider]",
    "sed -n '/^\\[provider\\]/,/^ca_bundle/p' config.toml", req="R2",
    shown="sed -n '/\\[provider\\]/,/ca_bundle/p' config.toml", highlight=["base_url", "embedding_model", "chat_model"])

cmd("Connectivity check against the real OpenAI API", "rag check --chat", req="R2",
    highlight=["OK"])

cmd("Three test sources with fictional, non-confidential data", "find testdata -type f -not -name '*.py' | sort",
    req="R3", highlight=["\\.pdf", "\\.docx", "\\.html"])

cmd("First manual index: extract → chunk → embed", "rag -v index 2>&1 | grep -vE 'httpx|^embedded [0-9]+/'",
    shown="rag -v index", req="R11", highlight=["scanned=", "embedded chunks"], max_lines=40)

cmd("Extraction output is saved as one JSON per document",
    "find data/extracted -name '*.json' | sort | head -8 && echo && "
    "python3 -c \"import json;d=json.load(open('data/extracted/reports/kelp3-field-trial-2025.pdf.json'));"
    "print(json.dumps({k:d[k] for k in ('source','path','doc_type','title')},indent=1));"
    "print(' blocks:', len(d['blocks']), 'pages ->', [b['page'] for b in d['blocks']])\"",
    shown="find data/extracted -name '*.json' && cat …/kelp3-field-trial-2025.pdf.json", req="R8",
    highlight=["blocks:"])

cmd("Running it again: unchanged files are skipped, nothing is re-embedded", "rag index", req="R11",
    highlight=["unchanged=6", "unchanged=2", "embedded chunks: 0"])

cmd("Index status and the embedding model lock", "rag status", req="R9", highlight=["model lock"])

cmd("Cline sees one search tool per source (MCP over stdio)", "python examples/mcp_tour.py tools", req="R4",
    highlight=["search_"])

cmd("Hybrid search (default) via MCP — every hit carries a citation",
    "python examples/mcp_tour.py call search_handbook "
    "'{\"query\": \"I got paged three times last night, can I start late?\", \"top_k\": 2}'",
    req="R5 · R7", highlight=["Cite as"], cut=shorten_results(3))

cmd("Exact mode for error codes and identifiers",
    "rag search api_docs 'TW-403-BLOCKED' --mode exact", req="R5", highlight=["TW-403-BLOCKED"],
    cut=shorten_results(6))

cmd("Semantic mode: paraphrased question, PDF citation with page number",
    "rag search reports 'how long will the battery last?' --mode semantic --top-k 2", req="R5 · R7",
    highlight=["Cite as", "seven years"], cut=shorten_results(4))

cmd("The model controls top_k and pages with offset",
    "rag search handbook 'security' --mode keyword --top-k 2 --offset 2", req="R6",
    highlight=["results 3-4", "offset="], cut=shorten_results(2))

cmd("Optional reranking (chat model scores the candidates)",
    "rag search reports 'what went wrong with the anchors during the trial' --rerank --top-k 2",
    req="R5", highlight=["rerank", "Cite as"], cut=shorten_results(3))

cmd("Q&A base: the assistant can only propose an entry…",
    "python examples/mcp_tour.py call propose_qa '{\"question\": \"How long does a rollback take?\", "
    "\"answer\": \"Under five minutes; previous images stay warm for 72 hours.\", "
    "\"citations\": [\"handbook: deployment.md, Deployment Process > Rollback\"]}'",
    req="R10", highlight=["NOT saved yet"])

cmd("…pending entries are not searchable", "rag search qa 'rollback duration'", req="R10",
    highlight=["no results"])

cmd("…until the user confirms", "rag qa approve 1", stdin="y\n", req="R10", highlight=["Saved Q&A"],
    cut=lambda o: o.replace("[y/N] ", "[y/N] y\n"))

cmd("Confirmed answers become searchable with their sources", "rag search qa 'how fast is a rollback' --top-k 1",
    req="R10", highlight=["Cite as", "Sources:"])

cmd("Model lock: a second embedding model is refused",
    "RAG_EMBEDDING_MODEL=text-embedding-3-large rag index", req="R9", highlight=["locked"])

cmd("Measured evaluation decides tuning (40 labelled questions)",
    "rag eval --modes hybrid,semantic,keyword --label demo", req="R12",
    highlight=["hybrid ", "semantic ", "keyword "], max_lines=20,
    cut=lambda o: "\n".join(l for l in o.splitlines() if "misses" not in l))

cmd("39 automated tests, fully offline (fake OpenAI-compatible endpoint)",
    "python3 -m pytest -q 2>&1 | tail -3", shown="pytest -q", cwd=PROJECT, req="all", highlight=["passed"])

title("Requirements covered",
      "",
      ["R1 local SQLite + stdio   ·   R2 one OpenAI-compatible provider   ·   R3 synthetic test data",
       "R4 one tool per source   ·   R5 hybrid/semantic/keyword/exact + rerank   ·   R6 top_k + paging",
       "R7 citations   ·   R8 extraction JSON   ·   R9 model lock   ·   R10 confirmed Q&A",
       "R11 manual incremental re-index   ·   R12 measured evaluation"],
      req="")

(HERE / "scenes.json").write_text(json.dumps(scenes, indent=1, ensure_ascii=False))
print("scenes:", len(scenes))
