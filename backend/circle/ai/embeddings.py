"""Embedding provider abstraction (spec §19).

EmbeddingProvider
    └── LocalEmbeddingProvider   (Ollama, default nomic-embed-text)

No paid/embedded-API dependency; the app works offline once models exist.
"""
from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from typing import Optional

import httpx

from circle.config import get_settings

log = logging.getLogger("circle.embeddings")


class EmbeddingProvider(ABC):
    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def embed(self, text: str) -> list[float]: ...

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...

    @abstractmethod
    def dimensions(self) -> int: ...

    @abstractmethod
    def status(self) -> dict: ...


class LocalEmbeddingProvider(EmbeddingProvider):
    def __init__(self):
        self.settings = get_settings()
        self._dims: Optional[int] = None
        self._lock = threading.Lock()
        self._failures = 0
        self._http: Optional[httpx.Client] = None
        self.last_metrics: dict = {}

    @property
    def url(self) -> str:
        return f"{self.settings.ollama_url.rstrip('/')}/api/embed"

    @property
    def model(self) -> str:
        return self.settings.ollama_embedding_model

    def available(self) -> bool:
        try:
            with httpx.Client(timeout=5.0) as client:
                r = client.get(f"{self.settings.ollama_url.rstrip('/')}/api/tags")
                if r.status_code != 200:
                    return False
                names = [m.get("name", "") for m in r.json().get("models", [])]
                return any(n.split(":")[0] == self.model.split(":")[0] for n in names) \
                    or bool(names)
        except Exception:
            return False

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with self._lock:
            self._ensure_client()
            for attempt in range(3):
                try:
                    assert self._http is not None
                    r = self._http.post(self.url, json={
                        "model": self.model, "input": texts,
                        "keep_alive": self.settings.ollama_keep_alive})
                    r.raise_for_status()
                    data = r.json()
                    embeddings = data.get("embeddings")
                    if embeddings is None and "embedding" in data:
                        embeddings = [data["embedding"]]
                    if not embeddings:
                        raise RuntimeError("empty embedding response")
                    self._dims = len(embeddings[0])
                    self.last_metrics = {
                        "count": len(embeddings),
                        "load_ms": round(float(data.get("load_duration") or 0) / 1e6, 1),
                        "total_ms": round(float(data.get("total_duration") or 0) / 1e6, 1),
                    }
                    self._failures = 0
                    return embeddings
                except Exception as e:
                    self._failures += 1
                    if attempt == 2:
                        log.error("embedding failed after retries: %s", e)
                        raise
        raise RuntimeError("unreachable")

    def dimensions(self) -> int:
        if self._dims:
            return self._dims
        try:
            self._dims = len(self.embed("dimension probe"))
        except Exception:
            self._dims = 768
        return self._dims

    def _ensure_client(self) -> None:
        if self._http is None:
            self._http = httpx.Client(timeout=120.0)

    def warmup(self) -> dict:
        import time
        t0 = time.time()
        try:
            self.embed("warmup")
            return {"warmed": True, "ms": int((time.time() - t0) * 1000),
                    "metrics": self.last_metrics}
        except Exception as e:
            return {"warmed": False, "error": str(e)[:200]}

    def status(self) -> dict:
        ok = self.available()
        return {"provider": "local", "model": self.model, "available": ok,
                "url": self.settings.ollama_url,
                "last_metrics": self.last_metrics,
                "detail": "local embeddings ready" if ok else "Ollama/model unreachable"}


_provider: Optional[EmbeddingProvider] = None


def get_embeddings() -> EmbeddingProvider:
    global _provider
    if _provider is None:
        _provider = LocalEmbeddingProvider()
    return _provider


def reset_provider() -> None:
    global _provider
    _provider = None
