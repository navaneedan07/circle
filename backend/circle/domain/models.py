"""Universal (source-agnostic) data model.

Every connector/parser normalizes into these entities. The AI/RAG layer only
ever sees these types -- never WhatsApp/Telegram/Instagram-specific shapes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DataOrigin(str, Enum):
    """Where a record came from, per the privacy model."""
    API = "api"                  # official API/connector
    IMPORTED = "imported"        # user-provided export/file
    LOCAL = "local"              # created locally in the app


class SourceType(str, Enum):
    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"
    INSTAGRAM = "instagram"
    X = "x"
    EMAIL = "email"
    CALENDAR = "calendar"
    CONTACTS = "contacts"
    NOTES = "notes"
    VOICE = "voice"
    CHAT = "chat"                # generic chat import
    DOCUMENT = "document"


class RelationshipStatus(str, Enum):
    VERY_ACTIVE = "Very Active"
    ACTIVE = "Active"
    OCCASIONAL = "Occasional"
    LOW_ACTIVITY = "Low Activity"
    NO_RECENT_ACTIVITY = "No Recent Activity"


class IdentityLink(BaseModel):
    """A raw identity observed in some source (username, phone, email...)."""
    kind: str            # email | phone | username | name
    value: str           # normalized value
    source: SourceType
    label: str = ""      # display label, e.g. Instagram handle
    origin: DataOrigin = DataOrigin.IMPORTED


class Person(BaseModel):
    id: Optional[str] = None
    display_name: str
    aliases: list[str] = Field(default_factory=list)
    identities: list[IdentityLink] = Field(default_factory=list)
    avatar_color: str = "#6366f1"
    is_user: bool = False            # the app owner
    merged_from: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    origin: DataOrigin = DataOrigin.IMPORTED


class Conversation(BaseModel):
    id: Optional[str] = None
    external_key: str = ""       # stable per-chat key (dedupe across exports)
    title: str = ""
    participant_ids: list[str] = Field(default_factory=list)
    source: SourceType
    origin: DataOrigin = DataOrigin.IMPORTED
    last_message_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)


class Message(BaseModel):
    id: Optional[str] = None
    conversation_id: Optional[str] = None
    person_id: Optional[str] = None     # resolved counterparty (None until resolved)
    sender_label: str = ""              # raw sender as seen in export
    sent_at: Optional[datetime] = None
    content: str = ""
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    reply_to: Optional[str] = None
    source: SourceType
    origin: DataOrigin = DataOrigin.IMPORTED
    external_id: str = ""               # deterministic dedupe key part
    media_ids: list[str] = Field(default_factory=list)   # linked attachments
    imported_at: datetime = Field(default_factory=utcnow)


class Email(BaseModel):
    id: Optional[str] = None
    person_id: Optional[str] = None
    direction: str = "inbound"          # inbound | outbound
    from_label: str = ""
    from_address: str = ""
    to: list[str] = Field(default_factory=list)
    subject: str = ""
    body: str = ""
    sent_at: Optional[datetime] = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    source: SourceType = SourceType.EMAIL
    origin: DataOrigin = DataOrigin.IMPORTED
    external_id: str = ""
    imported_at: datetime = Field(default_factory=utcnow)


class CalendarEvent(BaseModel):
    id: Optional[str] = None
    title: str = ""
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    location: str = ""
    description: str = ""
    participants: list[str] = Field(default_factory=list)   # names/emails
    person_ids: list[str] = Field(default_factory=list)
    source: SourceType = SourceType.CALENDAR
    origin: DataOrigin = DataOrigin.IMPORTED
    external_id: str = ""
    imported_at: datetime = Field(default_factory=utcnow)


class Note(BaseModel):
    id: Optional[str] = None
    person_id: Optional[str] = None
    title: str = ""
    body: str = ""
    noted_at: datetime = Field(default_factory=utcnow)
    source: SourceType = SourceType.NOTES
    origin: DataOrigin = DataOrigin.LOCAL
    external_id: str = ""
    imported_at: datetime = Field(default_factory=utcnow)


class VoiceRecording(BaseModel):
    id: Optional[str] = None
    person_id: Optional[str] = None
    filename: str = ""
    duration_seconds: Optional[float] = None
    recorded_at: Optional[datetime] = None
    transcript: str = ""
    language: str = ""
    checksum: str = ""
    processing_status: str = "QUEUED"   # QUEUED|PROCESSING|COMPLETED|FAILED
    error: str = ""
    source: SourceType = SourceType.VOICE
    origin: DataOrigin = DataOrigin.IMPORTED
    external_id: str = ""
    imported_at: datetime = Field(default_factory=utcnow)


class MediaKind(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    VOICE = "voice"        # voice note recorded in-app / PTT in WhatsApp
    AUDIO = "audio"
    STICKER = "sticker"
    DOCUMENT = "document"
    OTHER = "other"


class MediaAttachment(BaseModel):
    """A real media file ingested from an export and linked to its message.

    Files stay on this machine (copied into the local media store); only
    metadata, an optional local transcript and an optional local caption are
    stored in the database.
    """
    id: Optional[str] = None
    filename: str = ""
    filename_key: str = ""       # normalized lookup key (basename, lowercased)
    kind: MediaKind = MediaKind.OTHER
    mime_type: str = ""
    size_bytes: int = 0
    checksum: str = ""
    stored_path: str = ""           # inside the local media store
    source: SourceType = SourceType.WHATSAPP
    origin: DataOrigin = DataOrigin.IMPORTED
    person_id: Optional[str] = None
    message_id: Optional[str] = None
    conversation_id: Optional[str] = None
    occurred_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    language: str = ""
    transcript: str = ""            # local Whisper output (voice notes)
    caption: str = ""               # local Gemma vision output (images)
    status: str = "STORED"          # STORED | LINKED | FAILED
    error: str = ""
    imported_at: datetime = Field(default_factory=utcnow)


class Document(BaseModel):
    id: Optional[str] = None
    person_id: Optional[str] = None
    filename: str = ""
    title: str = ""
    text: str = ""
    mime_type: str = ""
    source: SourceType = SourceType.DOCUMENT
    origin: DataOrigin = DataOrigin.IMPORTED
    external_id: str = ""
    imported_at: datetime = Field(default_factory=utcnow)


class RelationshipEvent(BaseModel):
    """One observable interaction with a person (any source)."""
    id: Optional[str] = None
    person_id: str
    kind: str                      # message | email | meeting | note | voice | document
    source: SourceType
    occurred_at: datetime
    summary: str = ""              # short, non-sensitive
    record_id: str = ""            # pointer to originating record
    origin: DataOrigin = DataOrigin.IMPORTED


class Source(BaseModel):
    """Provenance record for a stored batch/file (shown as evidence)."""
    id: Optional[str] = None
    filename: str = ""
    source_type: SourceType
    origin: DataOrigin = DataOrigin.IMPORTED
    checksum: str = ""
    imported_at: datetime = Field(default_factory=utcnow)
    record_count: int = 0


class Memory(BaseModel):
    """A searchable chunk of evidence with its embedding."""
    id: Optional[str] = None
    person_id: Optional[str] = None
    kind: str                      # message | email | note | calendar | voice | document
    source: SourceType
    origin: DataOrigin = DataOrigin.IMPORTED
    occurred_at: Optional[datetime] = None
    text: str = ""                 # evidence text (retrieved, cited)
    summary: str = ""              # optional short summary
    record_id: str = ""
    source_id: str = ""
    citation: str = ""             # human label e.g. "WhatsApp — Sept 28, 8:42 PM"
    topics: list[str] = Field(default_factory=list)
    embedding: Optional[list[float]] = None
    imported_at: datetime = Field(default_factory=utcnow)


class TopicStat(BaseModel):
    topic: str
    count: int = 1
    last_seen: Optional[datetime] = None


class RelationshipProfile(BaseModel):
    person_id: str
    status: RelationshipStatus = RelationshipStatus.NO_RECENT_ACTIVITY
    status_reason: str = ""
    interaction_count: int = 0
    interactions_14d: int = 0
    interactions_60d: int = 0
    last_interaction_at: Optional[datetime] = None
    source_breakdown: dict[str, int] = Field(default_factory=dict)
    topics: list[TopicStat] = Field(default_factory=list)
    active_topics: list[TopicStat] = Field(default_factory=list)
    upcoming_events: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""              # evidence-grounded, generated locally
    summary_sources: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utcnow)


class ImportJob(BaseModel):
    id: Optional[str] = None
    filename: str = ""
    path: str = ""
    source: Optional[SourceType] = None
    status: str = "QUEUED"         # QUEUED|PROCESSING|COMPLETED|FAILED|QUARANTINED|SKIPPED
    checksum: str = ""
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: str = ""
    records_imported: int = 0
    records_skipped: int = 0
    origin: DataOrigin = DataOrigin.IMPORTED
    created_at: datetime = Field(default_factory=utcnow)
