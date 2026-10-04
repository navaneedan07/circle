/**
 * The one storage engine.
 *
 * MongoDB has been removed entirely: there is no second engine to keep in
 * lockstep, so there is no query-translation layer either. Each table keeps
 * the whole entity as a JSON `doc` column (so the round-trip is lossless)
 * plus real columns for the fields that get filtered, joined or sorted on.
 *
 * Embeddings are stored as a BLOB of float32, not JSON: JSON was ~10x larger
 * and had to be parsed on every row read.
 */
import { DatabaseSync } from "node:sqlite";
import fs from "node:fs";
import path from "node:path";
import { canonicalTime, nowIso } from "./domain.js";
import { applySchema, repairStoredNames, type MigrationReport } from "./migrate.js";
import type {
  CalendarEvent,
  Conversation,
  DocumentRecord,
  Email,
  ImportJob,
  MediaAttachment,
  Memory,
  Message,
  Note,
  Person,
  RelationshipEvent,
  RelationshipProfile,
  SourceRecord,
  VoiceRecording,
} from "./domain.js";

const SCHEMA = `
CREATE TABLE IF NOT EXISTS people (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  display_name_lower TEXT NOT NULL,
  is_user INTEGER NOT NULL DEFAULT 0,
  doc TEXT NOT NULL,
  created_at TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_people_name ON people(display_name_lower);
CREATE INDEX IF NOT EXISTS idx_people_updated ON people(updated_at DESC);

CREATE TABLE IF NOT EXISTS identities (
  person_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_identities_lookup ON identities(kind, value);
CREATE INDEX IF NOT EXISTS idx_identities_person ON identities(person_id);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  external_key TEXT,
  title TEXT,
  source TEXT,
  doc TEXT NOT NULL,
  last_message_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_key ON conversations(source, external_key);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT,
  person_id TEXT,
  sender_label TEXT,
  sent_at TEXT,
  content TEXT,
  doc TEXT NOT NULL,
  imported_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_person ON messages(person_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, sent_at);

CREATE TABLE IF NOT EXISTS emails (
  id TEXT PRIMARY KEY,
  person_id TEXT,
  from_address TEXT,
  subject TEXT,
  sent_at TEXT,
  doc TEXT NOT NULL,
  imported_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_emails_person ON emails(person_id, sent_at DESC);

CREATE TABLE IF NOT EXISTS calendar_events (
  id TEXT PRIMARY KEY,
  starts_at TEXT,
  title TEXT,
  doc TEXT NOT NULL,
  imported_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_calendar_starts ON calendar_events(starts_at);
CREATE TABLE IF NOT EXISTS calendar_people (
  event_id TEXT NOT NULL,
  person_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calendar_people ON calendar_people(person_id);

CREATE TABLE IF NOT EXISTS notes (
  id TEXT PRIMARY KEY,
  person_id TEXT,
  noted_at TEXT,
  doc TEXT NOT NULL,
  imported_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notes_person ON notes(person_id, noted_at DESC);

CREATE TABLE IF NOT EXISTS voice_recordings (
  id TEXT PRIMARY KEY,
  person_id TEXT,
  checksum TEXT,
  recorded_at TEXT,
  doc TEXT NOT NULL,
  imported_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_voice_checksum ON voice_recordings(checksum);

CREATE TABLE IF NOT EXISTS media (
  id TEXT PRIMARY KEY,
  message_id TEXT,
  person_id TEXT,
  conversation_id TEXT,
  filename_key TEXT,
  checksum TEXT,
  occurred_at TEXT,
  doc TEXT NOT NULL,
  imported_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_media_message ON media(message_id);
CREATE INDEX IF NOT EXISTS idx_media_person ON media(person_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_media_key ON media(filename_key);
CREATE INDEX IF NOT EXISTS idx_media_checksum ON media(checksum);

CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  external_id TEXT,
  person_id TEXT,
  doc TEXT NOT NULL,
  imported_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_external ON documents(external_id);

CREATE TABLE IF NOT EXISTS relationship_events (
  id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  kind TEXT,
  source TEXT,
  occurred_at TEXT NOT NULL,
  summary TEXT,
  record_id TEXT,
  doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_person ON relationship_events(person_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_occurred ON relationship_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_source ON relationship_events(source);

CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  checksum TEXT,
  filename TEXT,
  doc TEXT NOT NULL,
  imported_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_checksum ON sources(checksum);

CREATE TABLE IF NOT EXISTS import_jobs (
  id TEXT PRIMARY KEY,
  status TEXT,
  filename TEXT,
  path TEXT,
  checksum TEXT,
  doc TEXT NOT NULL,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON import_jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS processed_files (
  checksum TEXT PRIMARY KEY,
  path TEXT,
  job_id TEXT,
  processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_processed_path ON processed_files(path);

CREATE TABLE IF NOT EXISTS profiles (
  person_id TEXT PRIMARY KEY,
  doc TEXT NOT NULL,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  person_id TEXT,
  kind TEXT,
  source TEXT,
  occurred_at TEXT,
  record_id TEXT,
  doc TEXT NOT NULL,
  embedding BLOB,
  imported_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_person ON memories(person_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_source ON memories(source);
CREATE INDEX IF NOT EXISTS idx_memories_record ON memories(record_id);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  memory_id UNINDEXED,
  text,
  tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS identity_suggestions (
  id TEXT PRIMARY KEY,
  status TEXT,
  doc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
`;

function encodeEmbedding(vec: number[] | null | undefined): Uint8Array | null {
  if (!vec || vec.length === 0) return null;
  const buf = Buffer.allocUnsafe(vec.length * 4);
  for (let i = 0; i < vec.length; i++) buf.writeFloatLE(vec[i]!, i * 4);
  return new Uint8Array(buf);
}

