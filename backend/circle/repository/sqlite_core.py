"""SQLite plumbing: connection, schema, and a small Mongo-query translator.

MongoDB's query language leaks into callers (filters like {"occurred_at":
{"$gte": iso}}), so instead of rewriting every caller we translate the subset
that Circle actually uses into SQL. Anything unsupported raises, so a silent
wrong answer is impossible -- a query that returns nothing is a bug that shows
up in tests, a query that silently returns the wrong rows is a bug that does
not.

Storage layout: one table per collection with a ``doc`` JSON column holding the
whole document, plus real columns for the fields we filter, join or sort on.
That keeps the pydantic round-trip identical to Mongo while making the
hot paths indexable.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading

import numpy as np
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

# Fields promoted to real columns, per collection. Anything not listed stays in
# the JSON doc and is filtered with json_extract (correct, just slower).
COLUMNS: dict[str, dict[str, str]] = {
    "people": {"updated_at": "TEXT", "display_name": "TEXT", "is_user": "INTEGER"},
    "conversations": {"source": "TEXT", "external_key": "TEXT",
                      "last_message_at": "TEXT"},
    "messages": {"person_id": "TEXT", "conversation_id": "TEXT",
                 "sent_at": "TEXT", "source": "TEXT"},
    "emails": {"person_id": "TEXT", "sent_at": "TEXT"},
    "calendar_events": {"person_ids": "TEXT", "starts_at": "TEXT"},
    "notes": {"person_id": "TEXT", "noted_at": "TEXT"},
    "voice_recordings": {"person_id": "TEXT", "recorded_at": "TEXT",
                         "checksum": "TEXT", "imported_at": "TEXT"},
    "media": {"message_id": "TEXT", "person_id": "TEXT", "conversation_id": "TEXT",
              "filename_key": "TEXT", "checksum": "TEXT", "occurred_at": "TEXT",
              "imported_at": "TEXT"},
    "documents": {"person_id": "TEXT", "external_id": "TEXT", "imported_at": "TEXT"},
    "memories": {"person_id": "TEXT", "occurred_at": "TEXT", "source": "TEXT",
                 "record_id": "TEXT", "kind": "TEXT"},
    "relationship_events": {"person_id": "TEXT", "occurred_at": "TEXT",
                            "kind": "TEXT", "source": "TEXT", "record_id": "TEXT"},
    "sources": {"checksum": "TEXT", "imported_at": "TEXT"},
    "import_jobs": {"created_at": "TEXT", "status": "TEXT"},
    "profiles": {"person_id": "TEXT"},
    "processed_files": {"checksum": "TEXT", "path": "TEXT"},
    "identity_suggestions": {"status": "TEXT", "person_a_id": "TEXT",
                             "person_b_id": "TEXT"},
    "app_settings": {},
}

# Multi-field unique constraints that Mongo expresses as unique indexes.
# Sparse in Mongo (missing field never collides); here NULL never collides
# either, because SQLite treats NULLs as distinct in unique indexes.
UNIQUE: dict[str, tuple[tuple[str, ...], ...]] = {
    "conversations": (("source", "external_key"),),
    "voice_recordings": (("checksum",),),
    "media": (("checksum",),),
    "documents": (("external_id",),),
    "sources": (("checksum",),),
    "processed_files": (("checksum",),),
    "profiles": (("person_id",),),
}

# Scalar/JSON paths that are *arrays* in the document, so equality means
# "array contains" (Mongo's implicit array match).
ARRAY_PATHS: dict[str, tuple[str, ...]] = {
    "people": ("identities",),
    "conversations": ("participant_ids",),
    "calendar_events": ("person_ids",),
    "messages": ("attachments",),
}

# FTS5 mirror of the memories text index Mongo keeps.
MEMORY_FTS_FIELDS = ("text", "summary", "topics")


def _sql_type(value: Any) -> str:
    if isinstance(value, bool):
        return "INTEGER"
    if isinstance(value, (int, float)):
        return "REAL"
    return "TEXT"


class SqliteEngine:
    """Thin, thread-safe wrapper around one SQLite database file."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False,
                                    isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._ensure_schema()

    # ------------------------------------------------------------------ setup
    def _ensure_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        for coll, cols in COLUMNS.items():
            parts = ["id TEXT PRIMARY KEY", "doc TEXT NOT NULL"]
            for name, ctype in cols.items():
                parts.append(f"{name} {ctype}")
            if coll == "memories":
                parts.append("embedding BLOB")
            cur.execute(f"CREATE TABLE IF NOT EXISTS {coll} ({', '.join(parts)})")
            for name in cols:
                cur.execute(f"CREATE INDEX IF NOT EXISTS ix_{coll}_{name} "
                            f"ON {coll}({name})")
            for spec in UNIQUE.get(coll, ()):
                idx = "ux_" + coll + "_" + "_".join(spec)
                cur.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {idx} "
                            f"ON {coll}({', '.join(spec)})")
        cur.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5("
            "memory_id UNINDEXED, text, tokenize='porter unicode61')")
        cur.close()

    # ------------------------------------------------------------- primitives
    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, tuple(params)))

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, tuple(params))

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> int:
        """Run one statement over many parameter tuples.

        Used by the archive migration: the same insert written row by row is
        orders of magnitude slower than handing SQLite all the rows at once.
        """
        with self._lock:
            cur = self.conn.executemany(sql, [tuple(r) for r in rows])
            return cur.rowcount

    # ------------------------------------------------------ filter translation
    def _column_or_extract(self, coll: str, field: str, alias: str = "") -> str:
        """A column reference for `field`, promoted or via json_extract.

        `alias` is the table alias a caller is joining under, so it replaces
        the table name rather than being added to it.
        """
        table = alias or coll
        # _id is the primary key column, not a field inside the document.
        if field == "_id":
            return f"{table}.id"
        cols = COLUMNS.get(coll, {})
        if field in cols:
            return f"{table}.{field}"
        return f"json_extract({table}.doc, '$.{field}')"

    def _match_where(self, coll: str, query: dict, params: list,
                     alias: str = "") -> str:
        """Translate a Mongo match clause into a SQL boolean expression."""
        doc = f"{alias or coll}.doc"
        clauses: list[str] = []
        for field, cond in query.items():
            if field == "$or":
                parts = [self._match_where(coll, c, params, alias) for c in cond]
                clauses.append("(" + " OR ".join(parts) + ")")
                continue
            if field == "$and":
                parts = [self._match_where(coll, c, params, alias) for c in cond]
                clauses.append("(" + " AND ".join(parts) + ")")
                continue
            expr = self._column_or_extract(coll, field, alias)
            if isinstance(cond, dict) and any(
                    k.startswith("$") for k in cond):
                clauses.append(self._op_where(coll, field, cond, params, alias))
                continue
            array_field = field in ARRAY_PATHS.get(coll, ())
            if array_field:
                # Mongo equality against an array field means "contains".
                clauses.append(
                    f"EXISTS (SELECT 1 FROM json_each({doc}, "
                    f"'$.{field}') WHERE value = ?)")
                params.append(cond)
            else:
                if cond is None:
                    clauses.append(f"({expr} IS NULL "
                                   f"OR json_type({doc}, '$.{field}') = 'null')")
                else:
                    clauses.append(f"{expr} = ?")
                    params.append(canonical_time(cond))
        return " AND ".join(clauses) if clauses else "1=1"

    def _op_where(self, coll: str, field: str, cond: dict,
                  params: list, alias: str) -> str:
        doc = f"{alias or coll}.doc"
        expr = self._column_or_extract(coll, field, alias)
        out: list[str] = []
        for op, val in cond.items():
            if op == "$options":
                continue      # a modifier of $regex, not an operator
            val = canonical_time(val)
            if op == "$eq":
                out.append(f"({expr} = ?)")
                params.append(val)
            elif op in ("$ne", "$not"):
                out.append(f"({expr} IS NULL OR {expr} != ?)")
                params.append(val)
            elif op == "$in":
                if not val:
                    out.append("0=1")
                    continue
                marks = ",".join("?" for _ in val)
                out.append(f"({expr} IN ({marks}))")
                params.extend(canonical_time(v) for v in val)
            elif op == "$nin":
                marks = ",".join("?" for _ in val)
                out.append(f"({expr} IS NULL "
                           f"OR {expr} NOT IN ({marks}))")
                params.extend(canonical_time(v) for v in val)
            elif op in ("$gt", "$gte", "$lt", "$lte"):
                sym = {"$gt": ">", "$gte": ">=", "$lt": "<", "$lte": "<="}[op]
                out.append(f"{expr} {sym} ?")
                params.append(val)
            elif op == "$exists":
                exists = f"{expr} IS NOT NULL"
                out.append(exists if val else f"NOT ({exists})")
            elif op == "$regex":
                # SQL has no regex; an anchored pattern is an equality test
                # and anything else falls back to a substring LIKE. Circle
                # only uses anchored, re.escape'd patterns.
                pattern = str(val)
                flags = str(cond.get("$options", ""))
                anchored = pattern.startswith("^") and pattern.endswith("$")
                core = pattern[1:-1] if anchored else pattern
                if anchored and "i" in flags:
                    out.append(f"{expr} = ? COLLATE NOCASE")
                    params.append(core)
                else:
                    out.append(f"{expr} LIKE ? ESCAPE '\\'")
                    params.append("%" + _like_escape(core) + "%")
            elif op == "$elemMatch":
                sub = val if isinstance(val, dict) else {}
                inner = []
                iparams: list = []
                for k, v in sub.items():
                    inner.append("json_extract(value, '$.%s') = ?" % k)
                    iparams.append(canonical_time(v))
                out.append(
                    f"EXISTS (SELECT 1 FROM json_each({doc}, '$.{field}') "
                    f"WHERE {' AND '.join(inner)})")
                params.extend(iparams)
            elif op == "$not":
                out.append(f"NOT ({expr} = ?)")
                params.append(val)
            else:
                raise NotImplementedError(
                    f"SQLite store does not support Mongo operator {op!r}")
        return "(" + " AND ".join(out) + ")" if out else "1=1"

    # Public alias: stores that build their own joins need the translator.
    match_where = _match_where

    # ------------------------------------------------------------ document I/O
    def split_doc(self, coll: str, data: dict) -> tuple[dict, dict]:
        """Split a stored dict into (filter columns, full document).

        Columns duplicate fields rather than replacing them: the JSON doc stays
        the whole document, so reads never lose a model field. Only the
        embedding is dropped from the doc, because it is large and binary.
        """
        cols: dict[str, Any] = {}
        known = COLUMNS.get(coll, {})
        for key, value in data.items():
            if key != "_id" and key in known and not isinstance(value, (dict, list)):
                cols[key] = value
        return cols, dict(data)

    def encode(self, coll: str, data: dict) -> tuple[dict, str]:
        cols, blob = self._apply(coll, data)
        embedding = data.get("embedding")
        if embedding:
            # 768 float32s as JSON text is roughly ten times the size of the
            # raw bytes and has to be parsed on every row read. SQLite stores
            # it as a BLOB; numpy reads it back without a copy.
            cols["embedding"] = np.asarray(embedding,
                                           dtype=np.float32).tobytes()
        return cols, blob

    def insert(self, coll: str, doc: dict) -> bool:
        """Insert one document. False when the primary key already exists."""
        cols, blob = self.encode(coll, doc)
        names = ["id", "doc"] + list(cols)
        marks = ",".join("?" for _ in names)
        try:
            self.execute(
                f"INSERT INTO {coll} ({', '.join(names)}) VALUES ({marks})",
                [doc["_id"], blob] + list(cols.values()))
            return True
        except sqlite3.IntegrityError:
            return False

    def upsert(self, coll: str, doc: dict) -> None:
        """Replace-or-insert a whole document by _id."""
        cols, blob = self.encode(coll, doc)
        names = ["id", "doc"] + list(cols)
        marks = ",".join("?" for _ in names)
        self.execute(
            f"INSERT OR REPLACE INTO {coll} ({', '.join(names)}) "
            f"VALUES ({marks})", [doc["_id"], blob] + list(cols.values()))

    @staticmethod
    def decode_embedding(blob: Optional[bytes]) -> Optional[list[float]]:
        if not blob:
            return None
        return np.frombuffer(blob, dtype=np.float32).astype(float).tolist()

    def _apply(self, coll: str, doc: dict) -> tuple[dict, str]:
        """(filter columns, JSON document) for a document being written."""
        cols, rest = self.split_doc(coll, doc)
        rest.pop("embedding", None)
        rest.pop("_id", None)
        return ({k: canonical_time(v) for k, v in cols.items()},
                json.dumps(_canonicalize(rest), default=str))

    def set_fields(self, coll: str, doc_id: str, fields: dict) -> int:
        """Mongo $set of top-level fields on one document."""
        current = self.get(coll, doc_id)
        if current is None:
            return 0
        current.update(fields)
        cols, blob = self._apply(coll, current)
        assigns = ", ".join(f"{k} = ?" for k in cols)
        self.execute(f"UPDATE {coll} SET doc = ?, {assigns} WHERE id = ?",
                     [blob] + list(cols.values()) + [doc_id])
        return 1


    def get(self, coll: str, doc_id: str) -> Optional[dict]:
        rows = self.query(f"SELECT doc FROM {coll} WHERE id = ?", [doc_id])
        return _loads(rows[0]["doc"], doc_id) if rows else None

    def find(self, coll: str, query: Optional[dict] = None, *,
             sort: Optional[list[tuple[str, int]]] = None,
             limit: Optional[int] = None, skip: int = 0,
             projection: Optional[dict] = None) -> list[dict]:
        params: list = []
        where = self._match_where(coll, query or {}, params)
        sql = f"SELECT id, doc FROM {coll} WHERE {where}"
        if sort:
            parts = []
            for field, direction in sort:
                parts.append(f"{self._column_or_extract(coll, field)} "
                             f"{'DESC' if direction < 0 else 'ASC'}")
            sql += " ORDER BY " + ", ".join(parts)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
            if skip:
                sql += " OFFSET ?"
                params.append(int(skip))
        elif skip:
            sql += " LIMIT -1 OFFSET ?"
            params.append(int(skip))
        out = []
        for row in self.query(sql, params):
            doc = _loads(row["doc"], row["id"])
            if projection:
                _project(doc, projection)
            out.append(doc)
        return out

    def find_one(self, coll: str, query: dict, *,
                 sort: Optional[list[tuple[str, int]]] = None) -> Optional[dict]:
        rows = self.find(coll, query, sort=sort, limit=1)
        return rows[0] if rows else None

    def count(self, coll: str, query: Optional[dict] = None) -> int:
        params: list = []
        where = self._match_where(coll, query or {}, params)
        rows = self.query(f"SELECT COUNT(*) AS n FROM {coll} WHERE {where}",
                          params)
        return int(rows[0]["n"]) if rows else 0

    def delete(self, coll: str, doc_id: str) -> int:
        return self.execute(f"DELETE FROM {coll} WHERE id = ?",
                            [doc_id]).rowcount

    def delete_many(self, coll: str, query: dict) -> int:
        params: list = []
        where = self._match_where(coll, query, params)
        return self.execute(f"DELETE FROM {coll} WHERE {where}", params).rowcount

    def update_many(self, coll: str, query: dict, fields: dict) -> int:
        """Mongo update_many with a $set of top-level fields.

        Returns the number of documents actually CHANGED, matching Mongo's
        modified_count rather than matched_count: callers such as the person
        merge report how many records moved, and a second merge of the same
        pair must move zero.
        """
        params: list = []
        where = self._match_where(coll, query, params)
        rows = self.query(f"SELECT id, doc FROM {coll} WHERE {where}", params)
        changed = 0
        for row in rows:
            doc = json.loads(row["doc"])
            wanted = {k: _canonicalize(v) for k, v in fields.items()}
            if all(doc.get(k) == v for k, v in wanted.items()):
                continue
            doc.update(fields)
            cols, blob = self._apply(coll, doc)
            assigns = ", ".join(f"{k} = ?" for k in cols)
            self.execute(
                f"UPDATE {coll} SET doc = ?, {assigns} WHERE id = ?",
                [blob] + list(cols.values()) + [row["id"]])
            changed += 1
        return changed

    # ------------------------------------------------------------------- FTS5
    def memory_text(self, doc: dict) -> str:
        parts = []
        for field in MEMORY_FTS_FIELDS:
            value = doc.get(field)
            if isinstance(value, list):
                parts.extend(str(v) for v in value)
            elif value:
                parts.append(str(value))
        return "\n".join(parts)

    def index_memory(self, memory_id: str, doc: dict) -> None:
        self.execute("DELETE FROM memories_fts WHERE memory_id = ?", [memory_id])
        self.execute(
            "INSERT INTO memories_fts (memory_id, text) VALUES (?, ?)",
            [memory_id, self.memory_text(doc)])

    def reindex_memories_fts(self) -> int:
        self.execute("DELETE FROM memories_fts")
        n = 0
        for row in self.query("SELECT id, doc FROM memories"):
            doc = _loads(row["doc"], None) or {}
            self.execute("INSERT INTO memories_fts (memory_id, text) VALUES (?, ?)",
                         [row["id"], self.memory_text(doc)])
            n += 1
        return n


