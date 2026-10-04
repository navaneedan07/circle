"""Shared fixtures: test DB isolation + fake AI providers so tests run
without Ollama/network. Real-pipeline tests still use local MongoDB."""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

# Isolate config BEFORE circle.config is imported anywhere

os.environ.setdefault("DATABASE_NAME", "circle_test")
os.environ.setdefault("WATCHER_ENABLED", "false")
os.environ.setdefault("SENTRY_ENABLED", "false")
os.environ.setdefault("OLLAMA_URL", "http://localhost:11434")
# Which store the suite exercises. SQLite is the shipped default; set
# CIRCLE_TEST_BACKEND=mongo to run the same tests against MongoDB.
os.environ.setdefault("STORAGE_BACKEND", "sqlite")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class FakeEmbedder:
    """Deterministic pseudo-embeddings: similar text -> similar vectors."""

    def available(self) -> bool:
        return True

    def _vec(self, text: str) -> list[float]:
        out = []
        for i in range(16):
            h = hashlib.sha256(f"{i}:{text.lower()}".encode()).digest()
            out.append((h[0] / 255.0) * 2 - 1)
        return out

    def embed(self, text: str) -> list[float]:
        return self._vec(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def dimensions(self) -> int:
        return 16

    def status(self) -> dict:
        return {"provider": "fake", "available": True}


class FakeLLM:
    """Returns a valid cited answer; records prompts for assertions."""

    def __init__(self, answer: str | None = None):
        self.calls: list[dict] = []
        self.answer = answer

    def available(self) -> bool:
        return True

    def complete(self, prompt: str, system: str = "", temperature: float = 0.2,
                 max_tokens: int = 1024) -> str:
        self.calls.append({"prompt": prompt, "system": system})
        if self.answer is not None:
            return self.answer
        import re
        ids = sorted(set(re.findall(r"\[(S\d+)\]", prompt)))
        cite = f"[{ids[0]}]" if ids else "[S1]"
        return (f"Based on your imported data, you discussed the SIH project {cite}.\n"
                f"Sources: {cite}")

    def complete_json(self, prompt: str, system: str = "", temperature: float = 0.1,
                      max_tokens: int = 1024) -> dict:
        self.calls.append({"prompt": prompt, "system": system})
        return {"topics": ["SIH"], "same_person": False, "confidence": 0.4}

    def stream(self, prompt: str, system: str = "", temperature: float = 0.2,
               max_tokens: int = 512):
        """Stream the canned answer in small chunks (SSE path tests)."""
        self.calls.append({"prompt": prompt, "system": system})
        self.last_metrics = {"generated_tokens": 12, "tokens_per_second": 9.0}
        answer = self.complete(prompt, system, temperature, max_tokens)
        for i in range(0, len(answer), 12):
            yield answer[i:i + 12]

    def status(self) -> dict:
        return {"provider": "fake", "available": True}


@pytest.fixture(scope="session")
def store(tmp_path_factory):
    """The store under test, wiped before and after the session.

    SQLite is the only engine, so a storage bug cannot hide behind whichever
    engine happens to be installed.
    """
    from circle.config import get_settings
    from circle.repository.factory import get_store
    s = get_settings()
    path = tmp_path_factory.mktemp("circle-store") / "circle-test.db"
    st = get_store(s, sqlite_path=str(path))
    yield st
    st.close()


@pytest.fixture()
def clean_store(store):
    for table in ("people", "conversations", "messages", "emails",
                  "calendar_events", "notes", "voice_recordings",
                  "media", "documents", "memories", "relationship_events",
                  "sources", "import_jobs", "profiles", "processed_files",
                  "identity_suggestions", "app_settings"):
        store.engine.execute(f"DELETE FROM {table}")
        store.engine.execute("DELETE FROM memories_fts")
    yield store


@pytest.fixture()
def fake_embedder():
    return FakeEmbedder()


@pytest.fixture()
def fake_llm():
    return FakeLLM()


@pytest.fixture()
def resolver(clean_store):
    from circle.identity.resolver import IdentityResolver
    return IdentityResolver(clean_store)


@pytest.fixture()
def pipeline(clean_store, resolver, fake_embedder, fake_llm, tmp_path):
    from circle.config import get_settings
    from circle.ingestion.pipeline import IngestionPipeline
    settings = get_settings()
    # tmp_path IS the managed import root, so archiving behaves as it does in
    # production for Circle's own folder. Folders the user owns (Google Drive,
    # Downloads) are covered separately in test_watch_folders.py.
    settings.import_root = str(tmp_path)
    # tmp_path is Circle's own work folder here, so archiving behaves as it
    # does in production. Declared, not inferred.
    settings.work_root = str(tmp_path)
    settings.processed_root = str(tmp_path / "processed")
    settings.failed_root = str(tmp_path / "failed")
    settings.quarantine_root = str(tmp_path / "quarantine")
    for d in (settings.processed_dir(), settings.failed_dir(),
              settings.quarantine_dir()):
        d.mkdir(parents=True, exist_ok=True)
    return IngestionPipeline(store=clean_store, resolver=resolver,
                             embedder=fake_embedder, llm=fake_llm,
                             settings=settings)
