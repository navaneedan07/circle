"""Application context: wires store, pipeline, watcher and AI providers.

Everything hangs off one AppContext so the FastAPI layer stays thin.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from circle.ai.embeddings import EmbeddingProvider, get_embeddings
from circle.ai.llm import LLMProvider, get_llm
from circle.config import Settings, get_settings
from circle.identity.resolver import IdentityResolver
from circle.ingestion.pipeline import IngestionPipeline
from circle.ingestion.watcher import FolderWatcher
from circle.integration import cloudfolder
from circle.realtime.broker import EventBroker, broker
from circle.repository.mongo import MongoStore

log = logging.getLogger("circle.context")


@dataclass
class AppContext:
    settings: Settings
    store: MongoStore
    resolver: IdentityResolver
    embedder: EmbeddingProvider
    llm: LLMProvider
    pipeline: IngestionPipeline
    watcher: FolderWatcher
    broker: EventBroker = field(default_factory=lambda: broker)
    started: bool = False
    health: dict = field(default_factory=dict)

    @classmethod
    def build(cls, settings: Optional[Settings] = None) -> "AppContext":
        settings = settings or get_settings()
        store = MongoStore(settings)
        resolver = IdentityResolver(store)
        embedder = get_embeddings()
        llm = get_llm()
        pipeline = IngestionPipeline(store=store, resolver=resolver,
                                     embedder=embedder, llm=llm,
                                     broker=broker, settings=settings)
        roots = _roots_from_config(settings, store)
        pipeline.watch_roots = roots
        watcher = FolderWatcher(settings=settings,
                                process_fn=pipeline.process_path,
                                roots=roots)
        return cls(settings=settings, store=store, resolver=resolver,
                   embedder=embedder, llm=llm, pipeline=pipeline,
                   watcher=watcher)

    def startup(self) -> dict:
        """Startup sequence (spec §44)."""
        from circle.voice.stt import get_stt
        health: dict = {}

        # 1-2. database
        health["database"] = {"connected": self.store.ping(),
                              "engine": "mongodb-atlas" if self.settings.mongo_atlas
                              else "mongodb"}
        if not health["database"]["connected"]:
            health["database"]["detail"] = "cannot reach MongoDB at DATABASE_URL"
        else:
            # resume jobs interrupted by a restart
            resumed = self.store.resume_incomplete_jobs()
            if resumed:
                log.info("resumed %d interrupted job(s)", len(resumed))

        # 3-4. Ollama + model (then warm both models so the first ask is fast;
        # loading gemma3:4b cold costs several seconds on CPU)
        health["llm"] = self.llm.status()
        if health["llm"].get("available"):
            try:
                warm = self.llm.warmup() if hasattr(self.llm, "warmup") else {}
                health["llm"]["warmup"] = warm
                log.info("llm warmup: %s", warm)
            except Exception as e:
                log.warning("llm warmup failed: %s", e)
        # 5. embeddings
        if hasattr(self.embedder, "warmup"):
            try:
                health["embeddings_warmup"] = self.embedder.warmup()
            except Exception as e:
                log.warning("embedding warmup failed: %s", e)
        health["embeddings"] = self.embedder.status()
        # speech-to-text
        health["stt"] = get_stt().status()
        # tts (optional)
        from circle.voice.tts import tts_status
        health["tts"] = tts_status()

        # 6-7. watcher + scan import dirs
        if self.settings.watcher_enabled:
            try:
                self.watcher.start(rescan=True)
                health["watcher"] = {"running": True,
                                     "root": str(self.watcher.root),
                                     "roots": [str(p) for p in self.watcher.roots],
                                     "folder_count": len(self.watcher.roots)}
            except Exception as e:
                log.error("watcher failed to start: %s", e)
                health["watcher"] = {"running": False, "detail": str(e)}
        else:
            health["watcher"] = {"running": False,
                                 "detail": "disabled by configuration"}

        health["imports"] = {"root": str(self.watcher.root),
                             "roots": [str(p) for p in self.watcher.roots],
                             "cloud_drive": cloudfolder.google_drive_install(),
                             "processed": str(self.settings.processed_dir()),
                             "failed": str(self.settings.failed_dir()),
                             "quarantine": str(self.settings.quarantine_dir())}
        health["offline_ready"] = bool(health["llm"].get("available"))
        self.health = health
        self.started = True
        return health

    def shutdown(self) -> None:
        try:
            self.watcher.stop()
        except Exception:
            pass

    def watch_folders(self) -> list[Path]:
        return list(self.watcher.roots)

    def add_watch_folder(self, path: Path) -> list[Path]:
        """Add a folder at runtime and start watching it immediately."""
        roots = list(self.watcher.roots)
        key = str(path).lower()
        if not any(str(r).lower() == key for r in roots):
            roots.append(path)
        return self._apply_roots(roots)

    def remove_watch_folder(self, path: Path) -> list[Path]:
        key = str(path).lower()
        roots = [r for r in self.watcher.roots if str(r).lower() != key]
        if not roots:
            raise ValueError("the main import folder cannot be removed")
        return self._apply_roots(roots)

    def _apply_roots(self, roots: list[Path]) -> list[Path]:
        normalized = FolderWatcher._normalize(roots)
        self.store.set_setting(
            "watch_roots", [str(p) for p in normalized[1:]])
        self.watcher.set_roots(normalized)
        self.pipeline.watch_roots = list(self.watcher.roots)
        self.broker.publish("sync", {"action": "watch_folders",
                                     "roots": [str(p) for p in self.watcher.roots]})
        return list(self.watcher.roots)

    def ai_metrics(self) -> dict:
        """Live AI latency metrics for settings/diagnostics surfaces."""
        llm = getattr(self.llm, "last_metrics", {}) or {}
        emb = getattr(self.embedder, "last_metrics", {}) or {}
        return {
            "llm": llm,
            "embeddings": emb,
            "tuning": {
                "num_ctx": self.settings.ollama_num_ctx,
                "keep_alive": self.settings.ollama_keep_alive,
                "num_thread": self.settings.ollama_num_thread,
                "max_answer_tokens": self.settings.llm_max_answer_tokens,
                "max_prep_tokens": self.settings.llm_max_prep_tokens,
                "evidence_records": self.settings.rag_max_evidence,
                "evidence_chars": self.settings.rag_evidence_chars,
                "evidence_budget_chars": self.settings.rag_total_evidence_chars,
            },
            "advice": self._perf_advice(llm),
        }

    def _perf_advice(self, llm: dict) -> list[str]:
        tips: list[str] = []
        gen = llm.get("tokens_per_second") or 0
        prompt_ms = llm.get("prompt_eval_ms") or 0
        prompt_tokens = llm.get("prompt_tokens") or 0
        if prompt_tokens and prompt_ms and prompt_ms / max(prompt_tokens, 1) > 40:
            tips.append(
                "Prompt processing is slow on this CPU — keep questions focused "
                "or lower OLLAMA_MODEL to a smaller model (e.g. gemma3:1b).")
        if gen and gen < 4:
            tips.append("Generation is very slow; a smaller model will respond "
                        "much faster on this hardware.")
        if (llm.get("load_ms") or 0) > 2000:
            tips.append("Model reloading detected — increase OLLAMA_KEEP_ALIVE.")
        return tips

    def sync_status(self) -> dict:
        jobs = self.store.count_jobs_by_status()
        return {
            "watcher_running": self.watcher.stats.get("running", False),
            "root": str(self.watcher.root),
            "roots": [str(p) for p in self.watcher.roots],
            "folder_count": len(self.watcher.roots),
            "files": {
                "processed": self.watcher.stats.get("processed", 0),
                "failed": self.watcher.stats.get("failed", 0),
                "skipped": self.watcher.stats.get("skipped", 0),
            },
            "jobs": jobs,
            "last_event_at": self.watcher.stats.get("last_event_at"),
            "queue_size": self.watcher._queue.qsize(),
            "subscribers": self.broker.subscriber_count,
            "counts": {
                "people": self.store.count_people(),
                "messages": self.store.count_messages(),
                "memories": self.store.count_memories(),
            },
        }


_ctx: Optional[AppContext] = None


def _roots_from_config(settings: Settings, store: MongoStore) -> list[Path]:
    """Watch folders, in order: the managed root, then every extra folder.

    Extras come from the Settings screen when it has been used, otherwise from
    WATCH_ROOTS in the environment. The managed root is always first so that
    existing single-folder setups keep working unchanged.
    """
    stored = store.get_setting("watch_roots") or []
    raw = [str(p) for p in stored if str(p).strip()] if stored else []
    if not raw:
        raw = [str(p) for p in settings.extra_watch_roots()]
    roots: list[Path] = [settings.root_dir()]
    seen = {str(roots[0]).lower()}
    for entry in raw:
        try:
            resolved = Path(entry).expanduser().resolve()
        except OSError:
            continue
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        roots.append(resolved)
    return roots


def get_context() -> AppContext:
    global _ctx
    if _ctx is None:
        _ctx = AppContext.build()
    return _ctx


def set_context(ctx: AppContext) -> None:
    global _ctx
    _ctx = ctx