function decodeEmbedding(blob: unknown): number[] | null {
  if (!blob) return null;
  const buf = Buffer.from(blob as Uint8Array);
  const out = new Array<number>(buf.length / 4);
  for (let i = 0; i < out.length; i++) out[i] = buf.readFloatLE(i * 4);
  return out;
}

/**
 * Zero-copy view over a stored embedding blob.
 *
 * The bulk retrieval path needs every vector on every search. Decoding them to
 * boxed JS number[] each time is both slow and enormously memory-hungry (a
 * six-figure archive is hundreds of megabytes per query), so the index keeps
 * them as one contiguous Float32Array instead.
 */
function embeddingView(blob: unknown): Float32Array | null {
  if (!blob) return null;
  const u8 = blob instanceof Uint8Array ? blob : new Uint8Array(blob as ArrayBuffer);
  if (u8.byteLength === 0 || u8.byteLength % 4 !== 0) return null;
  const count = u8.byteLength / 4;
  if (u8.byteOffset % 4 === 0) return new Float32Array(u8.buffer, u8.byteOffset, count);
  const copy = new Uint8Array(u8.byteLength);
  copy.set(u8);
  return new Float32Array(copy.buffer, 0, count);
}

type Row = Record<string, unknown>;

/**
 * How long a stale embedding index may keep being served after new memories
 * arrive. Long enough that a queue of imports does not rebuild the matrix
 * between every file, short enough that an idle app is accurate again.
 */
const REBUILD_INTERVAL_MS = 30_000;

/** Rows read per page while building the index, between loop yields. */
const INDEX_PAGE = 5_000;

const yieldToLoop = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));

/** Flat matrix of every stored embedding: `vectors[i * dim .. i * dim + dim)`. */
export interface EmbeddingIndex {
  ids: string[];
  vectors: Float32Array;
  dim: number;
}

export class Store {
  readonly db: DatabaseSync;
  private embedIndex: EmbeddingIndex | null = null;
  private embedIndexBuilding = false;
  private embedIndexStale = false;
  /** >0 while an import is writing; suppresses expensive index rebuilds. */
  private bulkWrites = 0;
  private embedIndexBuiltAt = 0;
  private embedIndexBuild: Promise<void> = Promise.resolve();

  constructor(dbPath: string) {
    fs.mkdirSync(path.dirname(dbPath), { recursive: true });
    this.db = new DatabaseSync(dbPath);
    this.db.exec("PRAGMA journal_mode=WAL");
    this.db.exec("PRAGMA foreign_keys=ON");
    // Not `exec(SCHEMA)`: an existing database is left untouched by
    // CREATE TABLE IF NOT EXISTS, so an upgrade would fail on the first index
    // or query mentioning a newer column. See src/migrate.ts.
    this.migration = applySchema(this.db, SCHEMA);
    // Older builds stored contact names exactly as the export mangled them.
    // Repairing them here is what an already-imported archive needs; the parser
    // fix only covers exports read from now on.
    this.migration.names_repaired = repairStoredNames(this.db);
  }

  /** Columns this build had to add to an existing database, for diagnostics. */
  readonly migration: MigrationReport;

  close(): void {
    this.db.close();
  }

  ping(): boolean {
    try {
      this.db.prepare("SELECT 1").get();
      return true;
    } catch {
      return false;
    }
  }

  engineName(): string {
    return "sqlite";
  }

  private run(sql: string, params: unknown[] = []): void {
    this.db.prepare(sql).run(...(params as never[]));
  }

  private all(sql: string, params: unknown[] = []): Row[] {
    return this.db.prepare(sql).all(...(params as never[])) as Row[];
  }

  private get(sql: string, params: unknown[] = []): Row | undefined {
    return this.db.prepare(sql).get(...(params as never[])) as Row | undefined;
  }

  // ============================ Settings =============================
  getSetting(key: string): unknown {
    const row = this.get("SELECT value FROM app_settings WHERE key = ?", [key]);
    if (!row) return null;
    try {
      return JSON.parse(String(row.value));
    } catch {
      return row.value;
    }
  }

  setSetting(key: string, value: unknown): void {
    this.run(
      "INSERT INTO app_settings(key, value) VALUES(?, ?) " +
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
      [key, JSON.stringify(value)]
    );
  }

  // ============================== People =============================
  insertPerson(p: Person): Person {
    const id = p.id || `${slug(p.display_name)}-${shortId()}`;
    const doc = { ...p, id };
    this.run(
      "INSERT INTO people(id, display_name, display_name_lower, is_user, doc, created_at, updated_at) " +
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
      [id, p.display_name, p.display_name.toLowerCase(), p.is_user ? 1 : 0, JSON.stringify(doc), doc.created_at, doc.updated_at]
    );
    this.replaceIdentities(id, p);
    return doc;
  }

  updatePerson(p: Person): void {
    if (!p.id) return;
    const doc = { ...p, updated_at: nowIso() };
    this.run(
      "UPDATE people SET display_name = ?, display_name_lower = ?, is_user = ?, doc = ?, updated_at = ? WHERE id = ?",
      [doc.display_name, doc.display_name.toLowerCase(), doc.is_user ? 1 : 0, JSON.stringify(doc), doc.updated_at, doc.id]
    );
    this.replaceIdentities(doc.id, doc);
  }

  private replaceIdentities(personId: string, p: Person): void {
    this.run("DELETE FROM identities WHERE person_id = ?", [personId]);
    for (const link of p.identities || []) {
      this.run("INSERT INTO identities(person_id, kind, value) VALUES(?, ?, ?)", [
        personId,
        link.kind,
        link.value,
      ]);
    }
  }

  getPerson(id: string): Person | null {
    const row = this.get("SELECT doc FROM people WHERE id = ?", [id]);
    return row ? (JSON.parse(String(row.doc)) as Person) : null;
  }

