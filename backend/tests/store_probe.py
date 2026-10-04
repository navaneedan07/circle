"""Test read helpers.

Assertions about raw rows (counts, embeddings present, a specific record) are
worth making directly, but going through the store's internals in every test is
fragile. These helpers read through the SQLite store's own accessors.
"""
from __future__ import annotations

from typing import Any, Optional


def _docs(store, coll: str) -> Any:
    """All rows in a collection, as plain dicts."""
    return store.engine.find(coll)


def find(store, coll: str, query: dict, limit: Optional[int] = None) -> list[dict]:
    return store.engine.find(coll, query, limit=limit)


def find_one(store, coll: str, query: dict) -> Optional[dict]:
    rows = find(store, coll, query, limit=1)
    return rows[0] if rows else None


def count(store, coll: str, query: Optional[dict] = None) -> int:
    return store.engine.count(coll, query or {})


def drop(store, coll: str, query: Optional[dict] = None) -> int:
    """Delete matching rows and return how many went."""
    if query:
        return store.engine.delete_many(coll, query)
    return store.engine.execute(f"DELETE FROM {coll}").rowcount


def has_embedding(store, memory_id: str) -> bool:
    """True when a memory carries a non-empty embedding."""
    memory = store.get_memory(memory_id)
    return bool(memory and memory.embedding)
