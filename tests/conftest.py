from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake_openai import FakeOpenAI  # noqa: E402

from rag_mcp.app import App  # noqa: E402
from rag_mcp.config import load_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TESTDATA = ROOT / "testdata"

CONFIG_TEMPLATE = """
[storage]
db_path = "data/rag.sqlite"
extract_dir = "data/extracted"
qa_dir = "data/qa"
eval_dir = "eval"

[provider]
base_url = "{base_url}"
api_key_env = "FAKE_KEY"
embedding_model = "{model}"
chat_model = "fake-chat"
max_retries = 0
timeout_s = 10

[chunking]
max_chars = 800
overlap_chars = 100
min_chars = 100

[search]
default_top_k = 3
max_top_k = 20

[rerank]
method = "{rerank_method}"
model = "fake-reranker"

[qa]
cli_confirmation_only = {cli_only}

[[sources]]
name = "handbook"
description = "Engineering handbook"
path = "docs/handbook"
include = ["**/*.md"]

[[sources]]
name = "api_docs"
description = "API documentation"
path = "docs/api_docs"
include = ["**/*.md", "**/*.html"]

[[sources]]
name = "reports"
description = "Reports"
path = "docs/reports"
include = ["**/*.pdf", "**/*.docx"]
"""


@pytest.fixture(scope="session")
def fake_endpoint():
    with FakeOpenAI() as srv:
        yield srv


def _ensure_binary_testdata():
    reports = TESTDATA / "reports"
    if not (reports / "kelp3-field-trial-2025.pdf").exists() or not (reports / "forecast-pipeline-v3-review.docx").exists():
        sys.path.insert(0, str(TESTDATA))
        import make_testdata

        reports.mkdir(exist_ok=True)
        make_testdata.make_pdf(reports / "kelp3-field-trial-2025.pdf")
        make_testdata.make_docx(reports / "forecast-pipeline-v3-review.docx")


class Workspace:
    """Temp project dir; `ws / "x"` gives paths, `ws.write_config(...)` rewrites config.toml."""

    def __init__(self, path: Path, write_config):
        self.path = path
        self.write_config = write_config

    def __truediv__(self, other) -> Path:
        return self.path / other


@pytest.fixture
def workspace(tmp_path, fake_endpoint):
    """A temp project dir with a copy of the test documents and a config pointing at the fake endpoint."""
    _ensure_binary_testdata()
    for name in ("handbook", "api_docs", "reports"):
        shutil.copytree(TESTDATA / name, tmp_path / "docs" / name)
    shutil.copytree(ROOT / "eval", tmp_path / "eval", ignore=shutil.ignore_patterns("runs"))

    def write_config(model="fake-embed-a", rerank_method="llm", cli_only=False) -> Path:
        p = tmp_path / "config.toml"
        p.write_text(
            CONFIG_TEMPLATE.format(
                base_url=fake_endpoint.base_url, model=model, rerank_method=rerank_method,
                cli_only=str(cli_only).lower(),
            )
        )
        return p

    write_config()
    return Workspace(tmp_path, write_config)


@pytest.fixture
def make_app(workspace, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "test-key")
    apps = []

    def factory(**config_kwargs) -> App:
        cfg_path = workspace.write_config(**config_kwargs) if config_kwargs else workspace / "config.toml"
        app = App.create(load_config(cfg_path))
        apps.append(app)
        return app

    yield factory
    for a in apps:
        a.close()


@pytest.fixture
def indexed_app(make_app):
    app = make_app()
    app.indexer.index()
    return app