  findPersonByIdentity(kind: string, value: string): Person | null {
    const row = this.get(
      "SELECT p.doc AS doc FROM identities i JOIN people p ON p.id = i.person_id " +
        "WHERE i.kind = ? AND i.value = ? LIMIT 1",
      [kind, value]
    );
    return row ? (JSON.parse(String(row.doc)) as Person) : null;
  }

  findPersonByName(name: string): Person | null {
    const row = this.get(
      "SELECT doc FROM people WHERE display_name_lower = ? LIMIT 1",
      [name.trim().toLowerCase()]
    );
    return row ? (JSON.parse(String(row.doc)) as Person) : null;
  }

  listPeople(limit = 200, skip = 0): Person[] {
    return this.all(
      "SELECT doc FROM people ORDER BY updated_at DESC LIMIT ? OFFSET ?",
      [limit, skip]
    ).map((r) => JSON.parse(String(r.doc)) as Person);
  }

  countPeople(): number {
    return Number(this.get("SELECT COUNT(*) AS n FROM people")?.n ?? 0);
  }

  allPeopleDocs(): Person[] {
    return this.all("SELECT doc FROM people").map((r) => JSON.parse(String(r.doc)) as Person);
  }

  deletePerson(personId: string): void {
    this.run("DELETE FROM people WHERE id = ?", [personId]);
    this.run("DELETE FROM identities WHERE person_id = ?", [personId]);
    this.run("DELETE FROM profiles WHERE person_id = ?", [personId]);
  }

  // ========================== Conversations ==========================
  upsertConversation(c: Conversation): Conversation {
    const id = c.id || `${c.source}:${(c.external_key || c.title || "default").slice(0, 200)}`;
    const doc = { ...c, id };
    this.run(
      "INSERT INTO conversations(id, external_key, title, source, doc, last_message_at) " +
        "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
      [id, c.external_key, c.title, c.source, JSON.stringify(doc), canonicalTime(c.last_message_at)]
    );
    return doc;
  }

  getConversation(id: string): Conversation | null {
    const row = this.get("SELECT doc FROM conversations WHERE id = ?", [id]);
    return row ? (JSON.parse(String(row.doc)) as Conversation) : null;
  }

  updateConversation(c: Conversation): void {
    if (!c.id) return;
    this.run("UPDATE conversations SET doc = ?, last_message_at = ? WHERE id = ?", [
      JSON.stringify(c),
      canonicalTime(c.last_message_at),
      c.id,
    ]);
  }

  listConversations(limit = 500): Conversation[] {
    return this.all("SELECT doc FROM conversations ORDER BY last_message_at DESC LIMIT ?", [limit]).map(
      (r) => JSON.parse(String(r.doc)) as Conversation
    );
  }

  listConversationsForPerson(personId: string): Conversation[] {
    const rows = this.all("SELECT doc FROM conversations");
    return rows
      .map((r) => JSON.parse(String(r.doc)) as Conversation)
      .filter((c) => (c.participant_ids || []).includes(personId));
  }

  // ============================= Messages ============================
  insertMessageIfNew(m: Message): boolean {
    const exists = this.get("SELECT 1 AS x FROM messages WHERE id = ?", [m.id]);
    if (exists) return false;
    this.run(
      "INSERT INTO messages(id, conversation_id, person_id, sender_label, sent_at, content, doc, imported_at) " +
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
      [
        m.id,
        m.conversation_id,
        m.person_id,
        m.sender_label,
        canonicalTime(m.sent_at),
        m.content,
        JSON.stringify({ ...m, sent_at: canonicalTime(m.sent_at) }),
        m.imported_at,
      ]
    );
    return true;
  }

  updateMessageMediaIds(messageId: string, mediaIds: string[]): void {
    const row = this.get("SELECT doc FROM messages WHERE id = ?", [messageId]);
    if (!row) return;
    const doc = JSON.parse(String(row.doc)) as Message;
    doc.media_ids = mediaIds;
    this.run("UPDATE messages SET doc = ? WHERE id = ?", [JSON.stringify(doc), messageId]);
  }

  listMessagesForPerson(personId: string, limit = 100): Message[] {
    return this.all(
      "SELECT doc FROM messages WHERE person_id = ? ORDER BY sent_at DESC LIMIT ?",
      [personId, limit]
    ).map((r) => JSON.parse(String(r.doc)) as Message);
  }

  listMessagesForConversation(conversationId: string, limit = 500): Message[] {
    return this.all(
      "SELECT doc FROM messages WHERE conversation_id = ? ORDER BY sent_at ASC LIMIT ?",
      [conversationId, limit]
    ).map((r) => JSON.parse(String(r.doc)) as Message);
  }

  countMessages(): number {
    return Number(this.get("SELECT COUNT(*) AS n FROM messages")?.n ?? 0);
  }

  /**
   * Counts used by the person page.
   *
   * These were `listX(id, 100000).length`, which parsed every document just to
   * measure how many there were -- tens of thousands of JSON.parse calls on a
   * page load, and a visible stall once an archive gets to real-world size.
   */
  private countWhere(table: string, column: string, value: string): number {
    return Number(this.get(`SELECT COUNT(*) AS n FROM ${table} WHERE ${column} = ?`, [value])?.n ?? 0);
  }

  countMessagesForPerson(personId: string): number {
    return this.countWhere("messages", "person_id", personId);
  }

  countEmailsForPerson(personId: string): number {
    return this.countWhere("emails", "person_id", personId);
  }

  countNotesForPerson(personId: string): number {
    return this.countWhere("notes", "person_id", personId);
  }

  countMediaForPerson(personId: string): number {
    return this.countWhere("media", "person_id", personId);
  }