# Timestamps are stored as text and compared as text, so every one of them has
# to look the same. Pydantic writes "...Z" and datetime.isoformat() writes
# "...+00:00", and "Z" (0x5A) sorts AFTER "+" (0x2B): a mixed column puts some
# records a second out of order, which quietly breaks every time window.
_ISO_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?"
    r"(Z|[+-]\d{2}:?\d{2})?$")


def canonical_time(value: Any) -> Any:
    """Normalise a timestamp string so lexical order is chronological."""
    if not isinstance(value, str) or not _ISO_DATETIME.match(value):
        return value
    text = value.replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        return text.replace("+00:00", "")   # naive: nothing to normalise
    return parsed.astimezone(timezone.utc).isoformat()


def _canonicalize(value: Any) -> Any:
    """Walk a document, canonicalising every timestamp on the way."""
    if isinstance(value, str):
        return canonical_time(value)
    if isinstance(value, dict):
        return {k: _canonicalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_canonicalize(v) for v in value]
    return value


def _loads(blob: str, doc_id: Optional[str]) -> dict:
    try:
        data = json.loads(blob)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if doc_id is not None:
        data["_id"] = doc_id
    return data


def _project(doc: dict, projection: dict) -> None:
    """Honour the one Mongo projection Circle uses: exclude these fields."""
    for key, keep in projection.items():
        if isinstance(keep, dict):
            continue
        if keep == 0:
            doc.pop(key, None)


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def now_iso() -> str:
    """Current time in the same canonical form stored timestamps use."""
    return canonical_time(datetime.now(timezone.utc).isoformat())


def unique_id(prefix: str) -> str:
    return prefix + datetime.now(timezone.utc).strftime("%H%M%S%f")


_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")