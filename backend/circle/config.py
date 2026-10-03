"""Central configuration for Circle. All secrets come from environment/.env only."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # Database
    # Circle ships as one local application, so the default is a single
    # SQLite file: no server to install, no service to keep running, and the
    # whole archive is one thing to back up or delete. STORAGE_BACKEND=mongo
    # remains available for the large existing archive.
    storage_backend: str = "sqlite"
    sqlite_file: str = "circle.db"
    database_url: str = "mongodb://localhost:27017"
    database_name: str = "circle"
    mongo_atlas: bool = False
    atlas_vector_index: str = "circle_vector_index"

    # Local AI
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "gemma3:4b"
    ollama_embedding_model: str = "nomic-embed-text"

    # --- Local AI performance tuning (CPU-friendly defaults) ---
    # keep_alive keeps models resident between calls (avoids multi-second reloads)
    ollama_keep_alive: str = "30m"
    # context window: smaller = far faster prompt processing on CPU
    ollama_num_ctx: int = 4096
    # 0 = let Ollama choose (it uses physical cores); set to pin thread count
    ollama_num_thread: int = 0
    # answer length caps (fewer generated tokens = less time generating)
    llm_max_answer_tokens: int = 320
    llm_max_prep_tokens: int = 600
    # RAG context budget: prompt size dominates CPU latency, so evidence is
    # budgeted by TOTAL characters as well as record count (spec: answers stay
    # grounded — fewer, tighter records are faster *and* more focused).
    rag_max_evidence: int = 4
    rag_evidence_chars: int = 240
    rag_total_evidence_chars: int = 1000
    rag_prep_max_evidence: int = 6
    rag_prep_evidence_chars: int = 280
    rag_prep_total_evidence_chars: int = 1700

    # Import roots. Empty = created under ./Circle-data on first run.
    import_root: str = ""
    processed_root: str = ""
    failed_root: str = ""
    quarantine_root: str = ""

    # Additional watch folders, comma separated (WATCH_ROOTS). The primary
    # IMPORT_ROOT is always watched too, so this only adds folders:
    #   WATCH_ROOTS=G:\My Drive\CircleImports,D:\Backups
    # The Settings screen manages folders in the database and overrides this.
    watch_roots: str = ""

    # Cloud placeholders (Google Drive / OneDrive streaming mode) report their
    # full size before the bytes exist locally. Give them longer to land.
    cloud_materialize_timeout: float = 600.0

    # Watcher
    watcher_enabled: bool = True
    watcher_stability_seconds: float = 1.5
    file_max_bytes: int = 50 * 1024 * 1024

    # Speech to text
    stt_provider: str = "local-whisper"
    whisper_model: str = "base"

    # Observability (optional)
    sentry_dsn: str = ""
    sentry_enabled: bool = False

    # Optional voice output
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""

    # Server
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    log_level: str = "INFO"
    enable_docs: bool = True

    # --- Remote access -------------------------------------------------
    # Circle holds a person's entire message history and exposes an endpoint
    # that permanently deletes records. On localhost that is safe, but the
    # moment the API is reachable from another machine (a tunnel, a shared
    # link) that boundary is gone. ACCESS_KEY turns on a shared secret that
    # remote callers must present; empty means "localhost only, no secret",
    # which is the right default for a single user on their own machine.
    access_key: str = ""
    # Comma-separated extra origins allowed to call the API from a browser.
    # Needed when the UI is hosted separately (e.g. a static host) from the
    # local backend. Empty keeps the local dev origins only.
    cors_origins: str = ""
    # Set when the backend deliberately listens on a non-loopback address.
    # Loopback callers skip the access key regardless, so this only widens
    # who else may connect.
    allow_remote: bool = False

    # ---- derived roots -------------------------------------------------
    def root_dir(self) -> Path:
        return Path(self.import_root).expanduser().resolve() if self.import_root else (
            Path.cwd() / "Circle-data"
        ).resolve()

    def processed_dir(self) -> Path:
        return Path(self.processed_root).resolve() if self.processed_root else self.root_dir().parent / "processed"

    def failed_dir(self) -> Path:
        return Path(self.failed_root).resolve() if self.failed_root else self.root_dir().parent / "failed"

    def quarantine_dir(self) -> Path:
        return Path(self.quarantine_root).resolve() if self.quarantine_root else self.root_dir().parent / "quarantine"

    def temp_dir(self) -> Path:
        return self.root_dir().parent / "tmp"

    def data_dir(self) -> Path:
        """Where Circle keeps its own state (the SQLite archive lives here).

        Deliberately a sibling of the import folder, never a subfolder of it:
        the watcher walks the import root, and the archive is not an export.
        """
        return self.root_dir().parent / "circle-archive"

    def sqlite_path(self) -> Path:
        """Full path to the SQLite archive file."""
        raw = Path(self.sqlite_file).expanduser()
        return raw if raw.is_absolute() else self.data_dir() / raw

    def root_is_managed(self) -> bool:
        """True when the primary import root is Circle's own folder.

        Folders the user points Circle at (Google Drive, Downloads, a USB
        backup) are theirs: we read them and leave them alone. Only our own
        managed folder is reorganized after import.
        """
        from circle.integration.cloudfolder import classify_folder
        return classify_folder(self.root_dir()) == "local"

    def should_archive(self, path: Path) -> bool:
        """Whether an imported file may be moved into processed/.

        Moving a multi-gigabyte backup out of a synced Drive folder would
        delete it from the user's Drive, so anything outside our own managed
        root stays exactly where it is. Duplicate suppression is by content
        checksum, so leaving files in place costs nothing on rescan.
        """
        if not self.root_is_managed():
            return False
        try:
            Path(path).resolve().relative_to(self.root_dir())
            return True
        except (ValueError, OSError):
            return False

    def extra_watch_roots(self) -> list[Path]:
        """Folders from WATCH_ROOTS, in order, ignoring blanks and dupes."""
        out: list[Path] = []
        seen: set[str] = set()
        for part in (self.watch_roots or "").replace(";", ",").split(","):
            part = part.strip().strip('"')
            if not part:
                continue
            try:
                resolved = Path(part).expanduser().resolve()
            except OSError:
                continue
            key = str(resolved).lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(resolved)
        return out

    def cors_origin_list(self) -> list[str]:
        """Origins allowed to call this API from a browser.

        Localhost dev ports are always included; CORS_ORIGINS adds the hosted
        UI (for example a Render static site) on top.
        """
        base = ["http://localhost:5173", "http://127.0.0.1:5173",
                "http://localhost:4173", "http://127.0.0.1:4173"]
        extra = [o.strip().rstrip("/") for o in (self.cors_origins or "").split(",")]
        out: list[str] = []
        for origin in base + [o for o in extra if o]:
            if origin not in out:
                out.append(origin)
        return out

    # Known import subfolders (source detection by location + filename)
    SOURCE_SUBFOLDERS: ClassVar[tuple[str, ...]] = (
        "whatsapp", "telegram", "instagram", "x", "twitter",
        "chats", "voice", "email", "calendar", "contacts", "documents", "notes",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