  findMessagesByAttachmentKey(key: string, limit = 50): Message[] {
    return this.all(
      "SELECT doc FROM messages WHERE doc LIKE ? LIMIT ?",
      [`%${JSON.stringify(key).slice(1, -1)}%`, limit]
    )
      .map((r) => JSON.parse(String(r.doc)) as Message)
      .filter((m) => (m.attachments || []).some((a) => (a as { filename_key?: string }).filename_key === key));
  }

  getMessage(messageId: string): Message | null {
    const row = this.get("SELECT doc FROM messages WHERE id = ?", [messageId]);
    return row ? (JSON.parse(String(row.doc)) as Message) : null;
  }

  // ============================== Emails =============================
  insertEmailIfNew(e: Email): boolean {
    if (this.get("SELECT 1 AS x FROM emails WHERE id = ?", [e.id])) return false;
    this.run(
      "INSERT INTO emails(id, person_id, from_address, subject, sent_at, doc, imported_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
      [e.id, e.person_id, e.from_address, e.subject, canonicalTime(e.sent_at), JSON.stringify({ ...e, sent_at: canonicalTime(e.sent_at) }), e.imported_at]
    );
    return true;
  }

  listEmailsForPerson(personId: string, limit = 100): Email[] {
    return this.all("SELECT doc FROM emails WHERE person_id = ? ORDER BY sent_at DESC LIMIT ?", [personId, limit])
      .map((r) => JSON.parse(String(r.doc)) as Email);
  }

  getEmail(id: string): Email | null {
    const row = this.get("SELECT doc FROM emails WHERE id = ?", [id]);
    return row ? (JSON.parse(String(row.doc)) as Email) : null;
  }

  // ============================= Calendar ============================
  insertCalendarIfNew(ev: CalendarEvent): boolean {
    if (this.get("SELECT 1 AS x FROM calendar_events WHERE id = ?", [ev.id])) return false;
    this.run(
      "INSERT INTO calendar_events(id, starts_at, title, doc, imported_at) VALUES(?, ?, ?, ?, ?)",
      [ev.id, canonicalTime(ev.starts_at), ev.title, JSON.stringify({ ...ev, starts_at: canonicalTime(ev.starts_at) }), ev.imported_at]
    );
    for (const pid of ev.person_ids || []) {
      this.run("INSERT INTO calendar_people(event_id, person_id) VALUES(?, ?)", [ev.id, pid]);
    }
    return true;
  }

  listCalendarForPerson(personId: string, limit = 100): CalendarEvent[] {
    return this.all(
      "SELECT c.doc AS doc FROM calendar_people cp JOIN calendar_events c ON c.id = cp.event_id " +
        "WHERE cp.person_id = ? ORDER BY c.starts_at DESC LIMIT ?",
      [personId, limit]
    ).map((r) => JSON.parse(String(r.doc)) as CalendarEvent);
  }

  listCalendarUpcoming(personId: string, limit = 20): CalendarEvent[] {
    const now = nowIso();
    return this.all(
      "SELECT c.doc AS doc FROM calendar_people cp JOIN calendar_events c ON c.id = cp.event_id " +
        "WHERE cp.person_id = ? AND c.starts_at >= ? ORDER BY c.starts_at ASC LIMIT ?",
      [personId, now, limit]
    ).map((r) => JSON.parse(String(r.doc)) as CalendarEvent);
  }

  // ============================== Notes ==============================
  insertNoteIfNew(n: Note): boolean {
    if (this.get("SELECT 1 AS x FROM notes WHERE id = ?", [n.id])) return false;
    this.run("INSERT INTO notes(id, person_id, noted_at, doc, imported_at) VALUES(?, ?, ?, ?, ?)", [
      n.id, n.person_id, canonicalTime(n.noted_at), JSON.stringify(n), n.imported_at,
    ]);
    return true;
  }

  listNotesForPerson(personId: string, limit = 100): Note[] {
    return this.all("SELECT doc FROM notes WHERE person_id = ? ORDER BY noted_at DESC LIMIT ?", [personId, limit])
      .map((r) => JSON.parse(String(r.doc)) as Note);
  }

  listNotesRecent(limit = 100): Note[] {
    return this.all("SELECT doc FROM notes ORDER BY noted_at DESC LIMIT ?", [limit])
      .map((r) => JSON.parse(String(r.doc)) as Note);
  }

  // ============================== Voice ==============================
  insertVoiceIfNew(v: VoiceRecording): boolean {
    if (v.checksum && this.get("SELECT 1 AS x FROM voice_recordings WHERE checksum = ?", [v.checksum])) return false;
    if (this.get("SELECT 1 AS x FROM voice_recordings WHERE id = ?", [v.id])) return false;
    this.run(
      "INSERT INTO voice_recordings(id, person_id, checksum, recorded_at, doc, imported_at) VALUES(?, ?, ?, ?, ?, ?)",
      [v.id, v.person_id, v.checksum, canonicalTime(v.recorded_at), JSON.stringify(v), v.imported_at]
    );
    return true;
  }

  updateVoice(v: VoiceRecording): void {
    this.run("UPDATE voice_recordings SET person_id = ?, doc = ? WHERE id = ?", [
      v.person_id, JSON.stringify(v), v.id,
    ]);
  }

  listVoiceForPerson(personId: string, limit = 50): VoiceRecording[] {
    return this.all("SELECT doc FROM voice_recordings WHERE person_id = ? ORDER BY recorded_at DESC LIMIT ?", [personId, limit])
      .map((r) => JSON.parse(String(r.doc)) as VoiceRecording);
  }

  listVoiceRecent(limit = 50): VoiceRecording[] {
    return this.all("SELECT doc FROM voice_recordings ORDER BY imported_at DESC LIMIT ?", [limit])
      .map((r) => JSON.parse(String(r.doc)) as VoiceRecording);
  }

