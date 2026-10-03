"""Copy the existing MongoDB archive into SQLite.

Circle now stores everything in one local SQLite file, but anyone who has
already been running the MongoDB build has a real archive they should not lose.
This script copies every collection across, in batches, and reports progress.

    .venv/Scripts/python scripts/migrate_to_sqlite.py --dry-run
    .venv/Scripts/python scripts/migrate_to_sqlite.py

It refuses to run into a non-empty SQLite file unless --force is given, so a
half-finished previous run cannot be silently doubled up. Re-running is safe:
documents are written by id, so a second pass overwrites rather than duplicates.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from circle.config import get_settings

# Largest first: memories dominate by volume, so a slow start there tells you
# early whether the copy will finish in reasonable time.
COLLECTIONS = [
    "people", "conversations", "messages", "emails", "calendar_events",
    "notes", "voice_recordings", "media", "documents", "memories",
    "relationship_events", "sources", "import_jobs", "profiles",
    "processed_files", "identity_suggestions", "app_settings",
]

BATCH = 5000


def human(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Copy the MongoDB archive into the local SQLite file.")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be copied and change nothing")
    parser.add_argument("--force", action="store_true",
                        help="allow migrating into a database that has rows")
    args = parser.parse_args(argv[1:])

    settings = get_settings()
    from circle.repository.mongo import MongoStore
    from circle.repository.sqlite import SQLiteStore

    target = settings.sqlite_path()
    print(f"MongoDB: {settings.database_url}/{settings.database_name}")
    print(f"SQLite:  {target}")
    if not settings.mongo_atlas:
        print("(MongoDB Atlas is also supported: set MONGO_ATLAS=true)")

    src = MongoStore(settings)
    if not src.ping():
        print("cannot reach MongoDB; is mongod running?", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\n--dry-run: nothing will be written\n")
        total = 0
        for name in COLLECTIONS:
            n = src.db[name].count_documents({})
            total += n
            print(f"  {name:<22} {human(n):>8}")
        print(f"  {'TOTAL':<22} {human(total):>8}")
        return 0

    if target.exists() and target.stat().st_size > 0:
        existing = SQLiteStore(settings).engine.query(
            "SELECT COUNT(*) AS n FROM people")[0]["n"]
        if existing and not args.force:
            print(f"{target} already holds data ({existing} people). "
                  "Pass --force to migrate into it anyway.", file=sys.stderr)
            return 1

    dst = SQLiteStore(settings)
    started = time.time()
    grand = 0
    for name in COLLECTIONS:
        total = src.db[name].count_documents({})
        if not total:
            print(f"  {name:<22} {0:>8}  (empty)")
            continue
        copied = 0
        cursor = src.db[name].find({}).batch_size(BATCH)
        batch: list[dict] = []
        for doc in cursor:
            batch.append(doc)
            if len(batch) >= BATCH:
                copied += _copy_batch(dst, name, batch)
                batch = []
                _tick(name, copied, total, started)
        if batch:
            copied += _copy_batch(dst, name, batch)
        grand += copied
        # The FTS mirror is rebuilt once, in bulk: indexing row by row while
        # copying is slower than one pass at the end.
        print(f"  {name:<22} {copied:>8}  done ({human(copied)})")
    indexed = dst.engine.reindex_memories_fts()
    print(f"\nmigrated {human(grand)} documents and indexed "
          f"{human(indexed)} memories in {time.time() - started:.1f}s")
    print(f"SQLite archive: {target} "
          f"({target.stat().st_size / 1_000_000:.1f} MB)")
    print("\nSet STORAGE_BACKEND=mongo to keep using MongoDB, or leave it unset "
          "to use SQLite from now on.")
    return 0


def _normalize(value):
    """BSON -> JSON-safe.

    Mongo ids are ObjectId, not strings, and SQLite can only store text. The
    string form is what Circle already used everywhere else, so converting is
    lossless as far as the app is concerned.
    """
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _copy_batch(dst, coll: str, batch: list[dict]) -> int:
    """Write a batch in one executemany.

    Per-document statements run at a few dozen rows a second on a large
    archive; batching the same writes turns a multi-hour copy into minutes.
    INSERT OR REPLACE also makes a re-run safe: rows already copied are
    overwritten rather than rejected as duplicates, so an interrupted migration
    just picks up where it stopped.
    """
    # Documents in one collection do not all have the same columns (a memory
    # may or may not carry an embedding), so rows are grouped by their column
    # set and each group gets its own statement.
    groups: dict[tuple[str, ...], list[tuple]] = {}
    for doc in batch:
        if "_id" not in doc:
            continue
        try:
            doc = _normalize(doc)
            doc["_id"] = str(doc["_id"])
            cols, blob = dst.engine.encode(coll, doc)
            names = tuple(["id", "doc"] + list(cols))
            groups.setdefault(names, []).append(
                tuple([doc["_id"], blob] + list(cols.values())))
        except Exception as e:  # noqa: BLE001
            print(f"\n  ! skipped one {coll} document: {e}")
    for names, rows in groups.items():
        marks = ",".join("?" for _ in names)
        dst.engine.executemany(
            f"INSERT OR REPLACE INTO {coll} ({', '.join(names)}) "
            f"VALUES ({marks})", rows)
    return sum(len(r) for r in groups.values())


def _tick(coll: str, copied: int, total: int, started: float) -> None:
    pct = copied * 100 // max(total, 1)
    rate = copied / max(time.time() - started, 0.001)
    print(f"\r  {coll:<22} {human(copied):>8}/{human(total):<8} "
          f"{pct:>3}%  {rate:.0f}/s", end="", flush=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))