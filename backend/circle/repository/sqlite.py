"""SQLite implementation of the repository layer.

The only storage engine. Every method is a small, direct SQL operation, so the
behaviour difference is a bug in one method, not in the whole app.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np

from circle.config import Settings, get_settings
from circle.domain.models import (
    CalendarEvent, Conversation, Document, Email, ImportJob, MediaAttachment,
    Memory, Message, Note, Person, RelationshipEvent, RelationshipProfile,
    Source, VoiceRecording,
)
from circle.repository import serde
from circle.repository.serde import clean_profile_topics, deserialize, serialize
from circle.repository.sqlite_core import (
    SqliteEngine, canonical_time, now_iso, unique_id,
)

log = logging.getLogger("circle.repository.sqlite")

SUFFIXES = ("ing", "ed", "es", "s")


class SQLiteStore:
    """Concrete repositories over one local SQLite database file."""

    def __init__(self, settings: Optional[Settings] = None,
                 path: Optional[str] = None):
        self.settings = settings or get_settings()
        self.path = path or self.settings.sqlite_path()
        self.engine = SqliteEngine(self.path)
        self._refresh_person_names()

    # ------------------------------------------------------------------ basics
    def ping(self) -> bool:
        try:
            self.engine.query("SELECT 1")
            return True
        except Exception:
            return False

    def close(self) -> None:
        self.engine.close()

    def engine_name(self) -> str:
        return "sqlite"

    def _refresh_person_names(self) -> None:
        try:
            serde.refresh_person_names(
                (r["id"], r["display_name"])
                for r in self.engine.query(
                    "SELECT id, display_name FROM people"))
        except Exception as e:
            log.warning("person name cache unavailable: %s", e)

    # ======================= People ==================================
    def list_people(self, limit: int = 200, skip: int = 0) -> list[Person]:
        return [deserialize(Person, d) for d in self.engine.find(
            "people", sort=[("updated_at", -1)], limit=limit, skip=skip)]

    def get_person(self, person_id: str) -> Optional[Person]:
        return deserialize(Person, self.engine.get("people", person_id))

    def find_person_by_identity(self, kind: str, value: str) -> Optional[Person]:
        return deserialize(Person, self.engine.find_one("people", {
            "identities": {"$elemMatch": {"kind": kind, "value": value}}}))

    def insert_person(self, person: Person) -> Person:
        data = serialize(person)
        data["_id"] = person.id or (
            person.display_name.lower().replace(" ", "-") + "-"
            + datetime.now(timezone.utc).strftime("%H%M%S%f"))
        if not self.engine.insert("people", data):
            data["_id"] = data["_id"] + "-2"
            self.engine.upsert("people", data)
        # Targeted cache write: a full rescan per insert would be O(n^2)
        # across a large import, and the profile read path filters topics
        # against the person's own name.
        name = str(person.display_name or "").strip().lower()
        if name:
            serde.person_name_cache[data["_id"]] = name
        return person.model_copy(update={"id": data["_id"]})

    def update_person(self, person: Person) -> None:
        if not person.id:
            return
        person.updated_at = datetime.now(timezone.utc)
        data = serialize(person)
        data["_id"] = person.id
        self.engine.upsert("people", data)
        self._refresh_person_names()

    def delete_person(self, person_id: str) -> None:
        self.engine.delete("people", person_id)
        self.engine.delete("profiles", person_id)
        self._refresh_person_names()

    def count_people(self) -> int:
        return self.engine.count("people")

    # ================== Conversations / Messages =====================
    def get_conversation(self, cid: str) -> Optional[Conversation]:
        return deserialize(Conversation, self.engine.get("conversations", cid))

    def find_conversation(self, source: str, external_key: str
                          ) -> Optional[Conversation]:
        return deserialize(Conversation, self.engine.find_one(
            "conversations", {"source": source, "external_key": external_key}))

    def insert_conversation(self, conv: Conversation) -> Conversation:
        data = serialize(conv)
        external_key = (data.get("external_key") or data.get("title")
                        or conv.id or "default")
        data["external_key"] = external_key
        data["_id"] = conv.id or f"{conv.source.value}:{external_key}"[:200]
        if not self.engine.insert("conversations", data):
            pass   # Mongo's $setOnInsert: an existing chat is left untouched
        return conv.model_copy(update={"id": data["_id"]})

    def update_conversation(self, conv: Conversation) -> None:
        if not conv.id:
            return
        data = serialize(conv)
        data["_id"] = conv.id
        self.engine.upsert("conversations", data)

    def list_conversations_for_person(self, person_id: str
                                      ) -> list[Conversation]:
        return [deserialize(Conversation, d) for d in self.engine.find(
            "conversations", {"participant_ids": person_id})]

    def insert_message_if_new(self, message: Message) -> bool:
        data = serialize(message)
        data["_id"] = message.id
        return self.engine.insert("messages", data)

    def list_messages_for_person(self, person_id: str,
                                 limit: int = 100) -> list[Message]:
        return [deserialize(Message, d) for d in self.engine.find(
            "messages", {"person_id": person_id},
            sort=[("sent_at", -1)], limit=limit)]

    def list_messages_for_conversation(self, conversation_id: str,
                                       limit: int = 500) -> list[Message]:
        return [deserialize(Message, d) for d in self.engine.find(
            "messages", {"conversation_id": conversation_id},
            sort=[("sent_at", 1)], limit=limit)]

    def count_messages(self) -> int:
        return self.engine.count("messages")

    def get_message(self, message_id: str) -> Optional[dict]:
        """The raw stored message document (evidence panel shows it as-is)."""
        doc = self.engine.get("messages", message_id)
        if doc is not None:
            doc.pop("_id", None)
        return doc

    def attach_person_to_sourceless_messages(self) -> int:
        return self.engine.update_many(
            "messages", {"person_id": None}, {"person_id": None})

    # ========================= Emails ================================
    def insert_email_if_new(self, email: Email) -> bool:
        data = serialize(email)
        data["_id"] = email.id or email.external_id
        return self.engine.insert("emails", data)

    def list_emails_for_person(self, person_id: str, limit: int = 100
                               ) -> list[Email]:
        return [deserialize(Email, d) for d in self.engine.find(
            "emails", {"person_id": person_id}, sort=[("sent_at", -1)],
            limit=limit)]

    def get_email(self, email_id: str) -> Optional[Email]:
        return deserialize(Email, self.engine.get("emails", email_id))

    # ======================== Calendar ===============================
    def insert_calendar_if_new(self, event: CalendarEvent) -> bool:
        data = serialize(event)
        data["_id"] = event.id or event.external_id
        return self.engine.insert("calendar_events", data)

    def list_calendar_for_person(self, person_id: str, limit: int = 100
                                 ) -> list[CalendarEvent]:
        return [deserialize(CalendarEvent, d) for d in self.engine.find(
            "calendar_events", {"person_ids": person_id},
            sort=[("starts_at", -1)], limit=limit)]

    def list_calendar_upcoming(self, person_id: str, limit: int = 20
                               ) -> list[CalendarEvent]:
        return [deserialize(CalendarEvent, d) for d in self.engine.find(
            "calendar_events", {"person_ids": person_id,
                                "starts_at": {"$gte": now_iso()}},
            sort=[("starts_at", 1)], limit=limit)]

    def get_calendar(self, event_id: str) -> Optional[CalendarEvent]:
        return deserialize(CalendarEvent,
                           self.engine.get("calendar_events", event_id))

    # ========================== Notes ================================
    def insert_note_if_new(self, note: Note) -> bool:
        data = serialize(note)
        data["_id"] = note.id or note.external_id
        return self.engine.insert("notes", data)

    def insert_note(self, note: Note) -> Note:
        data = serialize(note)
        data["_id"] = note.id or unique_id("note-")
        self.engine.upsert("notes", data)
        return note.model_copy(update={"id": data["_id"]})

    def list_notes_for_person(self, person_id: str, limit: int = 100
                              ) -> list[Note]:
        return [deserialize(Note, d) for d in self.engine.find(
            "notes", {"person_id": person_id}, sort=[("noted_at", -1)],
            limit=limit)]

    def list_notes_recent(self, limit: int = 100) -> list[Note]:
        return [deserialize(Note, d) for d in self.engine.find(
            "notes", sort=[("noted_at", -1)], limit=limit)]

    def get_note(self, note_id: str) -> Optional[Note]:
        return deserialize(Note, self.engine.get("notes", note_id))

    # ==================== Voice recordings ===========================
    def insert_voice_if_new(self, rec: VoiceRecording) -> bool:
        data = serialize(rec)
        data["_id"] = rec.id or rec.checksum or rec.external_id
        return self.engine.insert("voice_recordings", data)

    def update_voice(self, rec: VoiceRecording) -> None:
        if not rec.id:
            return
        data = serialize(rec)
        data["_id"] = rec.id
        self.engine.upsert("voice_recordings", data)

    def list_voice_for_person(self, person_id: str, limit: int = 50
                              ) -> list[VoiceRecording]:
        return [deserialize(VoiceRecording, d) for d in self.engine.find(
            "voice_recordings", {"person_id": person_id},
            sort=[("recorded_at", -1)], limit=limit)]

    def list_voice_recent(self, limit: int = 50) -> list[VoiceRecording]:
        return [deserialize(VoiceRecording, d) for d in self.engine.find(
            "voice_recordings", sort=[("imported_at", -1)], limit=limit)]

    def get_voice(self, rec_id: str) -> Optional[VoiceRecording]:
        return deserialize(VoiceRecording,
                           self.engine.get("voice_recordings", rec_id))

    # ===================== Media attachments ========================
    def insert_media_if_new(self, media: MediaAttachment) -> bool:
        data = serialize(media)
        data["_id"] = media.id or "media-" + (media.checksum or "")[:24]
        return self.engine.insert("media", data)

    def update_media(self, media: MediaAttachment) -> None:
        if not media.id:
            return
        data = serialize(media)
        data["_id"] = media.id
        self.engine.upsert("media", data)

    def get_media(self, media_id: str) -> Optional[MediaAttachment]:
        return deserialize(MediaAttachment, self.engine.get("media", media_id))

    def find_media_by_filename_key(self, key: str) -> Optional[MediaAttachment]:
        if not key:
            return None
        return deserialize(MediaAttachment,
                           self.engine.find_one("media", {"filename_key": key}))

    def list_media_for_person(self, person_id: str,
                              limit: int = 200) -> list[MediaAttachment]:
        return [deserialize(MediaAttachment, d) for d in self.engine.find(
            "media", {"person_id": person_id}, sort=[("occurred_at", -1)],
            limit=limit)]

    def list_media_for_message(self, message_id: str) -> list[MediaAttachment]:
        return [deserialize(MediaAttachment, d) for d in self.engine.find(
            "media", {"message_id": message_id})]

    def list_media(self, limit: int = 200) -> list[MediaAttachment]:
        return [deserialize(MediaAttachment, d) for d in self.engine.find(
            "media", sort=[("imported_at", -1)], limit=limit)]

    def count_media(self) -> int:
        return self.engine.count("media")

    def find_messages_by_attachment_key(self, key: str,
                                        limit: int = 50) -> list[Message]:
        """Messages whose export referenced this media filename (late media)."""
        if not key:
            return []
        return [deserialize(Message, d) for d in self.engine.find(
            "messages", {"attachments": {"$elemMatch": {"filename_key": key}}},
            limit=limit)]

    def update_message_media_ids(self, message_id: str,
                                 media_ids: list[str]) -> None:
        if not message_id:
            return
        self.engine.set_fields("messages", message_id, {"media_ids": media_ids})
    # ======================== Documents ==============================
    def insert_document_if_new(self, doc: Document) -> bool:
        data = serialize(doc)
        data["_id"] = doc.id or doc.external_id
        return self.engine.insert("documents", data)

    def list_documents_for_person(self, person_id: str, limit: int = 50
                                  ) -> list[Document]:
        return [deserialize(Document, d) for d in self.engine.find(
            "documents", {"person_id": person_id}, sort=[("imported_at", -1)],
            limit=limit)]

    def get_document(self, doc_id: str) -> Optional[Document]:
        return deserialize(Document, self.engine.get("documents", doc_id))

    # ========================== Sources ==============================
    def insert_source(self, source: Source) -> Source:
        data = serialize(source)
        data["_id"] = source.id or source.checksum[:32]
        self.engine.upsert("sources", data)
        return source.model_copy(update={"id": data["_id"]})

    def get_source(self, source_id: str) -> Optional[Source]:
        return deserialize(Source, self.engine.get("sources", source_id))

    def find_source_by_checksum(self, checksum: str) -> Optional[Source]:
        return deserialize(
            Source, self.engine.find_one("sources", {"checksum": checksum}))

    # ======================== Import jobs ============================
    def insert_job(self, job: ImportJob) -> ImportJob:
        data = serialize(job)
        data["_id"] = job.id or unique_id("job-")
        self.engine.upsert("import_jobs", data)
        return job.model_copy(update={"id": data["_id"]})

    def update_job(self, job: ImportJob) -> None:
        if not job.id:
            return
        data = serialize(job)
        data["_id"] = job.id
        self.engine.upsert("import_jobs", data)

    def get_job(self, job_id: str) -> Optional[ImportJob]:
        return deserialize(ImportJob, self.engine.get("import_jobs", job_id))

    def list_jobs(self, limit: int = 50) -> list[ImportJob]:
        return [deserialize(ImportJob, d) for d in self.engine.find(
            "import_jobs", sort=[("created_at", -1)], limit=limit)]

    def resume_incomplete_jobs(self) -> list[ImportJob]:
        jobs = [deserialize(ImportJob, d) for d in self.engine.find(
            "import_jobs", {"status": "PROCESSING"})]
        for job in jobs:
            job.status = "QUEUED"
            job.error = ""
            self.update_job(job)
        return jobs

    def count_jobs_by_status(self) -> dict[str, int]:
        return {str(r["status"]): int(r["n"]) for r in self.engine.query(
            "SELECT status, COUNT(*) AS n FROM import_jobs GROUP BY status")}

    # ========================== Memories =============================
    def insert_memories(self, memories: list[Memory]) -> int:
        inserted = 0
        for mem in memories:
            data = serialize(mem)
            data["_id"] = mem.id or mem.record_id + ":" + (mem.summary
                                                           or mem.text)[:40]
            if not self.engine.insert("memories", data):
                continue
            self.engine.index_memory(data["_id"], data)
            inserted += 1
        return inserted

    def vector_search(self, vector: list[float], filters: dict[str, Any],
                      k: int = 12) -> list[tuple[Memory, float]]:
        query = {kk: vv for kk, vv in filters.items() if vv is not None}
        params: list = []
        where = self.engine.match_where("memories", query, params)
        rows = self.engine.query(
            "SELECT id, doc, embedding FROM memories "
            f"WHERE {where} LIMIT 5000", params)
        if not rows:
            return []
        keep_ids: list[str] = []
        vectors: list[Any] = []
        docs: dict[str, str] = {}
        for row in rows:
            if not row["embedding"]:
                continue
            keep_ids.append(row["id"])
            vectors.append(np.frombuffer(row["embedding"], dtype=np.float32))
            docs[row["id"]] = row["doc"]
        if not keep_ids:
            return []
        mat = np.array(vectors, dtype=np.float32)
        q = np.array(vector, dtype=np.float32)
        qn = q / (np.linalg.norm(q) or 1.0)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        sims = (mat / norms) @ qn
        out: list[tuple[Memory, float]] = []
        for i in np.argsort(-sims)[:k]:
            mid = keep_ids[int(i)]
            mem = deserialize(Memory, json.loads(docs[mid]))
            if mem:
                mem.embedding = self.engine.decode_embedding(
                    self._embedding_blob(mid))
                out.append((mem, float(sims[int(i)])))
        return out

    def _embedding_blob(self, memory_id: str):
        rows = self.engine.query(
            "SELECT embedding FROM memories WHERE id = ?", [memory_id])
        return rows[0]["embedding"] if rows else None

    def keyword_search(self, query: str, filters: dict[str, Any],
                       k: int = 12) -> list[tuple[Memory, float]]:
        """Lexical retrieval.

        A natural-language question carries mostly stopwords ("what did we
        discuss about..."), so the meaningful terms are OR'd into one FTS5
        query. bm25 returns a rank, not a comparable score, which is the same
        shape hybrid_retrieve fuses on.
        """
        terms = serde.content_terms(query)
        if not terms:
            return []
        base = {kk: vv for kk, vv in filters.items() if vv is not None}
        params: list = []
        # Alias the memories table as "m": the FTS join needs both, and the
        # translator qualifies columns with the alias when one is given.
        where = self.engine.match_where("memories", base, params, alias="m")
        match = " OR ".join(f'"{t}"' for t in terms)
        rows: list = []
        try:
            rows = self.engine.query(
                "SELECT m.id AS id, m.doc AS doc, bm25(memories_fts) AS rank "
                "FROM memories_fts JOIN memories m ON m.id = memories_fts.memory_id "
                f"WHERE memories_fts MATCH ? AND {where} "
                "ORDER BY rank LIMIT ?", [match] + params + [k])
        except Exception as e:
            log.debug("fts query failed, widening to LIKE: %s", e)
        if not rows:
            # Widening pass: the porter stemmer maps "promises" to "promis",
            # which FTS also stems, but LIKE covers tokenizer edge cases.
            likes = " OR ".join(["m.doc LIKE ?"] * len(terms))
            like_params = [f"%{t}%" for t in terms]
            rows = self.engine.query(
                "SELECT m.id AS id, m.doc AS doc FROM memories m "
                f"WHERE ({likes}) AND {where} LIMIT ?",
                like_params + params + [k * 3])
        out: list[tuple[Memory, float]] = []
        for i, row in enumerate(rows):
            mem = deserialize(Memory, json.loads(row["doc"]))
            if mem:
                rank = row["rank"] if "rank" in row.keys() else None
                out.append((mem, float(-rank) if rank is not None
                            else 1.0 / (i + 1)))
        return out[:k]

    def memories_for_person(self, person_id: str, limit: int = 500) -> list[Memory]:
        return [deserialize(Memory, d) for d in self.engine.find(
            "memories", {"person_id": person_id}, sort=[("occurred_at", -1)],
            limit=limit)]

    def get_memory(self, memory_id: str) -> Optional[Memory]:
        doc = self.engine.get("memories", memory_id)
        if not doc:
            return None
        mem = deserialize(Memory, doc)
        if mem:
            mem.embedding = self.engine.decode_embedding(
                self._embedding_blob(memory_id))
        return mem

    def count_memories(self) -> int:
        return self.engine.count("memories")

    def all_embeddings(self) -> list[tuple[str, list[float]]]:
        out: list[tuple[str, list[float]]] = []
        for row in self.engine.query(
                "SELECT id, embedding FROM memories "
                "WHERE embedding IS NOT NULL"):
            vec = self.engine.decode_embedding(row["embedding"])
            if vec:
                out.append((row["id"], vec))
        return out

    def update_memories_person(self, record_id: str, person_id: str) -> int:
        """Re-point one record's memories at a person (voice associate)."""
        return self.engine.update_many(
            "memories", {"record_id": record_id}, {"person_id": person_id})

    # ====================== Relationship =============================
    def insert_relationship_event(self, event: RelationshipEvent) -> None:
        data = serialize(event)
        data["_id"] = event.id or (unique_id("evt-")
                                   + "-" + (event.record_id or "")[:8])
        if not self.engine.insert("relationship_events", data):
            self.engine.upsert("relationship_events", data)

    def list_relationship_events(self, person_id: str, limit: int = 200
                                 ) -> list[dict]:
        return self.engine.find("relationship_events", {"person_id": person_id},
                                sort=[("occurred_at", -1)], limit=limit)

    @staticmethod
    def _window_clause(since, until=None) -> tuple[str, list]:
        """Bounded occurred_at range.

        Bounded at both ends on purpose: exports routinely carry timestamps in
        the future, and an open lower bound would count those as "this week".
        """
        now = datetime.now(timezone.utc)
        low = canonical_time((since or datetime(1970, 1, 1, tzinfo=timezone.utc)
                              ).isoformat())
        return ("occurred_at >= ? AND occurred_at <= ?",
                [low, canonical_time((until or now).isoformat())])

    def interaction_totals(self, limit: int = 10, since=None
                           ) -> list[tuple[str, int, Optional[str]]]:
        """Ranked (person_id, count, last_interaction_iso) by volume.

        Aggregate questions ("who do I talk to most") must be answered from
        these counts, not from whichever messages match the keywords.

        With no `since` the whole archive is ranked, future-dated records
        included: this is the lifetime leaderboard, not a period question.
        Only an explicit window bounds the far end, so exports that carry
        tomorrow's timestamps still count for who they are.
        """
        where, params = ("1=1", []) if since is None else self._window_clause(since)
        rows = self.engine.query(
            "SELECT person_id, COUNT(*) AS n, MAX(occurred_at) AS last "
            f"FROM relationship_events WHERE {where} "
            "AND person_id IS NOT NULL AND person_id != '' "
            "GROUP BY person_id ORDER BY n DESC, person_id ASC LIMIT ?",
            params + [limit])
        return [(str(r["person_id"]), int(r["n"]), r["last"]) for r in rows]

    def activity_totals(self, since=None, person_id: Optional[str] = None,
                        source: Optional[str] = None) -> dict[str, int]:
        """Interaction counts split by kind and source over a time window.

        "How many messages did I send last week" is a group-by, not a
        generation task.
        """
        where, params = self._window_clause(since)
        if person_id:
            where += " AND person_id = ?"
            params.append(person_id)
        if source:
            where += " AND source = ?"
            params.append(source)
        by_kind: dict[str, int] = {}
        by_source: dict[str, int] = {}
        for r in self.engine.query(
                "SELECT kind, source, COUNT(*) AS n FROM relationship_events "
                f"WHERE {where} GROUP BY kind, source", params):
            k = str(r["kind"] or "unknown")
            s = str(r["source"] or "unknown")
            n = int(r["n"])
            by_kind[k] = by_kind.get(k, 0) + n
            by_source[s] = by_source.get(s, 0) + n
        return {"total": sum(by_kind.values()), "by_kind": by_kind,
                "by_source": by_source}

    def distinct_people(self, since=None, limit: int = 100000) -> int:
        """How many distinct counterparties appear in the window.

        Counting rows in the archive is not the same as counting people: 336
        people produce 120,814 messages.
        """
        where, params = self._window_clause(since)
        where += " AND person_id IS NOT NULL AND person_id != ''"
        rows = self.engine.query(
            "SELECT COUNT(DISTINCT person_id) AS n FROM relationship_events "
            f"WHERE {where}", params)
        return min(int(rows[0]["n"]), limit)

    def last_interaction(self, person_id: Optional[str] = None
                         ) -> Optional[str]:
        """Most recent interaction timestamp, optionally for one person."""
        where, params = "1=1", []
        if person_id:
            where, params = "person_id = ?", [person_id]
        rows = self.engine.query(
            "SELECT MAX(occurred_at) AS last FROM relationship_events "
            f"WHERE {where}", params)
        return rows[0]["last"] if rows and rows[0]["last"] else None

    def recent_people(self, since, limit: int = 10
                      ) -> list[tuple[str, int, Optional[str]]]:
        """People active in the window, ranked by interactions in it."""
        return self.interaction_totals(limit=limit, since=since)

    _TOPIC_NAME_SCAN = 5000

    def topic_totals(self, limit: int = 10) -> list[tuple[str, int]]:
        """Most-discussed topics across every profile.

        Profiles carry per-person topic counts, so the global ranking is a sum
        over stored values. Asking the model to name your top topics makes it
        invent them from whichever topic words appear in the evidence.
        """
        from circle.relationship.metrics import _is_noise_topic
        known: set[str] = set()
        for p in self.list_people(limit=self._TOPIC_NAME_SCAN):
            if not p.display_name:
                continue
            low = p.display_name.strip().lower()
            known.add(low)
            # "Gladwin R" produces the topic "Gladwin": compare on tokens too.
            for token in re.split(r"[^a-z0-9]+", low):
                if len(token) >= 3:
                    known.add(token)
            for alias in p.aliases or []:
                al = str(alias).strip().lower()
                if al:
                    known.add(al)
        totals: dict[str, int] = {}
        for doc in self.engine.find("profiles"):
            for t in doc.get("topics") or []:
                topic = str(t.get("topic") or "").strip()
                if topic and not _is_noise_topic(topic, known):
                    totals[topic] = totals.get(topic, 0) + int(t.get("count") or 0)
        return sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]

    def event_counts_by_person(self) -> dict[str, int]:
        """Total interactions per person, for duplicate-name grouping."""
        return {str(r["person_id"]): int(r["n"]) for r in self.engine.query(
            "SELECT person_id, COUNT(*) AS n FROM relationship_events "
            "WHERE person_id IS NOT NULL GROUP BY person_id")}

    def all_people_docs(self) -> list[dict]:
        return self.engine.find("people")

    # ========================= Profiles ==============================
    def get_profile(self, person_id: str) -> Optional[RelationshipProfile]:
        doc = self.engine.get("profiles", person_id)
        if not doc:
            return None
        doc = dict(doc)
        doc["person_id"] = doc.pop("_id", person_id)
        profile = deserialize(RelationshipProfile, doc)
        if profile:
            clean_profile_topics(profile)
        return profile

    def upsert_profile(self, profile: RelationshipProfile) -> None:
        data = serialize(profile)
        data["_id"] = profile.person_id
        data["person_id"] = profile.person_id
        self.engine.upsert("profiles", data)

    def list_profiles(self) -> list[RelationshipProfile]:
        out: list[RelationshipProfile] = []
        for doc in self.engine.find("profiles"):
            doc = dict(doc)
            doc["person_id"] = doc.pop("_id", None)
            prof = deserialize(RelationshipProfile, doc)
            if prof:
                clean_profile_topics(prof)
                out.append(prof)
        return out

    # ==================== Processing state ===========================
    def already_processed(self, checksum: str) -> bool:
        return self.engine.find_one("processed_files",
                                    {"checksum": checksum}) is not None

    def mark_processed(self, checksum: str, path: str, job_id: str) -> None:
        self.engine.upsert("processed_files", {
            "_id": checksum, "checksum": checksum, "path": path,
            "job_id": job_id, "processed_at": now_iso()})

    def get_processed_by_path(self, path: str) -> Optional[dict]:
        return self.engine.find_one("processed_files", {"path": path})

    # ========================= Settings ==============================
    def get_setting(self, key: str) -> Any:
        doc = self.engine.get("app_settings", key)
        return doc.get("value") if doc else None

    def set_setting(self, key: str, value: Any) -> None:
        self.engine.upsert("app_settings", {"_id": key, "value": value})

    # ================= Identity suggestions =========================
    # Exposed through the store so the storage choice stays inside the
    # repository layer instead of leaking into the resolver.
    def upsert_identity_suggestion(self, suggestion_id: str,
                                   fields: dict[str, Any]) -> None:
        existing = self.engine.get("identity_suggestions", suggestion_id) or {}
        existing.update(fields)
        existing["_id"] = suggestion_id
        self.engine.upsert("identity_suggestions", existing)

    def get_identity_suggestion(self, suggestion_id: str) -> Optional[dict]:
        return self.engine.get("identity_suggestions", suggestion_id)

    def list_identity_suggestions(self, status: str = "pending") -> list[dict]:
        return self.engine.find("identity_suggestions", {"status": status})

    def set_identity_suggestion_status(self, suggestion_id: str,
                                       status: str) -> None:
        self.engine.set_fields("identity_suggestions", suggestion_id,
                               {"status": status})

    def search_messages(self, query: dict, limit: int = 50) -> list[dict]:
        """Messages matching a match clause, newest first."""
        return self.engine.find("messages", query, sort=[("sent_at", -1)],
                                limit=limit)

    def count_messages_matching(self, query: dict) -> int:
        return self.engine.count("messages", query)

    def reassign_person_records(self, keep_id: str, remove_id: str,
                                collections: dict[str, str]) -> int:
        """Move every record's person pointer from one person to another."""
        moved = 0
        for coll, field in collections.items():
            moved += self.engine.update_many(coll, {field: remove_id},
                                             {field: keep_id})
        return moved

    def replace_array_member(self, coll: str, field: str, old: str,
                             new: str) -> int:
        """Replace a whole array field wherever it holds `old`."""
        rows = self.engine.find(coll, {field: old})
        for doc in rows:
            self.engine.set_fields(coll, doc["_id"], {field: [new]})
        return len(rows)

    def pull_array_member(self, coll: str, field: str, value: str) -> int:
        """Remove one element from a JSON array column (Mongo $pull)."""
        rows = self.engine.find(coll, {field: value})
        for doc in rows:
            values = [v for v in (doc.get(field) or []) if v != value]
            self.engine.set_fields(coll, doc["_id"], {field: values})
        return len(rows)

    def delete_profile(self, person_id: str) -> int:
        return self.engine.delete("profiles", person_id)