  // ============================== Media ==============================
  insertMediaIfNew(m: MediaAttachment): boolean {
    if (m.checksum && this.get("SELECT 1 AS x FROM media WHERE checksum = ? AND message_id IS NOT NULL", [m.checksum])) {
      // same file already linked; still allow storing unlinked copy
    }
    if (this.get("SELECT 1 AS x FROM media WHERE id = ?", [m.id])) return false;
    this.run(
      "INSERT INTO media(id, message_id, person_id, conversation_id, filename_key, checksum, occurred_at, doc, imported_at) " +
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
      [m.id, m.message_id, m.person_id, m.conversation_id, m.filename_key, m.checksum, canonicalTime(m.occurred_at), JSON.stringify(m), m.imported_at]
    );
    return true;
  }

  updateMedia(m: MediaAttachment): void {
    this.run(
      "UPDATE media SET message_id = ?, person_id = ?, conversation_id = ?, occurred_at = ?, doc = ? WHERE id = ?",
      [m.message_id, m.person_id, m.conversation_id, canonicalTime(m.occurred_at), JSON.stringify(m), m.id]
    );
  }

  getMedia(id: string): MediaAttachment | null {
    const row = this.get("SELECT doc FROM media WHERE id = ?", [id]);
    return row ? (JSON.parse(String(row.doc)) as MediaAttachment) : null;
  }

  findMediaByFilenameKey(key: string): MediaAttachment | null {
    if (!key) return null;
    const row = this.get("SELECT doc FROM media WHERE filename_key = ? LIMIT 1", [key]);
    return row ? (JSON.parse(String(row.doc)) as MediaAttachment) : null;
  }

  listMediaForPerson(personId: string, limit = 200): MediaAttachment[] {
    return this.all("SELECT doc FROM media WHERE person_id = ? ORDER BY occurred_at DESC LIMIT ?", [personId, limit])
      .map((r) => JSON.parse(String(r.doc)) as MediaAttachment);
  }

  listMediaForMessage(messageId: string): MediaAttachment[] {
    return this.all("SELECT doc FROM media WHERE message_id = ?", [messageId])
      .map((r) => JSON.parse(String(r.doc)) as MediaAttachment);
  }

  listMedia(limit = 200): MediaAttachment[] {
    return this.all("SELECT doc FROM media ORDER BY imported_at DESC LIMIT ?", [limit])
      .map((r) => JSON.parse(String(r.doc)) as MediaAttachment);
  }

  countMedia(): number {
    return Number(this.get("SELECT COUNT(*) AS n FROM media")?.n ?? 0);
  }

  // ============================ Documents ============================
  insertDocumentIfNew(d: DocumentRecord): boolean {
    if (d.external_id && this.get("SELECT 1 AS x FROM documents WHERE external_id = ?", [d.external_id])) return false;
    this.run(
      "INSERT INTO documents(id, external_id, person_id, doc, imported_at) VALUES(?, ?, ?, ?, ?)",
      [d.id, d.external_id, d.person_id, JSON.stringify(d), d.imported_at]
    );
    return true;
  }

  listDocumentsForPerson(personId: string, limit = 50): DocumentRecord[] {
    return this.all("SELECT doc FROM documents WHERE person_id = ? ORDER BY imported_at DESC LIMIT ?", [personId, limit])
      .map((r) => JSON.parse(String(r.doc)) as DocumentRecord);
  }

  // ============================= Sources =============================
  insertSource(s: SourceRecord): void {
    this.run(
      "INSERT INTO sources(id, checksum, filename, doc, imported_at) VALUES(?, ?, ?, ?, ?) " +
        "ON CONFLICT(checksum) DO NOTHING",
      [s.id, s.checksum, s.filename, JSON.stringify(s), s.imported_at]
    );
  }

  // ============================ Import jobs ==========================
  insertJob(job: ImportJob): ImportJob {
    const id = job.id || `job-${shortId()}`;
    const doc = { ...job, id };
    this.run(
      "INSERT INTO import_jobs(id, status, filename, path, checksum, doc, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
      [id, job.status, job.filename, job.path, job.checksum, JSON.stringify(doc), job.created_at]
    );
    return doc;
  }

  updateJob(job: ImportJob): void {
    this.run("UPDATE import_jobs SET status = ?, doc = ? WHERE id = ?", [job.status, JSON.stringify(job), job.id]);
  }

  listJobs(limit = 50): ImportJob[] {
    return this.all("SELECT doc FROM import_jobs ORDER BY created_at DESC LIMIT ?", [limit])
      .map((r) => JSON.parse(String(r.doc)) as ImportJob);
  }

  countJobsByStatus(): Record<string, number> {
    const out: Record<string, number> = {};
    for (const row of this.all("SELECT status, COUNT(*) AS n FROM import_jobs GROUP BY status")) {
      out[String(row.status)] = Number(row.n);
    }
    return out;
  }

  resumeIncompleteJobs(): ImportJob[] {
    const rows = this.all("SELECT doc FROM import_jobs WHERE status = 'PROCESSING'")
      .map((r) => JSON.parse(String(r.doc)) as ImportJob);
    for (const job of rows) {
      job.status = "QUEUED";
      job.error = "";
      this.updateJob(job);
    }
    return rows;
  }

  // ======================== Processing state =========================
  alreadyProcessed(checksum: string): boolean {
    return !!this.get("SELECT 1 AS x FROM processed_files WHERE checksum = ?", [checksum]);
  }

  markProcessed(checksum: string, filePath: string, jobId: string): void {
    this.run(
      "INSERT INTO processed_files(checksum, path, job_id, processed_at) VALUES(?, ?, ?, ?) " +
        "ON CONFLICT(checksum) DO NOTHING",
      [checksum, filePath, jobId, nowIso()]
    );
  }

