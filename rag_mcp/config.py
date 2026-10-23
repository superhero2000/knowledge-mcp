"""Configuration loading.

Everything that differs between development (OpenAI API) and production (internal
OpenAI-compatible endpoint) lives in the TOML config file, never in code (R2).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

QA_SOURCE = "qa"
SEARCH_MODES = ("hybrid", "semantic", "keyword", "exact")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


class ConfigError(Exception):
    pass


@dataclass
class ProviderConfig:
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 0  # 0 = model default
    chat_model: str = "gpt-4o-mini"
    embed_batch_size: int = 64
    timeout_s: float = 60.0
    max_retries: int = 3
    verify_ssl: bool = True
    ca_bundle: str = ""  # path to a corporate CA bundle for the internal endpoint
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ChunkingConfig:
    max_chars: int = 1500
    overlap_chars: int = 200
    min_chars: int = 200


@dataclass
class SearchConfig:
    default_mode: str = "hybrid"
    default_top_k: int = 5
    max_top_k: int = 50
    candidate_pool: int = 100
    rrf_k: int = 60
    semantic_weight: float = 1.0
    keyword_weight: float = 1.0
    max_snippet_chars: int = 2000


@dataclass
class RerankConfig:
    enabled: bool = False  # default when the model does not ask explicitly
    method: str = "llm"  # "llm" (chat model scores passages) or "endpoint" (/rerank API)
    model: str = ""  # endpoint method: reranker model name; llm method: defaults to chat_model
    endpoint_path: str = "/rerank"
    candidates: int = 30


QA_CONFIRMATION_MODES = ("auto", "elicitation", "tool_approval", "cli")


@dataclass
class QAConfig:
    # How a proposed Q&A pair gets the user's confirmation (R10):
    #   auto          - ask via MCP elicitation if the client supports it, else rely on tool approval
    #   elicitation   - only via MCP elicitation; clients without it must use the CLI
    #   tool_approval - the client's tool-call approval of save_qa is the confirmation
    #   cli           - only `rag qa approve <id>` in a terminal
    confirmation: str = "auto"
    cli_confirmation_only: bool = False  # deprecated alias for confirmation = "cli"


TRANSPORTS = ("stdio", "streamable-http", "sse")


@dataclass
class ServerConfig:
    transport: str = "stdio"  # stdio | streamable-http | sse
    host: str = "127.0.0.1"
    port: int = 8765
    allow_remote: bool = False  # HTTP has no authentication: binding beyond localhost must be explicit


@dataclass
class SourceConfig:
    name: str
    description: str
    path: Path
    include: list[str] = field(default_factory=lambda: ["**/*"])
    exclude: list[str] = field(default_factory=list)


@dataclass
class Config:
    config_path: Path
    db_path: Path
    extract_dir: Path
    qa_dir: Path
    eval_dir: Path
    provider: ProviderConfig
    chunking: ChunkingConfig
    search: SearchConfig
    rerank: RerankConfig
    qa: QAConfig
    server: ServerConfig
    sources: list[SourceConfig]

    def source(self, name: str) -> SourceConfig:
        for s in self.sources:
            if s.name == name:
                return s
        raise ConfigError(f"Unknown source '{name}'. Known: {', '.join(self.source_names())}")

    def source_names(self) -> list[str]:
        return [s.name for s in self.sources]

    def all_search_sources(self) -> list[str]:
        return self.source_names() + [QA_SOURCE]


def _build(cls, data: dict | None, section: str):
    data = dict(data or {})
    known = set(cls.__dataclass_fields__)
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"Unknown keys in [{section}]: {', '.join(sorted(unknown))}")
    return cls(**data)


APP_NAME = "knowledge-mcp"


def user_config_dir() -> Path:
    """Per-user config folder (MCP clients start servers from arbitrary working directories)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_NAME


