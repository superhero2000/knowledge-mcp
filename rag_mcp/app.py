"""Wires config, storage, provider, indexer, search and Q&A together."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config, load_config
from .ingest import Indexer
from .provider import Provider, make_provider
from .qa import QABase
from .search import SearchEngine
from .store import Store


@dataclass
class App:
    cfg: Config
    store: Store
    provider: Provider
    indexer: Indexer
    search: SearchEngine
    qa: QABase

    @classmethod
    def create(cls, cfg: Config | None = None, provider: Provider | None = None, progress=None) -> "App":
        cfg = cfg or load_config()
        store = Store(cfg.db_path)
        provider = provider or make_provider(cfg.provider)
        indexer = Indexer(cfg, store, provider, progress=progress)
        return cls(
            cfg=cfg,
            store=store,
            provider=provider,
            indexer=indexer,
            search=SearchEngine(cfg, store, provider),
            qa=QABase(cfg, store, indexer),
        )

    def close(self) -> None:
        self.store.close()