  // ============================= Memories ============================
  insertMemories(memories: Memory[]): number {
    let inserted = 0;
    for (const mem of memories) {
      if (this.get("SELECT 1 AS x FROM memories WHERE id = ?", [mem.id])) continue;
      const doc = { ...mem, embedding: null };
      this.run(
        "INSERT INTO memories(id, person_id, kind, source, occurred_at, record_id, doc, embedding, imported_at) " +
          "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
          mem.id, mem.person_id, mem.kind, mem.source, canonicalTime(mem.occurred_at),
          mem.record_id, JSON.stringify(doc), encodeEmbedding(mem.embedding), mem.imported_at,
        ]
      );
      this.run("INSERT INTO memories_fts(memory_id, text) VALUES(?, ?)", [mem.id, mem.text]);
      inserted++;
    }
    if (inserted > 0) {
      // Mark stale rather than discarding. The matrix is expensive to build
      // and an import invalidates it after every single batch; throwing it
      // away meant the next question paid a full rebuild.
      this.embedIndexStale = true;
    }
    return inserted;
  }

  /**
   * Signal that a bulk write is in progress.
   *
   * While this is true the embedding index is never rebuilt: see
   * `embeddingIndex` for why that matters at this size.
   */
  beginBulkWrite(): void {
    this.bulkWrites += 1;
  }

  endBulkWrite(): void {
    this.bulkWrites = Math.max(0, this.bulkWrites - 1);
  }

  getMemory(id: string): Memory | null {
    const row = this.get("SELECT doc, embedding FROM memories WHERE id = ?", [id]);
    if (!row) return null;
    const doc = JSON.parse(String(row.doc)) as Memory;
    doc.embedding = decodeEmbedding(row.embedding);
    return doc;
  }

  memoriesForPerson(personId: string, limit = 500): Memory[] {
    return this.all(
      "SELECT doc FROM memories WHERE person_id = ? ORDER BY occurred_at DESC LIMIT ?",
      [personId, limit]
    ).map((r) => JSON.parse(String(r.doc)) as Memory);
  }

  countMemories(): number {
    return Number(this.get("SELECT COUNT(*) AS n FROM memories")?.n ?? 0);
  }

  allEmbeddings(): { id: string; embedding: number[] }[] {
    const rows = this.all("SELECT id, embedding FROM memories WHERE embedding IS NOT NULL");
    const out: { id: string; embedding: number[] }[] = [];
    for (const r of rows) {
      const vec = decodeEmbedding(r.embedding);
      if (vec) out.push({ id: String(r.id), embedding: vec });
    }
    return out;
  }

  /**
   * The cached embedding matrix, or null when it is not ready.
   *
   * This NEVER builds. Building reads every stored embedding and copies it into
   * one contiguous buffer -- on a real archive roughly 100k rows of 768 floats,
   * measured at tens of seconds. Doing that inline froze the window and the API
   * for the duration, which is what made the app look broken. The build now
   * happens in `warmEmbeddingIndex`, which yields between pages, and this
   * accessor only hands out what is already in memory.
   *
   * Until the index is ready, retrieval falls back to keyword (FTS5) search:
   * a worse answer, immediately, rather than a better one in a minute.
   */
  embeddingIndex(): EmbeddingIndex | null {
    return this.embedIndex;
  }

  /**
   * Build (or refresh) the embedding matrix in the background.
   *
   * Paged so the event loop is released regularly, and skipped while an import
   * is running: the matrix would be stale the moment that finished. Safe to
   * call often -- concurrent calls share the one in-flight build.
   */
  async warmEmbeddingIndex(): Promise<void> {
    if (this.embedIndexBuilding) return this.embedIndexBuild;
    if (this.embedIndex && !this.embedIndexStale) return;
    if (this.bulkWrites > 0) return;
    // Do not thrash: an import that just finished does not need the matrix
    // rebuilt immediately, and rebuilding costs real time.
    if (this.embedIndex && Date.now() - this.embedIndexBuiltAt < REBUILD_INTERVAL_MS) return;

    this.embedIndexBuild = this.buildEmbeddingIndex();
    return this.embedIndexBuild;
  }

  private async buildEmbeddingIndex(): Promise<void> {
    this.embedIndexBuilding = true;
    try {
      const total = Number(
        (this.get("SELECT COUNT(*) AS c FROM memories WHERE embedding IS NOT NULL") as
          | { c?: number }
          | undefined)?.c ?? 0
      );
      if (total === 0) return;

      // Read the first page to learn the vector width, then fill one buffer
      // directly. Collecting per-row copies first and concatenating afterwards
      // briefly held the whole archive twice -- around 300 MB of extra
      // allocations on a real archive, which was enough to kill the process
      // outright rather than throw.
      const first = this.all(
        "SELECT id, embedding FROM memories WHERE embedding IS NOT NULL ORDER BY rowid LIMIT ?",
        [INDEX_PAGE]
      );
      const probe = first.map((r) => embeddingView(r.embedding)).find((v) => v && v.length > 0);
      const dim = probe?.length ?? 0;
      if (dim === 0) return;

      const capacity = Math.max(total, first.length);
      const vectors = new Float32Array(capacity * dim);
      const ids: string[] = new Array(capacity);
      let written = 0;

      const absorb = (rows: Row[]): void => {
        for (const r of rows) {
          if (written >= capacity) return;
          const vec = embeddingView(r.embedding);
          if (!vec || vec.length !== dim) continue;
          vectors.set(vec, written * dim);
          ids[written] = String(r.id);
          written++;
        }
      };

      absorb(first);
      await yieldToLoop();

      for (let offset = first.length; offset < total; offset += INDEX_PAGE) {
        absorb(
          this.all(
            "SELECT id, embedding FROM memories WHERE embedding IS NOT NULL ORDER BY rowid LIMIT ? OFFSET ?",
            [INDEX_PAGE, offset]
          )
        );
        // Hand the event loop back between pages. Without this the window is
        // unresponsive for the whole build, which is what this method exists
        // to prevent.
        await yieldToLoop();
      }

      if (written === 0) return;
      this.embedIndex = { ids: ids.slice(0, written), vectors: vectors.subarray(0, written * dim), dim };
      this.embedIndexBuiltAt = Date.now();
      this.embedIndexStale = false;
    } finally {
      this.embedIndexBuilding = false;
    }
  }

  /** Full-text keyword retrieval over the FTS5 index. */
  keywordSearch(terms: string[], k = 12, personId?: string): { memory: Memory; score: number }[] {
    const cleaned = terms.filter((t) => t.length >= 2).slice(0, 8);
    if (cleaned.length === 0) return [];
    const match = cleaned.map((t) => `"${t.replace(/"/g, "")}"`).join(" OR ");
    let sql =
      "SELECT m.doc AS doc, bm25(memories_fts) AS rank FROM memories_fts " +
      "JOIN memories m ON m.id = memories_fts.memory_id WHERE memories_fts MATCH ?";
    const params: unknown[] = [match];
    if (personId) {
      sql += " AND m.person_id = ?";
      params.push(personId);
    }
    sql += " ORDER BY rank LIMIT ?";
    params.push(k);
    try {
      return this.all(sql, params).map((r, i) => ({
        memory: JSON.parse(String(r.doc)) as Memory,
        score: 1 / (i + 1),
      }));
    } catch {
      return [];
    }
  }

  // ========================== Relationship ===========================
  insertRelationshipEvent(ev: RelationshipEvent): void {
    this.run(
      "INSERT OR IGNORE INTO relationship_events(id, person_id, kind, source, occurred_at, summary, record_id, doc) " +
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
      [ev.id, ev.person_id, ev.kind, ev.source, canonicalTime(ev.occurred_at), ev.summary, ev.record_id, JSON.stringify(ev)]
    );
  }

  listRelationshipEvents(personId: string, limit = 200): RelationshipEvent[] {
    return this.all(
      "SELECT doc FROM relationship_events WHERE person_id = ? ORDER BY occurred_at DESC LIMIT ?",
      [personId, limit]
    ).map((r) => JSON.parse(String(r.doc)) as RelationshipEvent);
  }

  /**
   * True lifetime totals for one person. `listRelationshipEvents` returns a
   * capped window, so it must never be used to report a total -- doing so
   * pins every prolific contact to the cap. Windows are counted in SQL for
   * the same reason.
   */
  relationshipEventStats(
    personId: string,
    withinDays: number[] = []
  ): { total: number; by_source: Record<string, number>; by_window: Record<string, number> } {
    let total = 0;
    const bySource: Record<string, number> = {};
    for (const r of this.all(
      "SELECT source, COUNT(*) AS n FROM relationship_events WHERE person_id = ? GROUP BY source",
      [personId]
    )) {
      const n = Number(r.n);
      bySource[String(r.source || "unknown")] = n;
      total += n;
    }
    const byWindow: Record<string, number> = {};
    const end = nowIso();
    for (const days of withinDays) {
      const from = new Date(Date.now() - days * 86_400_000).toISOString();
      const row = this.get(
        "SELECT COUNT(*) AS n FROM relationship_events " +
          "WHERE person_id = ? AND occurred_at >= ? AND occurred_at <= ?",
        [personId, from, end]
      );
      byWindow[String(days)] = row ? Number(row.n) : 0;
    }
    return { total, by_source: bySource, by_window: byWindow };
  }

  private occurredWindow(since: string | null, until: string | null): [string, string] {
    const start = since || "1970-01-01T00:00:00.000Z";
    // Bounded at both ends: exports carry future-dated records, and an open
    // lower bound would count them as "this week".
    const end = until || nowIso();
    return [start, end];
  }

  interactionTotals(limit = 10, since: string | null = null): [string, number, string | null][] {
    const [start, end] = this.occurredWindow(since, null);
    return this.all(
      "SELECT person_id, COUNT(*) AS n, MAX(occurred_at) AS last FROM relationship_events " +
        "WHERE occurred_at >= ? AND occurred_at <= ? AND person_id IS NOT NULL " +
        "GROUP BY person_id ORDER BY n DESC LIMIT ?",
      [start, end, limit]
    ).map((r) => [String(r.person_id), Number(r.n), r.last ? String(r.last) : null]);
  }

  activityTotals(since: string | null = null, personId?: string, source?: string): {
    total: number;
    by_kind: Record<string, number>;
    by_source: Record<string, number>;
  } {
    const [start, end] = this.occurredWindow(since, null);
    let sql = "SELECT kind, source, COUNT(*) AS n FROM relationship_events WHERE occurred_at >= ? AND occurred_at <= ?";
    const params: unknown[] = [start, end];
    if (personId) {
      sql += " AND person_id = ?";
      params.push(personId);
    }
    if (source) {
      sql += " AND source = ?";
      params.push(source);
    }
    sql += " GROUP BY kind, source";
    const byKind: Record<string, number> = {};
    const bySource: Record<string, number> = {};
    for (const row of this.all(sql, params)) {
      const k = String(row.kind || "unknown");
      const s = String(row.source || "unknown");
      const n = Number(row.n);
      byKind[k] = (byKind[k] || 0) + n;
      bySource[s] = (bySource[s] || 0) + n;
    }
    return { total: Object.values(byKind).reduce((a, b) => a + b, 0), by_kind: byKind, by_source: bySource };
  }

  distinctPeople(since: string | null = null): number {
    const [start, end] = this.occurredWindow(since, null);
    const row = this.get(
      "SELECT COUNT(DISTINCT person_id) AS n FROM relationship_events " +
        "WHERE occurred_at >= ? AND occurred_at <= ? AND person_id IS NOT NULL AND person_id != ''",
      [start, end]
    );
    return Number(row?.n ?? 0);
  }

  lastInteraction(personId?: string): string | null {
    let sql = "SELECT MAX(occurred_at) AS last FROM relationship_events";
    const params: unknown[] = [];
    if (personId) {
      sql += " WHERE person_id = ?";
      params.push(personId);
    }
    const row = this.get(sql, params);
    return row?.last ? String(row.last) : null;
  }

  recentPeople(since: string | null, limit = 10): [string, number, string | null][] {
    return this.interactionTotals(limit, since);
  }

  eventCountsByPerson(): Record<string, number> {
    const out: Record<string, number> = {};
    for (const r of this.all(
      "SELECT person_id, COUNT(*) AS n FROM relationship_events GROUP BY person_id ORDER BY n DESC"
    )) {
      out[String(r.person_id)] = Number(r.n);
    }
    return out;
  }

  // ============================= Profiles ============================
  upsertProfile(profile: RelationshipProfile): void {
    this.run(
      "INSERT INTO profiles(person_id, doc, updated_at) VALUES(?, ?, ?) " +
        "ON CONFLICT(person_id) DO UPDATE SET doc = excluded.doc, updated_at = excluded.updated_at",
      [profile.person_id, JSON.stringify(profile), profile.updated_at]
    );
  }

  getProfile(personId: string): RelationshipProfile | null {
    const row = this.get("SELECT doc FROM profiles WHERE person_id = ?", [personId]);
    return row ? (JSON.parse(String(row.doc)) as RelationshipProfile) : null;
  }

  listProfiles(): RelationshipProfile[] {
    return this.all("SELECT doc FROM profiles").map((r) => JSON.parse(String(r.doc)) as RelationshipProfile);
  }

  /**
   * People whose cached profile no longer matches the events on disk. Cached
   * counts are only refreshed for people a finished import touched, so a
   * change in how counts are derived (or an interrupted import) leaves stale
   * numbers on screen. One query, so it is cheap enough to run at startup.
   */
  staleProfileIds(): string[] {
    return this.all(
      "SELECT p.person_id AS person_id FROM profiles p " +
        "JOIN (SELECT person_id, COUNT(*) AS n FROM relationship_events " +
        "      WHERE person_id IS NOT NULL GROUP BY person_id) e " +
        "  ON e.person_id = p.person_id " +
        "WHERE COALESCE(json_extract(p.doc, '$.interaction_count'), -1) != e.n"
    ).map((r) => String(r.person_id));
  }

  // ========================= Identity suggestions ====================
  upsertIdentitySuggestion(id: string, fields: Record<string, unknown>): void {
    const existing = this.get("SELECT doc FROM identity_suggestions WHERE id = ?", [id]);
    const doc = existing ? { ...(JSON.parse(String(existing.doc)) as Record<string, unknown>), ...fields } : fields;
    this.run(
      "INSERT INTO identity_suggestions(id, status, doc) VALUES(?, ?, ?) " +
        "ON CONFLICT(id) DO UPDATE SET status = excluded.status, doc = excluded.doc",
      [id, String(doc.status ?? "pending"), JSON.stringify(doc)]
    );
  }

  listIdentitySuggestions(status = "pending"): Record<string, unknown>[] {
    return this.all("SELECT doc FROM identity_suggestions WHERE status = ?", [status])
      .map((r) => JSON.parse(String(r.doc)) as Record<string, unknown>);
  }

  getIdentitySuggestion(id: string): Record<string, unknown> | null {
    const row = this.get("SELECT doc FROM identity_suggestions WHERE id = ?", [id]);
    return row ? (JSON.parse(String(row.doc)) as Record<string, unknown>) : null;
  }

  setIdentitySuggestionStatus(id: string, status: string): void {
    const row = this.get("SELECT doc FROM identity_suggestions WHERE id = ?", [id]);
    if (!row) return;
    const doc = JSON.parse(String(row.doc)) as Record<string, unknown>;
    doc.status = status;
    this.run("UPDATE identity_suggestions SET status = ?, doc = ? WHERE id = ?", [status, JSON.stringify(doc), id]);
  }

  // ============================ Reassignment =========================
  reassignPersonRecords(keepId: string, removeId: string): number {
    let moved = 0;
    const tables: [string, string][] = [
      ["messages", "person_id"],
      ["emails", "person_id"],
      ["notes", "person_id"],
      ["voice_recordings", "person_id"],
      ["media", "person_id"],
      ["documents", "person_id"],
      ["memories", "person_id"],
      ["relationship_events", "person_id"],
      ["calendar_people", "person_id"],
    ];
    for (const [table, field] of tables) {
      const before = this.get(`SELECT COUNT(*) AS n FROM ${table} WHERE ${field} = ?`, [removeId]);
      const n = Number(before?.n ?? 0);
      if (n === 0) continue;
      // JSON doc must move too so reads agree with the indexed column.
      if (table === "calendar_people") {
        this.run(`UPDATE ${table} SET ${field} = ? WHERE ${field} = ?`, [keepId, removeId]);
      } else {
        const rows = this.all(`SELECT id, doc FROM ${table} WHERE ${field} = ?`, [removeId]);
        for (const r of rows) {
          const doc = JSON.parse(String(r.doc)) as Record<string, unknown>;
          doc[field] = keepId;
          this.run(`UPDATE ${table} SET ${field} = ?, doc = ? WHERE id = ?`, [keepId, JSON.stringify(doc), r.id]);
        }
      }
      moved += n;
    }
    return moved;
  }
}

function slug(name: string): string {
  return (name || "person").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40);
}

function shortId(): string {
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}
