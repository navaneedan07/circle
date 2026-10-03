"""Repository interfaces.

The per-entity ABCs below describe the shape of each area. They are
documentation rather than enforcement: the concrete stores historically used
flat method names (``list_people``, not ``PersonRepo.list``), so both
``MongoStore`` and ``SQLiteStore`` implement the flat surface directly and
type-hint against ``Store``.

``Store`` is what callers actually depend on, which is what makes the storage
engine swappable without touching parsers, RAG, or the API.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from circle.domain.models import (
    CalendarEvent, Conversation, Document, Email, ImportJob, Memory,
    Message, Note, Person, RelationshipProfile, Source, VoiceRecording,
)


class PersonRepo(ABC):
    @abstractmethod
    def list(self, limit: int = 200, skip: int = 0) -> list[Person]: ...
    @abstractmethod
    def get(self, person_id: str) -> Optional[Person]: ...
    @abstractmethod
    def find_by_identity(self, kind: str, value: str) -> Optional[Person]: ...
    @abstractmethod
    def insert(self, person: Person) -> Person: ...
    @abstractmethod
    def update(self, person: Person) -> None: ...
    @abstractmethod
    def delete(self, person_id: str) -> None: ...
    @abstractmethod
    def count(self) -> int: ...


class ConversationRepo(ABC):
    @abstractmethod
    def get(self, conversation_id: str) -> Optional[Conversation]: ...
    @abstractmethod
    def find(self, source: str, external_key: str) -> Optional[Conversation]: ...
    @abstractmethod
    def insert(self, conversation: Conversation) -> Conversation: ...
    @abstractmethod
    def update(self, conversation: Conversation) -> None: ...
    @abstractmethod
    def list_for_person(self, person_id: str) -> list[Conversation]: ...


class MessageRepo(ABC):
    @abstractmethod
    def insert_if_new(self, message: Message) -> bool:
        """Insert unless a message with the same deterministic id exists.
        Returns True when inserted, False when skipped as duplicate."""
    @abstractmethod
    def list_for_person(self, person_id: str, limit: int = 100) -> list[Message]: ...
    @abstractmethod
    def list_for_conversation(self, conversation_id: str, limit: int = 500) -> list[Message]: ...
    @abstractmethod
    def count(self) -> int: ...


class EmailRepo(ABC):
    @abstractmethod
    def insert_if_new(self, email: Email) -> bool: ...
    @abstractmethod
    def list_for_person(self, person_id: str, limit: int = 100) -> list[Email]: ...
    @abstractmethod
    def get(self, email_id: str) -> Optional[Email]: ...


class CalendarRepo(ABC):
    @abstractmethod
    def insert_if_new(self, event: CalendarEvent) -> bool: ...
    @abstractmethod
    def list_for_person(self, person_id: str, limit: int = 100) -> list[CalendarEvent]: ...
    @abstractmethod
    def list_upcoming(self, person_id: str, limit: int = 20) -> list[CalendarEvent]: ...
    @abstractmethod
    def get(self, event_id: str) -> Optional[CalendarEvent]: ...


class NoteRepo(ABC):
    @abstractmethod
    def insert_if_new(self, note: Note) -> bool: ...
    @abstractmethod
    def insert(self, note: Note) -> Note: ...
    @abstractmethod
    def list_for_person(self, person_id: str, limit: int = 100) -> list[Note]: ...
    @abstractmethod
    def get(self, note_id: str) -> Optional[Note]: ...


class VoiceRepo(ABC):
    @abstractmethod
    def insert_if_new(self, rec: VoiceRecording) -> bool: ...
    @abstractmethod
    def update(self, rec: VoiceRecording) -> None: ...
    @abstractmethod
    def list_for_person(self, person_id: str, limit: int = 50) -> list[VoiceRecording]: ...
    @abstractmethod
    def get(self, rec_id: str) -> Optional[VoiceRecording]: ...


class DocumentRepo(ABC):
    @abstractmethod
    def insert_if_new(self, doc: Document) -> bool: ...
    @abstractmethod
    def list_for_person(self, person_id: str, limit: int = 50) -> list[Document]: ...
    @abstractmethod
    def get(self, doc_id: str) -> Optional[Document]: ...


class MemoryRepo(ABC):
    """Memories are the retrieval corpus (chunks + embeddings)."""
    @abstractmethod
    def insert_many(self, memories: list[Memory]) -> int: ...
    @abstractmethod
    def vector_search(
        self, vector: list[float], filters: dict[str, Any], k: int = 12
    ) -> list[tuple[Memory, float]]: ...
    @abstractmethod
    def keyword_search(
        self, query: str, filters: dict[str, Any], k: int = 12
    ) -> list[tuple[Memory, float]]: ...
    @abstractmethod
    def for_person(self, person_id: str, limit: int = 500) -> list[Memory]: ...
    @abstractmethod
    def get(self, memory_id: str) -> Optional[Memory]: ...
    @abstractmethod
    def count(self) -> int: ...
    @abstractmethod
    def all_embeddings(self) -> list[tuple[str, list[float]]]: ...


class SourceRepo(ABC):
    @abstractmethod
    def insert(self, source: Source) -> Source: ...
    @abstractmethod
    def get(self, source_id: str) -> Optional[Source]: ...
    @abstractmethod
    def find_by_checksum(self, checksum: str) -> Optional[Source]: ...


class JobRepo(ABC):
    @abstractmethod
    def insert(self, job: ImportJob) -> ImportJob: ...
    @abstractmethod
    def update(self, job: ImportJob) -> None: ...
    @abstractmethod
    def get(self, job_id: str) -> Optional[ImportJob]: ...
    @abstractmethod
    def list(self, limit: int = 50) -> list[ImportJob]: ...
    @abstractmethod
    def resume_incomplete(self) -> list[ImportJob]:
        """Reset PROCESSING jobs stuck by a previous crash back to QUEUED."""
    @abstractmethod
    def count_by_status(self) -> dict[str, int]: ...


class ProfileRepo(ABC):
    @abstractmethod
    def get(self, person_id: str) -> Optional[RelationshipProfile]: ...
    @abstractmethod
    def upsert(self, profile: RelationshipProfile) -> None: ...
    @abstractmethod
    def list(self) -> list[RelationshipProfile]: ...


class ProcessingStateRepo(ABC):
    """Persistent file-level state: what has been processed (survives restart)."""
    @abstractmethod
    def already_processed(self, checksum: str) -> bool: ...
    @abstractmethod
    def mark_processed(self, checksum: str, path: str, job_id: str) -> None: ...
    @abstractmethod
    def get_by_path(self, path: str) -> Optional[dict]: ...


class SettingRepo(ABC):
    @abstractmethod
    def get(self, key: str) -> Any: ...
    @abstractmethod
    def set(self, key: str, value: Any) -> None: ...


# The store the rest of the app talks to. Both engines satisfy it; see
# circle/repository/factory.py for how one is chosen.
Store = Any