def config_candidates() -> list[Path]:
    """Lookup order when no --config is given."""
    out = []
    for env in ("KNOWLEDGE_MCP_CONFIG", "RAG_CONFIG"):
        if os.environ.get(env):
            return [Path(os.path.expanduser(os.environ[env]))]
    out.append(Path.cwd() / "config.toml")
    out.append(user_config_dir() / "config.toml")
    return out


def default_config_path() -> Path:
    candidates = config_candidates()
    for p in candidates:
        if p.is_file():
            return p
    return candidates[0]


def load_config(path: str | os.PathLike | None = None) -> Config:
    if path:
        cfg_path = Path(os.path.expanduser(str(path)))
        searched = [cfg_path]
    else:
        searched = config_candidates()
        cfg_path = next((p for p in searched if p.is_file()), searched[0])
    if not cfg_path.is_file():
        raise ConfigError(
            "Config file not found. Looked in: " + ", ".join(str(p) for p in searched)
            + ". Pass --config, set KNOWLEDGE_MCP_CONFIG, or create one with `rag init`."
        )
    cfg_path = cfg_path.resolve()
    base = cfg_path.parent
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)

    def rel(p: str) -> Path:
        q = Path(os.path.expanduser(p))
        return q if q.is_absolute() else (base / q).resolve()

    storage = raw.get("storage", {})
    provider = _build(ProviderConfig, raw.get("provider"), "provider")
    # Optional environment overrides, handy for switching endpoints without editing the file.
    for env, attr in (
        ("RAG_BASE_URL", "base_url"),
        ("RAG_API_KEY_ENV", "api_key_env"),
        ("RAG_EMBEDDING_MODEL", "embedding_model"),
        ("RAG_CHAT_MODEL", "chat_model"),
    ):
        if os.environ.get(env):
            setattr(provider, attr, os.environ[env])

    search = _build(SearchConfig, raw.get("search"), "search")
    if search.default_mode not in SEARCH_MODES:
        raise ConfigError(f"search.default_mode must be one of {SEARCH_MODES}")
    qa = _build(QAConfig, raw.get("qa"), "qa")
    if qa.cli_confirmation_only:
        qa.confirmation = "cli"
    if qa.confirmation not in QA_CONFIRMATION_MODES:
        raise ConfigError(f"qa.confirmation must be one of {QA_CONFIRMATION_MODES}")
    server = _build(ServerConfig, raw.get("server"), "server")
    if server.transport not in TRANSPORTS:
        raise ConfigError(f"server.transport must be one of {TRANSPORTS}")
    rerank = _build(RerankConfig, raw.get("rerank"), "rerank")
    if rerank.method not in ("llm", "endpoint"):
        raise ConfigError("rerank.method must be 'llm' or 'endpoint'")

    sources: list[SourceConfig] = []
    for s in raw.get("sources", []):
        name = s.get("name", "")
        if not _NAME_RE.match(name):
            raise ConfigError(f"Invalid source name '{name}': use lowercase letters, digits, underscore")
        if name == QA_SOURCE:
            raise ConfigError(f"Source name '{QA_SOURCE}' is reserved for the Q&A base")
        if name in {x.name for x in sources}:
            raise ConfigError(f"Duplicate source name '{name}'")
        if "path" not in s:
            raise ConfigError(f"Source '{name}' needs a path")
        sources.append(
            SourceConfig(
                name=name,
                description=s.get("description", f"Documents from {name}"),
                path=rel(s["path"]),
                include=list(s.get("include", ["**/*"])),
                exclude=list(s.get("exclude", [])),
            )
        )

    return Config(
        config_path=cfg_path,
        db_path=rel(storage.get("db_path", "data/rag.sqlite")),
        extract_dir=rel(storage.get("extract_dir", "data/extracted")),
        qa_dir=rel(storage.get("qa_dir", "data/qa")),
        eval_dir=rel(storage.get("eval_dir", "eval")),
        provider=provider,
        chunking=_build(ChunkingConfig, raw.get("chunking"), "chunking"),
        search=search,
        rerank=rerank,
        qa=qa,
        server=server,
        sources=sources,
    )
