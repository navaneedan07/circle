"""Storage engine factory.

SQLite is the default because Circle ships as one local application: a single
file needs no server, and the entire archive is one thing to back up. Mongo
stays selectable for the existing large archive.
"""
from __future__ import annotations

from typing import Optional

from circle.config import Settings, get_settings


def get_store(settings: Optional[Settings] = None,
              sqlite_path: Optional[str] = None):
    """Build the configured store.

    Kept in its own module so both engines stay importable from one place and
    tests can parameterise over them without importing Mongo to get SQLite.
    """
    settings = settings or get_settings()
    backend = (settings.storage_backend or "sqlite").strip().lower()
    if backend in ("sqlite", "sqlite3"):
        from circle.repository.sqlite import SQLiteStore
        return SQLiteStore(settings, path=sqlite_path)
    if backend in ("mongo", "mongodb"):
        from circle.repository.mongo import MongoStore
        return MongoStore(settings)
    raise ValueError(
        f"unknown STORAGE_BACKEND {backend!r} (expected 'sqlite' or 'mongo')")