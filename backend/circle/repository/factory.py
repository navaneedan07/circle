"""Storage engine factory.

SQLite is the only engine. Circle ships as one local application: a single
file needs no server, is one thing to back up, and removes the class of bug
that comes from keeping two engines in lockstep. MongoDB is no longer
supported.
"""
from __future__ import annotations

from typing import Optional

from circle.config import Settings, get_settings


def get_store(settings: Optional[Settings] = None,
              sqlite_path: Optional[str] = None):
    """Build the store. Kept in its own module so the HTTP layer never imports
    a concrete repository directly."""
    settings = settings or get_settings()
    backend = (settings.storage_backend or "sqlite").strip().lower()
    if backend not in ("sqlite", "sqlite3"):
        raise ValueError(
            f"unsupported STORAGE_BACKEND {backend!r}: SQLite is the only "
            "engine in this build")
    from circle.repository.sqlite import SQLiteStore
    return SQLiteStore(settings, path=sqlite_path)
