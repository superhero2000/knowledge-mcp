"""A tiny OpenAI-compatible HTTP endpoint for offline tests.

It stands in for "the internal endpoint": tests point `provider.base_url` at it,
proving that switching providers is configuration only (R2).

* POST /v1/embeddings       – deterministic hashed bag-of-words + trigram vectors;
                              the vector space depends on the model name
* POST /v1/chat/completions – answers LLM-rerank prompts with token-overlap scores
* POST /v1/rerank           – Cohere/Jina-style rerank by token overlap
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

DIMS = {"fake-embed-a": 256, "fake-embed-b": 256, "fake-embed-small": 64}
_STOP = {"the", "a", "an", "of", "to", "is", "in", "and", "do", "i", "how", "what", "for", "my", "on", "does"}


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"\w+", text.lower()) if t not in _STOP]


def fake_embedding(text: str, model: str) -> list[float]:
    dim = DIMS.get(model, 128)
    v = np.zeros(dim, dtype=np.float32)
    feats = []
    for t in _tokens(text):
        feats.append((t, 1.0))
        stem = t[:5]
        feats.append(("s:" + stem, 0.5))
    for feat, w in feats:
        h = hashlib.md5(f"{model}|{feat}".encode()).digest()
        idx = int.from_bytes(h[:4], "little") % dim
        sign = 1.0 if h[4] % 2 else -1.0
        v[idx] += sign * w
    n = np.linalg.norm(v)
    if n == 0:
        v[0] = 1.0
        n = 1.0
    return (v / n).tolist()


def overlap(query: str, doc: str) -> float:
    q = set(_tokens(query))
    d = set(_tokens(doc))
    return len(q & d) / (len(q) or 1)


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeOpenAI/1.0"

    def log_message(self, *args):  # silence
        pass

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length) or b"{}")
        stats = self.server.stats  # type: ignore[attr-defined]
        path = self.path.rstrip("/")
        if self.server.fail:  # type: ignore[attr-defined]
            return self._send(500, {"error": {"message": "simulated outage"}})
        if path.endswith("/embeddings"):
            inputs = req["input"] if isinstance(req["input"], list) else [req["input"]]
            stats["embedding_requests"] += 1
            stats["embedded_texts"] += len(inputs)
            data = [
                {"object": "embedding", "index": i, "embedding": fake_embedding(t, req["model"])}
                for i, t in enumerate(inputs)
            ]
            return self._send(200, {"object": "list", "data": data, "model": req["model"],
                                    "usage": {"prompt_tokens": 1, "total_tokens": 1}})
        if path.endswith("/chat/completions"):
            stats["chat_requests"] += 1
            content = req["messages"][-1]["content"]
            m = re.match(r"Query: (.*?)\n\nPassages:\n(.*)", content, re.S)
            if m:
                query, passages = m.group(1), m.group(2)
                parts = re.split(r"\n\n(?=\[\d+\] )", passages)
                scores = {str(i): round(10 * overlap(query, p), 2) for i, p in enumerate(parts)}
                answer = json.dumps({"scores": scores})
            else:
                answer = "pong"
            return self._send(200, {
                "id": "chatcmpl-fake", "object": "chat.completion", "created": 0, "model": req["model"],
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": answer}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })
        if path.endswith("/rerank"):
            stats["rerank_requests"] += 1
            results = [{"index": i, "relevance_score": overlap(req["query"], d)} for i, d in enumerate(req["documents"])]
            return self._send(200, {"results": sorted(results, key=lambda r: -r["relevance_score"])})
        return self._send(404, {"error": {"message": f"unknown path {self.path}"}})


class FakeOpenAI:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.stats = {"embedding_requests": 0, "embedded_texts": 0, "chat_requests": 0, "rerank_requests": 0}
        self.httpd.fail = False
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    @property
    def stats(self) -> dict:
        return self.httpd.stats

    def set_failing(self, fail: bool) -> None:
        self.httpd.fail = fail

    def reset_stats(self) -> None:
        for k in self.httpd.stats:
            self.httpd.stats[k] = 0

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
