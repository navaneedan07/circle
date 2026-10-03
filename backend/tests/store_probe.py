"""Engine-agnostic read helpers for tests.

Assertions about raw collections (counts, embeddings present, a specific row)
are worth making directly, but going through ``store.db`` ties every test to
one engine. These helpers read through whichever store is configured.
"""
from __future__ import annotations

from typing import Any, Optional


def _docs(store, coll: str) -> Any:
    """All documents in a collection, as plain dicts."""
    if hasattr(store, "db"):                       # Mongo
        return list(store.db[coll].find({}))
    return store.engine.find(coll)


def find(store, coll: str, query: dict, limit: Optional[int] = None) -> list[dict]:
    if hasattr(store, "db"):                       # Mongo
        cur = store.db[coll].find(query)
        return list(cur.limit(limit)) if limit else list(cur)
    return store.engine.find(coll, query, limit=limit)


def find_one(store, coll: str, query: dict) -> Optional[dict]:
    rows = find(store, coll, query, limit=1)
    return rows[0] if rows else None


def count(store, coll: str, query: Optional[dict] = None) -> int:
    query = query or {}
    if hasattr(store, "db"):                       # Mongo
        return store.db[coll].count_documents(query)
    return store.engine.count(coll, query)


def drop(store, coll: str, query: Optional[dict] = None) -> int:
    """Delete matching rows and return how many went."""
    if hasattr(store, "db"):                       # Mongo
        return store.db[coll].delete_many(query or {}).deleted_count
    if query:
        return store.engine.delete_many(coll, query)
    return store.engine.execute(f"DELETE FROM {coll}").rowcount


def has_embedding(store, memory_id: str) -> bool:
    """True when a memory carries a non-empty embedding.

    Mongo reads it from the document; SQLite keeps it in a BLOB column, so the
    check goes through the store's own accessor.
    """
    if hasattr(store, "db"):                       # Mongo
        doc = store.db.memories.find_one({"_id": memory_id}, {"embedding": 1})
        return bool(doc and doc.get("embedding"))
    return bool(store.get_memory(memory_id)
                and store.get_memory(memory_id).embedding)