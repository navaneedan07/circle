/**
 * Universal (source-agnostic) data model.
 *
 * Every parser normalizes into these types; the AI/RAG layer only ever sees
 * them, never a WhatsApp/Telegram/Instagram-specific shape. Ported from the
 * Python `circle/domain/models.py` so behaviour stays identical.
 */

export type DataOrigin = "api" | "imported" | "local";

export type SourceType =
  | "whatsapp"
  | "telegram"
  | "instagram"
  | "x"
  | "email"
  | "calendar"
  | "contacts"
  | "notes"
  | "voice"
  | "chat"
  | "document";

export type RelationshipStatus =
  | "Very Active"
  | "Active"
  | "Occasional"
  | "Low Activity"
  | "No Recent Activity";

export type MediaKind =
  | "image"
  | "video"
  | "voice"
  | "audio"
  | "sticker"
  | "document"
  | "other";

export interface IdentityLink {
  kind: "email" | "phone" | "username" | "name" | string;
  value: string;
  source: SourceType;
  label?: string;
  origin?: DataOrigin;
}

export interface Person {
  id: string;
  display_name: string;
  aliases: string[];
  identities: IdentityLink[];
  avatar_color: string;
  is_user: boolean;
  merged_from: string[];
  created_at: string;
  updated_at: string;
  origin: DataOrigin;
}

export interface Conversation {
  id: string;
  external_key: string;
  title: string;
  participant_ids: string[];
  source: SourceType;
  origin: DataOrigin;
  last_message_at: string | null;
  created_at: string;
}

export interface Message {
  id: string;
  conversation_id: string | null;
  person_id: string | null;
  sender_label: string;
  sent_at: string | null;
  content: string;
  attachments: Record<string, unknown>[];
  reply_to: string | null;
  source: SourceType;
  origin: DataOrigin;
  external_id: string;
  media_ids: string[];
  imported_at: string;
}

export interface Email {
  id: string;
  person_id: string | null;
  direction: "inbound" | "outbound" | string;
  from_label: string;
  from_address: string;
  to: string[];
  subject: string;
  body: string;
  sent_at: string | null;
  attachments: Record<string, unknown>[];
  source: SourceType;
  origin: DataOrigin;
  external_id: string;
  imported_at: string;
}

export interface CalendarEvent {
  id: string;
  title: string;
  starts_at: string | null;
  ends_at: string | null;
  location: string;
  description: string;
  participants: string[];
  person_ids: string[];
  source: SourceType;
  origin: DataOrigin;
  external_id: string;
  imported_at: string;
}

export interface Note {
  id: string;
  person_id: string | null;
  title: string;
  body: string;
  noted_at: string;
  source: SourceType;
  origin: DataOrigin;
  external_id: string;
  imported_at: string;
}

export interface VoiceRecording {
  id: string;
  person_id: string | null;
  filename: string;
  duration_seconds: number | null;
  recorded_at: string | null;
  transcript: string;
  language: string;
  checksum: string;
  processing_status: "QUEUED" | "PROCESSING" | "COMPLETED" | "FAILED" | string;
  error: string;
  source: SourceType;
  origin: DataOrigin;
  external_id: string;
  imported_at: string;
}

export interface MediaAttachment {
  id: string;
  filename: string;
  filename_key: string;
  kind: MediaKind;
  mime_type: string;
  size_bytes: number;
  checksum: string;
  stored_path: string;
  source: SourceType;
  origin: DataOrigin;
  person_id: string | null;
  message_id: string | null;
  conversation_id: string | null;
  occurred_at: string | null;
  duration_seconds: number | null;
  language: string;
  transcript: string;
  caption: string;
  status: string;
  error: string;
  imported_at: string;
}

export interface DocumentRecord {
  id: string;
  person_id: string | null;
  filename: string;
  title: string;
  text: string;
  mime_type: string;
  source: SourceType;
  origin: DataOrigin;
  external_id: string;
  imported_at: string;
}

export interface RelationshipEvent {
  id: string;
  person_id: string;
  kind: string;
  source: SourceType;
  occurred_at: string;
  summary: string;
  record_id: string;
  origin: DataOrigin;
}

export interface SourceRecord {
  id: string;
  filename: string;
  source_type: SourceType;
  origin: DataOrigin;
  checksum: string;
  imported_at: string;
  record_count: number;
}

export interface Memory {
  id: string;
  person_id: string | null;
  kind: string;
  source: SourceType;
  origin: DataOrigin;
  occurred_at: string | null;
  text: string;
  summary: string;
  record_id: string;
  source_id: string;
  citation: string;
  topics: string[];
  embedding: number[] | null;
  imported_at: string;
}

export interface TopicStat {
  topic: string;
  count: number;
  last_seen: string | null;
}

export interface RelationshipProfile {
  person_id: string;
  status: RelationshipStatus;
  status_reason: string;
  interaction_count: number;
  interactions_14d: number;
  interactions_60d: number;
  last_interaction_at: string | null;
  source_breakdown: Record<string, number>;
  topics: TopicStat[];
  active_topics: TopicStat[];
  upcoming_events: { id: string; title: string; starts_at: string | null; location?: string }[];
  summary: string;
  summary_sources: string[];
  updated_at: string;
}

export interface ImportJob {
  id: string;
  filename: string;
  path: string;
  source: SourceType | null;
  status: "QUEUED" | "PROCESSING" | "COMPLETED" | "FAILED" | "QUARANTINED" | "SKIPPED" | string;
  records_imported: number;
  records_skipped: number;
  checksum: string;
  error: string;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

/** Parse result every parser returns. */
export interface ParseResult {
  people: Person[];
  conversations: {
    external_key: string;
    title: string;
    source: SourceType;
  }[];
  messages: Message[];
  emails: Email[];
  events: CalendarEvent[];
  notes: Note[];
  contacts: {
    name: string;
    emails: string[];
    phones: string[];
    aliases?: string[];
  }[];
  documents: { filename: string; title: string; text: string }[];
}

export function emptyParseResult(): ParseResult {
  return {
    people: [],
    conversations: [],
    messages: [],
    emails: [],
    events: [],
    notes: [],
    contacts: [],
    documents: [],
  };
}

export function isParseResultEmpty(r: ParseResult): boolean {
  return (
    r.people.length === 0 &&
    r.conversations.length === 0 &&
    r.messages.length === 0 &&
    r.emails.length === 0 &&
    r.events.length === 0 &&
    r.notes.length === 0 &&
    r.contacts.length === 0 &&
    r.documents.length === 0
  );
}

export function nowIso(): string {
  return new Date().toISOString();
}

/**
 * Canonical timestamp string.
 *
 * Every timestamp is stored as text and compared as text. Pydantic emitted
 * `...Z` while `isoformat()` emitted `...+00:00`; since `Z` (0x5A) sorts after
 * `+` (0x2B), mixing them silently misordered every time window. Everything is
 * normalized through `Date.toISOString()` (always `Z`, millisecond precision)
 * so comparisons are consistent.
 */
export function canonicalTime(value: Date | string | null | undefined): string | null {
  if (value === null || value === undefined || value === "") return null;
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return null;
  return d.toISOString();
}
