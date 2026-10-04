/**
 * Ingestion pipeline.
 *
 *   detect -> validate -> checksum -> duplicate check -> parse -> normalize
 *   -> resolve identities -> store -> embeddings -> profile -> notify UI
 *
 * Failures never stop the watcher. Files are only moved into processed/ when
 * they already live inside Circle's own managed folder; a folder the user
 * pointed us at is read in place and left exactly as it was.
 */
import fs from "node:fs";
import path from "node:path";
import { shouldArchive, type CirclePaths, type CircleSettings } from "./config.js";
import { isParseResultEmpty, nowIso } from "./domain.js";
import type { ImportJob, Memory, Message, ParseResult, SourceType } from "./domain.js";
import type { Store } from "./store.js";
import type { IdentityResolver } from "./identity.js";
import type { EmbeddingProvider } from "./ai/embeddings.js";
import { refreshProfile, recordEvent, sourceLabel } from "./relationship.js";
import { cleanText, deterministicId, extractTopics } from "./parsers/common.js";
import {
  AUDIO_EXTENSIONS,
  FileSecurityError,
  MEDIA_EXTENSIONS,
  moveFile,
  readHead,
  safeExtractZip,
  sanitizeFilename,
  sha256FileAsync,
  validateExtension,
  validateSize,
} from "./security.js";
import { MediaIngest, mediaFilenameKey } from "./media.js";
import { parseWhatsApp } from "./parsers/whatsapp.js";
import { parseTelegram } from "./parsers/telegram.js";
import { parseInstagram } from "./parsers/instagram.js";
import { parseX } from "./parsers/x.js";
import { parseEmail } from "./parsers/email_parser.js";
import { parseCalendar } from "./parsers/calendar.js";
import { parseContacts } from "./parsers/contacts.js";
import { documentResult } from "./parsers/documents.js";
import { parseGeneric } from "./parsers/generic.js";
import { skipReason as exportSkipReason } from "./parsers/export_scope.js";

const MAX_ZIP_DEPTH = 3;
const MAX_FILE_BYTES = 50 * 1024 * 1024;

/**
 * Hand control back to the event loop.
 *
 * `await` alone is not enough: work that resolves through microtasks keeps the
 * loop in the same tick, so the window and the local API never get a turn.
 * An immediate check does, and that is the difference between "busy" and
 * "not responding" during a large import.
 */
const yieldToLoop = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));

/** Yield every `n` records while walking a bulk collection. */
const YIELD_EVERY = 200;

const SOURCE_BY_FOLDER: Record<string, SourceType> = {
  whatsapp: "whatsapp",
  telegram: "telegram",
  instagram: "instagram",
  x: "x",
  twitter: "x",
  email: "email",
  calendar: "calendar",
  contacts: "contacts",
  voice: "voice",
  chats: "chat",
  chat: "chat",
  notes: "notes",
  documents: "document",
};

export interface PipelineResult {
  status: string;
  job: ImportJob | null;
  error: string;
}

export interface PipelineDeps {
  store: Store;
  resolver: IdentityResolver;
  /**
   * Live accessors, not captured values.
   *
   * Settings can change while the app runs (a different model, a different
   * folder), and `updateSettings` swaps the settings object rather than
   * mutating it -- so anything holding the old object keeps using the old
   * values indefinitely.
   */
  embedder: () => EmbeddingProvider;
  settings: () => CircleSettings;
  paths: CirclePaths;
  emit: (type: string, payload: Record<string, unknown>) => void;
}

export class IngestionPipeline {
  constructor(private deps: PipelineDeps) {}

  private get watchRoot(): string {
    return this.deps.settings().watchFolder;
  }

