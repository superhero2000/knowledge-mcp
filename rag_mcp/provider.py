"""The single model-provider interface (R2).

All inference (embeddings, chat for LLM reranking, optional /rerank endpoint) goes
through `OpenAICompatibleProvider`. Pointing it at OpenAI or at the internal
OpenAI-compatible endpoint is purely a matter of configuration.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, Sequence

import httpx
import numpy as np

from .config import ProviderConfig

log = logging.getLogger(__name__)

# Rough guard against inputs above the embedding model's context window.
MAX_EMBED_INPUT_CHARS = 24000


class ProviderError(Exception):
    pass


class Provider(Protocol):
    """What the rest of the code relies on. Tests supply a fake implementation."""

    embedding_model: str

    def embed(self, texts: Sequence[str]) -> np.ndarray: ...

    def chat(self, messages: list[dict], *, json_mode: bool = False, model: str | None = None) -> str: ...

    def rerank(self, query: str, documents: Sequence[str], *, model: str, path: str) -> list[float]: ...


def normalize(vectors: np.ndarray) -> np.ndarray:
    vectors = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


class OpenAICompatibleProvider:
    def __init__(self, cfg: ProviderConfig):
        from openai import OpenAI

        self.cfg = cfg
        self.embedding_model = cfg.embedding_model
        api_key = os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else ""
        if not api_key:
            log.warning(
                "Environment variable %s is not set; sending requests without a real API key",
                cfg.api_key_env,
            )
            api_key = "not-set"
        self._api_key = api_key
        verify: bool | str = cfg.ca_bundle if cfg.ca_bundle else cfg.verify_ssl
        self._http = httpx.Client(verify=verify, timeout=cfg.timeout_s)
        self.client = OpenAI(
            base_url=cfg.base_url,
            api_key=api_key,
            timeout=cfg.timeout_s,
            max_retries=cfg.max_retries,
            http_client=self._http,
            default_headers=cfg.extra_headers or None,
        )

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        out: list[list[float]] = []
        bs = max(1, self.cfg.embed_batch_size)
        for i in range(0, len(texts), bs):
            batch = [(t or " ")[:MAX_EMBED_INPUT_CHARS] for t in texts[i : i + bs]]
            kwargs = {"model": self.cfg.embedding_model, "input": batch}
            if self.cfg.embedding_dimensions:
                kwargs["dimensions"] = self.cfg.embedding_dimensions
            try:
                resp = self.client.embeddings.create(**kwargs)
            except Exception as e:  # surface a readable message to CLI / MCP
                raise ProviderError(f"Embedding request to {self.cfg.base_url} failed: {e}") from e
            data = sorted(resp.data, key=lambda d: d.index)
            out.extend(d.embedding for d in data)
        return normalize(np.array(out, dtype=np.float32))

    def chat(self, messages: list[dict], *, json_mode: bool = False, model: str | None = None) -> str:
        kwargs = {"model": model or self.cfg.chat_model, "messages": messages, "temperature": 0}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = self.client.chat.completions.create(**kwargs)
        except Exception as e:
            if json_mode:  # some compatible servers lack response_format; retry without it
                kwargs.pop("response_format", None)
                try:
                    resp = self.client.chat.completions.create(**kwargs)
                except Exception as e2:
                    raise ProviderError(f"Chat request failed: {e2}") from e2
            else:
                raise ProviderError(f"Chat request failed: {e}") from e
        return resp.choices[0].message.content or ""

    def rerank(self, query: str, documents: Sequence[str], *, model: str, path: str) -> list[float]:
        """Cohere/Jina/vLLM/TEI-style rerank endpoint: POST {base_url}{path}."""
        url = self.cfg.base_url.rstrip("/") + "/" + path.lstrip("/")
        headers = {"Authorization": f"Bearer {self._api_key}", **(self.cfg.extra_headers or {})}
        payload = {"model": model, "query": query, "documents": list(documents), "top_n": len(documents)}
        try:
            r = self._http.post(url, json=payload, headers=headers)
            r.raise_for_status()
            results = r.json().get("results", [])
        except Exception as e:
            raise ProviderError(f"Rerank request to {url} failed: {e}") from e
        scores = [0.0] * len(documents)
        for item in results:
            scores[item["index"]] = float(item.get("relevance_score", item.get("score", 0.0)))
        return scores


def make_provider(cfg: ProviderConfig) -> Provider:
    return OpenAICompatibleProvider(cfg)
