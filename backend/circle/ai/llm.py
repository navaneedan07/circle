"""Local LLM provider (spec §18, §53).

LLMProvider
    └── LocalGemmaProvider   (Ollama, default gemma3:4b)

The model is configurable; no proprietary cloud LLM is hard-coded.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from abc import ABC, abstractmethod
from typing import Optional

import httpx

from circle.config import get_settings

log = logging.getLogger("circle.llm")


class LLMProvider(ABC):
    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def complete(self, prompt: str, system: str = "",
                 temperature: float = 0.2, max_tokens: int = 1024) -> str: ...

    @abstractmethod
    def complete_json(self, prompt: str, system: str = "",
                      temperature: float = 0.1, max_tokens: int = 1024) -> dict: ...

    @abstractmethod
    def stream(self, prompt: str, system: str = "", temperature: float = 0.2,
               max_tokens: int = 512):
        """Yield answer text incrementally (SSE-friendly)."""

    @abstractmethod
    def status(self) -> dict: ...

    def describe_image(self, image_path: str, prompt: str = "") -> str:
        """Optional local vision; providers without vision raise."""
        raise NotImplementedError("no local vision model available")


class _NoCloseClient:
    """Context manager that reuses a shared httpx.Client without closing it."""

    def __init__(self, client: httpx.Client):
        self._client = client

    def __enter__(self) -> httpx.Client:
        return self._client

    def __exit__(self, *exc) -> None:
        return None


class LLMUnavailable(LLMProvider):
    def __init__(self, reason: str):
        self.reason = reason

    def available(self) -> bool:
        return False

    def complete(self, prompt: str, system: str = "", temperature: float = 0.2,
                 max_tokens: int = 1024) -> str:
        raise RuntimeError(f"LLM unavailable: {self.reason}")

    def complete_json(self, prompt: str, system: str = "", temperature: float = 0.1,
                      max_tokens: int = 1024) -> dict:
        raise RuntimeError(f"LLM unavailable: {self.reason}")

    def stream(self, prompt: str, system: str = "", temperature: float = 0.2,
               max_tokens: int = 512):
        raise RuntimeError(f"LLM unavailable: {self.reason}")
        yield  # pragma: no cover

    def describe_image(self, image_path: str, prompt: str = "") -> str:
        raise RuntimeError(f"LLM unavailable: {self.reason}")

    def status(self) -> dict:
        return {"provider": "none", "available": False, "detail": self.reason}


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model response."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
    raise ValueError("model did not return valid JSON")


class LocalGemmaProvider(LLMProvider):
    def __init__(self):
        self.settings = get_settings()
        self._lock = threading.Lock()
        self._model_ok: Optional[bool] = None
        self._http: Optional[httpx.Client] = None
        # Last call's Ollama metrics (tokens / durations) for profiling.
        self.last_metrics: dict = {}

    def _options(self, temperature: float, max_tokens: int) -> dict:
        opts: dict = {"temperature": temperature, "num_predict": max_tokens}
        if self.settings.ollama_num_ctx > 0:
            opts["num_ctx"] = self.settings.ollama_num_ctx
        if self.settings.ollama_num_thread > 0:
            opts["num_thread"] = self.settings.ollama_num_thread
        return opts

    @property
    def base(self) -> str:
        return self.settings.ollama_url.rstrip("/")

    def _client(self) -> httpx.Client:
        """Reuse a single keep-alive connection pool (avoids per-call setup)."""
        if self._http is None:
            self._http = httpx.Client(
                timeout=httpx.Timeout(300.0, connect=5.0),
                limits=httpx.Limits(max_keepalive_connections=4),
            )
        return _NoCloseClient(self._http)

    def warmup(self) -> dict:
        """Preload the model into memory so the first real answer is fast."""
        import time
        t0 = time.time()
        try:
            self._chat("", "ok", 0.0, 1)
            return {"warmed": True, "ms": int((time.time() - t0) * 1000),
                    "metrics": self.last_metrics}
        except Exception as e:
            return {"warmed": False, "error": str(e)[:200]}

    @property
    def model(self) -> str:
        return self.settings.ollama_model

    def list_models(self) -> list[str]:
        try:
            with httpx.Client(timeout=5.0) as client:
                r = client.get(f"{self.base}/api/tags")
                r.raise_for_status()
                return [m.get("name", "") for m in r.json().get("models", [])]
        except Exception:
            return []

    def available(self) -> bool:
        models = self.list_models()
        wanted = self.model.split(":")[0]
        return any(m.split(":")[0] == wanted for m in models)

    def _chat(self, system: str, prompt: str, temperature: float,
              max_tokens: int, json_mode: bool = False) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.settings.ollama_keep_alive,
            "options": self._options(temperature, max_tokens),
        }
        if json_mode:
            payload["format"] = "json"
        with self._lock:
            last_err: Exception | None = None
            for attempt in range(3):
                try:
                    with self._client() as client:
                        r = client.post(f"{self.base}/api/chat", json=payload)
                        if r.status_code == 404:
                            raise RuntimeError(f"model not found: {self.model}")
                        r.raise_for_status()
                        data = r.json()
                        content = (data.get("message") or {}).get("content", "")
                        if not content:
                            raise RuntimeError("empty LLM response")
                        self._record_metrics(data, prompt)
                        return content
                except Exception as e:
                    last_err = e
                    log.warning("LLM attempt %d failed: %s", attempt + 1, e)
            raise RuntimeError(f"LLM call failed: {last_err}")

    def _record_metrics(self, data: dict, prompt: str) -> None:
        """Store Ollama token/duration metrics for latency profiling."""
        def ms(key: str) -> float:
            return round(float(data.get(key) or 0) / 1e6, 1)

        eval_count = int(data.get("eval_count") or 0)
        eval_ms = ms("eval_duration")
        prompt_eval_count = int(data.get("prompt_eval_count") or 0)
        prompt_ms = ms("prompt_eval_duration")
        self.last_metrics = {
            "prompt_tokens": prompt_eval_count,
            "prompt_eval_ms": prompt_ms,
            "generated_tokens": eval_count,
            "generation_ms": eval_ms,
            "load_ms": ms("load_duration"),
            "prompt_chars": len(prompt),
            "tokens_per_second": round(eval_count / (eval_ms / 1000), 1) if eval_ms else 0,
        }
        ctx = self.settings.ollama_num_ctx
        if ctx and prompt_eval_count >= ctx - 8:
            log.warning("prompt (%d tokens) reached num_ctx=%d; raise OLLAMA_NUM_CTX",
                        prompt_eval_count, ctx)

    def complete(self, prompt: str, system: str = "", temperature: float = 0.2,
                 max_tokens: int = 1024) -> str:
        return self._chat(system, prompt, temperature, max_tokens)

    def stream(self, prompt: str, system: str = "", temperature: float = 0.2,
               max_tokens: int = 512):
        """Yield answer chunks as Ollama produces them (token streaming)."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "keep_alive": self.settings.ollama_keep_alive,
            "options": self._options(temperature, max_tokens),
        }
        with self._lock:
            with self._client() as client:
                with client.stream("POST", f"{self.base}/api/chat",
                                   json=payload, timeout=600.0) as r:
                    if r.status_code == 404:
                        raise RuntimeError(f"model not found: {self.model}")
                    r.raise_for_status()
                    for line in r.iter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        chunk = (data.get("message") or {}).get("content", "")
                        if chunk:
                            yield chunk
                        if data.get("done"):
                            self._record_metrics(data, prompt)
                            return

    def complete_json(self, prompt: str, system: str = "", temperature: float = 0.1,
                      max_tokens: int = 1024) -> dict:
        raw = self._chat(system, prompt, temperature, max_tokens, json_mode=True)
        try:
            return _extract_json(raw)
        except ValueError:
            # Retry once without json mode, then parse leniently
            raw = self._chat(system, prompt, 0.0, max_tokens, json_mode=False)
            return _extract_json(raw)

    def describe_image(self, image_path: str, prompt: str = "") -> str:
        """Caption a local image with the multimodal model (Ollama images)."""
        import base64
        from pathlib import Path
        data = Path(image_path).read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        messages = [{"role": "user",
                     "content": prompt or "Describe this image concisely.",
                     "images": [b64]}]
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.settings.ollama_keep_alive,
            "options": self._options(0.2, 220),
        }
        with self._lock:
            with self._client() as client:
                # Vision on CPU is slow; bound it so a describe request can
                # never hold a worker for minutes.
                r = client.post(f"{self.base}/api/chat", json=payload,
                                timeout=120.0)
                if r.status_code == 404:
                    raise RuntimeError(f"model not found: {self.model}")
                r.raise_for_status()
                content = (r.json().get("message") or {}).get("content", "")
                if not content:
                    raise RuntimeError("empty vision response")
                return content

    def status(self) -> dict:
        models = self.list_models()
        ok = self.available()
        return {
            "provider": "ollama",
            "model": self.model,
            "available": ok,
            "url": self.settings.ollama_url,
            "installed_models": models[:10],
            "num_ctx": self.settings.ollama_num_ctx,
            "keep_alive": self.settings.ollama_keep_alive,
            "last_metrics": self.last_metrics,
            "detail": f"local Gemma via Ollama ({self.model})" if ok
                      else f"model {self.model} not pulled yet",
        }


_provider: Optional[LLMProvider] = None


def get_llm() -> LLMProvider:
    global _provider
    if _provider is None:
        try:
            prov = LocalGemmaProvider()
            _provider = prov
        except Exception as e:  # pragma: no cover
            log.error("LLM init failed: %s", e)
            _provider = LLMUnavailable(str(e))
    return _provider


def reset_provider() -> None:
    global _provider
    _provider = None
