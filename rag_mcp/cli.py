"""Command-line interface: `rag index | status | search | reembed | qa | eval | check | serve`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .app import App
from .config import QA_SOURCE, SEARCH_MODES, ConfigError, load_config
from .provider import ProviderError
from .qa import QAError
from .search import SearchError, format_results
from .store import ModelLockError


def _apply_overrides(cfg, overrides: list[str]) -> None:
    """Apply `section.key=value` overrides (used for tuning experiments with `rag eval --set`)."""
    for item in overrides or []:
        if "=" not in item or "." not in item.split("=", 1)[0]:
            raise ValueError(f"--set expects section.key=value, got '{item}'")
        dotted, value = item.split("=", 1)
        section, key = dotted.split(".", 1)
        obj = getattr(cfg, section, None)
        if obj is None or not hasattr(obj, key):
            raise ValueError(f"Unknown setting '{dotted}'")
        current = getattr(obj, key)
        if isinstance(current, bool):
            cast = value.lower() in ("1", "true", "yes", "on")
        elif isinstance(current, (int, float)):
            cast = type(current)(value)
        else:
            cast = value
        setattr(obj, key, cast)


def _app(args) -> App:
    cfg = load_config(args.config)
    _apply_overrides(cfg, getattr(args, "set", None))
    return App.create(cfg, progress=(lambda m: print(m, file=sys.stderr)) if args.verbose else None)


def cmd_index(args) -> int:
    app = _app(args)
    report = app.indexer.index(
        args.source or None, force_extract=args.force_extract, rechunk=args.rechunk, prune=not args.no_prune
    )
    print(report.summary())
    return 1 if any(r.errors for r in report.sources.values()) else 0


def cmd_status(args) -> int:
    app = _app(args)
    stats = app.store.stats()
    lock = app.store.model_lock()
    print(f"config:          {app.cfg.config_path}")
    print(f"database:        {app.cfg.db_path}")
    print(f"extractions:     {app.cfg.extract_dir}")
    print(f"provider:        {app.cfg.provider.base_url}")
    print(f"embedding model: {app.cfg.provider.embedding_model} (configured)")
    if lock:
        state = "OK" if lock["model"] == app.cfg.provider.embedding_model else "MISMATCH -> run `rag reembed --switch-model`"
        print(f"model lock:      {lock['model']} dim={lock['dim']} since {lock['locked_at']}  [{state}]")
    else:
        print("model lock:      (none yet - set on first embedding)")
    print()
    print(f"{'source':<14}{'documents':>10}{'chunks':>10}{'unembedded':>12}  path")
    for src in app.cfg.sources:
        st = stats.get(src.name, {})
        print(f"{src.name:<14}{st.get('documents', 0):>10}{st.get('chunks', 0):>10}{st.get('unembedded', 0):>12}  {src.path}")
    st = stats.get(QA_SOURCE, {})
    print(f"{QA_SOURCE:<14}{st.get('documents', 0):>10}{st.get('chunks', 0):>10}{st.get('unembedded', 0):>12}  {app.cfg.qa_dir}")
    pending = app.qa.pending()
    if pending:
        print(f"\n{len(pending)} Q&A proposal(s) pending confirmation: `rag qa pending`")
    return 0


def cmd_search(args) -> int:
    app = _app(args)
    res = app.search.search(args.source, args.query, args.mode, args.top_k, args.offset, args.rerank, args.case_sensitive)
    print(json.dumps(res, indent=2, ensure_ascii=False) if args.json else format_results(res))
    return 0


def cmd_reembed(args) -> int:
    app = _app(args)
    if not args.switch_model:
        print("Refusing to rebuild vectors without --switch-model.", file=sys.stderr)
        return 2
    lock = app.store.model_lock()
    print(f"Re-embedding all chunks: {lock['model'] if lock else '(none)'} -> {app.cfg.provider.embedding_model}")
    n = app.indexer.reembed_all()
    print(f"embedded {n} chunks; index now locked to {app.cfg.provider.embedding_model}")
    return 0


def cmd_qa(args) -> int:
    app = _app(args)
    if args.qa_cmd == "pending":
        items = app.qa.pending()
        if not items:
            print("No pending proposals.")
        for p in items:
            print(f"#{p['id']}  ({p['created_at']})\n  Q: {p['question']}\n  A: {p['answer']}\n  Sources: {'; '.join(p['citations'])}\n")
    elif args.qa_cmd == "approve":
        p = app.qa.get_pending(args.id)
        if not args.yes:
            print(f"Q: {p['question']}\nA: {p['answer']}\nSources: {'; '.join(p['citations'])}")
            if input("Save this Q&A entry? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Not saved.")
                return 1
        entry = app.qa.approve(args.id, via="cli")
        print(f"Saved Q&A #{entry['id']} -> {entry['file']}")
    elif args.qa_cmd == "reject":
        app.qa.reject(args.id)
        print(f"Proposal {args.id} discarded.")
    elif args.qa_cmd == "list":
        for e in app.qa.entries():
            print(f"#{e['id']}  {e['question']}\n    {e['answer'][:200]}")
    elif args.qa_cmd == "delete":
        app.qa.delete(args.id)
        print(f"Deleted Q&A #{args.id}")
    return 0


def cmd_eval(args) -> int:
    from .evaluate import format_report, load_questions, run_eval, save_report

    app = _app(args)
    qfile = Path(args.questions) if args.questions else app.cfg.eval_dir / "questions.jsonl"
    questions = load_questions(qfile)
    configs = [{"mode": m, "rerank": False} for m in args.modes.split(",")]
    if args.rerank:
        configs += [{"mode": m, "rerank": True} for m in args.modes.split(",") if m != "exact"]
    report = run_eval(app, questions, configs, args.top_k)
    baseline = json.loads(Path(args.baseline).read_text()) if args.baseline else None
    out = save_report(app, report, qfile, args.top_k, args.label)
    print(f"{len(questions)} questions, top_k={args.top_k}, model={app.cfg.provider.embedding_model}\n")
    print(format_report(report, args.top_k, baseline))
    print(f"\nsaved: {out}")
    return 0


def cmd_check(args) -> int:
    app = _app(args)
    p = app.cfg.provider
    print(f"endpoint: {p.base_url}")
    v = app.provider.embed(["connectivity check"])
    print(f"embeddings OK: {p.embedding_model} -> {v.shape[1]} dimensions")
    try:
        app.store.check_model(p.embedding_model, int(v.shape[1]))
        print("model lock OK")
    except ModelLockError as e:
        print(f"model lock: {e}")
    if args.chat:
        out = app.provider.chat([{"role": "user", "content": "Reply with the single word: pong"}])
        print(f"chat OK: {p.chat_model} -> {out.strip()[:40]}")
    return 0


def cmd_serve(args) -> int:
    from .server import main as serve_main

    serve_main((["--config", args.config] if args.config else []) + list(args.serve_args or []))
    return 0


def cmd_init(args) -> int:
    from importlib.resources import files

    from .config import user_config_dir

    target = Path(args.path).expanduser() if args.path else user_config_dir() / "config.toml"
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(files("rag_mcp").joinpath("config_template.toml").read_text(encoding="utf-8"), encoding="utf-8")
    print(f"wrote {target}")
    print("Next: edit [[sources]] and [provider], set the API key variable, then run `rag index`.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rag", description="knowledge-mcp: local RAG knowledge base served over MCP")
    p.add_argument("--config", help="Path to config.toml (default: $KNOWLEDGE_MCP_CONFIG, ./config.toml, "
                                    "then the per-user config folder)")
    p.add_argument("-v", "--verbose", action="store_true", help="Show progress")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("index", help="Extract, chunk and embed sources; unchanged files are skipped")
    s.add_argument("--source", action="append", help="Only this source (repeatable)")
    s.add_argument("--rechunk", action="store_true", help="Re-chunk from saved extraction JSON (no re-extraction)")
    s.add_argument("--force-extract", action="store_true", help="Re-run extraction even for unchanged files")
    s.add_argument("--no-prune", action="store_true", help="Keep index entries for deleted files")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("status", help="Show index statistics and model lock")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("search", help="Search one source from the command line")
    s.add_argument("source")
    s.add_argument("query")
    s.add_argument("--mode", choices=SEARCH_MODES)
    s.add_argument("--top-k", type=int)
    s.add_argument("--offset", type=int, default=0)
    s.add_argument("--rerank", action="store_true", default=None)
    s.add_argument("--case-sensitive", action="store_true")
    s.add_argument("--json", action="store_true")
    s.add_argument("--set", action="append", metavar="SECTION.KEY=VALUE", help="Override a setting (repeatable)")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("reembed", help="Rebuild all vectors with the configured embedding model")
    s.add_argument("--switch-model", action="store_true", help="Confirm replacing the locked model")
    s.set_defaults(func=cmd_reembed)

    s = sub.add_parser("qa", help="Manage the confirmed Q&A base")
    qs = s.add_subparsers(dest="qa_cmd", required=True)
    qs.add_parser("pending", help="Show proposals awaiting confirmation")
    a = qs.add_parser("approve", help="Confirm and save a proposal")
    a.add_argument("id", type=int)
    a.add_argument("-y", "--yes", action="store_true", help="Skip the interactive prompt")
    r = qs.add_parser("reject", help="Discard a proposal")
    r.add_argument("id", type=int)
    qs.add_parser("list", help="List saved entries")
    d = qs.add_parser("delete", help="Delete a saved entry")
    d.add_argument("id", type=int)
    s.set_defaults(func=cmd_qa)

    s = sub.add_parser("eval", help="Run the retrieval evaluation set")
    s.add_argument("--questions", help="JSONL file (default: <eval_dir>/questions.jsonl)")
    s.add_argument("--modes", default="hybrid,semantic,keyword,exact")
    s.add_argument("--top-k", type=int, default=5)
    s.add_argument("--rerank", action="store_true", help="Also evaluate each mode with reranking")
    s.add_argument("--baseline", help="Previous run JSON to compare against")
    s.add_argument("--label", help="Name for the saved run")
    s.add_argument("--set", action="append", metavar="SECTION.KEY=VALUE",
                   help="Override a setting for this run, e.g. --set search.semantic_weight=2 (repeatable)")
    s.set_defaults(func=cmd_eval)

    s = sub.add_parser("check", help="Verify connectivity to the model provider")
    s.add_argument("--chat", action="store_true", help="Also test the chat model")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("serve", help="Run the MCP server (same as knowledge-mcp); extra args are passed through, "
                                     "e.g. rag serve --transport streamable-http --port 8765")
    s.add_argument("serve_args", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("init", help="Create a starter config.toml (default: the per-user config folder)")
    s.add_argument("--path", help="Where to write it instead")
    s.add_argument("--force", action="store_true", help="Overwrite an existing file")
    s.set_defaults(func=cmd_init)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        sys.exit(args.func(args))
    except (ConfigError, ModelLockError, ProviderError, SearchError, QAError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
