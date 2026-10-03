"""MongoDB implementation of the repository interfaces.

Works against a local MongoDB out of the box, and against MongoDB Atlas
Vector Search when MONGO_ATLAS=true (uses $vectorSearch with the configured
index). The local path pre-filters by metadata and ranks with cosine
similarity in-process -- same interface, same results.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

from circle.config import Settings, get_settings
from circle.domain.models import (
    CalendarEvent, Conversation, Document, Email, ImportJob, MediaAttachment,
    Memory, Message, Note, Person, RelationshipProfile, Source, VoiceRecording,
)
from circle.repository.base import (
    CalendarRepo, ConversationRepo, DocumentRepo, EmailRepo, JobRepo,
    MemoryRepo, MessageRepo, NoteRepo, PersonRepo, ProcessingStateRepo,
    ProfileRepo, SettingRepo, SourceRepo, VoiceRepo,
)
from circle.repository.serde import (
    _SUFFIXES, clean_profile_topics as _clean_profile_topics,
    content_terms as _content_terms, deserialize as _deserialize,
    person_name_cache as _PERSON_NAME_CACHE, refresh_person_names,
    serialize as _serialize,
)

_TEXT_INDEX_FIELDS = ["text", "summary", "topics"]

log = logging.getLogger("circle.repository")


class MongoStore:
    """Concrete repositories over one MongoDB database."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.client = MongoClient(
            self.settings.database_url,
            serverSelectionTimeoutMS=5000,
            uuidRepresentation="standard",
        )
        self.db = self.client[self.settings.database_name]
        self._lock = threading.Lock()
        self._ensure_indexes()
        self._refresh_person_names()

    def _refresh_person_names(self) -> None:
        """Cache display names so profile reads can drop self-named topics."""
        try:
            refresh_person_names(
                (str(doc["_id"]), doc.get("display_name"))
                for doc in self.db.people.find(
                    {}, {"display_name": 1, "aliases": 1}))
        except Exception as e:  # never block startup on a cache
            log.warning("person name cache unavailable: %s", e)

    # ------------------------------------------------------------------
    def ping(self) -> bool:
        try:
            self.client.admin.command("ping")
            return True
        except Exception:
            return False

    def _ensure_indexes(self) -> None:
        db = self.db
        db.people.create_index([("identities.kind", ASCENDING), ("identities.value", ASCENDING)])
        db.conversations.create_index([("source", ASCENDING), ("external_key", ASCENDING)], unique=True, sparse=True)
        db.messages.create_index([("person_id", ASCENDING), ("sent_at", DESCENDING)])
        db.messages.create_index([("conversation_id", ASCENDING), ("sent_at", ASCENDING)])
        db.emails.create_index([("person_id", ASCENDING), ("sent_at", DESCENDING)])
        db.calendar_events.create_index([("person_ids", ASCENDING), ("starts_at", ASCENDING)])
        db.notes.create_index([("person_id", ASCENDING), ("noted_at", DESCENDING)])
        db.voice_recordings.create_index([("checksum", ASCENDING)], unique=True, sparse=True)
        db.media.create_index([("message_id", ASCENDING)])
        db.media.create_index([("person_id", ASCENDING), ("occurred_at", DESCENDING)])
        db.media.create_index([("conversation_id", ASCENDING)])
        db.media.create_index([("filename_key", ASCENDING)])
        db.media.create_index([("checksum", ASCENDING)])
        db.documents.create_index([("external_id", ASCENDING)], unique=True, sparse=True)
        db.relationship_events.create_index([("person_id", ASCENDING), ("occurred_at", DESCENDING)])
        db.sources.create_index([("checksum", ASCENDING)], unique=True, sparse=True)
        db.import_jobs.create_index([("created_at", DESCENDING)])
        db.processed_files.create_index([("checksum", ASCENDING)], unique=True, sparse=True)
        db.processed_files.create_index([("path", ASCENDING)])
        db.profiles.create_index([("person_id", ASCENDING)], unique=True)
        try:
            db.memories.create_index([("person_id", ASCENDING), ("occurred_at", DESCENDING)])
            db.memories.create_index([("source", ASCENDING)])
            db.memories.create_index([("record_id", ASCENDING)])
            # keyword (lexical) retrieval support
            existing = [i.get("name") for i in db.memories.list_indexes()]
            if "memory_text" not in existing:
                db.memories.create_index(
                    [(f, "text") for f in _TEXT_INDEX_FIELDS],
                    name="memory_text",
                    default_language="english",
                )
        except Exception:
            pass

    # ======================= People ==================================
    def list_people(self, limit: int = 200, skip: int = 0) -> list[Person]:
        return [_deserialize(Person, d) for d in
                self.db.people.find().sort("updated_at", -1).skip(skip).limit(limit)]

    def get_person(self, person_id: str) -> Optional[Person]:
        return _deserialize(Person, self.db.people.find_one({"_id": person_id}))

    def find_person_by_identity(self, kind: str, value: str) -> Optional[Person]:
        return _deserialize(Person, self.db.people.find_one(
            {"identities": {"$elemMatch": {"kind": kind, "value": value}}}))

    def insert_person(self, person: Person) -> Person:
        data = _serialize(person)
        data["_id"] = person.id or person.display_name.lower().replace(" ", "-") + "-" + \
            datetime.now(timezone.utc).strftime("%H%M%S%f")
        data["id"] = data["_id"]
        with self._lock:
            try:
                self.db.people.insert_one({k: v for k, v in data.items() if k != "id"})
            except DuplicateKeyError:
                # fall back: append suffix
                data["_id"] = data["_id"] + "-2"
                data["id"] = data["_id"]
                self.db.people.insert_one({k: v for k, v in data.items() if k != "id"})
            # Targeted cache write: a full rescan per insert would be O(n^2)
            # across a large import, and this profile read path filters topics
            # against the person's own name.
            name = str(person.display_name or "").strip().lower()
            if name:
                _PERSON_NAME_CACHE[data["_id"]] = name
        return person.model_copy(update={"id": data["id"]})

    def update_person(self, person: Person) -> None:
        if not person.id:
            return
        person.updated_at = datetime.now(timezone.utc)
        data = _serialize(person)
        data.pop("id", None)
        self.db.people.update_one({"_id": person.id}, {"$set": data}, upsert=True)
        # A rename or new alias changes what counts as a self-named topic, so
        # refresh after the write rather than caching the pre-write name.
        self._refresh_person_names()

    def delete_person(self, person_id: str) -> None:
        self.db.people.delete_one({"_id": person_id})
        for coll in ("profiles",):
            self.db[coll].delete_one({"person_id": person_id})
        # Names changed: the topic filter reads from this cache.
        self._refresh_person_names()

    def count_people(self) -> int:
        return self.db.people.count_documents({})

    # ================== Conversations / Messages =====================
    def get_conversation(self, cid: str) -> Optional[Conversation]:
        return _deserialize(Conversation, self.db.conversations.find_one({"_id": cid}))

    def find_conversation(self, source: str, external_key: str) -> Optional[Conversation]:
        return _deserialize(Conversation, self.db.conversations.find_one(
            {"source": source, "external_key": external_key}))

    def insert_conversation(self, conv: Conversation) -> Conversation:
        data = _serialize(conv)
        external_key = data.pop("external_key", None) or data.get("title") or conv.id or "default"
        data["external_key"] = external_key
        data["_id"] = conv.id or f"{conv.source.value}:{external_key}"[:200]
        data["id"] = data["_id"]
        self.db.conversations.update_one(
            {"_id": data["_id"]}, {"$setOnInsert": {k: v for k, v in data.items() if k != "id"}},
            upsert=True)
        return conv.model_copy(update={"id": data["id"]})

    def update_conversation(self, conv: Conversation) -> None:
        if not conv.id:
            return
        data = _serialize(conv)
        data.pop("id", None)
        self.db.conversations.update_one({"_id": conv.id}, {"$set": data}, upsert=True)

    def list_conversations_for_person(self, person_id: str) -> list[Conversation]:
        return [_deserialize(Conversation, d) for d in
                self.db.conversations.find({"participant_ids": person_id})]

    def insert_message_if_new(self, message: Message) -> bool:
        data = _serialize(message)
        data["_id"] = message.id
        data.pop("id", None)
        try:
            self.db.messages.insert_one(data)
            return True
        except DuplicateKeyError:
            return False

    def list_messages_for_person(self, person_id: str, limit: int = 100) -> list[Message]:
        return [_deserialize(Message, d) for d in
                self.db.messages.find({"person_id": person_id})
                .sort("sent_at", -1).limit(limit)]

    def list_messages_for_conversation(self, conversation_id: str, limit: int = 500) -> list[Message]:
        return [_deserialize(Message, d) for d in
                self.db.messages.find({"conversation_id": conversation_id})
                .sort("sent_at", 1).limit(limit)]

    def count_messages(self) -> int:
        return self.db.messages.count_documents({})

    def attach_person_to_sourceless_messages(self) -> int:
        """Backfill person_id on messages after identity merges."""
        result = self.db.messages.update_many(
            {"person_id": None}, {"$set": {"person_id": None}})
        return result.modified_count

    # ========================= Emails ================================
    def insert_email_if_new(self, email: Email) -> bool:
        data = _serialize(email)
        data["_id"] = email.id or email.external_id
        data.pop("id", None)
        try:
            self.db.emails.insert_one(data)
            return True
        except DuplicateKeyError:
            return False

    def list_emails_for_person(self, person_id: str, limit: int = 100) -> list[Email]:
        return [_deserialize(Email, d) for d in
                self.db.emails.find({"person_id": person_id})
                .sort("sent_at", -1).limit(limit)]

    def get_email(self, email_id: str) -> Optional[Email]:
        return _deserialize(Email, self.db.emails.find_one({"_id": email_id}))

    # ======================== Calendar ===============================
    def insert_calendar_if_new(self, event: CalendarEvent) -> bool:
        data = _serialize(event)
        data["_id"] = event.id or event.external_id
        data.pop("id", None)
        try:
            self.db.calendar_events.insert_one(data)
            return True
        except DuplicateKeyError:
            return False

    def list_calendar_for_person(self, person_id: str, limit: int = 100) -> list[CalendarEvent]:
        return [_deserialize(CalendarEvent, d) for d in
                self.db.calendar_events.find({"person_ids": person_id})
                .sort("starts_at", -1).limit(limit)]

    def list_calendar_upcoming(self, person_id: str, limit: int = 20) -> list[CalendarEvent]:
        # stored datetimes are ISO strings (mode="json"): compare as strings
        now_iso = datetime.now(timezone.utc).isoformat()
        return [_deserialize(CalendarEvent, d) for d in
                self.db.calendar_events.find({"person_ids": person_id,
                                              "starts_at": {"$gte": now_iso}})
                .sort("starts_at", 1).limit(limit)]

    def get_calendar(self, event_id: str) -> Optional[CalendarEvent]:
        return _deserialize(CalendarEvent, self.db.calendar_events.find_one({"_id": event_id}))

    # ========================== Notes ================================
    def insert_note_if_new(self, note: Note) -> bool:
        data = _serialize(note)
        data["_id"] = note.id or note.external_id
        data.pop("id", None)
        try:
            self.db.notes.insert_one(data)
            return True
        except DuplicateKeyError:
            return False

    def insert_note(self, note: Note) -> Note:
        data = _serialize(note)
        data["_id"] = note.id or "note-" + datetime.now(timezone.utc).strftime("%H%M%S%f")
        data.pop("id", None)
        data["id"] = data["_id"]
        self.db.notes.insert_one({k: v for k, v in data.items() if k != "id"})
        return note.model_copy(update={"id": data["id"]})

    def list_notes_for_person(self, person_id: str, limit: int = 100) -> list[Note]:
        return [_deserialize(Note, d) for d in
                self.db.notes.find({"person_id": person_id})
                .sort("noted_at", -1).limit(limit)]

    def get_note(self, note_id: str) -> Optional[Note]:
        return _deserialize(Note, self.db.notes.find_one({"_id": note_id}))

    # ==================== Voice recordings ===========================
    def insert_voice_if_new(self, rec: VoiceRecording) -> bool:
        data = _serialize(rec)
        data["_id"] = rec.id or rec.checksum or rec.external_id
        data.pop("id", None)
        try:
            self.db.voice_recordings.insert_one(data)
            return True
        except DuplicateKeyError:
            return False

    def update_voice(self, rec: VoiceRecording) -> None:
        if not rec.id:
            return
        data = _serialize(rec)
        data.pop("id", None)
        self.db.voice_recordings.update_one({"_id": rec.id}, {"$set": data}, upsert=True)

    def list_voice_for_person(self, person_id: str, limit: int = 50) -> list[VoiceRecording]:
        return [_deserialize(VoiceRecording, d) for d in
                self.db.voice_recordings.find({"person_id": person_id})
                .sort("recorded_at", -1).limit(limit)]

    def get_voice(self, rec_id: str) -> Optional[VoiceRecording]:
        return _deserialize(VoiceRecording, self.db.voice_recordings.find_one({"_id": rec_id}))

    # ===================== Media attachments ========================
    def insert_media_if_new(self, media: MediaAttachment) -> bool:
        data = _serialize(media)
        data["_id"] = media.id or "media-" + (media.checksum or "")[:24]
        data.pop("id", None)
        try:
            self.db.media.insert_one(data)
            return True
        except DuplicateKeyError:
            return False

    def update_media(self, media: MediaAttachment) -> None:
        if not media.id:
            return
        data = _serialize(media)
        data.pop("id", None)
        self.db.media.update_one({"_id": media.id}, {"$set": data}, upsert=True)

    def get_media(self, media_id: str) -> Optional[MediaAttachment]:
        return _deserialize(MediaAttachment, self.db.media.find_one({"_id": media_id}))

    def find_media_by_filename_key(self, key: str) -> Optional[MediaAttachment]:
        if not key:
            return None
        return _deserialize(MediaAttachment,
                            self.db.media.find_one({"filename_key": key}))

    def list_media_for_person(self, person_id: str,
                              limit: int = 200) -> list[MediaAttachment]:
        return [_deserialize(MediaAttachment, d) for d in
                self.db.media.find({"person_id": person_id})
                .sort("occurred_at", -1).limit(limit)]

    def list_media_for_message(self, message_id: str) -> list[MediaAttachment]:
        return [_deserialize(MediaAttachment, d) for d in
                self.db.media.find({"message_id": message_id})]

    def list_media(self, limit: int = 200) -> list[MediaAttachment]:
        return [_deserialize(MediaAttachment, d) for d in
                self.db.media.find().sort("imported_at", -1).limit(limit)]

    def count_media(self) -> int:
        return self.db.media.count_documents({})

    def find_messages_by_attachment_key(self, key: str,
                                        limit: int = 50) -> list[Message]:
        """Messages whose export referenced this media filename (for late media)."""
        if not key:
            return []
        return [_deserialize(Message, d) for d in
                self.db.messages.find({"attachments.filename_key": key})
                .limit(limit)]

    def update_message_media_ids(self, message_id: str,
                                 media_ids: list[str]) -> None:
        if not message_id:
            return
        self.db.messages.update_one({"_id": message_id},
                                    {"$set": {"media_ids": list(media_ids)}})

    # ======================== Documents ==============================
    def insert_document_if_new(self, doc: Document) -> bool:
        data = _serialize(doc)
        data["_id"] = doc.id or doc.external_id
        data.pop("id", None)
        try:
            self.db.documents.insert_one(data)
            return True
        except DuplicateKeyError:
            return False

    def list_documents_for_person(self, person_id: str, limit: int = 50) -> list[Document]:
        return [_deserialize(Document, d) for d in
                self.db.documents.find({"person_id": person_id})
                .sort("imported_at", -1).limit(limit)]

    def get_document(self, doc_id: str) -> Optional[Document]:
        return _deserialize(Document, self.db.documents.find_one({"_id": doc_id}))

    # ========================== Sources ==============================
    def insert_source(self, source: Source) -> Source:
        data = _serialize(source)
        data["_id"] = source.id or source.checksum[:32]
        data.pop("id", None)
        data["id"] = data["_id"]
        try:
            self.db.sources.insert_one({k: v for k, v in data.items() if k != "id"})
        except DuplicateKeyError:
            pass
        return source.model_copy(update={"id": data["id"]})

    def get_source(self, source_id: str) -> Optional[Source]:
        return _deserialize(Source, self.db.sources.find_one({"_id": source_id}))

    def find_source_by_checksum(self, checksum: str) -> Optional[Source]:
        return _deserialize(Source, self.db.sources.find_one({"checksum": checksum}))

    # ======================== Import jobs ============================
    def insert_job(self, job: ImportJob) -> ImportJob:
        data = _serialize(job)
        data["_id"] = job.id or "job-" + datetime.now(timezone.utc).strftime("%H%M%S%f")
        data.pop("id", None)
        data["id"] = data["_id"]
        self.db.import_jobs.insert_one({k: v for k, v in data.items() if k != "id"})
        return job.model_copy(update={"id": data["id"]})

    def update_job(self, job: ImportJob) -> None:
        if not job.id:
            return
        data = _serialize(job)
        data.pop("id", None)
        self.db.import_jobs.update_one({"_id": job.id}, {"$set": data})

    def get_job(self, job_id: str) -> Optional[ImportJob]:
        return _deserialize(ImportJob, self.db.import_jobs.find_one({"_id": job_id}))

    def list_jobs(self, limit: int = 50) -> list[ImportJob]:
        return [_deserialize(ImportJob, d) for d in
                self.db.import_jobs.find().sort("created_at", -1).limit(limit)]

    def resume_incomplete_jobs(self) -> list[ImportJob]:
        jobs = [_deserialize(ImportJob, d) for d in
                self.db.import_jobs.find({"status": "PROCESSING"})]
        for job in jobs:
            job.status = "QUEUED"
            job.error = ""
            self.update_job(job)
        return jobs

    def count_jobs_by_status(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.db.import_jobs.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
            out[row["_id"]] = row["n"]
        return out

    # ========================== Memories =============================
    def insert_memories(self, memories: list[Memory]) -> int:
        inserted = 0
        for mem in memories:
            data = _serialize(mem)
            data["_id"] = mem.id or mem.record_id + ":" + (mem.summary or mem.text)[:40]
            data.pop("id", None)
            try:
                self.db.memories.insert_one(data)
                inserted += 1
            except DuplicateKeyError:
                continue
        return inserted

    def vector_search(self, vector: list[float], filters: dict[str, Any],
                      k: int = 12) -> list[tuple[Memory, float]]:
        query = {kk: vv for kk, vv in filters.items() if vv is not None}
        if self.settings.mongo_atlas:
            pipe = [{"$vectorSearch": {
                "index": self.settings.atlas_vector_index,
                "path": "embedding",
                "queryVector": vector,
                "numCandidates": max(k * 40, 400),
                "limit": k,
                "filter": query or None,
            }}]
            rows = list(self.db.memories.aggregate(pipe))
            out = []
            for i, row in enumerate(rows):
                mem = _deserialize(Memory, row)
                if mem:
                    out.append((mem, 1.0 - (i / max(len(rows), 1)) * 0.5))
            return out
        # Local path: metadata prefilter + cosine ranking in-process
        rows = list(self.db.memories.find(query, {"embedding": 1}).limit(5000))
        if not rows:
            return []
        id_emb = [(r["_id"], r.get("embedding")) for r in rows if r.get("embedding")]
        if not id_emb:
            return []
        keep_ids = [i for i, _ in id_emb]
        mat = np.array([e for _, e in id_emb], dtype=np.float32)
        q = np.array(vector, dtype=np.float32)
        qn = q / (np.linalg.norm(q) or 1.0)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        sims = (mat / norms) @ qn
        order = np.argsort(-sims)[:k]
        chosen_ids = [keep_ids[int(i)] for i in order]
        docs = list(self.db.memories.find({"_id": {"$in": chosen_ids}}))
        by_id = {d["_id"]: d for d in docs}
        out = []
        for mid in chosen_ids:
            doc = by_id.get(mid)
            if not doc:
                continue
            mem = _deserialize(Memory, doc)
            if mem:
                idx = keep_ids.index(mid)
                out.append((mem, float(sims[idx])))
        return out

    def keyword_search(self, query: str, filters: dict[str, Any],
                       k: int = 12) -> list[tuple[Memory, float]]:
        """Lexical retrieval.

        A natural-language question carries mostly stopwords ("what did we
        discuss about..."), and MongoDB's $text matches the whole string, so
        passing the question verbatim returns almost nothing. We therefore drop
        stopwords and search the meaningful terms, then fall back to a regex OR
        if the text index is unavailable.
        """
        terms = _content_terms(query)
        if not terms:
            return []
        base = {kk: vv for kk, vv in filters.items() if vv is not None}

        rows: list[dict] = []
        # Try $text with the content terms first (MongoDB stems and ORs them).
        try:
            rows = list(self.db.memories.find(
                {**base, "$text": {"$search": " ".join(terms)}},
                {"score": {"$meta": "textScore"}, "embedding": 0})
                .sort([("score", {"$meta": "textScore"})]).limit(k))
        except Exception:
            rows = []

        # Widening pass. $text cannot be combined with $or and returns nothing
        # when no single document matches, so fall back to per-term regex.
        # The original term is used too: my crude stemmer maps "promises" to
        # "promis", which regex would not match against "promises".
        patterns = []
        for t in terms:
            stem = t
            for suf in _SUFFIXES:
                if t.endswith(suf) and len(t) - len(suf) >= 3:
                    stem = t[: -len(suf)]
                    break
            for cand in dict.fromkeys([t, stem]):
                patterns.append({"text": {"$regex": re.escape(cand), "$options": "i"}})
        seen = {r["_id"] for r in rows}
        extra = [r for r in self.db.memories.find(
            {**base, "$or": patterns}, {"embedding": 0}).limit(k * 3)
            if r["_id"] not in seen]
        rows = rows + extra

        out: list[tuple[Memory, float]] = []
        for i, row in enumerate(rows[:k]):
            score = row.pop("score", None)
            mem = _deserialize(Memory, row)
            if mem:
                out.append((mem, float(score) if score else 1.0 / (i + 1)))
        return out

    def memories_for_person(self, person_id: str, limit: int = 500) -> list[Memory]:
        return [_deserialize(Memory, d) for d in
                self.db.memories.find({"person_id": person_id}, {"embedding": 0})
                .sort("occurred_at", -1).limit(limit)]

    def get_memory(self, memory_id: str) -> Optional[Memory]:
        doc = self.db.memories.find_one({"_id": memory_id})
        return _deserialize(Memory, doc)

    def count_memories(self) -> int:
        return self.db.memories.count_documents({})

    def all_embeddings(self) -> list[tuple[str, list[float]]]:
        return [(d["_id"], d.get("embedding") or [])
                for d in self.db.memories.find({}, {"embedding": 1})
                if d.get("embedding")]

    # ====================== Relationship =============================
    def insert_relationship_event(self, event) -> None:
        data = _serialize(event)
        data.pop("id", None)
        data["_id"] = event.id or "evt-" + datetime.now(timezone.utc).strftime("%H%M%S%f") + \
            "-" + (event.record_id or "")[:8]
        try:
            self.db.relationship_events.insert_one(data)
        except DuplicateKeyError:
            pass

    def list_relationship_events(self, person_id: str, limit: int = 200) -> list[dict]:
        return list(self.db.relationship_events.find({"person_id": person_id})
                    .sort("occurred_at", -1).limit(limit))

    def interaction_totals(self, limit: int = 10,
                           since: Optional[datetime] = None
                           ) -> list[tuple[str, int, Optional[str]]]:
        """Ranked (person_id, count, last_interaction_iso) by interaction volume.

        Aggregate questions ("who do I talk to most") must be answered from
        these counts, not from whichever messages happen to match the keywords.
        """
        match: dict[str, Any] = {}
        if since is not None:
            # Bounded both ends: "in the last 7 days" must not sweep in records
            # dated in the future (exports do carry those).
            match = dict(self._occurred_window(since))
        pipe: list[dict[str, Any]] = []
        if match:
            pipe.append({"$match": match})
        pipe += [
            {"$group": {
                "_id": "$person_id",
                "n": {"$sum": 1},
                "last": {"$max": "$occurred_at"},
            }},
            {"$sort": {"n": -1}},
            {"$limit": limit},
        ]
        return [(str(r["_id"]), int(r["n"]),
                 r.get("last").isoformat() if hasattr(r.get("last"), "isoformat")
                 else r.get("last"))
                for r in self.db.relationship_events.aggregate(pipe)]

    # How many people to scan when filtering topic phrases that are just
    # someone's name. Bounded so this stays a fixed-cost query.
    _TOPIC_NAME_SCAN = 5000

    # ================= Deterministic archive facts =====================
    #
    # These back the counting intents. They are aggregates over the archive,
    # which is the only place the true value exists: no language model can
    # count 103,745 events, and asking one to try produces a confident wrong
    # number copied out of whichever message it happened to retrieve.

    @staticmethod
    def _occurred_window(since: Optional[datetime],
                         until: Optional[datetime] = None) -> dict[str, Any]:
        """Bounded occurred_at range.

        Bounded at both ends on purpose: exports routinely carry timestamps in
        the future, and an open $gte would count those as "this week".
        """
        now = datetime.now(timezone.utc)
        return {"occurred_at": {"$gte": (since or datetime(1970, 1, 1,
                                                           tzinfo=timezone.utc)
                                         ).isoformat(),
                                "$lte": (until or now).isoformat()}}

    def activity_totals(self, since: Optional[datetime] = None,
                        person_id: Optional[str] = None,
                        source: Optional[str] = None
                        ) -> dict[str, int]:
        """Interaction counts split by kind and source over a time window.

        "How many messages did I send last week" is a group-by, not a
        generation task.
        """
        match: dict[str, Any] = dict(self._occurred_window(since))
        if person_id:
            match["person_id"] = person_id
        if source:
            match["source"] = source
        by_kind: dict[str, int] = {}
        by_source: dict[str, int] = {}
        for row in self.db.relationship_events.aggregate([
                {"$match": match},
                {"$group": {"_id": {"k": "$kind", "s": "$source"},
                            "n": {"$sum": 1}}}]):
            k = str(row["_id"].get("k") or "unknown")
            s = str(row["_id"].get("s") or "unknown")
            by_kind[k] = by_kind.get(k, 0) + int(row["n"])
            by_source[s] = by_source.get(s, 0) + int(row["n"])
        return {"total": sum(by_kind.values()), "by_kind": by_kind,
                "by_source": by_source}

    def distinct_people(self, since: Optional[datetime] = None,
                        limit: int = 100000) -> int:
        """How many distinct counterparties appear in the window.

        Counting rows in the archive is not the same as counting people: 336
        people produce 120,814 messages.
        """
        match: dict[str, Any] = dict(self._occurred_window(since))
        match["person_id"] = {"$nin": [None, ""]}
        return len(self.db.relationship_events.distinct("person_id", match)) \
            if limit >= 100000 else min(
                len(self.db.relationship_events.distinct("person_id", match)),
                limit)

    def last_interaction(self, person_id: Optional[str] = None
                         ) -> Optional[str]:
        """Most recent interaction timestamp, optionally for one person."""
        match: dict[str, Any] = {}
        if person_id:
            match["person_id"] = person_id
        doc = self.db.relationship_events.find_one(
            match, sort=[("occurred_at", -1)])
        if not doc:
            return None
        value = doc.get("occurred_at")
        return value.isoformat() if hasattr(value, "isoformat") else value

    def recent_people(self, since: Optional[datetime], limit: int = 10
                      ) -> list[tuple[str, int, Optional[str]]]:
        """People active in the window, ranked by interactions in it.

        Distinct from interaction_totals() only in intent: this answers "who
        did I talk to in the last 7 days", which has no superlative in it but
        is still a group-by and not a retrieval question.
        """
        return self.interaction_totals(limit=limit, since=since)

    def topic_totals(self, limit: int = 10) -> list[tuple[str, int]]:
        """Most-discussed topics across every profile.

        Profiles already carry per-person topic counts, so the global ranking
        is a sum over stored values. Asking the model to name your top topics
        makes it invent them from whatever topic words appear in the evidence.
        """
        # Filters existing stored topics too, so profiles written before the
        # noise filter existed do not need a re-import to become useful.
        from circle.relationship.metrics import _is_noise_topic
        known: set[str] = set()
        for p in self.list_people(limit=self._TOPIC_NAME_SCAN):
            if not p.display_name:
                continue
            low = p.display_name.strip().lower()
            known.add(low)
            # "Gladwin R" produces the topic "Gladwin": compare on tokens too,
            # otherwise every first name leaks through as a "topic".
            for token in re.split(r"[^a-z0-9]+", low):
                if len(token) >= 3:
                    known.add(token)
            for alias in p.aliases or []:
                al = str(alias).strip().lower()
                if al:
                    known.add(al)
        totals: dict[str, int] = {}
        for doc in self.db.profiles.find({}, {"topics": 1}):
            for t in doc.get("topics") or []:
                topic = str(t.get("topic") or "").strip()
                if topic and not _is_noise_topic(topic, known):
                    totals[topic] = totals.get(topic, 0) + int(t.get("count") or 0)
        ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked[:limit]

    # ========================= Profiles ==============================
    def get_profile(self, person_id: str) -> Optional[RelationshipProfile]:
        doc = self.db.profiles.find_one({"_id": person_id})
        if doc:
            person_id_val = doc.pop("_id", None)
            doc["person_id"] = person_id_val
        profile = _deserialize(RelationshipProfile, doc)
        if profile:
            _clean_profile_topics(profile)
        return profile

    def upsert_profile(self, profile: RelationshipProfile) -> None:
        data = _serialize(profile)
        data.pop("id", None)
        # person_id must be written: the profiles collection has a unique
        # index on it (missing field would collide as null on every doc).
        data["person_id"] = profile.person_id
        self.db.profiles.update_one(
            {"_id": profile.person_id}, {"$set": data}, upsert=True)

    def list_profiles(self) -> list[RelationshipProfile]:
        out = []
        for doc in self.db.profiles.find():
            pid = doc.pop("_id", None)
            doc["person_id"] = pid
            prof = _deserialize(RelationshipProfile, doc)
            if prof:
                _clean_profile_topics(prof)
                out.append(prof)
        return out

    # ==================== Processing state ===========================
    def already_processed(self, checksum: str) -> bool:
        return self.db.processed_files.find_one({"checksum": checksum}) is not None

    def mark_processed(self, checksum: str, path: str, job_id: str) -> None:
        try:
            self.db.processed_files.insert_one(
                {"checksum": checksum, "path": path, "job_id": job_id,
                 "processed_at": datetime.now(timezone.utc)})
        except DuplicateKeyError:
            pass

    def get_processed_by_path(self, path: str) -> Optional[dict]:
        return self.db.processed_files.find_one({"path": path})

    # ========================= Settings ==============================
    def get_setting(self, key: str) -> Any:
        doc = self.db.app_settings.find_one({"_id": key})
        return doc.get("value") if doc else None

    def set_setting(self, key: str, value: Any) -> None:
        self.db.app_settings.update_one({"_id": key}, {"$set": {"value": value}}, upsert=True)

    # ================= Identity suggestions =========================
    # Exposed through the store so the storage choice stays inside the
    # repository layer instead of leaking into the resolver.
    def upsert_identity_suggestion(self, suggestion_id: str,
                                   fields: dict[str, Any]) -> None:
        self.db.identity_suggestions.update_one(
            {"_id": suggestion_id}, {"$set": fields}, upsert=True)

    def get_identity_suggestion(self, suggestion_id: str) -> Optional[dict]:
        return self.db.identity_suggestions.find_one({"_id": suggestion_id})

    def list_identity_suggestions(self, status: str = "pending") -> list[dict]:
        return list(self.db.identity_suggestions.find({"status": status}))

    def set_identity_suggestion_status(self, suggestion_id: str,
                                       status: str) -> None:
        self.db.identity_suggestions.update_one(
            {"_id": suggestion_id}, {"$set": {"status": status}})

    def event_counts_by_person(self) -> dict[str, int]:
        """Total interactions per person, for duplicate-name grouping."""
        return {str(r["_id"]): int(r["n"]) for r in
                self.db.relationship_events.aggregate([
                    {"$group": {"_id": "$person_id", "n": {"$sum": 1}}},
                    {"$sort": {"n": -1}}])}

    def all_people_docs(self) -> list[dict]:
        return list(self.db.people.find({}))

    def get_message(self, message_id: str) -> Optional[dict]:
        """The raw stored message document (the evidence panel shows it as-is)."""
        doc = self.db.messages.find_one({"_id": message_id})
        if doc is not None:
            doc.pop("_id", None)
        return doc

    def list_notes_recent(self, limit: int = 100) -> list[Note]:
        return [_deserialize(Note, d) for d in
                self.db.notes.find().sort("noted_at", -1).limit(limit)]

    def list_voice_recent(self, limit: int = 50) -> list[VoiceRecording]:
        return [_deserialize(VoiceRecording, d) for d in
                self.db.voice_recordings.find().sort("imported_at", -1).limit(limit)]

    def update_memories_person(self, record_id: str, person_id: str) -> int:
        """Re-point one record's memories at a person (voice associate)."""
        return self.db.memories.update_many(
            {"record_id": record_id}, {"$set": {"person_id": person_id}}).modified_count

    def search_messages(self, query: dict, limit: int = 50) -> list[dict]:
        """Messages matching a match clause, newest first."""
        return list(self.db.messages.find(query, {"embedding": 0})
                    .sort("sent_at", -1).limit(limit))

    def count_messages_matching(self, query: dict) -> int:
        return self.db.messages.count_documents(query)

    def reassign_person_records(self, keep_id: str, remove_id: str,
                                collections: dict[str, str]) -> int:
        """Move every record's person pointer from one person to another."""
        moved = 0
        for coll, field in collections.items():
            moved += self.db[coll].update_many(
                {field: remove_id}, {"$set": {field: keep_id}}).modified_count
        return moved

    def replace_array_member(self, coll: str, field: str, old: str,
                             new: str) -> int:
        """Swap one element inside a document array (Mongo $set on arrays)."""
        return self.db[coll].update_many(
            {field: old}, {"$set": {field: [new]}}).modified_count

    def pull_array_member(self, coll: str, field: str, value: str) -> int:
        """Remove one element from a document array (Mongo $pull)."""
        return self.db[coll].update_many(
            {field: value}, {"$pull": {field: value}}).modified_count

    def delete_profile(self, person_id: str) -> int:
        return self.db.profiles.delete_one({"_id": person_id}).deleted_count

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass

    def engine_name(self) -> str:
        return "mongodb-atlas" if self.settings.mongo_atlas else "mongodb"
