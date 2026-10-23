"""Retrieval evaluation (R12): measured numbers decide tuning, not impressions.

Questions live in a JSONL file, one object per line:

    {"id": "h1", "source": "handbook", "question": "How do I roll back a release?",
     "expected": [{"document": "deployment.md", "contains": "rollback"}]}

A result counts as relevant when its document matches and, if given, its text
contains the `contains` snippet (case- and whitespace-insensitive). Metrics per
configuration: hit@1, hit@k (recall), MRR@k and mean latency.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .app import App


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def load_questions(path: Path) -> list[dict]:
    items = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        q = json.loads(line)
        for key in ("id", "source", "question", "expected"):
            if key not in q:
                raise ValueError(f"{path}:{n}: missing '{key}'")
        items.append(q)
    return items


def is_relevant(result: dict, expected: list[dict]) -> bool:
    doc = result["citation"]["document"]
    for e in expected:
        if e["document"] != doc and not doc.endswith("/" + e["document"]):
            continue
        if "contains" in e and _norm(e["contains"]) not in _norm(result["text"]):
            continue
        return True
    return False


def run_eval(app: App, questions: list[dict], configs: list[dict], top_k: int) -> dict:
    report = {"configs": []}
    for conf in configs:
        rows = []
        for q in questions:
            t0 = time.perf_counter()
            res = app.search.search(q["source"], q["question"], mode=conf["mode"], top_k=top_k, rerank=conf["rerank"])
            ms = (time.perf_counter() - t0) * 1000
            rank = next((r["rank"] for r in res["results"] if is_relevant(r, q["expected"])), None)
            rows.append({"id": q["id"], "source": q["source"], "rank": rank, "ms": round(ms, 1),
                         "top_doc": res["results"][0]["citation"]["document"] if res["results"] else None,
                         "warnings": res["warnings"]})
        n = len(rows) or 1
        metrics = {
            "hit@1": sum(1 for r in rows if r["rank"] == 1) / n,
            f"hit@{top_k}": sum(1 for r in rows if r["rank"]) / n,
            f"mrr@{top_k}": sum(1 / r["rank"] for r in rows if r["rank"]) / n,
            "mean_ms": sum(r["ms"] for r in rows) / n,
        }
        per_source = {}
        for src in sorted({r["source"] for r in rows}):
            sub = [r for r in rows if r["source"] == src]
            per_source[src] = {"n": len(sub), f"hit@{top_k}": sum(1 for r in sub if r["rank"]) / len(sub)}
        report["configs"].append({**conf, "metrics": metrics, "per_source": per_source, "questions": rows})
    return report


def save_report(app: App, report: dict, questions_file: Path, top_k: int, label: str | None) -> Path:
    cfg = app.cfg
    report["meta"] = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "label": label,
        "questions_file": str(questions_file),
        "top_k": top_k,
        "embedding_model": cfg.provider.embedding_model,
        "chat_model": cfg.provider.chat_model,
        "base_url": cfg.provider.base_url,
        "chunking": asdict(cfg.chunking),
        "search": asdict(cfg.search),
        "rerank": asdict(cfg.rerank),
    }
    out_dir = cfg.eval_dir / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"{stamp}{'-' + re.sub(r'[^A-Za-z0-9_-]', '_', label) if label else ''}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return out


def format_report(report: dict, top_k: int, baseline: dict | None = None) -> str:
    keys = ["hit@1", f"hit@{top_k}", f"mrr@{top_k}", "mean_ms"]
    base = {}
    if baseline:
        for c in baseline.get("configs", []):
            base[(c["mode"], c["rerank"])] = c["metrics"]
    lines = [f"{'config':<22}" + "".join(f"{k:>18}" for k in keys)]
    for c in report["configs"]:
        name = c["mode"] + (" +rerank" if c["rerank"] else "")
        cells = []
        for k in keys:
            v = c["metrics"].get(k, 0.0)
            cell = f"{v:.1f}" if k == "mean_ms" else f"{v:.3f}"
            b = base.get((c["mode"], c["rerank"]), {}).get(k)
            if b is not None and k != "mean_ms":
                cell += f" ({v - b:+.3f})"
            cells.append(f"{cell:>18}")
        lines.append(f"{name:<22}" + "".join(cells))
    lines.append("")
    for c in report["configs"]:
        misses = [q["id"] for q in c["questions"] if not q["rank"]]
        name = c["mode"] + (" +rerank" if c["rerank"] else "")
        per = ", ".join(f"{s}={m[f'hit@{top_k}']:.2f}" for s, m in c["per_source"].items())
        lines.append(f"{name}: per source hit@{top_k}: {per}")
        if misses:
            shown = ", ".join(misses[:12]) + (f" … (+{len(misses) - 12} more)" if len(misses) > 12 else "")
            lines.append(f"    misses ({len(misses)}): {shown}")
    return "\n".join(lines)