  async processPath(filePath: string): Promise<PipelineResult> {
    let job: ImportJob | null = null;
    try {
      if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
        return { status: "SKIPPED", job: null, error: "" };
      }

      // Inside a Meta data export almost nothing is a conversation. Skip the
      // noise before opening the file: it keeps account settings and ad
      // interests from being parsed as chats, and stops thousands of photos
      // from being stored as unlinked media.
      if (exportSkipReason(filePath)) {
        return { status: "SKIPPED", job: null, error: "" };
      }

      const source = this.detectSource(filePath);
      const ext = path.extname(filePath).toLowerCase();
      if (ext === ".zip") {
        // handled below; size cap still applies
        validateSize(filePath, MAX_FILE_BYTES * 10);
      } else if (MEDIA_EXTENSIONS.has(ext)) {
        if (fs.statSync(filePath).size === 0) throw new FileSecurityError("empty file");
      } else {
        validateExtension(filePath);
        validateSize(filePath, MAX_FILE_BYTES);
      }

      const checksum = await sha256FileAsync(filePath);
      if (this.deps.store.alreadyProcessed(checksum)) {
        this.deps.emit("job", { status: "SKIPPED", filename: sanitizeFilename(path.basename(filePath)), reason: "duplicate" });
        this.archive(filePath);
        return { status: "SKIPPED", job: null, error: "" };
      }

      job = this.deps.store.insertJob({
        id: "",
        filename: sanitizeFilename(path.basename(filePath)),
        path: filePath,
        source,
        status: "PROCESSING",
        records_imported: 0,
        records_skipped: 0,
        checksum,
        error: "",
        created_at: nowIso(),
        started_at: nowIso(),
        completed_at: null,
      });
      this.deps.emit("job", { id: job.id, status: "PROCESSING", filename: job.filename });

      const media = new MediaIngest(this.deps.store, this.deps.paths, this.deps.settings());
      let imported = 0;
      let skipped = 0;
      if (ext === ".zip") {
        [imported, skipped] = await this.processZip(filePath, job, source, 0, media);
      } else {
        [imported, skipped] = await this.processOne(filePath, job, source, checksum, media);
      }

      job.status = "COMPLETED";
      job.completed_at = nowIso();
      job.records_imported = imported;
      job.records_skipped = skipped;
      this.deps.store.updateJob(job);
      this.deps.store.markProcessed(checksum, filePath, job.id);
      this.archive(filePath);
      this.deps.emit("job", { id: job.id, status: "COMPLETED", filename: job.filename, records: imported });
      return { status: "COMPLETED", job, error: "" };
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      const quarantine = err instanceof FileSecurityError;
      return this.fail(filePath, job, quarantine ? `security: ${message}` : message, quarantine);
    }
  }

  private archive(filePath: string): void {
    if (!shouldArchive(filePath, this.deps.paths, this.deps.settings())) return;
    try {
      moveFile(filePath, this.deps.paths.processedDir);
    } catch {
      /* leaving the file is safe: the checksum suppresses a re-import */
    }
  }

  private fail(filePath: string, job: ImportJob | null, error: string, quarantine: boolean): PipelineResult {
    const status = quarantine ? "QUARANTINED" : "FAILED";
    if (job) {
      job.status = status;
      job.error = error.slice(0, 500);
      job.completed_at = nowIso();
      this.deps.store.updateJob(job);
    }
    try {
      if (fs.existsSync(filePath) && shouldArchive(filePath, this.deps.paths, this.deps.settings())) {
        moveFile(filePath, quarantine ? this.deps.paths.quarantineDir : this.deps.paths.failedDir);
      }
    } catch {
      /* ignore */
    }
    this.deps.emit("job", { id: job?.id ?? null, status, filename: path.basename(filePath), error: error.slice(0, 200) });
    return { status, job, error };
  }

  // ------------------------------ detection ------------------------------
  detectSource(filePath: string): SourceType | null {
    const root = this.watchRoot;
    if (root) {
      const rel = path.relative(path.resolve(root), path.resolve(filePath));
      if (!rel.startsWith("..")) {
        for (const part of rel.split(path.sep).slice(0, -1)) {
          const key = part.toLowerCase().trim();
          if (SOURCE_BY_FOLDER[key]) return SOURCE_BY_FOLDER[key]!;
        }
      }
    }
    const n = path.basename(filePath).toLowerCase();
    if (n.endsWith("_chat.txt") || n.includes("whatsapp")) return "whatsapp";
    if (n.includes("telegram")) return "telegram";
    if (n.includes("instagram")) return "instagram";
    if (n.startsWith("direct-message") || n.includes("twitter") || (n.endsWith(".js") && n.includes("data"))) return "x";
    return null;
  }

  private async sniff(filePath: string, source: SourceType | null): Promise<SourceType | null> {
    if (source) return source;
    const ext = path.extname(filePath).toLowerCase();
    if (ext === ".js") return "x";
    if (ext === ".eml" || ext === ".mbox") return "email";
    if (ext === ".ics") return "calendar";
    if (ext === ".vcf") return "contacts";
    if (AUDIO_EXTENSIONS.has(ext)) return "voice";
    if (ext === ".json" || ext === ".txt") {
      try {
        // Only the head of the file: reading a whole multi-megabyte export just
        // to recognise it is wasted work, and on a cloud drive it is a stall.
        const head = readHead(filePath, 4000);
        if (head.includes("sender_name") && head.includes("timestamp_ms")) return "instagram";
        if (head.includes('"messages"') && head.includes('"from"')) return "telegram";
        if (head.includes("participants") && head.includes("title")) return "instagram";
        if (head.includes("message_create") || head.includes("YTD")) return "x";
      } catch {
        return null;
      }
    }
    return null;
  }

  // -------------------------------- zip ----------------------------------
  private async processZip(zipPath: string, job: ImportJob, source: SourceType | null, depth: number, media: MediaIngest): Promise<[number, number]> {
    const tmp = fs.mkdtempSync(path.join(this.deps.paths.tmpDir, "zip-"));
    let ok = 0;
    let skip = 0;
    try {
      const entries = safeExtractZip(zipPath, tmp);
      const staged = new Set<string>();

      // Pass 1: media attachments, so a photo/voice note links regardless of order.
      for (const entry of entries) {
        const base = path.basename(entry);
        if (base.startsWith("~$") || base.startsWith(".")) continue;
        const ext = path.extname(entry).toLowerCase();
        if (!MEDIA_EXTENSIONS.has(ext)) continue;
        const subSource = source ?? this.detectSource(entry) ?? (await this.sniff(entry, null));
        if (subSource === "voice" && AUDIO_EXTENSIONS.has(ext)) continue;
        const att = media.add(entry, subSource ?? "chat");
        if (att) {
          staged.add(entry);
          ok++;
        } else skip++;
      }

      for (const entry of entries) {
        const base = path.basename(entry);
        if (base.startsWith("~$") || base.startsWith(".")) continue;
        if (staged.has(entry)) continue;
        try {
          validateExtension(entry);
        } catch {
          skip++;
          continue;
        }
        const subSource = source ?? this.detectSource(entry) ?? (await this.sniff(entry, null));
        try {
          if (path.extname(entry).toLowerCase() === ".zip") {
            if (depth + 1 >= MAX_ZIP_DEPTH) {
              skip++;
              continue;
            }
            const [o, s] = await this.processZip(entry, job, subSource, depth + 1, media);
            ok += o;
            skip += s;
            continue;
          }
          const csum = await sha256FileAsync(entry);
          if (this.deps.store.alreadyProcessed(csum)) {
            skip++;
            continue;
          }
          const [o, s] = await this.processOne(entry, job, subSource, csum, media);
          ok += o;
          skip += s;
        } catch {
          skip++;
        }
      }
      return [ok, skip];
    } finally {
      fs.rmSync(tmp, { recursive: true, force: true });
    }
  }

  // ------------------------------ single file ----------------------------
  private async processOne(filePath: string, job: ImportJob, source: SourceType | null, checksum: string, media: MediaIngest): Promise<[number, number]> {
    const resolved = await this.sniff(filePath, source);
    const ext = path.extname(filePath).toLowerCase();

    if (MEDIA_EXTENSIONS.has(ext) && ext !== ".zip") {
      const att = media.add(filePath, resolved ?? "chat");
      if (!att) throw new Error("unsupported or rejected media file");
      const linked = this.linkMediaToExistingMessages(att);
      this.deps.emit("media", { id: att.id, filename: att.filename, kind: att.kind, linked });
      return [1, 0];
    }

    const result = this.parse(filePath, resolved, ext);
    if (isParseResultEmpty(result)) throw new Error("no usable records found in file");
    return this.persist(result, resolved, checksum, filePath, media);
  }

  private parse(filePath: string, source: SourceType | null, ext: string): ParseResult {
    if (source === "whatsapp" && ext === ".txt") return parseWhatsApp(filePath);
    if (source === "telegram" && ext === ".json") return parseTelegram(filePath);
    if (source === "instagram" && (ext === ".json" || ext === ".txt")) return parseInstagram(filePath);
    if (source === "x" && (ext === ".json" || ext === ".js")) return parseX(filePath);
    if (source === "email" && [".eml", ".mbox", ".csv", ".json"].includes(ext)) return parseEmail(filePath);
    if (source === "calendar" && (ext === ".ics" || ext === ".csv")) return parseCalendar(filePath);
    if (source === "contacts" && (ext === ".csv" || ext === ".vcf")) return parseContacts(filePath);
    if (source === "document") return documentResult(filePath);

    // Fallback chain by extension.
    const attempts: ((p: string) => ParseResult)[] = [];
    if (ext === ".txt" || ext === ".md") attempts.push(parseWhatsApp, parseGeneric);
    else if (ext === ".json") attempts.push(parseTelegram, parseInstagram, parseX, parseGeneric);
    else if (ext === ".js") attempts.push(parseX);
    else if (ext === ".csv") attempts.push(parseContacts, parseCalendar, parseEmail, parseGeneric);
    else if (ext === ".html" || ext === ".htm") attempts.push(parseGeneric, documentResult);
    else if (ext === ".eml" || ext === ".mbox") attempts.push(parseEmail);
    else if (ext === ".ics") attempts.push(parseCalendar);
    else if (ext === ".vcf") attempts.push(parseContacts);
    else attempts.push(parseGeneric, documentResult);

    for (const fn of attempts) {
      try {
        const res = fn(filePath);
        if (!isParseResultEmpty(res)) return res;
      } catch {
        /* try next */
      }
    }
    throw new Error("could not parse file");
  }

  // ------------------------------ persistence ----------------------------
  private async persist(result: ParseResult, source: SourceType | null, checksum: string, filePath: string, media: MediaIngest): Promise<[number, number]> {
    let imported = 0;
    let skipped = 0;

    if (result.contacts.length > 0) {
      const created = this.deps.resolver.ingestContacts(result.contacts);
      imported += result.contacts.length;
      this.deps.emit("contacts", { count: result.contacts.length, created });
    }

    const convMap = new Map<string, string>();
    for (const conv of result.conversations) {
      if (!conv.external_key) continue;
      const stored = this.deps.store.upsertConversation({
        id: "",
        external_key: conv.external_key,
        title: conv.title,
        participant_ids: [],
        source: conv.source,
        origin: "imported",
        last_message_at: null,
        created_at: nowIso(),
      });
      convMap.set(conv.external_key, stored.id);
    }

    const affected = new Set<string>();
    if (result.messages.length > 0) {
      const [i, s, ids] = await this.storeMessages(result.messages, convMap, checksum, media);
      imported += i;
      skipped += s;
      for (const id of ids) affected.add(id);
    }

    for (const email of result.emails) {
      let person = email.from_address
        ? this.deps.store.findPersonByIdentity("email", email.from_address.toLowerCase())
        : null;
      if (!person) person = this.deps.resolver.resolveSender(email.from_label || email.from_address, "email", { email: email.from_address }).person;
      if (person?.id) {
        email.person_id = person.id;
        affected.add(person.id);
      }
      if (this.deps.store.insertEmailIfNew(email)) {
        imported++;
        if (person?.id && email.sent_at) {
          recordEvent(this.deps.store, {
            person_id: person.id, kind: "email", source: "email",
            occurred_at: email.sent_at, summary: email.subject || "(no subject)",
            record_id: email.id,
          });
        }
      } else skipped++;
    }

    for (const event of result.events) {
      const personIds: string[] = [];
      for (const name of event.participants) {
        const person = this.deps.store.findPersonByName(name) ?? this.deps.resolver.resolveSender(name, "calendar").person;
        if (person?.id) {
          personIds.push(person.id);
          affected.add(person.id);
        }
      }
      event.person_ids = [...new Set(personIds)];
      if (this.deps.store.insertCalendarIfNew(event)) {
        imported++;
        for (const pid of event.person_ids) {
          if (event.starts_at) {
            recordEvent(this.deps.store, {
              person_id: pid, kind: "meeting", source: "calendar",
              occurred_at: event.starts_at, summary: event.title, record_id: event.id,
            });
          }
        }
      } else skipped++;
    }

    for (const note of result.notes) {
      if (this.deps.store.insertNoteIfNew(note)) imported++;
      else skipped++;
    }

    for (const doc of result.documents) {
      const docId = `doc-${deterministicId(doc.filename, doc.text.slice(0, 400))}`;
      if (this.deps.store.insertDocumentIfNew({
        id: docId, person_id: null, filename: doc.filename, title: doc.title,
        text: doc.text.slice(0, 500_000), mime_type: path.extname(doc.filename),
        source: "document", origin: "imported", external_id: docId, imported_at: nowIso(),
      })) imported++;
      else skipped++;
    }

    const memories: Memory[] = [
      ...this.memoriesFromResult(result, checksum),
      ...result.documents.flatMap((d) => this.chunkDocument(d.filename, d.title, d.text, checksum)),
    ];
    if (memories.length > 0) await this.embedAndStore(memories);

    let refreshed = 0;
    for (const pid of affected) {
      refreshProfile(this.deps.store, pid);
      if (++refreshed % 10 === 0) await yieldToLoop();
    }

    this.deps.store.insertSource({
      id: `src-${checksum.slice(0, 32)}`, filename: path.basename(filePath),
      source_type: source ?? "chat", origin: "imported", checksum,
      imported_at: nowIso(), record_count: imported,
    });
    this.deps.emit("sync", { filename: path.basename(filePath), imported, skipped, people_updated: affected.size });
    // The archive grew, so the vector index is out of date. Refresh it in the
    // background now that this file is done, unless more files are waiting.
    void this.deps.store.warmEmbeddingIndex();
    return [imported, skipped];
  }

  private async storeMessages(messages: Message[], convMap: Map<string, string>, checksum: string, media: MediaIngest): Promise<[number, number, string[]]> {
    let imported = 0;
    let skipped = 0;
    const affected = new Set<string>();
    let sinceYield = 0;

    const byConv = new Map<string, Message[]>();
    for (const m of messages) {
      const key = m.conversation_id ?? "default";
      const list = byConv.get(key) ?? [];
      list.push(m);
      byConv.set(key, list);
    }

    for (const [convKey, msgs] of byConv) {
      const cid = convMap.get(convKey);
      const senderPerson = new Map<string, ReturnType<IdentityResolver["resolveSender"]>>();
      for (const m of msgs) {
        if (!senderPerson.has(m.sender_label)) {
          senderPerson.set(m.sender_label, this.deps.resolver.resolveSender(m.sender_label, m.source));
        }
      }
      const nonUser = [...senderPerson.entries()].filter(([, r]) => r.person && !r.person.is_user);
      const oneToOne = nonUser.length === 1 ? nonUser[0]![1].person : null;

      if (cid) {
        const conv = this.deps.store.getConversation(cid);
        if (conv) {
          conv.participant_ids = [...new Set([...senderPerson.values()].map((r) => r.person.id).filter(Boolean))];
          this.deps.store.updateConversation(conv);
        }
      }

      for (const m of msgs) {
        let person = senderPerson.get(m.sender_label)?.person ?? null;
        if (!person && oneToOne) person = oneToOne;
        if (person?.id) {
          m.person_id = person.id;
          affected.add(person.id);
        }
        if (cid) m.conversation_id = cid;
        if (this.deps.store.insertMessageIfNew(m)) {
          imported++;
          if (m.person_id && m.sent_at) {
            const isUserMsg = this.deps.resolver.isUserLabel(m.sender_label);
            if (!isUserMsg || oneToOne) {
              recordEvent(this.deps.store, {
                person_id: m.person_id, kind: "message", source: m.source,
                occurred_at: m.sent_at, summary: `${m.sender_label}: ${m.content.slice(0, 80)}`,
                record_id: m.id,
              });
            }
          }
        } else skipped++;

        for (const att of m.attachments ?? []) {
          const key = (att as { filename_key?: string }).filename_key;
          if (!key) continue;
          const found = media.findByKey(key) ?? this.deps.store.findMediaByFilenameKey(key);
          if (found) {
            this.linkMedia(found, m);
            if (found.person_id) affected.add(found.person_id);
          }
        }

        // A single export can hold tens of thousands of messages. Without this
        // the main thread would not get a single turn until the file finished.
        if (++sinceYield >= YIELD_EVERY) {
          sinceYield = 0;
          await yieldToLoop();
        }
      }
    }
    return [imported, skipped, [...affected]];
  }

  // ------------------------------- media ---------------------------------
  private linkMediaToExistingMessages(mediaAttachment: { filename: string }): number {
    const key = mediaFilenameKey(mediaAttachment.filename);
    if (!key) return 0;
    const msgs = this.deps.store.findMessagesByAttachmentKey(key);
    for (const m of msgs) {
      const att = this.deps.store.findMediaByFilenameKey(key);
      if (att) this.linkMedia(att, m);
    }
    return msgs.length;
  }

  private linkMedia(media: { id: string; message_id: string | null; person_id: string | null; conversation_id: string | null; occurred_at: string | null; status: string }, message: Message): void {
    const stored = this.deps.store.getMedia(media.id);
    if (!stored || !message.id) return;
    stored.message_id = message.id;
    stored.conversation_id = message.conversation_id ?? stored.conversation_id;
    if (message.person_id) stored.person_id = message.person_id;
    stored.occurred_at = stored.occurred_at ?? message.sent_at;
    stored.status = "LINKED";
    this.deps.store.updateMedia(stored);
    if (!(message.media_ids ?? []).includes(stored.id)) {
      message.media_ids = [...new Set([...(message.media_ids ?? []), stored.id])];
      this.deps.store.updateMessageMediaIds(message.id, message.media_ids);
    }
  }

  // ------------------------------ memories -------------------------------
  private memoriesFromResult(result: ParseResult, checksum: string): Memory[] {
    const out: Memory[] = [];
    for (const m of result.messages) {
      const text = cleanText(m.content);
      if (!text) continue;
      const mem: Memory = {
        id: `mem-${m.id}`,
        person_id: m.person_id,
        kind: "message",
        source: m.source,
        origin: m.origin,
        occurred_at: m.sent_at,
        text: m.sender_label ? `${m.sender_label}: ${text}` : text,
        summary: "",
        record_id: m.id,
        source_id: checksum,
        citation: citation(m.source, "message", m.sent_at),
        topics: extractTopics(text),
        embedding: null,
        imported_at: nowIso(),
      };
      out.push(mem);
    }
    for (const e of result.emails) {
      const body = cleanText(e.body || e.subject);
      if (!body) continue;
      out.push({
        id: `mem-${e.id}`, person_id: e.person_id, kind: "email", source: "email", origin: e.origin,
        occurred_at: e.sent_at, text: `${e.subject}\n${body}`.slice(0, 8000), summary: "",
        record_id: e.id, source_id: checksum, citation: citation("email", "email", e.sent_at, e.subject),
        topics: extractTopics(`${e.subject} ${body}`), embedding: null, imported_at: nowIso(),
      });
    }
    for (const ev of result.events) {
      const pid = ev.person_ids[0] ?? null;
      const text = cleanText(`${ev.title}. ${ev.participants.slice(0, 4).join(" / ")}. ${ev.description} ${ev.location}`.trim());
      out.push({
        id: `mem-${ev.id}`, person_id: pid, kind: "calendar", source: "calendar", origin: ev.origin,
        occurred_at: ev.starts_at, text, summary: "", record_id: ev.id, source_id: checksum,
        citation: citation("calendar", "event", ev.starts_at, ev.title), topics: extractTopics(text),
        embedding: null, imported_at: nowIso(),
      });
    }
    for (const n of result.notes) {
      const text = cleanText(`${n.title}. ${n.body}`.trim());
      if (!text) continue;
      out.push({
        id: `mem-${n.id}`, person_id: n.person_id, kind: "note", source: "notes", origin: n.origin,
        occurred_at: n.noted_at, text, summary: "", record_id: n.id, source_id: checksum,
        citation: citation("note", "note", n.noted_at), topics: extractTopics(text),
        embedding: null, imported_at: nowIso(),
      });
    }
    return out;
  }

  private chunkDocument(filename: string, title: string, text: string, checksum: string): Memory[] {
    const clean = cleanText(text);
    if (!clean) return [];
    const size = 1200;
    const chunks: string[] = [];
    if (clean.length <= size) chunks.push(clean);
    else for (let i = 0; i < clean.length; i += size) chunks.push(clean.slice(i, i + size));
    const docId = `doc-${deterministicId(filename, text.slice(0, 400))}`;
    return chunks.slice(0, 100).map((chunk, i) => ({
      id: `mem-${docId}-${i}`, person_id: null, kind: "document", source: "document" as SourceType,
      origin: "imported" as const, occurred_at: null, text: i === 0 ? `${title}: ${chunk}` : chunk,
      summary: "", record_id: docId, source_id: checksum, citation: filename,
      topics: extractTopics(chunk), embedding: null, imported_at: nowIso(),
    }));
  }

  private async embedAndStore(memories: Memory[]): Promise<number> {
    // Tell the store that a bulk write is under way so it leaves the embedding
    // index alone until the import settles (see Store#embeddingIndex).
    this.deps.store.beginBulkWrite();
    try {
      return await this.embedAndStoreUngated(memories);
    } finally {
      this.deps.store.endBulkWrite();
    }
  }

  private async embedAndStoreUngated(memories: Memory[]): Promise<number> {
    if (memories.length === 0) return 0;
    // Larger batches keep the number of embedding requests down; the provider
    // sends each batch in a single call. `await` matters as much as batching:
    // the request used to be synchronous, which froze the whole app for the
    // length of every batch.
    const batchSize = 64;
    let inserted = 0;
    for (let i = 0; i < memories.length; i += batchSize) {
      const batch = memories.slice(i, i + batchSize);
      let vectors: (number[] | null)[] = batch.map(() => null);
      try {
        vectors = await this.deps.embedder().embedBatch(batch.map((m) => m.text));
      } catch {
        /* store without vectors; keyword search still works */
      }
      batch.forEach((m, idx) => {
        m.embedding = vectors[idx] ?? null;
      });
      inserted += this.deps.store.insertMemories(batch);
      // Yield between batches so HTTP requests, window paints and watcher
      // events keep flowing while a large import runs.
      await new Promise((resolve) => setImmediate(resolve));
    }
    return inserted;
  }
}

function citation(source: SourceType | string, kind: string, when: string | null, extra = ""): string {
  const label = sourceLabel(source);
  const date = when ? new Date(when).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : "";
  return `${label} — ${kind}${extra ? ` "${extra}"` : ""}${date ? `, ${date}` : ""}`;
}
